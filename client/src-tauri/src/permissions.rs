//! Pre-flight permission + signal checks before every recording start
//! (user requirement: reference app produced an empty first recording).

use serde::Serialize;

use crate::capture;

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum PermissionState {
    Granted,
    Denied,
    NotDetermined,
    Unavailable,
}

/// 1-second probe capture to verify the mic actually delivers samples.
pub(crate) fn probe_mic(threshold_rms: f32) -> Result<bool, String> {
    let device = capture::mic_device()?;
    let buffer = std::sync::Arc::new(std::sync::Mutex::new(
        capture::SampleBuffer::default(),
    ));
    let _stream = capture::open_stream(&device, buffer.clone())?;
    std::thread::sleep(std::time::Duration::from_secs(1));
    let buf = buffer.lock().map_err(|e| e.to_string())?;
    if !buf.errors.is_empty() {
        return Err(buf.errors.join("; "));
    }
    Ok(capture::rms(&buf.samples) > threshold_rms)
}

#[cfg(target_os = "macos")]
pub fn mic_permission() -> PermissionState {
    // macOS 14+: AVAudioApplication shared record permission.
    objc2_avf_audio::AVAudioApplication::sharedInstanceAuthorizationStatus().into()
}

#[cfg(target_os = "macos")]
impl From<objc2_avf_audio::AVAudioApplicationRecordPermission> for PermissionState {
    fn from(v: objc2_avf_audio::AVAudioApplicationRecordPermission) -> Self {
        use objc2_avf_audio::AVAudioApplicationRecordPermission::*;
        match v {
            Undetermined => PermissionState::NotDetermined,
            Denied => PermissionState::Denied,
            Granted => PermissionState::Granted,
            #[allow(unreachable_patterns)]
            _ => PermissionState::Unavailable,
        }
    }
}

#[cfg(not(target_os = "macos"))]
pub fn mic_permission() -> PermissionState {
    // Windows: WASAPI opens without explicit OS permission gate; actual
    // device-open failure surfaces in probe_mic.
    PermissionState::NotDetermined
}

#[derive(Debug, Clone, Serialize)]
pub struct PreFlightReport {
    pub mic_permission: PermissionState,
    pub mic_device_present: bool,
    pub mic_signal: Option<bool>,
    pub system_device_present: bool,
    pub error: Option<String>,
}

/// Run all pre-flight checks. `probe` enables the 1s RMS probe.
pub fn pre_flight(probe: bool) -> PreFlightReport {
    let mut report = PreFlightReport {
        mic_permission: mic_permission(),
        mic_device_present: true,
        mic_signal: None,
        system_device_present: true,
        error: None,
    };

    match capture::mic_device() {
        Ok(_) => {}
        Err(e) => {
            report.mic_device_present = false;
            report.error = Some(e);
            return report;
        }
    }
    match capture::system_device() {
        Ok(_) => {}
        Err(_) => report.system_device_present = false,
    }

    if probe {
        match probe_mic(0.0015) {
            Ok(signal) => report.mic_signal = Some(signal),
            Err(e) => report.error = Some(e),
        }
    }
    report
}
