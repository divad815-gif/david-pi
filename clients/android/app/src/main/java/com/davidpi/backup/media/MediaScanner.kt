package com.davidpi.backup.media

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import com.davidpi.backup.data.BackupDao
import com.davidpi.backup.data.IntegrityRevalidationBudget
import com.davidpi.backup.data.QueueObservation
import com.davidpi.backup.data.QueuedMedia
import com.davidpi.backup.data.QueuedMediaObservation
import com.davidpi.backup.data.SyncState
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import java.security.MessageDigest

enum class MediaAccessScope { FULL, PARTIAL, NONE }

object MediaAccessPolicy {
    fun scope(images: Boolean, videos: Boolean, selectedOnly: Boolean): MediaAccessScope = when {
        images && videos -> MediaAccessScope.FULL
        images || videos || selectedOnly -> MediaAccessScope.PARTIAL
        else -> MediaAccessScope.NONE
    }
}

data class CompleteMediaScan(
    val scanGeneration: String,
    val visibleClientItemIds: List<String>,
    val newlyQueuedCount: Int,
)

class IncompleteMediaScanException(message: String) : IllegalStateException(message)

internal class CompleteScanAccumulator(private val generation: String) {
    private val completedCollections = mutableSetOf<String>()
    private val visible = mutableSetOf<String>()
    private var newlyQueued = 0
    private var cancelled = false

    fun completeCollection(type: String, ids: Collection<String>, discovered: Int) {
        check(type == "image" || type == "video")
        check(type !in completedCollections)
        completedCollections += type
        visible += ids
        newlyQueued += discovered
    }

    fun cancel() {
        cancelled = true
    }

    fun finish(): CompleteMediaScan {
        if (cancelled || completedCollections != setOf("image", "video")) {
            throw IncompleteMediaScanException(
                "Both image and video collections must complete without cancellation."
            )
        }
        return CompleteMediaScan(generation, visible.sorted(), newlyQueued)
    }
}

internal class TransactionBatcher<T, R>(
    private val batchSize: Int,
    private val transaction: suspend (List<T>) -> List<R>,
) {
    private val pending = mutableListOf<T>()

    init {
        require(batchSize > 0)
    }

    suspend fun add(item: T): List<R> {
        pending += item
        return if (pending.size == batchSize) flush() else emptyList()
    }

    suspend fun finish(): List<R> = flush()

    private suspend fun flush(): List<R> {
        if (pending.isEmpty()) return emptyList()
        val batch = pending.toList()
        pending.clear()
        return transaction(batch).also {
            check(it.size == batch.size) { "Observation transaction returned a partial result" }
        }
    }
}

internal class ScanObservationCollector {
    private val visible = mutableListOf<String>()
    var newlyQueuedCount: Int = 0
        private set

    val visibleClientItemIds: List<String>
        get() = visible

    fun collect(results: List<QueueObservation>) {
        results.forEach { observation ->
            visible += observation.clientItemId
            if (observation.newlyQueued) newlyQueuedCount++
        }
    }
}

object SourceSignature {
    fun fromMetadata(
        type: String,
        mediaStoreId: Long,
        byteSize: Long,
        mimeType: String,
        sourceVersion: Long,
    ): String {
        val canonical = listOf(
            type, mediaStoreId.toString(), byteSize.toString(), mimeType, sourceVersion.toString()
        ).joinToString("\u0000")
        return MessageDigest.getInstance("SHA-256")
            .digest(canonical.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }
}

class MediaScanner(private val context: Context, private val dao: BackupDao) {
    fun accessScope(): MediaAccessScope {
        val imagePermission = if (Build.VERSION.SDK_INT >= 33) {
            Manifest.permission.READ_MEDIA_IMAGES
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }
        val videoPermission = if (Build.VERSION.SDK_INT >= 33) {
            Manifest.permission.READ_MEDIA_VIDEO
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }
        val selectedOnly = Build.VERSION.SDK_INT >= 34 &&
            ContextCompat.checkSelfPermission(
                context, Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED
            ) == PackageManager.PERMISSION_GRANTED
        return MediaAccessPolicy.scope(
            images = ContextCompat.checkSelfPermission(
                context, imagePermission
            ) == PackageManager.PERMISSION_GRANTED,
            videos = ContextCompat.checkSelfPermission(
                context, videoPermission
            ) == PackageManager.PERMISSION_GRANTED,
            selectedOnly = selectedOnly,
        )
    }

    fun hasFullAccess(): Boolean = accessScope() == MediaAccessScope.FULL

    suspend fun scan(
        includeScreenshots: Boolean = false,
        includeDownloads: Boolean = false,
        scheduleIntegrityRevalidation: Boolean = false,
    ): CompleteMediaScan {
        if (!hasFullAccess()) {
            throw IncompleteMediaScanException(
                "Full photo and video permission is required for reconciliation."
            )
        }
        val generation = UuidV7Generator.next()
        val integrityNow = System.currentTimeMillis()
        val revalidationBudget = if (scheduleIntegrityRevalidation) {
            IntegrityRevalidationBudget(
                dao.getState(IntegrityRevalidationBudget.STATE_KEY)
            )
        } else {
            null
        }
        val accumulator = CompleteScanAccumulator(generation)
        try {
            val images = scanCollection(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                "image",
                generation,
                includeScreenshots,
                includeDownloads,
                revalidationBudget,
                integrityNow,
            )
            accumulator.completeCollection("image", images.ids, images.newlyQueued)
            currentCoroutineContext().ensureActive()
            val videos = scanCollection(
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                "video",
                generation,
                includeScreenshots,
                includeDownloads,
                revalidationBudget,
                integrityNow,
            )
            accumulator.completeCollection("video", videos.ids, videos.newlyQueued)
            currentCoroutineContext().ensureActive()
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            accumulator.cancel()
            throw cancelled
        }
        val complete = accumulator.finish()
        // Only a fully completed image+video observation may retire unseen
        // queue rows. Active server sessions remain durably referenced in a
        // cleanup state until the worker's authenticated DELETE succeeds.
        dao.retireUnseenAfterCompleteScan(generation)
        if (revalidationBudget != null) {
            dao.putState(
                SyncState(
                    IntegrityRevalidationBudget.STATE_KEY,
                    revalidationBudget.completedScanCursor(),
                    integrityNow,
                )
            )
        }
        return complete
    }

    private data class CollectionResult(val ids: List<String>, val newlyQueued: Int)

    private suspend fun scanCollection(
        collection: Uri,
        type: String,
        generation: String,
        includeScreenshots: Boolean,
        includeDownloads: Boolean,
        revalidationBudget: IntegrityRevalidationBudget?,
        integrityNow: Long,
    ): CollectionResult {
        val sourceVersionColumn = if (Build.VERSION.SDK_INT >= 30) {
            MediaStore.MediaColumns.GENERATION_MODIFIED
        } else {
            MediaStore.MediaColumns.DATE_MODIFIED
        }
        val columns = arrayOf(
            MediaStore.MediaColumns._ID,
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.SIZE,
            MediaStore.MediaColumns.DATE_TAKEN,
            MediaStore.MediaColumns.DATE_ADDED,
            sourceVersionColumn,
            MediaStore.MediaColumns.BUCKET_DISPLAY_NAME,
        )
        val observed = ScanObservationCollector()
        val batcher = TransactionBatcher<QueuedMediaObservation, QueueObservation>(
            BackupDao.OBSERVATION_BATCH_SIZE,
        ) { observations ->
            dao.recordObservations(observations, revalidationBudget, integrityNow)
        }
        val cursor = context.contentResolver.query(
            collection,
            columns,
            null,
            null,
            "${MediaStore.MediaColumns._ID} ASC",
        ) ?: throw IncompleteMediaScanException("The $type media query did not complete.")
        cursor.use {
            val idIndex = it.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
            val nameIndex = it.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
            val mimeIndex = it.getColumnIndexOrThrow(MediaStore.MediaColumns.MIME_TYPE)
            val sizeIndex = it.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)
            val takenIndex = it.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_TAKEN)
            val addedIndex = it.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_ADDED)
            val sourceVersionIndex = it.getColumnIndexOrThrow(sourceVersionColumn)
            val bucketIndex = it.getColumnIndexOrThrow(MediaStore.MediaColumns.BUCKET_DISPLAY_NAME)
            while (it.moveToNext()) {
                currentCoroutineContext().ensureActive()
                val id = it.getLong(idIndex)
                val bucket = it.getString(bucketIndex)
                val uri = ContentUris.withAppendedId(collection, id)
                val mimeType = it.getString(mimeIndex) ?: "application/octet-stream"
                val size = it.getLong(sizeIndex)
                val sourceVersion = it.getLong(sourceVersionIndex)
                val signature = SourceSignature.fromMetadata(
                    type, id, size, mimeType, sourceVersion
                )
                val taken = it.getLong(takenIndex).takeIf { value -> value > 0 }
                    ?: it.getLong(addedIndex).takeIf { value -> value > 0 }?.times(1000)
                val selectedForBackup = MediaPolicy.includeBucket(
                    bucket, includeScreenshots, includeDownloads
                )
                observed.collect(batcher.add(
                    QueuedMediaObservation(
                        media = QueuedMedia(
                            queueId = MediaPolicy.queueId(type, id, signature),
                            mediaStoreId = id,
                            mediaType = type,
                            contentUri = uri.toString(),
                            displayName = it.getString(nameIndex) ?: "$type-$id",
                            mimeType = mimeType,
                            byteSize = size,
                            captureTimestamp = taken,
                            bucketName = bucket,
                            scanGeneration = generation,
                            sourceSignature = signature,
                        ),
                        enqueueIfMissing = selectedForBackup,
                    )
                ))
            }
        }
        observed.collect(batcher.finish())
        return CollectionResult(
            observed.visibleClientItemIds,
            observed.newlyQueuedCount,
        )
    }
}
