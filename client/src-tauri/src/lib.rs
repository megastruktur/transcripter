pub mod capture;
pub mod encode;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
pub mod permissions;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
pub mod recording;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
pub mod spool;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
pub mod uploader;
#[cfg(target_os = "macos")]
mod capture_macos;
#[cfg(target_os = "windows")]
mod capture_windows;

use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager};
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
use tauri::{
    image::Image,
    menu::{MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};
use tokio::runtime::Runtime;

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
use crate::permissions::PreFlightReport;
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
use crate::spool::{Spool, SpoolSession};

static RUNTIME: std::sync::LazyLock<Runtime> =
    std::sync::LazyLock::new(|| Runtime::new().expect("tokio runtime"));

/// Keepalive service for Android background recording. The recording itself
/// runs in the WebView (getUserMedia → MediaRecorder); this service exists so
/// the plugin raises a microphone-type foreground service with a persistent
/// notification while recording, keeping the process alive and mic access
/// legal when the user backgrounds the app. `run` does no work — it only
/// waits for the JS side to stop the service via `stopService()`.
struct RecordingKeepalive;

#[async_trait::async_trait]
impl<R: tauri::Runtime> tauri_plugin_background_service::BackgroundService<R>
    for RecordingKeepalive
{
    async fn init(
        &mut self,
        _ctx: &tauri_plugin_background_service::ServiceContext<R>,
    ) -> Result<(), tauri_plugin_background_service::ServiceError> {
        Ok(())
    }

    async fn run(
        &mut self,
        ctx: &tauri_plugin_background_service::ServiceContext<R>,
    ) -> Result<(), tauri_plugin_background_service::ServiceError> {
        ctx.shutdown.cancelled().await;
        Ok(())
    }
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // notification MUST register before background-service (it is the
        // plugin's notifier backend).
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_background_service::init_with_service(
            || RecordingKeepalive,
        ))
        .setup(|_app| {
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            {
                let show = MenuItemBuilder::with_id("show", "Show Transcriptor Maximus").build(_app)?;
                let quit = MenuItemBuilder::with_id("quit", "Quit").build(_app)?;
                let menu = MenuBuilder::new(_app).items(&[&show, &quit]).build()?;

                #[cfg(target_os = "macos")]
                let tray_icon_bytes: &[u8] = include_bytes!("../icons/tray/32x32.png");
                #[cfg(not(target_os = "macos"))]
                let tray_icon_bytes: &[u8] = include_bytes!("../icons/32x32.png");

                TrayIconBuilder::with_id("transcripter-tray")
                    .icon(Image::from_bytes(tray_icon_bytes)?)
                    .icon_as_template(cfg!(target_os = "macos"))
                    .tooltip("Transcriptor Maximus")
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
                    .build(_app)?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_list_audio_devices,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_pre_flight,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_start_recording,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_stop_recording,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_recording_frames,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_recording_degraded,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_upload_now,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_retry_pending,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_pending_uploads,
            #[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
            cmd_apply_window_mode,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[tauri::command]
fn cmd_list_audio_devices() -> Result<capture::AudioDevices, String> {
    capture::list_devices()
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
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

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[tauri::command]
fn cmd_start_recording(
    app: AppHandle,
    title: Option<String>,
    tags: Option<Vec<String>>,
    microphone: Option<String>,
    system_output: Option<String>,
    capture_system: Option<bool>,
) -> Result<String, String> {
    let spool = spool_from_app(&app)?;
    recording::start(
        &spool,
        title.as_deref().unwrap_or(""),
        tags.as_deref().unwrap_or(&[]),
        microphone.as_deref(),
        system_output.as_deref(),
        capture_system.unwrap_or(true),
    )
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
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
            app,
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

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[tauri::command]
fn cmd_retry_pending(app: AppHandle, base_url: String, token: String) -> Result<u32, String> {
    let spool = spool_from_app(&app)?;
    let pending = spool.pending().map_err(|e| e.to_string())?;
    let count = pending.len() as u32;
    let spool_dir = spool.root().to_path_buf();
    for session in pending {
        enqueue_upload(
            app.clone(),
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

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[tauri::command]
fn cmd_recording_frames() -> Result<u64, String> {
    recording::frames_written()
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[tauri::command]
fn cmd_recording_degraded() -> Option<String> {
    recording::degraded_reason()
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
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

/// UI event: one spool session's upload lifecycle.
/// `state`: "queued" | "uploading" | "done" | "failed".
#[derive(Clone, serde::Serialize)]
pub struct UploadStatusEvent {
    pub session_id: String,
    pub title: String,
    pub state: String,
    pub committed: u64,
    pub total: u64,
    pub error: Option<String>,
}

const UPLOAD_STATUS_EVENT: &str = "upload://status";

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
fn emit_upload_status(
    app: &AppHandle,
    session: &SpoolSession,
    state: &str,
    committed: u64,
    total: u64,
    error: Option<String>,
) {
    let _ = app.emit(
        UPLOAD_STATUS_EVENT,
        UploadStatusEvent {
            session_id: session.id.clone(),
            title: session.title.clone(),
            state: state.into(),
            committed,
            total,
            error,
        },
    );
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
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
        app,
        spool_dir,
        session.clone(),
        UploadCfg { base_url, token },
    );
    Ok(())
}

/// Spool sessions not yet uploaded — lets the UI seed its upload state
/// on startup (before any event arrives) instead of showing a stale zero.
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[tauri::command]
fn cmd_pending_uploads(app: AppHandle) -> Result<Vec<SpoolSession>, String> {
    let spool = spool_from_app(&app)?;
    spool.pending().map_err(|e| e.to_string())
}

/// catch_unwind for futures (tokio has no built-in; use futures crate).
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
async fn futures_catch<F: std::future::Future>(
    fut: std::panic::AssertUnwindSafe<F>,
) -> Result<F::Output, Box<dyn std::any::Any + Send>> {
    use futures::FutureExt;
    fut.catch_unwind().await
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
#[derive(Clone)]
struct UploadJob {
    app: AppHandle,
    spool_dir: std::path::PathBuf,
    session: SpoolSession,
    cfg: UploadCfg,
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
enum QueueMsg {
    Job(UploadJob),
}

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
static UPLOAD_QUEUE: std::sync::LazyLock<tokio::sync::mpsc::UnboundedSender<QueueMsg>> =
    std::sync::LazyLock::new(|| {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<QueueMsg>();
        RUNTIME.spawn(async move {
            while let Some(QueueMsg::Job(job)) = rx.recv().await {
                emit_upload_status(
                    &job.app,
                    &job.session,
                    "uploading",
                    job.session.uploaded_offset,
                    0,
                    None,
                );
                let result = {
                    let app = job.app.clone();
                    let session = job.session.clone();
                    let progress = move |p: uploader::UploadProgress| {
                        emit_upload_status(&app, &session, "uploading", p.committed, p.total, None);
                    };
                    let fut = std::panic::AssertUnwindSafe(try_upload(
                        &job.spool_dir,
                        &job.session,
                        &job.cfg,
                        Some(&progress),
                    ));
                    match futures_catch(fut).await {
                        Ok(res) => res,
                        Err(panic) => {
                            eprintln!(
                                "[uploader] session {} PANICKED: {:?}",
                                job.session.id, panic
                            );
                            emit_upload_status(
                                &job.app,
                                &job.session,
                                "failed",
                                0,
                                0,
                                Some("uploader panicked".into()),
                            );
                            QUEUED.lock().map(|mut q| q.remove(&job.session.id)).ok();
                            continue;
                        }
                    }
                };
                QUEUED.lock().map(|mut q| q.remove(&job.session.id)).ok();
                match result {
                    Ok(()) => emit_upload_status(&job.app, &job.session, "done", 0, 0, None),
                    Err(e) => {
                        eprintln!("[uploader] session {} failed: {e}", job.session.id);
                        emit_upload_status(
                            &job.app,
                            &job.session,
                            "failed",
                            0,
                            0,
                            Some(e.to_string()),
                        );
                    }
                }
            }
        });
        tx
    });

/// Sessions already queued (dedup between stop-path and cmd_retry_pending).
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
static QUEUED: std::sync::LazyLock<std::sync::Mutex<std::collections::HashSet<String>>> =
    std::sync::LazyLock::new(|| std::sync::Mutex::new(std::collections::HashSet::new()));

/// Enqueue an upload; the single queue worker processes jobs sequentially.
/// Returns immediately; failures are logged and the spool entry stays pending.
#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
fn enqueue_upload(
    app: AppHandle,
    spool_dir: std::path::PathBuf,
    session: SpoolSession,
    cfg: UploadCfg,
) {
    // Emit before the dedupe check: a re-enqueue of an in-flight id is a
    // user retry signal; the UI treats "uploading" (re-)events as progress.
    emit_upload_status(&app, &session, "queued", 0, 0, None);
    if QUEUED
        .lock()
        .map(|mut q| q.insert(session.id.clone()))
        .unwrap_or(false)
        && UPLOAD_QUEUE
            .send(QueueMsg::Job(UploadJob {
                app,
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

#[cfg(any(target_os = "linux", target_os = "macos", target_os = "windows"))]
async fn try_upload(
    spool_dir: &std::path::Path,
    session: &SpoolSession,
    cfg: &UploadCfg,
    progress: Option<&(dyn Fn(uploader::UploadProgress) + Send + Sync)>,
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
        let res = match progress {
            Some(cb) => uploader.upload(spool_dir, &current, &mut |p| cb(p)).await,
            None => uploader.upload(spool_dir, &current, &mut |_| {}).await,
        };
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
