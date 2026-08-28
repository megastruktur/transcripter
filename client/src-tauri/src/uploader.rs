//! Resumable uploader: spool → server, chunked offset PUT, retry, cleanup.

use crate::spool::SpoolSession;

pub const CHUNK_SIZE: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, serde::Serialize)]
pub struct UploadProgress {
    pub session_id: String,
    pub committed: u64,
    pub total: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum UploadError {
    #[error("network: {0}")]
    Network(String),
    #[error("server {status}: {detail}")]
    Server { status: u16, detail: String },
    #[error("io: {0}")]
    Io(String),
}

impl UploadError {
    /// True when retrying cannot possibly help: the server rejected the
    /// payload itself (bad token, silent capture, hash mismatch) rather
    /// than failing transiently. 408/429 stay retryable.
    pub fn is_permanent(&self) -> bool {
        match self {
            UploadError::Server { status, .. } => {
                (400..500).contains(status) && !matches!(status, 408 | 429)
            }
            _ => false,
        }
    }
}

pub struct Uploader {
    pub base_url: String,
    pub token: String,
    client: reqwest::Client,
}

impl Uploader {
    pub fn new(base_url: String, token: String) -> Self {
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            token,
            client: reqwest::Client::new(),
        }
    }

    /// This build has no TLS backend (ring does not compile under the
    /// host toolchain); refuse https fast instead of burning retries.
    /// Scheme check is parser-based: case-insensitive, slash-variants
    /// (https:/x, https:\x) are normalized like fetch/reqwest would.
    pub fn scheme_supported(base_url: &str) -> bool {
        match url::Url::parse(base_url.trim()) {
            Ok(u) => !u.scheme().eq_ignore_ascii_case("https"),
            // Unparseable → let the request layer produce a real error.
            Err(_) => true,
        }
    }

    fn auth(&self, rb: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        rb.bearer_auth(&self.token)
    }

    async fn create_recording(&self, s: &SpoolSession, total: u64) -> Result<String, UploadError> {
        let rb = self.client.post(format!("{}/recordings", self.base_url));
        let rb = self.auth(rb);
        let resp = rb
            .json(&serde_json::json!({
                "title": s.title,
                "total_bytes": total,
                "tags": s.tags,
            }))
            .send()
            .await
            .map_err(|e| UploadError::Network(e.to_string()))?;
        let status = resp.status();
        if !status.is_success() {
            return Err(UploadError::Server {
                status: status.as_u16(),
                detail: resp.text().await.unwrap_or_default(),
            });
        }
        let data: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| UploadError::Network(e.to_string()))?;
        data["id"]
            .as_str()
            .map(String::from)
            .ok_or(UploadError::Server {
                status: 500,
                detail: "no id in response".into(),
            })
    }

    async fn committed(&self, rec_id: &str) -> Result<u64, UploadError> {
        let rb = self
            .client
            .get(format!("{}/recordings/{rec_id}", self.base_url));
        let rb = self.auth(rb);
        let resp = rb
            .send()
            .await
            .map_err(|e| UploadError::Network(e.to_string()))?;
        let status = resp.status();
        if !status.is_success() {
            return Err(UploadError::Server {
                status: status.as_u16(),
                detail: resp.text().await.unwrap_or_default(),
            });
        }
        let data: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| UploadError::Network(e.to_string()))?;
        Ok(data["committed_bytes"].as_u64().unwrap_or(0))
    }

    /// Upload with resume; returns when server finalized.
    pub async fn upload(
        &self,
        spool_root: &std::path::Path,
        session: &SpoolSession,
        mut progress: impl FnMut(UploadProgress) + Send,
    ) -> Result<(), UploadError> {
        let audio = spool_root.join(&session.id).join("audio.flac");
        let data = std::fs::read(&audio).map_err(|e| UploadError::Io(e.to_string()))?;
        let total = data.len() as u64;

        let rec_id = match &session.server_rec_id {
            Some(id) => id.clone(),
            None => {
                let id = self.create_recording(session, total).await?;
                let mut updated = session.clone();
                updated.server_rec_id = Some(id.clone());
                persist_session(spool_root, &updated)?;
                id
            }
        };
        let mut offset = self.committed(&rec_id).await?;
        progress(UploadProgress {
            session_id: session.id.clone(),
            committed: offset,
            total,
        });

        while offset < total {
            let end = ((offset as usize) + CHUNK_SIZE).min(data.len());
            let chunk = &data[offset as usize..end];
            let rb = self.client.put(format!(
                "{}/recordings/{rec_id}/audio?offset={offset}",
                self.base_url
            ));
            let rb = self.auth(rb);
            let resp = rb
                .header("content-length", chunk.len().to_string())
                .body(chunk.to_vec())
                .send()
                .await
                .map_err(|e| UploadError::Network(e.to_string()))?;
            let status = resp.status();
            if !status.is_success() {
                return Err(UploadError::Server {
                    status: status.as_u16(),
                    detail: resp.text().await.unwrap_or_default(),
                });
            }
            let ack: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| UploadError::Network(e.to_string()))?;
            offset = ack["committed"].as_u64().ok_or(UploadError::Server {
                status: 500,
                detail: "no committed in ack".into(),
            })?;
            progress(UploadProgress {
                session_id: session.id.clone(),
                committed: offset,
                total,
            });
        }

        let sha = crate::encode::file_sha256(&audio).map_err(|e| UploadError::Io(e.to_string()))?;
        let rb = self
            .client
            .post(format!("{}/recordings/{rec_id}/finalize", self.base_url));
        let rb = self.auth(rb);
        let resp = rb
            .json(&serde_json::json!({
                "sha256": sha,
                "duration_sec": session.duration_sec,
            }))
            .send()
            .await
            .map_err(|e| UploadError::Network(e.to_string()))?;
        if !resp.status().is_success() {
            return Err(UploadError::Server {
                status: resp.status().as_u16(),
                detail: resp.text().await.unwrap_or_default(),
            });
        }
        Ok(())
    }
}

fn persist_session(
    spool_root: &std::path::Path,
    session: &SpoolSession,
) -> Result<(), UploadError> {
    crate::spool::Spool::open_root(spool_root)
        .and_then(|s| s.write_session(session))
        .map_err(|e| UploadError::Io(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scheme_gate_blocks_https_variants() {
        for u in [
            "https://x",
            "HTTPS://x",
            "Https://X",
            "https:/x",
            "https:\\\\x",
            "  https://x  ",
        ] {
            assert!(!Uploader::scheme_supported(u), "should block: {u}");
        }
    }

    #[test]
    fn scheme_gate_allows_http_and_passthrough() {
        for u in [
            "http://x",
            "HTTP://x",
            "  http://x  ",
            "localhost:8090",
            "not a url",
        ] {
            assert!(Uploader::scheme_supported(u), "should allow: {u}");
        }
    }

    #[test]
    fn permanent_rejections_are_not_retried() {
        // 422 = silent capture, 401 = bad token, 409 = hash mismatch:
        // no amount of retrying changes the outcome.
        for status in [400, 401, 403, 409, 413, 422] {
            let e = UploadError::Server {
                status,
                detail: String::new(),
            };
            assert!(e.is_permanent(), "should be permanent: {status}");
        }
    }

    #[test]
    fn transient_failures_stay_retryable() {
        for status in [408, 429, 500, 502, 503, 507] {
            let e = UploadError::Server {
                status,
                detail: String::new(),
            };
            assert!(!e.is_permanent(), "should be retryable: {status}");
        }
        assert!(!UploadError::Network("reset".into()).is_permanent());
        assert!(!UploadError::Io("disk".into()).is_permanent());
    }
}
