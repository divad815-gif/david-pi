package com.davidpi.backup.offline

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.StatFs
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.davidpi.backup.net.DavidPiHttp
import com.davidpi.backup.net.DavidPiOrigin
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.security.MessageDigest

class OfflineDownloadWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    private val storageDirectory = OfflineAudiobookFiles.directory(appContext)
    private val dao = OfflineAudiobookDatabase.get(appContext).dao()
    private val client by lazy { DavidPiHttp.forSession(OkHttpClient.Builder().build()) }

    override suspend fun doWork(): Result {
        val id = inputData.getString(KEY_BOOK_ID).orEmpty()
        if (!OfflineDownloadPolicy.validBookId(id)) return Result.failure()
        return OfflineAudiobookFileLocks.withLock(id) { downloadLocked(id) }
    }

    private suspend fun downloadLocked(id: String): Result {
        val book = dao.get(id) ?: return Result.success()
        val downloadUrl = DavidPiOrigin.canonicalAudiobookDownloadUrl(book.remoteUrl, id)
        if (downloadUrl == null) {
            updateState(book, STATE_FAILED, book.bytesDownloaded, book.etag, "Saved download source is invalid.")
            return Result.failure()
        }
        setForeground(foreground(book.title, 0))
        val partial = File(storageDirectory, "$id.part")
        val final = File(storageDirectory, "$id.audio")
        if (final.isFile && final.length() == book.byteSize) {
            val expectedHash = book.contentSha256
            val observedHash = if (expectedHash == null) null else try {
                sha256(final)
            } catch (_: Exception) {
                updateState(
                    book, STATE_FAILED, final.length(), book.etag,
                    "The saved copy could not be checked. It was left unchanged.",
                )
                return Result.retry()
            }
            val copyAction = OfflineDownloadPolicy.localCopyAction(
                expectedBytes = book.byteSize,
                actualBytes = final.length(),
                expectedSha256 = expectedHash,
                observedSha256 = observedHash,
            )
            if (copyAction != OfflineDownloadPolicy.LocalCopyAction.REPAIR) {
                updateState(
                    book,
                    STATE_READY,
                    final.length(),
                    book.etag,
                    "",
                    expectedHash?.let { System.currentTimeMillis() },
                )
                return Result.success()
            }
            if (!final.delete()) {
                updateState(
                    book, STATE_FAILED, final.length(), null,
                    "The saved copy failed its integrity check and could not be repaired.",
                )
                return Result.failure()
            }
        } else if (final.exists() && !final.delete()) {
            updateState(
                book, STATE_FAILED, final.length(), null,
                "The incomplete saved copy could not be repaired.",
            )
            return Result.failure()
        }

        val discoveredPartialBytes = partial.takeIf { it.isFile }?.length() ?: 0L
        val existing = OfflineDownloadPolicy.resumableBytes(
            discoveredPartialBytes,
            book.byteSize,
            book.etag,
        )
        if (discoveredPartialBytes != existing && partial.exists() && !partial.delete()) {
            updateState(book, STATE_FAILED, discoveredPartialBytes, null, "Could not restart the saved download safely.")
            return Result.failure()
        }
        val needed = (book.byteSize - existing).coerceAtLeast(0L)
        val available = StatFs(partial.parentFile!!.absolutePath).availableBytes
        if (available < needed + OfflineDownloadPolicy.FREE_SPACE_RESERVE) {
            updateState(book, STATE_FAILED, existing, book.etag, "Not enough app storage.")
            return Result.failure()
        }

        var activeEtag = if (existing > 0L) OfflineDownloadPolicy.strongEtag(book.etag) else null
        updateState(book, STATE_DOWNLOADING, existing, activeEtag, "")
        val builder = Request.Builder().url(downloadUrl).get()
        if (existing > 0) {
            builder.header("Range", "bytes=$existing-")
            builder.header("If-Range", activeEtag!!)
        }

        return try {
            com.davidpi.backup.net.PortalIdentity.verify(applicationContext)
            client.newCall(builder.build()).execute().use { response ->
                val body = response.body
                val contract = body?.let {
                    OfflineDownloadPolicy.responseContract(
                        status = response.code,
                        existingBytes = existing,
                        expectedTotal = book.byteSize,
                        storedEtag = activeEtag,
                        responseEtag = response.header("ETag"),
                        contentRange = response.header("Content-Range"),
                        contentLength = it.contentLength(),
                    )
                }
                if (contract == null) {
                    val message = if (response.code == 401 || response.code == 403) {
                        "Open David-Pi online, then try saving again."
                    } else {
                        "Server download response was invalid (${response.code})."
                    }
                    partial.delete()
                    updateState(book, STATE_FAILED, 0L, null, message)
                    return Result.failure()
                }
                val append = contract.mode == OfflineDownloadPolicy.ResponseMode.APPEND
                var written = if (append) existing else 0L
                var copied = 0L
                activeEtag = contract.responseEtag
                updateState(book, STATE_DOWNLOADING, written, activeEtag, "")
                if (isStopped) return Result.failure()
                FileOutputStream(partial, append).use { output ->
                    body.byteStream().use { input ->
                        val buffer = ByteArray(DEFAULT_BUFFER_SIZE * 4)
                        var lastPublished = written
                        while (true) {
                            if (isStopped) return Result.failure()
                            val count = input.read(buffer)
                            if (count < 0) break
                            copied = OfflineDownloadPolicy.checkedSegmentBytes(
                                copiedBytes = copied,
                                nextCount = count,
                                expectedSegmentBytes = contract.expectedSegmentBytes,
                                existingBytes = if (append) existing else 0L,
                                expectedTotal = book.byteSize,
                            )
                            output.write(buffer, 0, count)
                            written = (if (append) existing else 0L) + copied
                            if (written - lastPublished >= 2L * 1024 * 1024) {
                                lastPublished = written
                                val percent = ((written * 100) / book.byteSize.coerceAtLeast(1)).toInt().coerceIn(0, 100)
                                updateState(book, STATE_DOWNLOADING, written, activeEtag, "")
                                setForeground(foreground(book.title, percent))
                            }
                        }
                        if (copied != contract.expectedSegmentBytes) {
                            throw DownloadIntegrityException("Download response ended early")
                        }
                        output.fd.sync()
                    }
                }
                if (written != book.byteSize) {
                    updateState(book, STATE_FAILED, written, activeEtag, "Downloaded file was incomplete.")
                    return Result.failure()
                }
                val expectedHash = book.contentSha256
                if (
                    expectedHash != null && !runCatching {
                        OfflineDownloadPolicy.digestMatches(expectedHash, sha256(partial))
                    }.getOrDefault(false)
                ) {
                    partial.delete()
                    updateState(
                        book, STATE_FAILED, 0L, null,
                        "Downloaded file failed its integrity check. Try saving again.",
                    )
                    return Result.failure()
                }
                if (isStopped) return Result.failure()
                if (!partial.renameTo(final)) {
                    updateState(book, STATE_FAILED, written, activeEtag, "Downloaded file was incomplete.")
                    return Result.failure()
                }
                updateState(
                    book,
                    STATE_READY,
                    written,
                    activeEtag,
                    "",
                    expectedHash?.let { System.currentTimeMillis() },
                )
                Result.success()
            }
        } catch (_: DownloadIntegrityException) {
            partial.delete()
            updateState(book, STATE_FAILED, 0L, null, "Server download integrity check failed. Try saving again.")
            Result.failure()
        } catch (error: Exception) {
            val saved = partial.takeIf { it.isFile }?.length() ?: existing
            updateState(book, STATE_FAILED, saved, activeEtag, "Download paused. Try again when connected.")
            Result.retry()
        }
    }

    private suspend fun updateState(
        book: OfflineAudiobook,
        state: String,
        bytes: Long,
        etag: String?,
        error: String,
        integrityVerifiedAt: Long? = null,
    ) {
        // A confirmed removal deletes the row first. Guarded UPDATE calls keep a
        // stopping worker from resurrecting that book or overwriting its position.
        dao.updateDownload(
            book.id,
            state,
            bytes,
            etag,
            error,
            System.currentTimeMillis(),
            integrityVerifiedAt,
        )
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        FileInputStream(file).use { input ->
            val buffer = ByteArray(256 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                if (count == 0) continue
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun foreground(title: String, percent: Int): ForegroundInfo {
        val manager = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(NotificationChannel(
            CHANNEL_ID,
            "Offline audiobooks",
            NotificationManager.IMPORTANCE_LOW,
        ))
        val notification = NotificationCompat.Builder(applicationContext, CHANNEL_ID)
            .setSmallIcon(com.davidpi.backup.R.drawable.david_pi_launcher)
            .setContentTitle("Saving $title")
            .setContentText(if (percent == 0) "Starting…" else "$percent%")
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setProgress(100, percent, percent == 0)
            .build()
        return ForegroundInfo(NOTIFICATION_ID_BASE + id.hashCode().and(0x0fff), notification)
    }

    companion object {
        const val KEY_BOOK_ID = "book_id"
        const val STATE_QUEUED = "queued"
        const val STATE_DOWNLOADING = "downloading"
        const val STATE_READY = "ready"
        const val STATE_FAILED = "failed"
        private const val CHANNEL_ID = "david_pi_offline_audiobooks"
        private const val NOTIFICATION_ID_BASE = 12_000
        fun workName(id: String) = "offline-audiobook-$id"
        fun tag(id: String) = "offline-audiobook-$id"
    }
}
