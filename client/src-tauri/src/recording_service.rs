//! App-local Tauri plugin bridging the Kotlin `RecordingService` (Android
//! microphone-type foreground service) to the frontend.
//!
//! Why app-local: `tauri-plugin-background-service` assumes the host app
//! ships a headless native core (`libapp_core.so` with JNI exports) and
//! rejects the service start without it — its "lifecycle-only path" is not
//! actually supported by its own `LifecycleService`. Our keepalive need is
//! one service with a persistent notification, so we own the ~100 lines of
//! Kotlin instead of shimming JNI for someone else's headless daemon.
//!
//! The plugin is inert on desktop: the commands exist (so `pnpm check` and
//! the frontend invoke contract stay uniform) but return an error.

use tauri::{
    plugin::{Builder, TauriPlugin},
    Runtime,
};

#[cfg(target_os = "android")]
use tauri::{AppHandle, Manager, State};

#[cfg(target_os = "android")]
pub struct RecordingService<R: Runtime>(tauri::plugin::PluginHandle<R>);

#[cfg(target_os = "android")]
#[derive(serde::Serialize)]
struct StartArgs {
    label: String,
}

#[cfg(target_os = "android")]
#[tauri::command]
async fn start<R: Runtime>(
    app: AppHandle<R>,
    service: State<'_, RecordingService<R>>,
    label: String,
) -> Result<(), String> {
    let _ = app;
    service
        .0
        .run_mobile_plugin::<serde_json::Value>("startRecordingService", StartArgs { label })
        .map(|_| ())
        .map_err(|e| format!("failed to start foreground service: {e}"))
}

#[cfg(target_os = "android")]
#[tauri::command]
async fn stop<R: Runtime>(
    app: AppHandle<R>,
    service: State<'_, RecordingService<R>>,
) -> Result<(), String> {
    let _ = app;
    service
        .0
        .run_mobile_plugin::<serde_json::Value>("stopRecordingService", ())
        .map(|_| ())
        .map_err(|e| format!("failed to stop foreground service: {e}"))
}

#[cfg(not(target_os = "android"))]
#[tauri::command]
async fn start(label: String) -> Result<(), String> {
    let _ = label;
    Err("recording-service plugin is only available on Android".into())
}

#[cfg(not(target_os = "android"))]
#[tauri::command]
async fn stop() -> Result<(), String> {
    Err("recording-service plugin is only available on Android".into())
}

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("recording-service")
        .setup(|app, api| {
            #[cfg(target_os = "android")]
            {
                let handle =
                    api.register_android_plugin("com.megastruktur.transcripter", "RecordingPlugin")?;
                app.manage(RecordingService(handle));
            }
            let _ = app;
            let _ = api;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![start, stop])
        .build()
}
