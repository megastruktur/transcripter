package com.megastruktur.transcripter

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * Microphone-type foreground service: the keepalive that lets WebView
 * MediaRecorder capture continue while the user sits in other apps or turns
 * the screen off. Recording itself happens in the WebView; this service only
 * holds the process alive and keeps mic access legal (Android 14+ requires a
 * mic-type FGS for background microphone).
 *
 * Lifecycle: RecordingPlugin.startRecordingService -> ACTION_START ->
 * startForeground(mic type). Stopped by RecordingPlugin.stopRecordingService
 * (recording ended) or by MainActivity.onDestroy(isFinishing) — a swiped-away
 * app has a dead WebView, so the "recording" notification must not outlive it.
 */
class RecordingService : Service() {
    companion object {
        const val ACTION_START = "com.megastruktur.transcripter.recording.START"
        const val ACTION_STOP = "com.megastruktur.transcripter.recording.STOP"
        const val EXTRA_LABEL = "label"
        private const val CHANNEL_ID = "recording"
        private const val NOTIFICATION_ID = 1001
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_START -> {
                val label = intent.getStringExtra(EXTRA_LABEL) ?: "Идёт запись"
                startForegroundWithNotification(label)
                return START_STICKY
            }
            else -> {
                // OS restart after process death (START_STICKY redelivery):
                // the WebView recorder is gone, so keeping a "recording"
                // notification up would be a lie — stop instead.
                stopSelf()
                return START_NOT_STICKY
            }
        }
    }

    private fun startForegroundWithNotification(label: String) {
        val notificationManager =
            getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        notificationManager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Запись", NotificationManager.IMPORTANCE_LOW)
        )

        // Tap on the notification brings the recording UI back up.
        val openIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(label)
            .setContentText("Transcripter записывает звонок")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(openIntent)
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }
}
