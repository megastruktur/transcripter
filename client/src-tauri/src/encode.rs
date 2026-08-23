//! FLAC encoding for the spool via flacenc (pure Rust).
//!
//! During recording we append raw interleaved i16 PCM to `<id>.pcm`; on stop,
//! `FileSource` feeds it to `encode_with_fixed_block_size` which produces a
//! fully valid FLAC stream (STREAMINFO with md5 + total samples). This keeps
//! correctness high (reference encoder path) and RAM flat (disk-backed).

use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use flacenc::bitsink::ByteSink;
use flacenc::component::BitRepr;

const BLOCK_SIZE: usize = 4096;

/// Raw PCM spool sidecar + final FLAC encoder.
pub struct FlacWriter {
    pcm_path: PathBuf,
    pcm: std::fs::File,
    channels: u16,
    sample_rate: u32,
    frames_written: u64,
}

impl FlacWriter {
    pub fn create(path: &Path, sample_rate: u32, channels: u16) -> anyhow::Result<Self> {
        // `path` is the target .flac; PCM goes next to it during recording.
        let pcm_path = path.with_extension("pcm");
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let pcm = std::fs::File::create(&pcm_path)?;
        Ok(Self {
            pcm_path,
            pcm,
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

    /// Append interleaved f32 samples (converted to 16-bit) to the PCM sidecar.
    pub fn write_interleaved(&mut self, samples: &[f32]) -> anyhow::Result<()> {
        let ch = self.channels as usize;
        if ch == 0 || samples.is_empty() {
            return Ok(());
        }
        let usable = (samples.len() / ch) * ch;
        let mut buf = Vec::with_capacity(usable * 2);
        for &s in &samples[..usable] {
            let v = (s.clamp(-1.0, 1.0) * 32767.0) as i16;
            buf.extend_from_slice(&v.to_le_bytes());
        }
        self.pcm.write_all(&buf)?;
        self.frames_written += (usable / ch) as u64;
        Ok(())
    }

    /// Encode PCM sidecar to the final FLAC file; removes the sidecar.
    pub fn finish(mut self, flac_path: &Path) -> anyhow::Result<u64> {
        self.pcm.flush()?;
        drop(self.pcm);

        let source = FileSource::open(&self.pcm_path, self.sample_rate, self.channels)?;
        use flacenc::error::Verify;
        let cfg = flacenc::config::Encoder::default()
            .into_verified()
            .map_err(|e| anyhow::anyhow!("config: {e:?}"))?;
        let stream = flacenc::encode_with_fixed_block_size(&cfg, source, BLOCK_SIZE)
            .map_err(|e| anyhow::anyhow!("encode: {e:?}"))?;

        let mut sink = ByteSink::new();
        stream
            .write(&mut sink)
            .map_err(|e| anyhow::anyhow!("bit write: {e:?}"))?;
        std::fs::write(flac_path, sink.as_slice())?;
        std::fs::remove_file(&self.pcm_path).ok();
        Ok(self.frames_written)
    }
}

/// Disk-backed `Source` reading interleaved 16-bit LE PCM.
struct FileSource {
    file: std::fs::File,
    sample_rate: usize,
    channels: usize,
    total_frames: usize,
}

impl FileSource {
    fn open(path: &Path, sample_rate: u32, channels: u16) -> anyhow::Result<Self> {
        let file = std::fs::File::open(path)?;
        let len = file.metadata()?.len() as usize;
        let frame_bytes = 2 * channels as usize;
        anyhow::ensure!(frame_bytes > 0, "zero channels");
        Ok(Self {
            file,
            sample_rate: sample_rate as usize,
            channels: channels as usize,
            total_frames: len / frame_bytes,
        })
    }
}

impl flacenc::source::Source for FileSource {
    fn read_samples<F: flacenc::source::Fill>(
        &mut self,
        block_size: usize,
        framebuf: &mut F,
    ) -> Result<usize, flacenc::error::SourceError> {
        let want = block_size * self.channels * 2; // bytes
        let mut bytes = vec![0u8; want];
        let mut filled = 0usize;
        while filled < want {
            let n = self.file.read(&mut bytes[filled..]).map_err(|_| {
                flacenc::error::SourceError::by_reason(
                    flacenc::error::SourceErrorReason::InvalidFormat,
                )
            })?;
            if n == 0 {
                break;
            }
            filled += n;
        }
        let usable = (filled / 2) * 2; // whole samples
        if usable == 0 {
            return Ok(0);
        }
        framebuf.fill_le_bytes(&bytes[..usable], 2)?;
        Ok(usable / 2 / self.channels)
    }

    fn sample_rate(&self) -> usize {
        self.sample_rate
    }

    fn channels(&self) -> usize {
        self.channels
    }

    fn bits_per_sample(&self) -> usize {
        16
    }

    fn len_hint(&self) -> Option<usize> {
        Some(self.total_frames)
    }
}

/// SHA-256 of a file, hex string.
pub fn file_sha256(path: &Path) -> anyhow::Result<String> {
    use sha2::{Digest, Sha256};

    let mut hasher = Sha256::new();
    let mut f = std::fs::File::open(path)?;
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = f.read(&mut buf)?;
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

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir() -> std::path::PathBuf {
        std::env::var("CARGO_TARGET_TMPDIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::env::temp_dir())
    }

    #[test]
    fn flac_write_and_finish() {
        let base = tmpdir().join(format!("t-{}", uuid::Uuid::new_v4()));
        let flac = base.with_extension("flac");
        let mut w = FlacWriter::create(&flac, 16000, 2).unwrap();
        assert_eq!(w.sample_rate(), 16000);
        let samples: Vec<f32> = (0..200).map(|i| (i as f32 / 400.0) - 0.25).collect();
        w.write_interleaved(&samples).unwrap();
        let frames = w.finish(&flac).unwrap();
        assert_eq!(frames, 100);
        assert!(flac.exists());
        assert!(flac.metadata().unwrap().len() > 90);
        assert!(!base.with_extension("pcm").exists()); // sidecar removed
        std::fs::remove_file(flac).ok();
    }

    #[test]
    fn flac_multi_block() {
        let base = tmpdir().join(format!("t-{}", uuid::Uuid::new_v4()));
        let flac = base.with_extension("flac");
        let mut w = FlacWriter::create(&flac, 44100, 1).unwrap();
        let samples: Vec<f32> = (0..BLOCK_SIZE * 3 + 17)
            .map(|i| (i % 100) as f32 / 300.0)
            .collect();
        w.write_interleaved(&samples).unwrap();
        let frames = w.finish(&flac).unwrap();
        assert_eq!(frames as usize, BLOCK_SIZE * 3 + 17);
        assert!(flac.metadata().unwrap().len() > 500);
        std::fs::remove_file(flac).ok();
    }

    #[test]
    fn sha256_known_vector() {
        let path = tmpdir().join(format!("t-{}.bin", uuid::Uuid::new_v4()));
        std::fs::write(&path, b"abc").unwrap();
        let hex = file_sha256(&path).unwrap();
        assert_eq!(
            hex,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        std::fs::remove_file(path).ok();
    }
}

#[cfg(test)]
mod dump_tests {
    use super::FlacWriter;

    fn tmpdir() -> std::path::PathBuf {
        std::env::var("CARGO_TARGET_TMPDIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::env::temp_dir())
    }

    /// Writes a 3-second 440Hz mono FLAC for external validation (ffmpeg).
    #[test]
    fn flac_dump_for_ffmpeg_check() {
        let base = tmpdir().join("ffmpeg-check");
        let flac = base.with_extension("flac");
        let mut w = FlacWriter::create(&flac, 44100, 1).unwrap();
        let n = 44100 * 3;
        let samples: Vec<f32> = (0..n)
            .map(|i| (i as f32 * 440.0 * 2.0 * std::f32::consts::PI / 44100.0).sin() * 0.3)
            .collect();
        w.write_interleaved(&samples).unwrap();
        let frames = w.finish(&flac).unwrap();
        assert_eq!(frames as usize, n);
    }
}
