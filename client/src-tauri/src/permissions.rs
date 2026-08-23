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

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum SourceState {
    Disabled,
    Ready,
    Silent,
    PermissionDenied,
    Unavailable,
    Failed,
}

#[derive(Debug, Clone, Serialize)]
pub struct PreFlightReport {
    pub mic_permission: PermissionState,
    pub mic_state: SourceState,
    pub mic_signal: Option<bool>,
    pub system_state: SourceState,
    pub system_signal: Option<bool>,
    pub error: Option<String>,
}

#[cfg(target_os = "macos")]
pub fn mic_permission() -> PermissionState {
    unsafe {
        objc2_avf_audio::AVAudioApplication::sharedInstance()
            .recordPermission()
            .into()
    }
}

#[cfg(target_os = "macos")]
impl From<objc2_avf_audio::AVAudioApplicationRecordPermission> for PermissionState {
    fn from(value: objc2_avf_audio::AVAudioApplicationRecordPermission) -> Self {
        use objc2_avf_audio::AVAudioApplicationRecordPermission as P;
        if value == P::Undetermined {
            Self::NotDetermined
        } else if value == P::Denied {
            Self::Denied
        } else if value == P::Granted {
            Self::Granted
        } else {
            Self::Unavailable
        }
    }
}

#[cfg(target_os = "windows")]
pub fn mic_permission() -> PermissionState {
    // Desktop (Win32) apps have no per-app permission prompt on Windows; access
    // for non-packaged apps is gated by the global "Let desktop apps access
    // your microphone" toggle, readable from HKCU ConsentStore\microphone
    // \NonPackaged. When denied, WASAPI opens the stream without error but
    // delivers silence, so surface it here instead of reporting "no signal".
    let value = windows_registry::CURRENT_USER
        .open(
            "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone\\NonPackaged",
        )
        .and_then(|key| key.get_string("Value"));
    match value.as_deref() {
        Ok("Deny") => PermissionState::Denied,
        Ok(_) => PermissionState::Granted,
        Err(_) => PermissionState::NotDetermined,
    }
}

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
pub fn mic_permission() -> PermissionState {
    PermissionState::NotDetermined
}

pub fn pre_flight(
    probe: bool,
    microphone: Option<&str>,
    system_output: Option<&str>,
    check_system: bool,
) -> PreFlightReport {
    let permission = mic_permission();
    let mut report = PreFlightReport {
        mic_permission: permission.clone(),
        mic_state: SourceState::Unavailable,
        mic_signal: None,
        system_state: if check_system {
            SourceState::Unavailable
        } else {
            SourceState::Disabled
        },
        system_signal: None,
        error: None,
    };
    if permission == PermissionState::Denied {
        report.mic_state = SourceState::PermissionDenied;
        report.error = Some("microphone permission denied".into());
        return report;
    }

    match if probe {
        capture::open_mic_stream(microphone).map(Some)
    } else {
        capture::mic_device(microphone).map(|_| None)
    } {
        Ok(stream) => {
            if let Some(stream) = stream {
                std::thread::sleep(std::time::Duration::from_millis(500));
                let mut samples = Vec::new();
                stream.drain_into(&mut samples, capture::CAPTURE_RATE as usize);
                let signal = capture::rms(&samples) > 0.0015;
                report.mic_signal = Some(signal);
                report.mic_state = if signal {
                    SourceState::Ready
                } else {
                    SourceState::Silent
                };
            } else {
                report.mic_state = SourceState::Ready;
            }
        }
        Err(error) => {
            report.mic_state = SourceState::Unavailable;
            report.error = Some(error);
            return report;
        }
    }

    if check_system {
        match if probe {
            capture::open_system_stream(system_output).map(Some)
        } else {
            capture::system_device(system_output).map(|_| None)
        } {
            Ok(stream) => {
                if let Some(stream) = stream {
                    std::thread::sleep(std::time::Duration::from_millis(500));
                    let mut samples = Vec::new();
                    stream.drain_into(&mut samples, capture::CAPTURE_RATE as usize);
                    let signal = capture::rms(&samples) > 0.0005;
                    report.system_signal = Some(signal);
                    report.system_state = if signal {
                        SourceState::Ready
                    } else {
                        SourceState::Silent
                    };
                } else {
                    report.system_state = SourceState::Ready;
                }
            }
            Err(error) => {
                report.system_state = if error.to_ascii_lowercase().contains("permission") {
                    SourceState::PermissionDenied
                } else {
                    SourceState::Unavailable
                };
                report.error = Some(format!("system audio: {error}"));
            }
        }
    }
    report
}
