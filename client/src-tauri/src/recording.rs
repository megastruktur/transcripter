//! Recording session orchestration: capture → FLAC spool → session.json.

use std::sync::Mutex;
use std::time::Instant;

use crate::capture::{self, CapturedStream};
use crate::encode::FlacWriter;
use crate::permissions::{pre_flight, PreFlightReport};
use crate::spool::{Spool, SpoolSession};

pub struct ActiveSession {
    pub session: SpoolSession,
    pub mic: Option<CapturedStream>,
    pub system: Option<CapturedStream>,
    pub writer: Mutex<Option<FlacWriter>>,
    pub started: Instant,
}

// cpal Stream is !Send-safe only via the callback thread it spawns on;
// we never move streams across threads after creation.
unsafe impl Send for ActiveSession {}
unsafe impl Sync for ActiveSession {}

pub static SESSION: Mutex<Option<ActiveSession>> = Mutex::new(None);

/// Id of the in-flight recording session, if any (lock-guarded read).
pub fn active_session_id() -> Option<String> {
    let guard = SESSION.lock().ok()?;
    guard.as_ref().map(|a| a.session.id.clone())
}

pub fn pre_flight_check(probe: bool) -> PreFlightReport {
    pre_flight(probe)
}

pub fn start(spool: &Spool, title: &str, with_system: bool) -> Result<String, String> {
    let mut guard = SESSION.lock().map_err(|e| e.to_string())?;
    if guard.is_some() {
        return Err("recording already active".into());
    }

    let mic_dev = capture::mic_device()?;
    let mic = capture::open_stream(
        &mic_dev,
        std::sync::Arc::new(std::sync::Mutex::new(capture::SampleBuffer::default())),
    )?;

    let (system, system_active) = if with_system {
        match capture::system_device().ok().and_then(|d| {
            capture::open_stream(
                &d,
                std::sync::Arc::new(std::sync::Mutex::new(capture::SampleBuffer::default())),
            )
            .ok()
        }) {
            // Stream opened but its audio is NOT persisted in MVP (drain+discard);
            // system_active stays false so session.json tells the truth.
            Some(s) => (Some(s), false),
            None => (None, false),
        }
    } else {
        (None, false)
    };

    let id = uuid::Uuid::new_v4().to_string();
    let sample_rate = mic.config.sample_rate.0;
    let channels = mic.config.channels.max(1);

    let writer = FlacWriter::create(&spool.audio_path(&id), sample_rate, channels)
        .map_err(|e| e.to_string())?;

    let session = SpoolSession {
        id: id.clone(),
        title: title.to_string(),
        started_at: unix_now_iso(),
        duration_sec: 0.0,
        sample_rate,
        channels,
        mic_active: true,
        system_active,
        uploaded_offset: 0,
        server_rec_id: None,
        finalized: false,
    };
    spool.create(&session).map_err(|e| e.to_string())?;

    *guard = Some(ActiveSession {
        session,
        mic: Some(mic),
        system,
        writer: Mutex::new(Some(writer)),
        started: Instant::now(),
    });
    Ok(id)
}

/// Drain captured buffers into the FLAC writer. Called on a timer.
///
/// Mic samples feed the encoder. The system stream, where a host exposes a
/// duplex default device, is drained-and-discarded: cpal 0.15 has no loopback
/// capture on Win/mac (plan Scenario 1; real system-audio path is the
/// win/mac targets' work). Draining prevents unbounded buffer growth.
pub fn pump(_spool: &Spool) -> Result<u64, String> {
    let guard = SESSION.lock().map_err(|e| e.to_string())?;
    let active = guard.as_ref().ok_or("no active session")?;

    if let Some(system) = &active.system {
        system.drain(); // discard: not muxed into FLAC in MVP
    }

    let samples = active.mic.as_ref().map(|m| m.drain()).unwrap_or_default();
    let frames = (samples.len() / active.session.channels as usize) as u64;
    if samples.is_empty() {
        return Ok(0);
    }
    if let Some(w) = active
        .writer
        .lock()
        .map_err(|e| e.to_string())?
        .as_mut()
    {
        w.write_interleaved(&samples).map_err(|e| e.to_string())?;
    }
    Ok(frames)
}

pub fn stop(spool: &Spool) -> Result<SpoolSession, String> {
    let mut guard = SESSION.lock().map_err(|e| e.to_string())?;
    let mut active = guard.take().ok_or("no active session")?;
    drop(active.mic.take());
    drop(active.system.take());

    // Failure semantics:
    // - writer-lock / write_session failures: retryable — session
    //   re-inserted, stop can run again.
    // - encode-finish failure: FATAL — writer is consumed (finish(mut
    //   self)); a re-inserted session would be a recording zombie with
    //   no writer and a flac that never completes. Prefix such errors
    //   with FATAL_STOP; the UI resets to idle instead of retrying.
    let outcome = (|| -> Result<SpoolSession, (String, bool)> {
        let mut session = active.session.clone();
        // A poisoned writer mutex never recovers. Try to salvage: the
        // guard still owns the writer — attempt finish() on it; only if
        // that also fails is the recording truly lost (FATAL).
        let writer = match active.writer.lock() {
            Ok(mut g) => g.take(),
            Err(poisoned) => {
                let maybe_writer = poisoned.into_inner().take();
                match maybe_writer {
                    Some(w) => {
                        let mut session = active.session.clone();
                        session.duration_sec = active.started.elapsed().as_secs_f64();
                        match w.finish(&spool.audio_path(&session.id)) {
                            Ok(_) => {
                                if let Err(e) = spool.write_session(&session) {
                                    eprintln!("[recording] salvage write_session failed: {e}");
                                }
                                return Ok(session);
                            }
                            Err(e) => {
                                return Err((format!("FATAL_STOP: salvage finish failed: {e}"), true));
                            }
                        }
                    }
                    None => {
                        return Err(("FATAL_STOP: writer lock poisoned and consumed".to_string(), true));
                    }
                }
            }
        };
        session.duration_sec = active.started.elapsed().as_secs_f64();
        if let Some(w) = writer {
            let flac = spool.audio_path(&session.id);
            w.finish(&flac)
                .map_err(|e| (format!("FATAL_STOP: encode failed: {e}"), true))?;
        }
        spool.write_session(&session)
            .map_err(|e: anyhow::Error| (e.to_string(), false))?;
        Ok(session)
    })();
    match outcome {
        Ok(session) => Ok(session),
        Err((e, fatal)) if !fatal => {
            *guard = Some(active);
            Err(e)
        }
        Err((e, _)) => {
            // Fatal: leave spool consistent — mark the session terminal so
            // pending() stops re-enqueueing a doomed upload; keep the .pcm
            // sidecar (recoverable audio) next to the marker.
            let mut dead = active.session.clone();
            dead.duration_sec = active.started.elapsed().as_secs_f64();
            dead.finalized = true; // terminal: excluded from pending()
            dead.title = format!("{} [ENCODE FAILED — pcm kept]", dead.title);
            if let Err(mark_err) = spool.write_session(&dead) {
                eprintln!(
                    "[recording] fatal-marker write failed for {}: {mark_err} \
                    (session may re-enqueue as doomed upload; pcm sidecar kept)",
                    dead.id
                );
            }
            Err(e)
        }
    }
}

fn unix_now_iso() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_default()
}
