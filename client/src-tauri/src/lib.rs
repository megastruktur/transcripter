#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub mod capture;
pub mod encode;
pub mod permissions;
pub mod recording;
pub mod spool;
pub mod uploader;

pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            cmd_pre_flight,
            cmd_start_recording,
            cmd_stop_recording,
            cmd_pump,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

use tauri::AppHandle;

use crate::permissions::PreFlightReport;
use crate::recording;
use crate::spool::Spool;

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
fn cmd_stop_recording(app: AppHandle) -> Result<crate::spool::SpoolSession, String> {
    let spool = spool_from_app(&app)?;
    recording::stop(&spool)
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
