package com.megastruktur.transcripter

import android.content.Intent
import android.os.Bundle
import androidx.activity.enableEdgeToEdge

class MainActivity : TauriActivity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    enableEdgeToEdge()
    super.onCreate(savedInstanceState)
  }

  override fun onDestroy() {
    // Swipe-close (isFinishing) kills the WebView — and with it the
    // MediaRecorder that is actually capturing. The keepalive service must
    // not outlive the recorder it claims to serve.
    if (isFinishing) {
      stopService(Intent(this, RecordingService::class.java))
    }
    super.onDestroy()
  }
}
