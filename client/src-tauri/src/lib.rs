pub mod capture;
#[cfg(target_os = "macos")]
mod capture_macos;
#[cfg(target_os = "windows")]
mod capture_windows;
pub mod encode;
pub mod permissions;
pub mod recording;
pub mod spool;
pub mod uploader;

use std::time::Duration;

use tauri::{
    image::Image,
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};
use tokio::runtime::Runtime;

use crate::permissions::PreFlightReport;
use crate::spool::{Spool, SpoolSession};

static RUNTIME: std::sync::LazyLock<Runtime> =
    std::sync::LazyLock::new(|| Runtime::new().expect("tokio runtime"));

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[tauri::command]
fn cmd_apply_window_mode(app: AppHandle, collapsed: bool) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    // Collapsed: pinned 76x76 mark that floats above other apps (on every
    // macOS space). Expanded: a regular window in the normal z-order.
    let size = if collapsed {
        tauri::LogicalSize::new(76u32, 76u32)
    } else {
        tauri::LogicalSize::new(440u32, 720u32)
    };
    let _ = window.set_size(size);
    let _ = window.set_always_on_top(collapsed);
    #[cfg(target_os = "macos")]
    let _ = window.set_visible_on_all_workspaces(collapsed);
}

pub fn run() {
    // Spool entries from previous runs are retried when the frontend calls
    // cmd_retry_pending (Recordings mount, with configured credentials).
    tauri::Builder::default()
        .setup(|app| {
            let show = MenuItemBuilder::with_id("show", "Show Transcripter").build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Quit").build(app)?;
            let menu = MenuBuilder::new(app).items(&[&show, &quit]).build()?;

            #[cfg(target_os = "macos")]
            let tray_icon_bytes: &[u8] = include_bytes!("../icons/tray/32x32.png");
            #[cfg(not(target_os = "macos"))]
            let tray_icon_bytes: &[u8] = include_bytes!("../icons/32x32.png");

            TrayIconBuilder::with_id("transcripter-tray")
                .icon(Image::from_bytes(tray_icon_bytes)?)
                .icon_as_template(cfg!(target_os = "macos"))
                .tooltip("Transcripter")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => show_main_window(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                })
                .build(app)?;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            cmd_list_audio_devices,
            cmd_pre_flight,
            cmd_start_recording,
            cmd_stop_recording,
            cmd_recording_frames,
            cmd_recording_degraded,
            cmd_upload_now,
            cmd_retry_pending,
            cmd_apply_window_mode,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[tauri::command]
fn cmd_list_audio_devices() -> Result<capture::AudioDevices, String> {
    capture::list_devices()
}

#[tauri::command]
fn cmd_pre_flight(
    probe: Option<bool>,
    microphone: Option<String>,
    system_output: Option<String>,
    check_system: Option<bool>,
) -> PreFlightReport {
    recording::pre_flight_check(
        probe.unwrap_or(false),
        microphone.as_deref(),
        system_output.as_deref(),
        check_system.unwrap_or(true),
    )
}

#[tauri::command]
fn cmd_start_recording(
    app: AppHandle,
    title: Option<String>,
    microphone: Option<String>,
    system_output: Option<String>,
    capture_system: Option<bool>,
) -> Result<String, String> {
    let spool = spool_from_app(&app)?;
    recording::start(
        &spool,
        title.as_deref().unwrap_or(""),
        microphone.as_deref(),
        system_output.as_deref(),
        capture_system.unwrap_or(true),
    )
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
        enqueue_upload(
            spool_dir,
            for_upload,
            UploadCfg {
                base_url: url,
                token,
            },
        );
    } else {
        eprintln!("[uploader] no server config from UI; recording stays in spool");
    }
    Ok(session)
}

#[tauri::command]
fn cmd_retry_pending(app: AppHandle, base_url: String, token: String) -> Result<u32, String> {
    let spool = spool_from_app(&app)?;
    let pending = spool.pending().map_err(|e| e.to_string())?;
    let count = pending.len() as u32;
    let spool_dir = spool.root().to_path_buf();
    for session in pending {
        enqueue_upload(
            spool_dir.clone(),
            session,
            UploadCfg {
                base_url: base_url.clone(),
                token: token.clone(),
            },
        );
    }
    Ok(count)
}

#[tauri::command]
fn cmd_recording_frames() -> Result<u64, String> {
    recording::frames_written()
}

#[tauri::command]
fn cmd_recording_degraded() -> Option<String> {
    recording::degraded_reason()
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
    enqueue_upload(spool_dir, session.clone(), UploadCfg { base_url, token });
    Ok(())
}

/// catch_unwind for futures (tokio has no built-in; use futures crate).
async fn futures_catch<F: std::future::Future>(
    fut: std::panic::AssertUnwindSafe<F>,
) -> Result<F::Output, Box<dyn std::any::Any + Send>> {
    use futures::FutureExt;
    fut.catch_unwind().await
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

static UPLOAD_QUEUE: std::sync::LazyLock<tokio::sync::mpsc::UnboundedSender<QueueMsg>> =
    std::sync::LazyLock::new(|| {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<QueueMsg>();
        RUNTIME.spawn(async move {
            while let Some(QueueMsg::Job(job)) = rx.recv().await {
                let result = {
                    let fut = std::panic::AssertUnwindSafe(try_upload(
                        &job.spool_dir,
                        &job.session,
                        &job.cfg,
                    ));
                    match futures_catch(fut).await {
                        Ok(res) => res,
                        Err(panic) => {
                            eprintln!(
                                "[uploader] session {} PANICKED: {:?}",
                                job.session.id, panic
                            );
                            QUEUED.lock().map(|mut q| q.remove(&job.session.id)).ok();
                            continue;
                        }
                    }
                };
                if let Err(e) = result {
                    eprintln!("[uploader] session {} failed: {e}", job.session.id);
                }
                // Remove after processing (incl. panic): in-flight jobs still
                // dedupe re-enqueues; a panicked worker does not leak the id.
                QUEUED.lock().map(|mut q| q.remove(&job.session.id)).ok();
            }
        });
        tx
    });

/// Sessions already queued (dedup between stop-path and cmd_retry_pending).
static QUEUED: std::sync::LazyLock<std::sync::Mutex<std::collections::HashSet<String>>> =
    std::sync::LazyLock::new(|| std::sync::Mutex::new(std::collections::HashSet::new()));

/// Enqueue an upload; the single queue worker processes jobs sequentially.
/// Returns immediately; failures are logged and the spool entry stays pending.
fn enqueue_upload(spool_dir: std::path::PathBuf, session: SpoolSession, cfg: UploadCfg) {
    if QUEUED
        .lock()
        .map(|mut q| q.insert(session.id.clone()))
        .unwrap_or(false)
        && UPLOAD_QUEUE
            .send(QueueMsg::Job(UploadJob {
                spool_dir,
                session: session.clone(),
                cfg,
            }))
            .is_err()
    {
        // Worker gone (until restart): roll back the id so a
        // post-restart enqueue is not deduped by a stale entry.
        QUEUED.lock().map(|mut q| q.remove(&session.id)).ok();
    }
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
        let current = match Spool::open_root(spool_dir)
            .map_err(|e| anyhow::anyhow!("spool open: {e}"))
            .and_then(|s| {
                s.read_session(&session.id)
                    .map_err(|e| anyhow::anyhow!("session re-read: {e}"))
            }) {
            Ok(s) => s,
            Err(e) => {
                // Spool entry vanished (deleted/uploaded elsewhere): stop.
                eprintln!("[uploader] session {} gone: {e}", session.id);
                return Ok(());
            }
        };
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
                if e.is_permanent() {
                    // Server rejected the payload itself; retrying just
                    // burns the backoff. Keep the spool entry so the audio
                    // is not lost, and surface the reason.
                    anyhow::bail!("permanent rejection, not retrying: {e}");
                }
                if attempt < 5 {
                    tokio::time::sleep(delay).await;
                    delay *= 2;
                }
            }
        }
    }
    anyhow::bail!("gave up after retries")
}
