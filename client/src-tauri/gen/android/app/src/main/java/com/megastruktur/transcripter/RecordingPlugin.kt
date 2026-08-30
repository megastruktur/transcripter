package com.megastruktur.transcripter

import android.app.Activity
import android.content.Intent
import androidx.core.content.ContextCompat
import app.tauri.annotation.Command
import app.tauri.annotation.InvokeArg
import app.tauri.annotation.TauriPlugin
import app.tauri.plugin.Invoke
import app.tauri.plugin.Plugin

@InvokeArg
class StartRecordingServiceArgs {
    var label: String = "Идёт запись"
}

/**
 * Tauri bridge for RecordingService: the Rust `recording-service` plugin
 * (src-tauri/src/recording_service.rs) instantiates this class by name via
 * register_android_plugin and forwards its `start`/`stop` commands here.
 */
@TauriPlugin
class RecordingPlugin(private val activity: Activity) : Plugin(activity) {

    @Command
    fun startRecordingService(invoke: Invoke) {
        val args = invoke.parseArgs(StartRecordingServiceArgs::class.java)
        val intent = Intent(activity, RecordingService::class.java).apply {
            action = RecordingService.ACTION_START
            putExtra(RecordingService.EXTRA_LABEL, args.label)
        }
        try {
            // Throws ForegroundServiceStartNotAllowedException (Android 12+)
            // when called from the background; the JS caller treats any
            // failure here as fatal for the recording start (fail-loud), so
            // propagate instead of swallowing.
            ContextCompat.startForegroundService(activity, intent)
            invoke.resolve()
        } catch (e: Exception) {
            invoke.reject("failed to start foreground service: ${e.message}")
        }
    }

    @Command
    fun stopRecordingService(invoke: Invoke) {
        activity.stopService(Intent(activity, RecordingService::class.java))
        invoke.resolve()
    }
}
