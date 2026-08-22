//! Microphone + system audio capture via cpal (reference: ActaVoces).
//!
//! System audio: input stream on the selected output device, falling back to
//! the OS default (WASAPI loopback on Windows, CoreAudio path on macOS).
//! Each source is captured independently; missing system stream does not
//! block mic recording.

use std::sync::{Arc, Mutex};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, SampleFormat, Stream, StreamConfig};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct CaptureStatus {
    pub mic_active: bool,
    pub system_active: bool,
    pub sample_rate: u32,
    pub channels: u16,
}

#[derive(Debug, Clone, Serialize)]
pub struct AudioDevices {
    pub microphones: Vec<String>,
    pub system_outputs: Vec<String>,
    pub default_microphone: Option<String>,
    pub default_system_output: Option<String>,
}

#[derive(Default)]
pub struct SampleBuffer {
    pub samples: Vec<f32>,
    pub errors: Vec<String>,
}

pub struct CapturedStream {
    _stream: Stream,
    pub buffer: Arc<Mutex<SampleBuffer>>,
    pub config: StreamConfig,
}

impl CapturedStream {
    pub fn drain(&self) -> Vec<f32> {
        match self.buffer.lock() {
            Ok(mut b) => std::mem::take(&mut b.samples),
            Err(_) => Vec::new(),
        }
    }
}

pub fn list_devices() -> Result<AudioDevices, String> {
    let host = cpal::default_host();
    let mut microphones = host
        .input_devices()
        .map_err(|e| e.to_string())?
        .filter_map(|device| device.name().ok())
        .collect::<Vec<_>>();
    let mut system_outputs = host
        .output_devices()
        .map_err(|e| e.to_string())?
        .filter_map(|device| device.name().ok())
        .collect::<Vec<_>>();

    microphones.sort();
    microphones.dedup();
    system_outputs.sort();
    system_outputs.dedup();

    Ok(AudioDevices {
        microphones,
        system_outputs,
        default_microphone: host
            .default_input_device()
            .and_then(|device| device.name().ok()),
        default_system_output: host
            .default_output_device()
            .and_then(|device| device.name().ok()),
    })
}

/// Open the selected input device, or the OS default when no name is provided.
pub fn mic_device(name: Option<&str>) -> Result<Device, String> {
    let host = cpal::default_host();
    if let Some(name) = name.filter(|name| !name.is_empty()) {
        return host
            .input_devices()
            .map_err(|e| e.to_string())?
            .find(|device| device.name().ok().as_deref() == Some(name))
            .ok_or_else(|| format!("microphone not found: {name}"));
    }
    host.default_input_device()
        .ok_or_else(|| "no microphone".into())
}

/// Open the selected output device for the platform system-audio path.
pub fn system_device(name: Option<&str>) -> Result<Device, String> {
    let host = cpal::default_host();
    if let Some(name) = name.filter(|name| !name.is_empty()) {
        return host
            .output_devices()
            .map_err(|e| e.to_string())?
            .find(|device| device.name().ok().as_deref() == Some(name))
            .ok_or_else(|| format!("system output not found: {name}"));
    }
    host.default_output_device()
        .ok_or_else(|| "no system output device".into())
}

pub fn open_stream(
    device: &Device,
    buffer: Arc<Mutex<SampleBuffer>>,
) -> Result<CapturedStream, String> {
    let config = device.default_input_config().map_err(|e| e.to_string())?;
    let fmt: SampleFormat = config.sample_format();
    let stream_config: StreamConfig = config.into();

    let err_buf = buffer.clone();
    let err_fn = move |e: cpal::StreamError| {
        if let Ok(mut b) = err_buf.lock() {
            b.errors.push(e.to_string());
        }
    };

    let buf = buffer.clone();
    let stream = match fmt {
        SampleFormat::F32 => device
            .build_input_stream(
                &stream_config,
                move |data: &[f32], _| {
                    if let Ok(mut b) = buf.lock() {
                        b.samples.extend_from_slice(data);
                    }
                },
                err_fn,
                None,
            )
            .map_err(|e| e.to_string())?,
        SampleFormat::I16 => device
            .build_input_stream(
                &stream_config,
                move |data: &[i16], _| {
                    if let Ok(mut b) = buf.lock() {
                        b.samples.extend(data.iter().map(|&s| s as f32 / 32768.0));
                    }
                },
                err_fn,
                None,
            )
            .map_err(|e| e.to_string())?,
        SampleFormat::U16 => device
            .build_input_stream(
                &stream_config,
                move |data: &[u16], _| {
                    if let Ok(mut b) = buf.lock() {
                        b.samples
                            .extend(data.iter().map(|&s| (s as f32 - 32768.0) / 32768.0));
                    }
                },
                err_fn,
                None,
            )
            .map_err(|e| e.to_string())?,
        other => return Err(format!("unsupported sample format {other:?}")),
    };
    stream.play().map_err(|e| e.to_string())?;
    Ok(CapturedStream {
        _stream: stream,
        buffer,
        config: stream_config,
    })
}

/// RMS of recent samples, for silence detection (pre-flight warning).
pub fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum: f32 = samples.iter().map(|s| s * s).sum();
    (sum / samples.len() as f32).sqrt()
}
