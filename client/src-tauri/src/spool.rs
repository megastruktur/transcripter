//! Recording spool on disk: session metadata + FLAC audio.
//!
//! Layout: {app_data}/spool/<uuid>/{session.json, audio.flac}
//! Spool survives app restarts; uploader cleans after server finalize ack.

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SpoolSession {
    pub id: String,
    pub title: String,
    pub started_at: String,
    pub duration_sec: f64,
    pub sample_rate: u32,
    pub channels: u16,
    pub mic_active: bool,
    pub system_active: bool,
    /// Upload progress: next byte offset acked by server.
    pub uploaded_offset: u64,
    /// Set when server confirmed finalize.
    pub finalized: bool,
}

#[derive(Debug)]
pub struct Spool {
    root: PathBuf,
}

impl Spool {
    pub fn new(app_data: &Path) -> anyhow::Result<Self> {
        let root = app_data.join("spool");
        fs::create_dir_all(&root)?;
        Ok(Self { root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn session_dir(&self, id: &str) -> PathBuf {
        self.root.join(id)
    }

    pub fn create(&self, session: &SpoolSession) -> anyhow::Result<()> {
        let dir = self.session_dir(&session.id);
        fs::create_dir_all(&dir)?;
        self.write_session(session)
    }

    pub fn write_session(&self, session: &SpoolSession) -> anyhow::Result<()> {
        let dir = self.session_dir(&session.id);
        fs::create_dir_all(&dir)?;
        fs::write(
            dir.join("session.json"),
            serde_json::to_string_pretty(session)?,
        )?;
        Ok(())
    }

    pub fn read_session(&self, id: &str) -> anyhow::Result<SpoolSession> {
        let path = self.session_dir(id).join("session.json");
        let raw = fs::read_to_string(path)?;
        Ok(serde_json::from_str(&raw)?)
    }

    pub fn audio_path(&self, id: &str) -> PathBuf {
        self.session_dir(id).join("audio.flac")
    }

    pub fn remove(&self, id: &str) -> anyhow::Result<()> {
        fs::remove_dir_all(self.session_dir(id))?;
        Ok(())
    }

    /// All sessions not yet finalized (uploader worklist).
    pub fn pending(&self) -> anyhow::Result<Vec<SpoolSession>> {
        let mut out = Vec::new();
        if !self.root.exists() {
            return Ok(out);
        }
        for entry in fs::read_dir(&self.root)? {
            let entry = entry?;
            if !entry.file_type()?.is_dir() {
                continue;
            }
            let id = entry.file_name().to_string_lossy().to_string();
            if let Ok(s) = self.read_session(&id) {
                if !s.finalized {
                    out.push(s);
                }
            }
        }
        Ok(out)
    }
}
