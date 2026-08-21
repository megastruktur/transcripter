pub mod capture;
pub mod encode;
pub mod permissions;
pub mod recording;
pub mod spool;
pub mod uploader;

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use tauri::AppHandle;
use tokio::runtime::Runtime;

use crate::permissions::PreFlightReport;
use crate::spool::{Spool, SpoolSession};

static UPLOADING: AtomicBool = AtomicBool::new(false);
static RUNTIME: std::sync::LazyLock<Runtime> =
    std::sync::LazyLock::new(|| Runtime::new().expect("tokio runtime"));

pub fn run() {
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
fn cmd_stop_recording(app: AppHandle) -> Result<SpoolSession, String> {
    let spool = spool_from_app(&app)?;
    let session = recording::stop(&spool)?;

    // Kick the uploader for this session (roborev T10: never silently drop).
    let cfg = upload_config(&app);
    let for_upload = session.clone();
    let spool_dir = spool.root().to_path_buf();
    RUNTIME.spawn(async move {
        if let Err(e) = upload_with_retry(&spool_dir, &for_upload, cfg).await {
            eprintln!("[uploader] session {} failed: {e}", for_upload.id);
        }
    });
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

struct UploadCfg {
    base_url: String,
    token: String,
}

fn upload_config(_app: &AppHandle) -> UploadCfg {
    // Frontend owns settings in localStorage; Rust reads env fallbacks only.
    // The frontend also invokes cmd_upload_now with explicit config (below).
    UploadCfg {
        base_url: std::env::var("TRANSCRIPTER_URL").unwrap_or_default(),
        token: std::env::var("TRANSCRIPTER_TOKEN").unwrap_or_default(),
    }
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
    RUNTIME.spawn(async move {
        if let Err(e) = upload_with_retry(&spool_dir, &session, UploadCfg { base_url, token }).await {
            eprintln!("[uploader] session {} failed: {e}", session.id);
        }
    });
    Ok(())
}

/// Upload with backoff; stops retrying after ~5 minutes and leaves the spool
/// entry pending (next app start or cmd_upload_now retries it).
async fn upload_with_retry(
    spool_dir: &std::path::Path,
    session: &SpoolSession,
    cfg: UploadCfg,
) -> anyhow::Result<()> {
    if UPLOADING.swap(true, Ordering::SeqCst) {
        anyhow::bail!("another upload in flight");
    }
    let result = try_upload(spool_dir, session, &cfg).await;
    UPLOADING.store(false, Ordering::SeqCst);
    result
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
