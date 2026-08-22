package com.davidpi.backup

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

private const val AUDIOBOOK_KEEPALIVE_CHANNEL = "david_pi_audiobooks"
private const val AUDIOBOOK_KEEPALIVE_NOTIFICATION = 7314

class AudiobookKeepAliveService : Service() {
    override fun onCreate() {
        super.onCreate()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(
                    AUDIOBOOK_KEEPALIVE_CHANNEL,
                    "Audiobook playback",
                    NotificationManager.IMPORTANCE_LOW
                )
            )
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Use the exact MediaStyle notification owned by the bridge. Previously this
        // service published a second plain notification with the same ID, racing with
        // and replacing the lock-screen controls after the first pause or resume.
        val notification = NativeMediaRegistry.current?.buildNotification()
            ?: NotificationCompat.Builder(this, AUDIOBOOK_KEEPALIVE_CHANNEL)
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setContentTitle("David-Pi Audiobook")
                .setContentText("Open David-Pi to resume playback")
                .setOnlyAlertOnce(true)
                .setOngoing(true)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .build()
        startForeground(AUDIOBOOK_KEEPALIVE_NOTIFICATION, notification)
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
