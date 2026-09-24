package com.davidpi.backup.offline

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import com.davidpi.backup.net.DavidPiOrigin
import org.json.JSONObject

class OfflineAudiobookBridge internal constructor(
    context: Context,
    private val repository: OfflineAudiobookRepository,
    private val scope: CoroutineScope,
    private val recoverQueue: (Context) -> Unit,
    private val enqueue: suspend (Context, String) -> Unit,
    private val verifyIdentity: suspend (Context) -> Unit,
) {
    private val appContext = context.applicationContext

    constructor(context: Context) : this(
        context = context,
        repository = OfflineAudiobookDatabase.get(context.applicationContext).dao(),
        scope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
        recoverQueue = OfflineDownloadScheduler::recover,
        enqueue = OfflineDownloadScheduler::enqueueAndConfirm,
        verifyIdentity = com.davidpi.backup.net.PortalIdentity::verify,
    )

    init {
        // A process can die after the durable Room write but before WorkManager
        // confirms its own transaction. Every new portal bridge retries that
        // narrow gap without requiring the user to remember the request.
        recoverQueue(appContext)
    }

    fun saveAudiobook(raw: String, reply: (String) -> Unit) {
        scope.launch {
            reply(saveAudiobookDurably(raw))
        }
    }

    fun progress(raw: String, reply: (String) -> Unit) {
        scope.launch {
            reply(runCatching {
                verifyIdentity(appContext)
                OfflineProgressStore(appContext).request(JSONObject(raw)).toString()
            }.getOrElse { "error:${it.message ?: "Unable to synchronize progress."}" })
        }
    }

    fun release() {
        scope.cancel()
    }

    private suspend fun saveAudiobookDurably(raw: String): String = runCatching {
        verifyIdentity(appContext)
        val json = JSONObject(raw)
        val id = json.optString("book_id")
        val downloadUrl = json.optString("download_url")
        require(OfflineDownloadPolicy.validBookId(id)) { "Invalid audiobook id." }
        val canonicalDownloadUrl = requireNotNull(
            DavidPiOrigin.canonicalAudiobookDownloadUrl(downloadUrl, id)
        ) { "Invalid audiobook source." }

        val byteSize = json.optLong("byte_size", -1)
        require(byteSize in 1..OfflineDownloadPolicy.MAX_BOOK_BYTES) { "Unsupported audiobook size." }
        val contentSha256 = json.optString("content_sha256")
        require(OfflineDownloadPolicy.validSha256(contentSha256)) {
            "Invalid audiobook integrity value."
        }
        val existing = repository.get(id)
        val progressScope = json.optString("progress_scope")
        require(Regex("[0-9a-f]{32}").matches(progressScope)) { "Open the audiobook shelf to verify the listening identity." }
        require(existing?.progressScope == null || existing.progressScope == progressScope) { "Listening identity changed." }
        val preserveLocal = existing != null && (existing.progressDirty || existing.progressScope == null)
        val book = OfflineAudiobook(
            id = id,
            title = json.optString("title").take(240).ifBlank { "Audiobook" },
            author = json.optString("author").take(240),
            series = json.optString("series").take(240),
            durationSeconds = json.optDouble("duration_seconds", 0.0).coerceAtLeast(0.0),
            byteSize = byteSize,
            contentSha256 = contentSha256,
            contentType = json.optString("content_type").take(120),
            remoteUrl = canonicalDownloadUrl,
            coverUrl = validatedCoverUrl(json.optString("cover_url"), id),
            localFileName = "$id.audio",
            state = OfflineDownloadWorker.STATE_QUEUED,
            bytesDownloaded = if (existing?.contentSha256 == contentSha256) {
                existing.bytesDownloaded
            } else 0,
            etag = existing?.etag.takeIf { existing?.contentSha256 == contentSha256 },
            positionSeconds = if (preserveLocal) existing!!.positionSeconds else json.optDouble("position_seconds", 0.0).coerceAtLeast(0.0),
            completed = if (preserveLocal) existing!!.completed else json.optBoolean("completed", false),
            progressScope = if (existing != null && existing.progressScope == null) null else progressScope,
            progressRevision = if (preserveLocal) existing!!.progressRevision else json.optLong("progress_revision", 0),
            progressSession = existing?.progressSession?.takeIf { it.isNotBlank() } ?: java.util.UUID.randomUUID().toString().replace("-", ""),
            progressSequence = existing?.progressSequence ?: 0,
            progressDirty = existing?.progressDirty ?: false,
            integrityVerifiedAt = existing?.integrityVerifiedAt
                .takeIf { existing?.contentSha256 == contentSha256 },
        )
        repository.saveDownload(book)
        // Do not acknowledge "queued" until WorkManager's transaction is
        // durable. If confirmation fails, the queued Room row remains and the
        // recovery worker will safely enqueue it after this process restarts.
        enqueue(appContext, id)
        "queued"
    }.getOrElse { "error:${it.message ?: "Unable to save audiobook."}" }

    private fun validatedCoverUrl(value: String, bookId: String): String =
        requireNotNull(DavidPiOrigin.canonicalAudiobookCoverUrl(value, bookId)) {
            "Invalid audiobook cover source."
        }
}
