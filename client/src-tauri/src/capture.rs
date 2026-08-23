use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, FromSample, Sample, SampleFormat, Stream, StreamConfig, SupportedStreamConfig};
use ringbuf::traits::{Consumer, Observer, Producer, Split};
use ringbuf::{HeapCons, HeapRb};
use serde::Serialize;

#[cfg(target_os = "macos")]
use crate::capture_macos::MacLoopbackDevice;
#[cfg(target_os = "windows")]
use crate::capture_windows;

pub const CAPTURE_RATE: u32 = 48_000;
const QUEUE_SECONDS: usize = 5;

#[derive(Debug, Clone, Serialize)]
pub struct AudioDeviceInfo {
    pub id: String,
    pub label: String,
    pub is_default: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct AudioDevices {
    pub microphones: Vec<AudioDeviceInfo>,
    pub system_outputs: Vec<AudioDeviceInfo>,
    pub default_microphone: Option<String>,
    pub default_system_output: Option<String>,
}

pub struct CapturedStream {
    _stream: Stream,
    consumer: Mutex<HeapCons<f32>>,
    xruns: Arc<AtomicU64>,
    dropped: Arc<AtomicU64>,
    last_error: Arc<Mutex<Option<String>>>,
    pub config: StreamConfig,
    #[cfg(target_os = "macos")]
    _loopback: Option<MacLoopbackDevice>,
}

impl CapturedStream {
    pub fn drain_into(&self, out: &mut Vec<f32>, limit: usize) -> usize {
        let Ok(mut consumer) = self.consumer.lock() else {
            return 0;
        };
        let start = out.len();
        let count = limit.min(consumer.occupied_len());
        out.resize(start + count, 0.0);
        let read = consumer.pop_slice(&mut out[start..]);
        out.truncate(start + read);
        read
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    pub fn xruns(&self) -> u64 {
        self.xruns.load(Ordering::Relaxed)
    }

    pub fn take_error(&self) -> Option<String> {
        self.last_error.lock().ok()?.take()
    }
}

pub fn list_devices() -> Result<AudioDevices, String> {
    let host = cpal::default_host();
    let default_mic = host
        .default_input_device()
        .and_then(|d| d.id().ok())
        .map(|id| id.to_string());
    let default_output = host
        .default_output_device()
        .and_then(|d| d.id().ok())
        .map(|id| id.to_string());
    let mut microphones = collect_devices(
        host.input_devices().map_err(|e| e.to_string())?,
        default_mic.as_deref(),
    );
    let mut system_outputs = collect_devices(
        host.output_devices().map_err(|e| e.to_string())?,
        default_output.as_deref(),
    );
    microphones.sort_by(|a, b| a.label.cmp(&b.label).then(a.id.cmp(&b.id)));
    system_outputs.sort_by(|a, b| a.label.cmp(&b.label).then(a.id.cmp(&b.id)));
    Ok(AudioDevices {
        microphones,
        system_outputs,
        default_microphone: default_mic,
        default_system_output: default_output,
    })
}

fn collect_devices(
    devices: impl Iterator<Item = Device>,
    default_id: Option<&str>,
) -> Vec<AudioDeviceInfo> {
    devices
        .filter_map(|device| {
            let id = device.id().ok()?.to_string();
            let label = device.description().ok()?.name().to_string();
            Some(AudioDeviceInfo {
                is_default: default_id == Some(id.as_str()),
                id,
                label,
            })
        })
        .collect()
}

pub fn mic_device(id: Option<&str>) -> Result<Device, String> {
    resolve_device(id, true)
}

pub fn system_device(id: Option<&str>) -> Result<Device, String> {
    resolve_device(id, false)
}

fn resolve_device(id: Option<&str>, input: bool) -> Result<Device, String> {
    let host = cpal::default_host();
    if let Some(id) = id.filter(|id| !id.is_empty()) {
        let parsed = id
            .parse::<cpal::DeviceId>()
            .map_err(|e| format!("invalid audio device id: {e}"))?;
        return host
            .device_by_id(&parsed)
            .ok_or_else(|| "selected audio device is no longer available".into());
    }
    if input {
        host.default_input_device()
            .ok_or_else(|| "no microphone".into())
    } else {
        host.default_output_device()
            .ok_or_else(|| "no system output device".into())
    }
}

pub fn open_mic_stream(id: Option<&str>) -> Result<CapturedStream, String> {
    let device = mic_device(id)?;
    let config = preferred_input_config(&device)?;
    open_stream(&device, config)
}

pub fn open_system_stream(id: Option<&str>) -> Result<CapturedStream, String> {
    #[cfg(target_os = "windows")]
    {
        let device = capture_windows::resolve_output(id)?;
        let config = capture_windows::loopback_config(&device)?;
        open_stream(&device, config)
    }
    #[cfg(target_os = "macos")]
    {
        let output = system_device(id)?;
        let loopback = crate::capture_macos::create_loopback(&output)?;
        let config = preferred_input_config(loopback.device())?;
        let mut stream = open_stream(loopback.device(), config)?;
        stream._loopback = Some(loopback);
        Ok(stream)
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let _ = id;
        Err("system audio capture is supported on macOS and Windows only".into())
    }
}

pub(crate) fn preferred_input_config(device: &Device) -> Result<SupportedStreamConfig, String> {
    device
        .supported_input_configs()
        .map_err(|error| error.to_string())?
        .filter_map(|range| range.try_with_sample_rate(CAPTURE_RATE))
        .filter(|config| sample_rank(config.sample_format()).is_some())
        .max_by_key(|config| sample_rank(config.sample_format()).unwrap_or_default())
        .ok_or_else(|| format!("audio input does not support {CAPTURE_RATE} Hz"))
}

#[cfg(target_os = "windows")]
pub(crate) fn preferred_output_config(device: &Device) -> Result<SupportedStreamConfig, String> {
    device
        .supported_output_configs()
        .map_err(|error| error.to_string())?
        .filter_map(|range| range.try_with_sample_rate(CAPTURE_RATE))
        .filter(|config| sample_rank(config.sample_format()).is_some())
        .max_by_key(|config| sample_rank(config.sample_format()).unwrap_or_default())
        .ok_or_else(|| format!("system output does not support {CAPTURE_RATE} Hz"))
}

fn sample_rank(format: SampleFormat) -> Option<u8> {
    match format {
        SampleFormat::F32 => Some(10),
        SampleFormat::F64 => Some(9),
        SampleFormat::I32 => Some(8),
        SampleFormat::I16 => Some(7),
        SampleFormat::I64 => Some(6),
        SampleFormat::I8 => Some(5),
        SampleFormat::U32 => Some(4),
        SampleFormat::U16 => Some(3),
        SampleFormat::U64 => Some(2),
        SampleFormat::U8 => Some(1),
        _ => None,
    }
}

fn open_stream(device: &Device, config: SupportedStreamConfig) -> Result<CapturedStream, String> {
    if config.sample_rate() != CAPTURE_RATE {
        return Err(format!(
            "audio device must support {CAPTURE_RATE} Hz (selected {} Hz)",
            config.sample_rate()
        ));
    }
    let channels = config.channels().max(1) as usize;
    let stream_config: StreamConfig = config.config();
    let queue = HeapRb::<f32>::new(CAPTURE_RATE as usize * QUEUE_SECONDS);
    let (producer, consumer) = queue.split();
    let xruns = Arc::new(AtomicU64::new(0));
    let dropped = Arc::new(AtomicU64::new(0));
    let last_error = Arc::new(Mutex::new(None));
    let errors = last_error.clone();
    let error_xruns = xruns.clone();
    let error_fn = move |error: cpal::Error| {
        if error.kind() == cpal::ErrorKind::Xrun {
            error_xruns.fetch_add(1, Ordering::Relaxed);
        } else if let Ok(mut slot) = errors.lock() {
            *slot = Some(error.to_string());
        }
    };
    let format = config.sample_format();
    let stream = match format {
        SampleFormat::F32 => build::<f32>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::F64 => build::<f64>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::I8 => build::<i8>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::I16 => build::<i16>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::I32 => build::<i32>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::I64 => build::<i64>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::U8 => build::<u8>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::U16 => build::<u16>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::U32 => build::<u32>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        SampleFormat::U64 => build::<u64>(
            device,
            stream_config,
            channels,
            producer,
            dropped.clone(),
            error_fn,
        ),
        _ => return Err(format!("unsupported sample format {format:?}")),
    }?;
    stream.play().map_err(|e| e.to_string())?;
    Ok(CapturedStream {
        _stream: stream,
        consumer: Mutex::new(consumer),
        dropped,
        xruns,
        last_error,
        config: stream_config,
        #[cfg(target_os = "macos")]
        _loopback: None,
    })
}

fn build<T>(
    device: &Device,
    config: StreamConfig,
    channels: usize,
    mut producer: ringbuf::HeapProd<f32>,
    dropped: Arc<AtomicU64>,
    error_fn: impl FnMut(cpal::Error) + Send + 'static,
) -> Result<Stream, String>
where
    T: Sample + cpal::SizedSample,
    f32: FromSample<T>,
{
    device
        .build_input_stream(
            config,
            move |data: &[T], _| {
                for frame in data.chunks_exact(channels) {
                    let mono =
                        frame.iter().copied().map(f32::from_sample).sum::<f32>() / channels as f32;
                    if producer.try_push(mono).is_err() {
                        dropped.fetch_add(1, Ordering::Relaxed);
                    }
                }
            },
            error_fn,
            None,
        )
        .map_err(|e| e.to_string())
}

pub fn rms(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    (samples.iter().map(|s| s * s).sum::<f32>() / samples.len() as f32).sqrt()
}
