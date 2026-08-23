use std::str::FromStr;

use cpal::traits::HostTrait;

pub fn resolve_output(id: Option<&str>) -> Result<cpal::Device, String> {
    let host = cpal::default_host();
    match id.filter(|id| !id.is_empty()) {
        Some(id) => {
            let id = cpal::DeviceId::from_str(id)
                .map_err(|e| format!("invalid output device id: {e}"))?;
            if id.host() != cpal::platform::HostId::Wasapi {
                return Err("selected system output is not a WASAPI device".into());
            }
            host.device_by_id(&id)
                .ok_or_else(|| "selected system output is no longer available".into())
        }
        None => host
            .default_output_device()
            .ok_or_else(|| "no system output device".into()),
    }
}

pub fn loopback_config(device: &cpal::Device) -> Result<cpal::SupportedStreamConfig, String> {
    crate::capture::preferred_output_config(device)
}
