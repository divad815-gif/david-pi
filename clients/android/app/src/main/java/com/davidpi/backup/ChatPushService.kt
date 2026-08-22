package com.davidpi.backup

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

class ChatPushService : FirebaseMessagingService() {
    override fun onMessageReceived(message: RemoteMessage) {
        val manager = getSystemService(NotificationManager::class.java)
        val channel = "david_pi_chat"
        manager.createNotificationChannel(NotificationChannel(channel, "David-Pi chat", NotificationManager.IMPORTANCE_DEFAULT))
        val path = message.data["url"]?.takeIf { it.startsWith("/chat") } ?: "/chat"
        val intent = Intent(this, MainActivity::class.java)
            .putExtra("david_pi_chat_path", path)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pending = PendingIntent.getActivity(this, 4102, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        manager.notify(4102, NotificationCompat.Builder(this, channel)
            .setSmallIcon(R.drawable.david_pi_launcher)
            .setContentTitle("David-Pi")
            .setContentText("New David-Pi message")
            .setContentIntent(pending).setAutoCancel(true).build())
    }
}
