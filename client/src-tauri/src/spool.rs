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
    #[serde(default)]
    pub tags: Vec<String>,
    pub started_at: String,
    pub duration_sec: f64,
    pub sample_rate: u32,
    pub channels: u16,
    pub mic_active: bool,
    pub system_active: bool,
    #[serde(default)]
    pub mic_dropped_frames: u64,
    #[serde(default)]
    pub system_dropped_frames: u64,
    #[serde(default)]
    pub mic_xruns: u64,
    #[serde(default)]
    pub system_xruns: u64,
    #[serde(default)]
    pub capture_error: Option<String>,
    /// Upload progress: next byte offset acked by server.
    pub uploaded_offset: u64,
    /// Server-assigned recording id (set on first create; reused on retries).
    pub server_rec_id: Option<String>,
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

    /// Open an already-spool root (no `spool` segment appended) — used by
    /// the uploader retry loop, which receives `Spool::root()` paths.
    pub fn open_root(root: &Path) -> anyhow::Result<Self> {
        fs::create_dir_all(root)?;
        Ok(Self {
            root: root.to_path_buf(),
        })
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

    /// All sessions not yet finalized and not currently active
    /// (uploader worklist; the active session's audio is still growing).
    pub fn pending(&self) -> anyhow::Result<Vec<SpoolSession>> {
        let active_id = crate::recording::active_session_id();
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
            if Some(&id) == active_id.as_ref() {
                continue;
            }
            if let Ok(s) = self.read_session(&id) {
                if !s.finalized {
                    out.push(s);
                }
            }
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn session(id: &str) -> SpoolSession {
        SpoolSession {
            id: id.into(),
            title: "t".into(),
            tags: vec![],
            duration_sec: 1.5,
            started_at: "0".into(),
            sample_rate: 48000,
            channels: 2,
            mic_active: true,
            system_active: false,
            mic_dropped_frames: 0,
            system_dropped_frames: 0,
            capture_error: None,
            uploaded_offset: 0,
            mic_xruns: 0,
            system_xruns: 0,
            server_rec_id: None,
            finalized: false,
        }
    }

    #[test]
    fn create_read_roundtrip() {
        let tmp = std::env::temp_dir().join(format!("spool-test-{}", uuid::Uuid::new_v4()));
        let spool = Spool::new(&tmp).unwrap();
        spool.create(&session("abc")).unwrap();
        let loaded = spool.read_session("abc").unwrap();
        assert_eq!(loaded.id, "abc");
        assert_eq!(loaded.duration_sec, 1.5);
        assert!(spool.audio_path("abc").ends_with("audio.flac"));
        std::fs::remove_dir_all(tmp).ok();
    }

    #[test]
    fn pending_excludes_finalized() {
        let tmp = std::env::temp_dir().join(format!("spool-test-{}", uuid::Uuid::new_v4()));
        let spool = Spool::new(&tmp).unwrap();
        spool.create(&session("open")).unwrap();
        let mut done = session("done");
        done.finalized = true;
        spool.create(&done).unwrap();
        let ids: Vec<String> = spool.pending().unwrap().into_iter().map(|s| s.id).collect();
        assert_eq!(ids, vec!["open".to_string()]);
        std::fs::remove_dir_all(tmp).ok();
    }

    #[test]
    fn open_root_reads_same_sessions_as_new() {
        // Uploader retry path: Spool::open_root(root_of_existing_spool)
        // must see sessions created via Spool::new (no spool/spool double-join).
        let base = std::env::temp_dir().join(format!("spool-root-{}", uuid::Uuid::new_v4()));
        let spool = Spool::new(&base).unwrap();
        spool.create(&session("rid")).unwrap();

        let reopened = Spool::open_root(spool.root()).unwrap();
        let loaded = reopened.read_session("rid").unwrap();
        assert_eq!(loaded.id, "rid");

        // And the write path: persist server_rec_id, re-read sees it.
        let mut updated = loaded;
        updated.server_rec_id = Some("server-123".into());
        reopened.write_session(&updated).unwrap();
        let again = reopened.read_session("rid").unwrap();
        assert_eq!(again.server_rec_id.as_deref(), Some("server-123"));

        std::fs::remove_dir_all(base).ok();
    }

    #[test]
    fn remove_deletes_dir() {
        let tmp = std::env::temp_dir().join(format!("spool-test-{}", uuid::Uuid::new_v4()));
        let spool = Spool::new(&tmp).unwrap();
        spool.create(&session("gone")).unwrap();
        spool.remove("gone").unwrap();
        assert!(!spool.session_dir("gone").exists());
        std::fs::remove_dir_all(tmp).ok();
    }

    #[test]
    fn legacy_session_defaults_capture_diagnostics() {
        let raw = r#"{
            "id":"legacy","title":"t","started_at":"0","duration_sec":1.0,
            "sample_rate":48000,"channels":1,"mic_active":true,"system_active":true,
            "uploaded_offset":0,"server_rec_id":null,"finalized":false
        }"#;
        let loaded: SpoolSession = serde_json::from_str(raw).unwrap();
        assert_eq!(loaded.mic_dropped_frames, 0);
        assert_eq!(loaded.system_dropped_frames, 0);
        assert_eq!(loaded.mic_xruns, 0);
        assert_eq!(loaded.system_xruns, 0);
        assert_eq!(loaded.capture_error, None);
    }
}
