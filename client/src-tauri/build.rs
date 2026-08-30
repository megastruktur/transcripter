fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().plugin(
        "recording-service",
        tauri_build::InlinedPlugin::new()
            .commands(&["start", "stop"])
            .default_permission(tauri_build::DefaultPermissionRule::AllowAllCommands),
    ))
    .expect("failed to run tauri-build");
}
