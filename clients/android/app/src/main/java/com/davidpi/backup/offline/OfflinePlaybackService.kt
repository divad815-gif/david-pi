package com.davidpi.backup.offline

import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class OfflinePlaybackService : MediaSessionService() {
    private lateinit var player: ExoPlayer
    private lateinit var mediaSession: MediaSession
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private lateinit var repository: OfflineAudiobookDao
    private var progressJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        repository = OfflineAudiobookDatabase.get(applicationContext).dao()
        player = ExoPlayer.Builder(this).build()
        mediaSession = MediaSession.Builder(this, player).build()
        player.addListener(object : Player.Listener {
            override fun onIsPlayingChanged(isPlaying: Boolean) {
                if (isPlaying) startProgressUpdates() else savePosition()
            }

            override fun onPlaybackStateChanged(playbackState: Int) {
                if (playbackState == Player.STATE_ENDED) savePosition(completed = true)
            }
        })
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession = mediaSession

    override fun onDestroy() {
        savePosition()
        progressJob?.cancel()
        mediaSession.release()
        player.release()
        scope.cancel()
        super.onDestroy()
    }

    private fun startProgressUpdates() {
        if (progressJob?.isActive == true) return
        progressJob = scope.launch {
            while (isActive) {
                delay(5_000)
                savePosition()
            }
        }
    }

    private fun savePosition(completed: Boolean = false) {
        val id = player.currentMediaItem?.mediaId.orEmpty()
        if (!OfflineDownloadPolicy.validBookId(id)) return
        val position = player.currentPosition.coerceAtLeast(0L) / 1_000.0
        val duration = player.duration.takeIf { it > 0 } ?: Long.MAX_VALUE
        val isCompleted = completed || position * 1_000 >= duration - 2_000
        scope.launch {
            withContext(Dispatchers.IO) {
                repository.updatePosition(
                    id, position, isCompleted, System.currentTimeMillis(),
                )
            }
        }
    }
}
