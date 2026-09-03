use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use crate::capture::{self, CapturedStream, CAPTURE_RATE};
use crate::encode::FlacWriter;
use crate::permissions::{pre_flight, PreFlightReport};
use crate::spool::{Spool, SpoolSession};

pub struct ActiveSession {
    pub session: SpoolSession,
    mic: Arc<CapturedStream>,
    system: Option<Arc<CapturedStream>>,
    writer: Arc<Mutex<Option<FlacWriter>>>,
    running: Arc<AtomicBool>,
    worker: Option<JoinHandle<Result<(), String>>>,
    frames_written: Arc<AtomicU64>,
    capture_error: Arc<Mutex<Option<String>>>,
    /// Non-fatal system-audio loss; surfaced to the UI as a warning.
    degraded: Arc<Mutex<Option<String>>>,
    pub started: Instant,
}

pub static SESSION: Mutex<Option<ActiveSession>> = Mutex::new(None);

pub fn active_session_id() -> Option<String> {
    SESSION
        .lock()
        .ok()?
        .as_ref()
        .map(|active| active.session.id.clone())
}

pub fn pre_flight_check(
    probe: bool,
    microphone: Option<&str>,
    system_output: Option<&str>,
    check_system: bool,
) -> PreFlightReport {
    pre_flight(probe, microphone, system_output, check_system)
}

pub fn start(
    spool: &Spool,
    title: &str,
    tags: &[String],
    microphone: Option<&str>,
    system_output: Option<&str>,
    with_system: bool,
) -> Result<String, String> {
    let mut guard = SESSION.lock().map_err(|e| e.to_string())?;
    if guard.is_some() {
        return Err("recording already active".into());
    }

    let mic = Arc::new(capture::open_mic_stream(microphone)?);
    let system = if with_system {
        Some(Arc::new(capture::open_system_stream(system_output)?))
    } else {
        None
    };

    let id = uuid::Uuid::new_v4().to_string();
    // Dual-source recordings are stored as stereo: mic → L, system → R.
    // The server splits the channels before transcription so the two
    // sources never contaminate each other's transcript.
    let channels: u16 = if system.is_some() { 2 } else { 1 };
    let writer = Arc::new(Mutex::new(Some(
        FlacWriter::create(&spool.audio_path(&id), CAPTURE_RATE, channels).map_err(|e| e.to_string())?,
    )));
    let session = SpoolSession {
        id: id.clone(),
        title: title.to_string(),
        tags: tags.to_vec(),
        started_at: unix_now_iso(),
        duration_sec: 0.0,
        sample_rate: CAPTURE_RATE,
        channels,
        mic_active: true,
        system_active: system.is_some(),
        mic_dropped_frames: 0,
        system_dropped_frames: 0,
        mic_xruns: 0,
        system_xruns: 0,
        capture_error: None,
        uploaded_offset: 0,
        server_rec_id: None,
        finalized: false,
    };
    if let Err(error) = spool.create(&session) {
        let _ = spool.remove(&id);
        return Err(error.to_string());
    }

    let running = Arc::new(AtomicBool::new(true));
    let frames_written = Arc::new(AtomicU64::new(0));
    let capture_error = Arc::new(Mutex::new(None));
    let degraded = Arc::new(Mutex::new(None));
    let worker = spawn_mixer(
        mic.clone(),
        system.clone(),
        writer.clone(),
        running.clone(),
        frames_written.clone(),
        capture_error.clone(),
        degraded.clone(),
    );
    *guard = Some(ActiveSession {
        session,
        mic,
        system,
        writer,
        running,
        worker: Some(worker),
        frames_written,
        capture_error,
        degraded,
        started: Instant::now(),
    });
    Ok(id)
}
fn spawn_mixer(
    mic: Arc<CapturedStream>,
    system: Option<Arc<CapturedStream>>,
    writer: Arc<Mutex<Option<FlacWriter>>>,
    running: Arc<AtomicBool>,
    frames_written: Arc<AtomicU64>,
    capture_error: Arc<Mutex<Option<String>>>,
    degraded_slot: Arc<Mutex<Option<String>>>,
) -> JoinHandle<Result<(), String>> {
    // Layout follows the sources: a system tap means stereo (mic → L,
    // system → R) for the whole file, matching FlacWriter::create above.
    let channels: u16 = if system.is_some() { 2 } else { 1 };
    std::thread::spawn(move || {
        let started = Instant::now();
        let mut written = 0u64;
        let mut mic_samples = Vec::with_capacity(2048);
        let mut system_samples = Vec::with_capacity(2048);
        let mut mixed = Vec::with_capacity(2048);
        let mut last_mic_progress = None;
        let mut last_system_progress = None;
        let mut degraded: Option<String> = None;
        loop {
            let target = (started.elapsed().as_secs_f64() * CAPTURE_RATE as f64) as u64;
            let needed = target.saturating_sub(written).min(2400) as usize;
            if needed > 0 {
                mic_samples.clear();
                system_samples.clear();
                let mic_read = mic.drain_into(&mut mic_samples, needed);
                if mic_read > 0 {
                    last_mic_progress = Some(Instant::now());
                }
                if let Some(source) = &system {
                    let read = source.drain_into(&mut system_samples, needed);
                    if read > 0 {
                        last_system_progress = Some(Instant::now());
                    }
                }
                if running.load(Ordering::Acquire) {
                    // Microphone is the authoritative source: its failure
                    // (stream error or stall) is fatal for the recording.
                    let mic_error = mic
                        .take_error()
                        .map(|error| format!("microphone stream failed: {error}"))
                        .or_else(|| {
                            stall_reason(Instant::now(), started, last_mic_progress)
                        });
                    if let Some(error) = mic_error {
                        if let Ok(mut slot) = capture_error.lock() {
                            *slot = Some(error);
                        }
                        running.store(false, Ordering::Release);
                        break;
                    }
                    // System audio is a bonus source: a tap that never
                    // delivers (cold aggregate IO, no audio flowing) must
                    // not kill the recording — drop it and keep the mic.
                    if degraded.is_none() {
                        let system_failure = system.as_ref().and_then(|source| {
                            source
                                .take_error()
                                .map(|error| format!("system audio stream failed: {error}"))
                        });
                        if let Some(error) = system_failure {
                            degraded = Some(error);
                        } else if let Some(reason) = source_stall_reason(
                            "system audio",
                            Instant::now(),
                            started,
                            last_system_progress,
                            SYSTEM_START_TIMEOUT,
                        ) {
                            degraded = Some(reason);
                        }
                        if degraded.is_some() {
                            // Keep the stream object alive until stop()
                            // collects dropped/xrun counters; it stopped
                            // contributing to the mix either way.
                            system_samples.clear();
                        }
                    }
                }

                // Stereo layout is fixed at start: once the FLAC header
                // says 2 channels, every write MUST carry both — a layout
                // switch mid-file would halve the frame count. The layout
                // (channels) and the source (system samples) are separate
                // concerns: a degraded tap keeps the R channel at digital
                // silence for the rest of the file (sticky, matching the
                // "system audio dropped" warning), never un-degrades.
                mix_samples(
                    &mic_samples,
                    match (&system, &degraded) {
                        (Some(_), None) => Some(system_samples.as_slice()),
                        _ => None,
                    },
                    channels,
                    needed,
                    &mut mixed,
                );
                let mut slot = writer.lock().map_err(|e| e.to_string())?;
                let sink = slot
                    .as_mut()
                    .ok_or_else(|| "audio writer unavailable".to_string())?;
                sink.write_interleaved(&mixed).map_err(|e| e.to_string())?;
                written += needed as u64;
                frames_written.store(written, Ordering::Relaxed);
            }
            if !running.load(Ordering::Acquire) {
                let final_target = (started.elapsed().as_secs_f64() * CAPTURE_RATE as f64) as u64;
                if written >= final_target {
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        if degraded.is_some() {
            if let Ok(mut slot) = degraded_slot.lock() {
                *slot = degraded;
            }
        }
        Ok(())
    })
}

const SOURCE_STALL_TIMEOUT: Duration = Duration::from_millis(250);
/// The microphone is authoritative: if it delivers nothing this fast, the
/// recording cannot proceed.
const MIC_START_TIMEOUT: Duration = Duration::from_secs(1);
/// How long the system-audio tap may take to deliver its first samples.
/// Taps only produce audio while output flows; a slow first spin-up of the
/// aggregate IO must not kill the recording (mic stays authoritative).
const SYSTEM_START_TIMEOUT: Duration = Duration::from_secs(10);

fn stall_reason(
    now: Instant,
    started: Instant,
    last_mic_progress: Option<Instant>,
) -> Option<String> {
    source_stall_reason("microphone", now, started, last_mic_progress, MIC_START_TIMEOUT)
}

fn source_stall_reason(
    label: &str,
    now: Instant,
    started: Instant,
    last_progress: Option<Instant>,
    start_timeout: Duration,
) -> Option<String> {
    match last_progress {
        Some(last) if now.duration_since(last) >= SOURCE_STALL_TIMEOUT => {
            Some(format!("{label} stopped delivering samples"))
        }
        None if now.duration_since(started) >= start_timeout => {
            Some(format!("{label} did not start delivering samples"))
        }
        _ => None,
    }
}

fn mix_samples(
    mic: &[f32],
    system: Option<&[f32]>,
    channels: u16,
    frames: usize,
    output: &mut Vec<f32>,
) {
    // Interleaved output for the FLAC writer. Mono (mic-only): one sample
    // per frame, gain untouched. Stereo (mic + system taps): the sources
    // stay SEPARATE — mic → left, system → right — so the server can split
    // the channels back into per-source tracks and transcribe/diarize them
    // independently (summing them here would destroy speaker attribution
    // for every overlapping moment). A `channels: 2` call with `None`
    // (degraded tap) emits digital silence on the right: the layout is
    // fixed at start and never switches mid-file.
    output.clear();
    if channels <= 1 {
        output.extend_from_slice(&mic[..frames.min(mic.len())]);
        output.resize(frames, 0.0);
        return;
    }
    for index in 0..frames {
        let left = mic.get(index).copied().unwrap_or(0.0);
        let right = system
            .and_then(|samples| samples.get(index))
            .copied()
            .unwrap_or(0.0);
        output.push(left);
        output.push(right);
    }
}

pub fn frames_written() -> Result<u64, String> {
    let guard = SESSION.lock().map_err(|e| e.to_string())?;
    let active = guard.as_ref().ok_or("no active session")?;
    if let Some(error) = active
        .capture_error
        .lock()
        .map_err(|e| e.to_string())?
        .clone()
    {
        return Err(format!("capture stopped: {error}"));
    }
    Ok(active.frames_written.load(Ordering::Relaxed))
}

/// Non-fatal system-audio loss, if one occurred this session.
pub fn degraded_reason() -> Option<String> {
    SESSION.lock().ok().and_then(|guard| {
        guard
            .as_ref()
            .and_then(|active| active.degraded.lock().ok().and_then(|slot| slot.clone()))
    })
}

pub fn stop(spool: &Spool) -> Result<SpoolSession, String> {
    let mut active = SESSION
        .lock()
        .map_err(|e| e.to_string())?
        .take()
        .ok_or("no active session")?;
    active.running.store(false, Ordering::Release);
    if let Some(worker) = active.worker.take() {
        match worker.join() {
            Ok(Ok(())) => {}
            Ok(Err(error)) => return fatal(spool, &active, format!("mixer failed: {error}")),
            Err(_) => return fatal(spool, &active, "mixer thread panicked".into()),
        }
    }

    let writer = match active.writer.lock() {
        Ok(mut slot) => slot.take(),
        Err(error) => return fatal(spool, &active, format!("writer lock poisoned: {error}")),
    };
    let Some(writer) = writer else {
        return fatal(spool, &active, "audio writer was already consumed".into());
    };
    if let Err(error) = writer.finish(&spool.audio_path(&active.session.id)) {
        return fatal(spool, &active, format!("encode failed: {error}"));
    }

    let mic_dropped_frames = active.mic.dropped();
    let system_dropped_frames = active.system.as_ref().map_or(0, |source| source.dropped());
    let mic_xruns = active.mic.xruns();
    let system_xruns = active.system.as_ref().map_or(0, |source| source.xruns());
    let capture_error = active
        .capture_error
        .lock()
        .ok()
        .and_then(|error| error.clone())
        .or_else(|| {
            let degraded = active
                .degraded
                .lock()
                .ok()
                .and_then(|slot| slot.clone());
            degraded.map(|reason| format!("system audio dropped: {reason}"))
        });

    let mut session = active.session;
    session.duration_sec = active.started.elapsed().as_secs_f64();
    session.mic_dropped_frames = mic_dropped_frames;
    session.system_dropped_frames = system_dropped_frames;
    session.mic_xruns = mic_xruns;
    session.system_xruns = system_xruns;
    session.capture_error = capture_error;
    let mut last_error = None;
    for attempt in 0..3 {
        match spool.write_session(&session) {
            Ok(()) => return Ok(session),
            Err(error) => {
                last_error = Some(error.to_string());
                if attempt < 2 {
                    std::thread::sleep(Duration::from_millis(250 * (1 << attempt)));
                }
            }
        }
    }
    Err(last_error.unwrap_or_else(|| "could not persist session".into()))
}

fn fatal(spool: &Spool, active: &ActiveSession, message: String) -> Result<SpoolSession, String> {
    let mut dead = active.session.clone();
    dead.duration_sec = active.started.elapsed().as_secs_f64();
    dead.mic_dropped_frames = active.mic.dropped();
    dead.system_dropped_frames = active.system.as_ref().map_or(0, |source| source.dropped());
    dead.mic_xruns = active.mic.xruns();
    dead.system_xruns = active.system.as_ref().map_or(0, |source| source.xruns());
    dead.capture_error = active
        .capture_error
        .lock()
        .ok()
        .and_then(|error| error.clone())
        .or_else(|| Some(message.clone()));
    dead.finalized = true;
    dead.title = format!("{} [CAPTURE FAILED — pcm kept]", dead.title);
    let _ = spool.write_session(&dead);
    Err(format!("FATAL_STOP: {message}"))
}

fn unix_now_iso() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs().to_string())
        .unwrap_or_default()
}
#[cfg(test)]
mod tests {
    use super::{
        mix_samples, source_stall_reason, stall_reason, MIC_START_TIMEOUT, SOURCE_STALL_TIMEOUT,
        SYSTEM_START_TIMEOUT,
    };
    use std::time::{Duration, Instant};

    #[test]
    fn mic_only_preserves_level_and_fills_gaps() {
        let mut output = Vec::new();
        mix_samples(&[0.25, -0.5], None, 1, 3, &mut output);
        assert_eq!(output, vec![0.25, -0.5, 0.0]);
    }

    #[test]
    fn dual_source_keeps_channels_separate() {
        // mic → L, system → R, interleaved; no summing, no gain, no clamp
        // interaction between the two independent sources.
        let mut output = Vec::new();
        mix_samples(&[0.5, -1.0], Some(&[0.25, 0.75]), 2, 2, &mut output);
        assert_eq!(output, vec![0.5, 0.25, -1.0, 0.75]);
    }

    #[test]
    fn degraded_tap_writes_digital_silence_on_right() {
        // Layout is fixed at start: stereo recording with a dead system
        // tap keeps writing 2 samples per frame (R = silence).
        let mut output = Vec::new();
        mix_samples(&[0.2, 0.2], None, 2, 2, &mut output);
        assert_eq!(output, vec![0.2, 0.0, 0.2, 0.0]);
    }

    #[test]
    fn missing_system_frames_become_silence_without_shortening_output() {
        let mut output = Vec::new();
        mix_samples(&[0.2, 0.2], Some(&[]), 2, 2, &mut output);
        assert_eq!(output, vec![0.2, 0.0, 0.2, 0.0]);
    }


    #[test]
    fn mic_stall_guard_distinguishes_startup_from_midstream_failure() {
        let now = Instant::now();
        let started = now - MIC_START_TIMEOUT - Duration::from_millis(1);
        let stalled = now - SOURCE_STALL_TIMEOUT - Duration::from_millis(1);
        assert_eq!(
            stall_reason(now, started, None).as_deref(),
            Some("microphone did not start delivering samples")
        );
        assert_eq!(
            stall_reason(now, started, Some(stalled)).as_deref(),
            Some("microphone stopped delivering samples")
        );
        let within_grace = now - MIC_START_TIMEOUT + Duration::from_millis(1);
        assert_eq!(stall_reason(now, within_grace, None), None);
        assert_eq!(stall_reason(now, now, None), None);
    }

    #[test]
    fn system_start_window_is_far_wider_than_the_mic_one() {
        let now = Instant::now();
        let started = now - MIC_START_TIMEOUT - Duration::from_millis(1);
        // The tap gets a much wider startup window than the mic: past the
        // mic deadline but inside the system grace there is no failure yet.
        assert_eq!(
            source_stall_reason(
                "system audio",
                now,
                started,
                None,
                SYSTEM_START_TIMEOUT
            ),
            None
        );
        let cold = now - SYSTEM_START_TIMEOUT - Duration::from_millis(1);
        assert_eq!(
            source_stall_reason("system audio", now, cold, None, SYSTEM_START_TIMEOUT).as_deref(),
            Some("system audio did not start delivering samples")
        );
        let stalled = now - SOURCE_STALL_TIMEOUT - Duration::from_millis(1);
        assert_eq!(
            source_stall_reason("system audio", now, now, Some(stalled), SYSTEM_START_TIMEOUT)
                .as_deref(),
            Some("system audio stopped delivering samples")
        );
    }
}
