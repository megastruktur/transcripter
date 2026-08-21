//! Recording session orchestration: capture → FLAC spool → session.json.

use std::sync::{Arc, Mutex};
use std::time::Instant;

use crate::capture::{self, SampleBuffer};
use crate::encode::FlacWriter;
use crate::permissions::{pre_flight, PreFlightReport};
use crate::spool::{Spool, SpoolSession};

pub(crate) struct ActiveSession {
    pub session: SpoolSession,
    mic: Option<capture::CapturedStream>,
    system: Option<capture::CapturedStream>,
    writer: Mutex<Option<FlacWriter>>,
    started: Instant,
}

pub(crate) static SESSION: Mutex<Option<ActiveSession>> = Mutex::new(None);

pub fn pre_flight_check(probe: bool) -> PreFlightReport {
    pre_flight(probe)
}

pub fn start(spool: &Spool, title: &str, with_system: bool) -> Result<String, String> {
    let mut guard = SESSION.lock().map_err(|e| e.to_string())?;
    if guard.is_some() {
        return Err("recording already active".into());
    }

    let mic_dev = capture::mic_device()?;
    let mic_buf = Arc::new(Mutex::new(SampleBuffer::default()));
    let mic = capture::open_stream(&mic_dev, mic_buf.clone()).map_err(|e| e)?;

    let (system, system_active) = if with_system {
        match capture::system_device().ok().and_then(|d| {
            capture::open_stream(
                &d,
                Arc::new(Mutex::new(SampleBuffer::default())),
            )
            .ok()
        }) {
            Some(s) => (Some(s), true),
            None => (None, false), // plan Scenario 1: record mic-only + warn
        }
    } else {
        (None, false)
    };

    let id = uuid::Uuid::new_v4().to_string();
    let sample_rate = mic.config.sample_rate;
    let channels = mic.config.channels.max(1);

    let writer = FlacWriter::create(&spool.audio_path(&id), sample_rate, channels)
        .map_err(|e| e.to_string())?;

    let session = SpoolSession {
        id: id.clone(),
        title: title.to_string(),
        started_at: chrono_now_iso(),
        duration_sec: 0.0,
        sample_rate,
        channels,
        mic_active: true,
        system_active,
        uploaded_offset: 0,
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

/// Drain captured buffers into the FLAC writer. Called from a timer.
pub fn pump(spool: &Spool) -> Result<u64, String> {
    let guard = SESSION.lock().map_err(|e| e.to_string())?;
    let active = guard.as_ref().ok_or("no active session")?;

    let mut written = 0u64;
    if let Some(mic) = &active.mic {
        let mut buf = mic.buffer.lock().map_err(|e| e.to_string())?;
        if !buf.samples.is_empty() {
            let samples: Vec<f32> = buf.samples.drain(..).collect();
            let frames = (samples.len() / active.session.channels as usize) as u64;
            if let Some(w) = active.writer.lock().map_err(|e| e.to_string())?.as_mut() {
                w.write_interleaved(&samples).map_err(|e| e.to_string())?;
                written += frames;
            }
        }
    }
    Ok(written)
}

pub fn stop(spool: &Spool) -> Result<SpoolSession, String> {
    let mut guard = SESSION.lock().map_err(|e| e.to_string())?;
    let active = guard.take().ok_or("no active session")?;
    let _ = active.mic;
    let _ = active.system;

    let writer = active
        .writer
        .lock()
        .map_err(|e| e.to_string())?
        .take();
    if let Some(w) = writer {
        w.finish().map_err(|e| e.to_string())?;
    }

    let mut session = active.session;
    session.duration_sec = active.started.elapsed().as_secs_f64();
    spool.write_session(&session).map_err(|e| e.to_string())?;
    Ok(session)
}

fn chrono_now_iso() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_default()
}
