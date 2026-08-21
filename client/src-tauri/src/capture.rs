//! Microphone + system audio capture via cpal (reference: ActaVoces).
//!
//! System audio: input stream on the default OUTPUT device (WASAPI loopback
//! on Windows, CoreAudio trick on macOS — see plan Scenario 1).
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

/// Open the default input device (microphone).
pub fn mic_device() -> Result<Device, String> {
    let host = cpal::default_host();
    host.default_input_device().ok_or_else(|| "no microphone".into())
}

/// Open the default output device as an input (loopback) stream.
pub fn system_device() -> Result<Device, String> {
    let host = cpal::default_host();
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
