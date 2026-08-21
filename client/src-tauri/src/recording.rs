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

    // From here on, any failure re-inserts the session so the error
    // honestly means "still live and stop-retryable" (capture streams
    // are already dropped; buffers no longer grow).
    let outcome = (|| -> Result<SpoolSession, String> {
        let writer = active.writer.lock().map_err(|e| e.to_string())?.take();
        let mut session = active.session.clone();
        if let Some(w) = writer {
            let flac = spool.audio_path(&session.id);
            w.finish(&flac).map_err(|e| e.to_string())?;
        }
        session.duration_sec = active.started.elapsed().as_secs_f64();
        spool.write_session(&session).map_err(|e| e.to_string())?;
        Ok(session)
    })();
    match outcome {
        Ok(session) => Ok(session),
        Err(e) => {
            *guard = Some(active);
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
