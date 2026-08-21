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
    // Retry spool entries from previous runs once the frontend provides
    // config (it calls cmd_upload_now per pending session after Settings ok).
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            cmd_pre_flight,
            cmd_start_recording,
            cmd_stop_recording,
            cmd_pump,
            cmd_upload_now,
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
            if let Err(e) = upload_with_retry(&spool_dir, &for_upload, UploadCfg {
                base_url: url,
                token,
            }).await {
                eprintln!("[uploader] session {} failed: {e}", for_upload.id);
            }
        });
    } else {
        eprintln!("[uploader] no server config from UI; recording stays in spool");
    }
    Ok(session)
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

/// Upload with backoff; gives up after ~5 min and leaves the spool entry
/// pending (pending() scan on next start or cmd_upload_now retries it).
async fn upload_with_retry(
    spool_dir: &std::path::Path,
    session: &SpoolSession,
    cfg: UploadCfg,
) -> anyhow::Result<()> {
    enqueue_upload(spool_dir.to_path_buf(), session.clone(), cfg);
    Ok(())
}

async fn try_upload(
    spool_dir: &std::path::Path,
    session: &SpoolSession,
    cfg: &UploadCfg,
) -> anyhow::Result<()> {
    if cfg.base_url.is_empty() {
        anyhow::bail!("no server URL configured");
    }
    let uploader = uploader::Uploader::new(cfg.base_url.clone(), cfg.token.clone());
    let mut delay = Duration::from_secs(2);
    for attempt in 0..6 {
        let res = uploader.upload(spool_dir, session, &mut |_p| {}).await;
        match res {
            Ok(()) => {
                if let Ok(s) = Spool::new(spool_dir) {
                    s.remove(&session.id).ok();
                }
                return Ok(());
            }
            Err(e) => {
                eprintln!("[uploader] attempt {}: {e}", attempt + 1);
                tokio::time::sleep(delay).await;
                delay *= 2;
            }
        }
    }
    anyhow::bail!("gave up after retries")
}
