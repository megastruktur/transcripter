//! FLAC encoding for the spool (streamed, no RAM buffering).

use std::fs::File;
use std::io::Write;
use std::path::Path;

use flac_bound::FlacEncoder;

/// Append-only FLAC file writer for interleaved f32 frames.
pub struct FlacWriter {
    encoder: FlacEncoder<File>,
    channels: u16,
    sample_rate: u32,
    frames_written: u64,
}

impl FlacWriter {
    pub fn create(
        path: &Path,
        sample_rate: u32,
        channels: u16,
    ) -> anyhow::Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = File::create(path)?;
        let encoder = FlacEncoder::new_typed()
            .channels(channels as u32)
            .bits_per_sample(16)
            .sample_rate(sample_rate)
            .init_file(file)?;
        Ok(Self {
            encoder,
            channels,
            sample_rate,
            frames_written: 0,
        })
    }

    pub fn channels(&self) -> u16 {
        self.channels
    }

    pub fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    /// Append f32 interleaved samples; converts to i16 on the fly.
    pub fn write_interleaved(&mut self, samples: &[f32]) -> anyhow::Result<()> {
        if samples.is_empty() {
            return Ok(());
        }
        let i16s: Vec<i16> = samples
            .iter()
            .map(|&s| (s.clamp(-1.0, 1.0) * 32767.0) as i16)
            .collect();
        // flac-bound expects one channel slice per channel: interleave→split
        let ch = self.channels as usize;
        for c in 0..ch {
            let chan: Vec<i16> = i16s[c..].iter().step_by(ch).copied().collect();
            self.encoder
                .process_interleaved_reorder(&i16s, i16s.len() / ch)?;
            break; // process_interleaved handles all channels at once
        }
        self.frames_written += (i16s.len() / ch) as u64;
        Ok(())
    }

    pub fn finish(mut self) -> anyhow::Result<u64> {
        self.encoder.finish_io()?;
        Ok(self.frames_written)
    }
}

/// SHA-256 of a file, hex string.
pub fn file_sha256(path: &Path) -> anyhow::Result<String> {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    let mut f = File::open(path)?;
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = std::io::Read::read(&mut f, &mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let out = hasher.finalize();
    let mut hex = String::with_capacity(64);
    for b in out {
        hex.push_str(&format!("{b:02x}"));
    }
    Ok(hex)
}

// Silence unused import when not used on this platform path
#[allow(unused)]
fn _touch() {
    let _ = std::io::sink().write(&[]);
}
