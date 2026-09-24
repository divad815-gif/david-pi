package com.davidpi.backup.offline

import android.app.Application
import android.content.ComponentName
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit

class OfflineAudiobookViewModel(application: Application) : AndroidViewModel(application) {
    private val dao = OfflineAudiobookDatabase.get(application).dao()
    private val workManager = WorkManager.getInstance(application)
    private val directExecutor: Executor = ContextCompat.getMainExecutor(application)
    private var controller: MediaController? = null

    init {
        OfflineDownloadScheduler.recover(application)
    }

    val books = dao.observeAll().stateIn(
        viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList(),
    )

    fun play(book: OfflineAudiobook) {
        val file = OfflineAudiobookFiles.final(getApplication(), book.id)
        if (book.state != OfflineDownloadWorker.STATE_READY || !file.isFile) return
        val startPlayback: (MediaController) -> Unit = { mediaController ->
            val item = MediaItem.Builder()
                .setMediaId(book.id)
                .setUri(Uri.fromFile(file))
                .setMediaMetadata(MediaMetadata.Builder()
                    .setTitle(book.title)
                    .setArtist(book.author)
                    .build())
                .build()
            mediaController.setMediaItem(item, (book.positionSeconds * 1_000).toLong())
            mediaController.prepare()
            mediaController.play()
        }
        controller?.let(startPlayback) ?: run {
            val token = SessionToken(
                getApplication(),
                ComponentName(getApplication<Application>(), OfflinePlaybackService::class.java),
            )
            val future = MediaController.Builder(getApplication<Application>(), token).buildAsync()
            future.addListener({
                runCatching { future.get() }.onSuccess {
                    controller = it
                    startPlayback(it)
                }
            }, directExecutor)
        }
    }

    fun retry(book: OfflineAudiobook) {
        viewModelScope.launch(Dispatchers.IO) {
            dao.upsert(book.copy(state = OfflineDownloadWorker.STATE_QUEUED, error = ""))
            val request = OneTimeWorkRequestBuilder<OfflineDownloadWorker>()
                .setInputData(Data.Builder().putString(OfflineDownloadWorker.KEY_BOOK_ID, book.id).build())
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .addTag(OfflineDownloadWorker.tag(book.id))
                .build()
            workManager.enqueueUniqueWork(
                OfflineDownloadWorker.workName(book.id), ExistingWorkPolicy.REPLACE, request,
            )
        }
    }

    fun remove(book: OfflineAudiobook) {
        viewModelScope.launch(Dispatchers.IO) {
            // Cancellation changes WorkManager's durable state, but a running
            // CoroutineWorker may need a moment to observe it. The per-book
            // file fence is the completion barrier: once acquired, no worker
            // can open or rename this book after the final cleanup.
            runCatching {
                workManager.cancelUniqueWork(OfflineDownloadWorker.workName(book.id))
                    .result.get(20, TimeUnit.SECONDS)
            }
            OfflineAudiobookFileLocks.withLock(book.id) {
                val partial = OfflineAudiobookFiles.partial(getApplication(), book.id)
                val final = OfflineAudiobookFiles.final(getApplication(), book.id)
                val partialRemoved = !partial.exists() || partial.delete()
                val finalRemoved = !final.exists() || final.delete()
                if (partialRemoved && finalRemoved) {
                    dao.delete(book.id)
                } else {
                    dao.updateDownload(
                        book.id,
                        OfflineDownloadWorker.STATE_FAILED,
                        final.takeIf { it.isFile }?.length()
                            ?: partial.takeIf { it.isFile }?.length()
                            ?: 0L,
                        book.etag,
                        "The offline copy could not be removed. Try again.",
                        System.currentTimeMillis(),
                        book.integrityVerifiedAt,
                    )
                }
            }
        }
    }

    override fun onCleared() {
        controller?.release()
        controller = null
        super.onCleared()
    }
}

@Composable
fun OfflineAudiobookScreen(
    padding: PaddingValues,
    model: OfflineAudiobookViewModel = viewModel(),
) {
    val books by model.books.collectAsStateWithLifecycle()
    var removeTarget by remember { mutableStateOf<OfflineAudiobook?>(null) }

    LazyColumn(
        Modifier.fillMaxSize().padding(padding).padding(horizontal = 18.dp),
        contentPadding = PaddingValues(vertical = 18.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Offline audiobooks", style = MaterialTheme.typography.headlineMedium)
                Text("Saved only inside this app. Removing a copy here never deletes the original on David-Pi.")
            }
        }
        if (books.isEmpty()) item {
            Text("Open Audiobooks while connected and choose Save in app.")
        }
        items(books, key = { it.id }) { book ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(book.title, style = MaterialTheme.typography.titleMedium)
                    if (book.author.isNotBlank()) Text(book.author)
                    Text(statusText(book), color = MaterialTheme.colorScheme.secondary)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        if (book.state == OfflineDownloadWorker.STATE_READY) {
                            Button(onClick = { model.play(book) }) {
                                Text(if (book.positionSeconds > 1) "Resume" else "Play")
                            }
                        }
                        if (book.state == OfflineDownloadWorker.STATE_FAILED) {
                            Button(onClick = { model.retry(book) }) { Text("Retry") }
                        }
                        OutlinedButton(onClick = { removeTarget = book }) { Text("Remove copy") }
                    }
                }
            }
        }
    }

    removeTarget?.let { book ->
        AlertDialog(
            onDismissRequest = { removeTarget = null },
            title = { Text("Remove offline copy?") },
            text = { Text("This removes only the app-private copy of ${book.title}. The David-Pi original is unchanged.") },
            confirmButton = {
                TextButton(onClick = { model.remove(book); removeTarget = null }) { Text("Remove") }
            },
            dismissButton = {
                TextButton(onClick = { removeTarget = null }) { Text("Cancel") }
            },
        )
    }
}

private fun statusText(book: OfflineAudiobook): String = when (book.state) {
    OfflineDownloadWorker.STATE_READY -> {
        val progress = if (book.durationSeconds > 0) {
            ((book.positionSeconds / book.durationSeconds) * 100).toInt().coerceIn(0, 100)
        } else 0
        val integrity = if (book.contentSha256 == null) {
            " · legacy copy; save again online to verify"
        } else ""
        "Ready offline${if (progress > 0) " · $progress% listened" else ""}$integrity"
    }
    OfflineDownloadWorker.STATE_DOWNLOADING -> {
        val percent = ((book.bytesDownloaded * 100) / book.byteSize.coerceAtLeast(1)).coerceIn(0, 100)
        "Saving · $percent%"
    }
    OfflineDownloadWorker.STATE_FAILED -> book.error.ifBlank { "Save paused" }
    else -> "Waiting for a connection"
}
