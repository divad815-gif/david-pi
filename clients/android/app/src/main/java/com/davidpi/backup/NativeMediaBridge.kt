package com.davidpi.backup

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Build
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebView
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import android.support.v4.media.MediaMetadataCompat
import android.support.v4.media.session.MediaSessionCompat
import android.support.v4.media.session.PlaybackStateCompat
import org.json.JSONObject

private const val MEDIA_CHANNEL = "david_pi_audiobooks"
private const val MEDIA_NOTIFICATION = 7314
private const val ACTION_PLAY = "com.davidpi.backup.MEDIA_PLAY"
private const val ACTION_PAUSE = "com.davidpi.backup.MEDIA_PAUSE"
private const val ACTION_BACK = "com.davidpi.backup.MEDIA_BACK"
private const val ACTION_FORWARD = "com.davidpi.backup.MEDIA_FORWARD"

internal object NativeMediaRegistry {
    @Volatile var current: NativeMediaBridge? = null
}

class NativeMediaCommandReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        NativeMediaRegistry.current?.command(intent.action.orEmpty())
    }
}

class NativeMediaBridge(private val context: Context, private val webView: WebView) {
    private val session = MediaSessionCompat(context, "DavidPiAudiobooks")
    private var title = "David-Pi Audiobook"
    private var author = ""
    private var playing = false
    private var position = 0L
    private var duration = 0L
    private var rate = 1f
    private var artwork: Bitmap? = null

    init {
        // The foreground playback service must retain a live command target while the
        // activity is backgrounded. release() clears this reference with the WebView.
        NativeMediaRegistry.current = this
        session.setCallback(object : MediaSessionCompat.Callback() {
            override fun onPlay() = send("play")
            override fun onPause() = send("pause")
            override fun onSeekTo(pos: Long) = send("seek", pos / 1000.0)
            override fun onSkipToNext() = send("nextchapter")
            override fun onSkipToPrevious() = send("previouschapter")
        })
        session.isActive = true
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(MEDIA_CHANNEL, "Audiobook playback", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    @JavascriptInterface
    fun setMetadata(newTitle: String, newAuthor: String) {
        title = newTitle.take(180).ifBlank { "David-Pi Audiobook" }
        author = newAuthor.take(180)
        publish()
    }

    @JavascriptInterface
    fun setArtworkDataUrl(dataUrl: String) {
        val comma = dataUrl.indexOf(',')
        val prefix = if (comma > 0) dataUrl.substring(0, comma).lowercase() else ""
        val encoded = if (comma > 0) dataUrl.substring(comma + 1) else ""
        if ((!prefix.startsWith("data:image/jpeg;base64") &&
                !prefix.startsWith("data:image/png;base64")) || encoded.length > 2_800_000) return
        try {
            val bytes = Base64.decode(encoded, Base64.DEFAULT)
            if (bytes.isEmpty() || bytes.size > 2_000_000) return
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
            if (bounds.outWidth !in 1..4096 || bounds.outHeight !in 1..4096) return
            var sample = 1
            while (bounds.outWidth / sample > 768 || bounds.outHeight / sample > 768) sample *= 2
            artwork = BitmapFactory.decodeByteArray(
                bytes, 0, bytes.size, BitmapFactory.Options().apply { inSampleSize = sample }
            ) ?: return
            publish()
        } catch (_: IllegalArgumentException) {
            // Ignore malformed or oversized artwork while preserving playback controls.
        }
    }

    @JavascriptInterface
    fun clearArtwork() {
        artwork = null
        publish()
    }

    @JavascriptInterface
    fun updatePlayback(isPlaying: Boolean, seconds: Double, durationSeconds: Double, playbackRate: Double) {
        playing = isPlaying
        position = (seconds.coerceAtLeast(0.0) * 1000).toLong()
        duration = (durationSeconds.coerceAtLeast(0.0) * 1000).toLong()
        rate = playbackRate.coerceIn(0.25, 4.0).toFloat()
        val keepAlive = Intent(context, AudiobookKeepAliveService::class.java)
        // Keep the media process alive while paused as well as while playing.
        // Otherwise Android can suspend the WebView after a lock-screen pause,
        // leaving a visible Play button with no live JavaScript audio owner.
        if (isPlaying && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(keepAlive)
        } else {
            context.startService(keepAlive)
        }
        publish()
    }

    @JavascriptInterface
    fun clear() {
        playing = false
        context.stopService(Intent(context, AudiobookKeepAliveService::class.java))
        publish()
        NotificationManagerCompat.from(context).cancel(MEDIA_NOTIFICATION)
    }

    fun command(action: String) {
        when (action) {
            ACTION_PLAY -> send("play")
            ACTION_PAUSE -> send("pause")
            ACTION_BACK -> send("back")
            ACTION_FORWARD -> send("forward")
        }
    }

    private fun send(command: String, value: Double? = null) {
        val quoted = JSONObject.quote(command)
        val argument = value?.toString() ?: "null"
        webView.post { webView.evaluateJavascript("window.davidPiNativeAudioCommand?.($quoted,$argument)", null) }
    }

    private fun pending(action: String, requestCode: Int): PendingIntent = PendingIntent.getBroadcast(
        context, requestCode, Intent(context, NativeMediaCommandReceiver::class.java).setAction(action),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
    )

    private fun publish() {
        val actions = PlaybackStateCompat.ACTION_PLAY or PlaybackStateCompat.ACTION_PAUSE or
            PlaybackStateCompat.ACTION_PLAY_PAUSE or PlaybackStateCompat.ACTION_SEEK_TO or
            PlaybackStateCompat.ACTION_SKIP_TO_NEXT or PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
        session.setPlaybackState(PlaybackStateCompat.Builder()
            .setActions(actions)
            .setState(if (playing) PlaybackStateCompat.STATE_PLAYING else PlaybackStateCompat.STATE_PAUSED,
                position, if (playing) rate else 0f)
            .build())
        val metadata = MediaMetadataCompat.Builder()
            .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
            .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, author)
            .putLong(MediaMetadataCompat.METADATA_KEY_DURATION, duration)
        artwork?.let {
            metadata.putBitmap(MediaMetadataCompat.METADATA_KEY_ART, it)
            metadata.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, it)
        }
        session.setMetadata(metadata.build())
        try { NotificationManagerCompat.from(context).notify(MEDIA_NOTIFICATION, buildNotification()) } catch (_: SecurityException) {}
    }

    internal fun buildNotification(): android.app.Notification {
        val toggleAction = if (playing) ACTION_PAUSE else ACTION_PLAY
        val toggleIcon = if (playing) android.R.drawable.ic_media_pause else android.R.drawable.ic_media_play
        val toggleText = if (playing) "Pause" else "Play"
        val notification = NotificationCompat.Builder(context, MEDIA_CHANNEL)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(title)
            .setContentText(author.ifBlank { "David-Pi Audiobooks" })
            .setLargeIcon(artwork)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setOnlyAlertOnce(true)
            // Keep paused playback resumable from the lock screen. clear() removes it.
            .setOngoing(true)
            .addAction(android.R.drawable.ic_media_rew, "Back 15", pending(ACTION_BACK, 1))
            .addAction(toggleIcon, toggleText, pending(toggleAction, 2))
            .addAction(android.R.drawable.ic_media_ff, "Forward 30", pending(ACTION_FORWARD, 3))
            .setStyle(androidx.media.app.NotificationCompat.MediaStyle()
                .setMediaSession(session.sessionToken)
                .setShowActionsInCompactView(0, 1, 2))
            .build()
        return notification
    }

    fun release() {
        context.stopService(Intent(context, AudiobookKeepAliveService::class.java))
        NotificationManagerCompat.from(context).cancel(MEDIA_NOTIFICATION)
        session.release()
        if (NativeMediaRegistry.current === this) NativeMediaRegistry.current = null
    }
}
