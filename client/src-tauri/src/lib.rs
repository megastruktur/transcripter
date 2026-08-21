pub mod capture;
pub mod encode;
pub mod permissions;
pub mod recording;
pub mod spool;
pub mod uploader;

use std::time::Duration;

use tauri::AppHandle;
use tokio::runtime::Runtime;

use crate::permissions::PreFlightReport;
use crate::spool::{Spool, SpoolSession};

static RUNTIME: std::sync::LazyLock<Runtime> =
    std::sync::LazyLock::new(|| Runtime::new().expect("tokio runtime"));

pub fn run() {
    // Spool entries from previous runs are retried when the frontend calls
    // cmd_retry_pending (Recordings mount, with configured credentials).
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            cmd_pre_flight,
            cmd_start_recording,
            cmd_stop_recording,
            cmd_pump,
            cmd_upload_now,
            cmd_retry_pending,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[tauri::command]
fn cmd_pre_flight(probe: Option<bool>) -> PreFlightReport {
    recording::pre_flight_check(probe.unwrap_or(false))
}

#[tauri::command]
fn cmd_start_recording(app: AppHandle, title: Option<String>) -> Result<String, String> {
    let spool = spool_from_app(&app)?;
    recording::start(&spool, title.as_deref().unwrap_or(""), true)
}

#[tauri::command]
fn cmd_stop_recording(
    app: AppHandle,
    server_url: Option<String>,
    server_token: Option<String>,
) -> Result<SpoolSession, String> {
    let spool = spool_from_app(&app)?;
    let session = recording::stop(&spool)?;

    // Kick the uploader with the frontend-provided config (roborev T10:
    // env-var-only config never worked for UI-configured users).
    if let (Some(url), Some(token)) = (server_url, server_token) {
        let for_upload = session.clone();
        let spool_dir = spool.root().to_path_buf();
        RUNTIME.spawn(async move {
            upload_with_retry(&spool_dir, &for_upload, UploadCfg { base_url: url, token });
        });
    } else {
        eprintln!("[uploader] no server config from UI; recording stays in spool");
    }
    Ok(session)
}

#[tauri::command]
fn cmd_retry_pending(
    app: AppHandle,
    base_url: String,
    token: String,
) -> Result<u32, String> {
    let spool = spool_from_app(&app)?;
    let pending = spool.pending().map_err(|e| e.to_string())?;
    let count = pending.len() as u32;
    let spool_dir = spool.root().to_path_buf();
    for session in pending {
        enqueue_upload(
            spool_dir.clone(),
            session,
            UploadCfg { base_url: base_url.clone(), token: token.clone() },
        );
    }
    Ok(count)
}

#[tauri::command]
fn cmd_pump(app: AppHandle) -> Result<u64, String> {
    let spool = spool_from_app(&app)?;
    recording::pump(&spool)
}

fn spool_from_app(app: &AppHandle) -> Result<Spool, String> {
    use tauri::Manager;
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    Spool::new(&dir).map_err(|e| e.to_string())
}

#[derive(Clone)]
struct UploadCfg {
    base_url: String,
    token: String,
}

#[tauri::command]
fn cmd_upload_now(
    app: AppHandle,
    base_url: String,
    token: String,
    session_id: String,
) -> Result<(), String> {
    let spool = spool_from_app(&app)?;
    let session = spool.read_session(&session_id).map_err(|e| e.to_string())?;
    let spool_dir = spool.root().to_path_buf();
    enqueue_upload(
        spool_dir,
        session.clone(),
        UploadCfg { base_url, token },
    );
    Ok(())
}

#[derive(Clone)]
struct UploadJob {
    spool_dir: std::path::PathBuf,
    session: SpoolSession,
    cfg: UploadCfg,
}

enum QueueMsg {
    Job(UploadJob),
}

static UPLOAD_QUEUE: std::sync::LazyLock<
    tokio::sync::mpsc::UnboundedSender<QueueMsg>,
> = std::sync::LazyLock::new(|| {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<QueueMsg>();
    RUNTIME.spawn(async move {
        while let Some(QueueMsg::Job(job)) = rx.recv().await {
            if let Err(e) = try_upload(&job.spool_dir, &job.session, &job.cfg).await {
                eprintln!("[uploader] session {} failed: {e}", job.session.id);
            }
        }
    });
    tx
});

/// Enqueue an upload; the single queue worker processes jobs sequentially.
/// Returns immediately; failures are logged and the spool entry stays pending.
fn enqueue_upload(spool_dir: std::path::PathBuf, session: SpoolSession, cfg: UploadCfg) {
    let _ = UPLOAD_QUEUE.send(QueueMsg::Job(UploadJob { spool_dir, session, cfg }));
}

/// Enqueue an upload (kept as a named step for the stop-path call site).
fn upload_with_retry(spool_dir: &std::path::Path, session: &SpoolSession, cfg: UploadCfg) {
    enqueue_upload(spool_dir.to_path_buf(), session.clone(), cfg);
}

async fn try_upload(
    spool_dir: &std::path::Path,
    session: &SpoolSession,
    cfg: &UploadCfg,
) -> anyhow::Result<()> {
    if cfg.base_url.is_empty() {
        anyhow::bail!("no server URL configured");
    }
    if !uploader::Uploader::scheme_supported(&cfg.base_url) {
        anyhow::bail!("https unsupported in this build (LAN MVP is http-only)");
    }
    let uploader = uploader::Uploader::new(cfg.base_url.clone(), cfg.token.clone());
    let mut delay = Duration::from_secs(2);
    for attempt in 0..6 {
        // Re-read session.json each attempt: upload() persists server_rec_id
        // there on first create, and we must resume THAT recording.
        let current = Spool::open_root(spool_dir)
            .ok()
            .and_then(|s| s.read_session(&session.id).ok())
            .unwrap_or_else(|| session.clone());
        let res = uploader.upload(spool_dir, &current, &mut |_p| {}).await;
        match res {
            Ok(()) => {
                if let Ok(s) = Spool::open_root(spool_dir) {
                    s.remove(&session.id).ok();
                }
                return Ok(());
            }
            Err(e) => {
                eprintln!("[uploader] attempt {}: {e}", attempt + 1);
                if attempt < 5 {
                    tokio::time::sleep(delay).await;
                    delay *= 2;
                }
            }
        }
    }
    anyhow::bail!("gave up after retries")
}
