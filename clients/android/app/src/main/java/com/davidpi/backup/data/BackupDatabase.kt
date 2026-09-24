package com.davidpi.backup.data

import android.content.Context
import androidx.room.*
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import kotlinx.coroutines.flow.Flow

@Entity(
    tableName = "media_queue",
    indices = [Index(value = ["mediaStoreId", "mediaType"], unique = true), Index("state")]
)
data class QueuedMedia(
    @PrimaryKey val queueId: String,
    val mediaStoreId: Long,
    val mediaType: String,
    val contentUri: String,
    val displayName: String,
    val mimeType: String,
    val byteSize: Long,
    val captureTimestamp: Long?,
    val bucketName: String?,
    val sha256: String? = null,
    val serverUploadId: String? = null,
    val acceptedOffset: Long = 0,
    val state: String = "discovered",
    val retryCount: Int = 0,
    val errorCode: String? = null,
    val updatedAt: Long = System.currentTimeMillis(),
    val scanGeneration: String = "legacy-existing",
    val sourceSignature: String = "",
)

@Entity(tableName = "sync_state")
data class SyncState(
    @PrimaryKey val key: String,
    val value: String,
    val updatedAt: Long = System.currentTimeMillis()
)

data class QueueObservation(val clientItemId: String, val newlyQueued: Boolean)

data class QueuedMediaObservation(
    val media: QueuedMedia,
    val enqueueIfMissing: Boolean,
)

internal data class ObservationBatchPlan(
    val deletes: List<QueuedMedia>,
    val updates: List<QueuedMedia>,
    val inserts: List<QueuedMedia>,
    val results: List<QueueObservation>,
)

internal object QueueObservationPlanner {
    fun plan(
        observations: List<QueuedMediaObservation>,
        existingRows: List<QueuedMedia>,
        nowMillis: Long = System.currentTimeMillis(),
        revalidationBudget: IntegrityRevalidationBudget? = null,
    ): ObservationBatchPlan {
        val existingById = existingRows.associateBy { it.mediaStoreId }
        val deletes = mutableListOf<QueuedMedia>()
        val updates = mutableListOf<QueuedMedia>()
        val inserts = mutableListOf<QueuedMedia>()
        val results = mutableListOf<QueueObservation>()

        observations.forEach { observation ->
            val existing = existingById[observation.media.mediaStoreId]
            if (existing == null) {
                if (observation.enqueueIfMissing) inserts += observation.media
                // Reconciliation describes the complete physical observation,
                // not only items selected for upload. The deterministic opaque
                // ID is safe to report even when no queue row is created.
                results += QueueObservation(
                    observation.media.queueId,
                    observation.enqueueIfMissing,
                )
                return@forEach
            }
            val observed = QueueMergePolicy.normalizeObservationIdentity(
                existing, observation.media
            )
            if (QueueMergePolicy.resetRequired(existing, observed)) {
                if (QueueSourceChangePolicy.requiresServerCleanup(existing)) {
                    updates += QueueSourceChangePolicy.deferReplacement(
                        existing,
                        observed,
                        observation.enqueueIfMissing,
                    )
                } else {
                    deletes += existing
                    if (observation.enqueueIfMissing) inserts += observed
                }
                results += QueueObservation(observed.queueId, observation.enqueueIfMissing)
            } else {
                val merged = if (
                    observation.enqueueIfMissing &&
                    QueueIntegrityRevalidationPolicy.isDue(existing, nowMillis) &&
                    revalidationBudget?.reserve(existing) == true
                ) {
                    QueueIntegrityRevalidationPolicy.schedule(
                        existing,
                        observed,
                        nowMillis,
                    )
                } else {
                    QueueMergePolicy.merge(existing, observed)
                }
                updates += merged
                results += QueueObservation(merged.queueId, false)
            }
        }
        return ObservationBatchPlan(deletes, updates, inserts, results)
    }
}

object QueueSourceChangePolicy {
    const val STATE = "source_changed"
    const val REHASH_STATE = "source_rehash"
    const val REPLACE = "source_changed_replace"
    const val REMOVE = "source_changed_remove"
    const val MAX_UNREADABLE_ATTEMPTS = 3

    fun checkpointAfterServerCleanup(item: QueuedMedia): QueuedMedia = item.copy(
        serverUploadId = null,
        acceptedOffset = 0,
        state = REHASH_STATE,
        updatedAt = System.currentTimeMillis(),
    )

    fun shouldQuarantineUnreadable(failedAttempts: Int): Boolean =
        failedAttempts >= MAX_UNREADABLE_ATTEMPTS

    fun stateAfterTemporaryFailure(currentState: String): String = when (currentState) {
        STATE -> STATE
        REHASH_STATE -> REHASH_STATE
        else -> "retryable_error"
    }

    fun requiresServerCleanup(existing: QueuedMedia): Boolean =
        existing.serverUploadId != null && existing.state !in setOf(
            "primary_verified", "secondary_pending", "fully_protected", "permanent_error"
        )

    fun deferReplacement(
        existing: QueuedMedia,
        observed: QueuedMedia,
        enqueueReplacement: Boolean,
    ): QueuedMedia = observed.copy(
        // Preserve the primary key and server session until an authenticated
        // cleanup succeeds. The replacement ID is deterministically recovered
        // from the newly observed source signature afterwards.
        queueId = existing.queueId,
        sha256 = null,
        serverUploadId = existing.serverUploadId,
        acceptedOffset = existing.acceptedOffset,
        state = STATE,
        retryCount = existing.retryCount,
        errorCode = if (enqueueReplacement) REPLACE else REMOVE,
        updatedAt = System.currentTimeMillis(),
    )
}

class IntegrityRevalidationBudget(
    storedCursor: String?,
    private val maxItems: Int = MAX_ITEMS_PER_SCAN,
    private val maxBytes: Long = MAX_BYTES_PER_SCAN,
) {
    companion object {
        const val STATE_KEY = "media_integrity_revalidation_cursor_v1"
        const val MAX_ITEMS_PER_SCAN = 4096
        const val MAX_BYTES_PER_SCAN = 64L * 1024 * 1024 * 1024
        // The persisted cursor alternates media types each day. These limits
        // therefore cover up to 50,000 items and 832 GiB *per media type* in
        // at most 26 days. Larger libraries continue deterministically; their
        // volume bound is 2 * ceil(bytes / 64 GiB) calendar days.
        const val REFERENCE_LIBRARY_ITEMS = 50_000
        const val REFERENCE_LIBRARY_BYTES_PER_TYPE = 832L * 1024 * 1024 * 1024
        const val COUNT_BOUND_CYCLE_DAYS = 26
        const val VOLUME_BOUND_CYCLE_DAYS = 26L
        private const val INITIAL_TYPE = "image"
        private val CURSOR_PATTERN = Regex("[01]:[0-9]{20}")

        fun stableKey(item: QueuedMedia): String {
            val prefix = when (item.mediaType) {
                "image" -> "0"
                "video" -> "1"
                else -> "2"
            }
            return "$prefix:${item.mediaStoreId.coerceAtLeast(0).toString().padStart(20, '0')}"
        }
    }

    private data class SavedState(
        val imageCursor: String,
        val videoCursor: String,
        val activeType: String,
    )

    private val saved = parse(storedCursor)
    private var itemCount = 0
    private var byteCount = 0L
    private var lastScheduledKey: String? = null
    private var stoppedAtBudget = false

    init {
        require(maxItems > 0)
        require(maxBytes > 0)
    }

    fun reserve(item: QueuedMedia): Boolean {
        if (stoppedAtBudget || item.mediaType != saved.activeType) return false
        val key = stableKey(item)
        val startingCursor = if (saved.activeType == "image") {
            saved.imageCursor
        } else {
            saved.videoCursor
        }
        if (key <= startingCursor) return false
        val bytes = item.byteSize.coerceIn(0, maxBytes)
        if (itemCount >= maxItems || (itemCount > 0 && byteCount > maxBytes - bytes)) {
            stoppedAtBudget = true
            return false
        }
        itemCount += 1
        byteCount += bytes
        lastScheduledKey = key
        if (itemCount >= maxItems || byteCount >= maxBytes) stoppedAtBudget = true
        return true
    }

    fun checkpointCursor(): String = encode(
        activeCursor(completed = false),
        saved.activeType,
    )

    fun completedScanCursor(): String = encode(
        activeCursor(completed = true),
        if (saved.activeType == "image") "video" else "image",
    )

    fun scheduledItems(): Int = itemCount

    fun scheduledBytes(): Long = byteCount

    fun activeMediaType(): String = saved.activeType

    private fun activeCursor(completed: Boolean): String {
        val starting = if (saved.activeType == "image") saved.imageCursor else saved.videoCursor
        return if (completed && !stoppedAtBudget) "" else lastScheduledKey ?: starting
    }

    private fun encode(activeCursor: String, nextType: String): String {
        val image = if (saved.activeType == "image") activeCursor else saved.imageCursor
        val video = if (saved.activeType == "video") activeCursor else saved.videoCursor
        return "v1|$image|$video|$nextType"
    }

    private fun parse(value: String?): SavedState {
        val fields = value.orEmpty().split("|", limit = 4)
        if (
            fields.size != 4 || fields[0] != "v1" ||
            fields[1].let { it.isNotEmpty() && !CURSOR_PATTERN.matches(it) } ||
            fields[2].let { it.isNotEmpty() && !CURSOR_PATTERN.matches(it) } ||
            fields[3] !in setOf("image", "video")
        ) {
            return SavedState("", "", INITIAL_TYPE)
        }
        if (
            fields[1].isNotEmpty() && !fields[1].startsWith("0:") ||
            fields[2].isNotEmpty() && !fields[2].startsWith("1:")
        ) {
            return SavedState("", "", INITIAL_TYPE)
        }
        return SavedState(fields[1], fields[2], fields[3])
    }
}

object QueueIntegrityRevalidationPolicy {
    const val INTERVAL_MILLIS = 24L * 60 * 60 * 1000
    const val ERROR_PREFIX = "integrity_revalidate:"
    const val FAILURE_STATE_PREFIX = "media_integrity_failure:"
    private const val FALLBACK_ERROR = "terminal_error"
    private val RESTORABLE_STATES = setOf(
        "primary_verified", "secondary_pending", "fully_protected", "permanent_error"
    )

    fun isDue(item: QueuedMedia, nowMillis: Long): Boolean =
        item.state in RESTORABLE_STATES &&
            item.updatedAt <= nowMillis - INTERVAL_MILLIS

    fun schedule(
        existing: QueuedMedia,
        observed: QueuedMedia,
        nowMillis: Long,
    ): QueuedMedia {
        require(existing.state in RESTORABLE_STATES)
        val priorError = existing.errorCode.orEmpty()
        val checkpoint = listOf(existing.state, priorError).joinToString("|")
        return existing.copy(
            contentUri = observed.contentUri,
            displayName = observed.displayName,
            mimeType = observed.mimeType,
            captureTimestamp = observed.captureTimestamp,
            bucketName = observed.bucketName,
            scanGeneration = observed.scanGeneration,
            sourceSignature = observed.sourceSignature,
            state = QueueSourceChangePolicy.REHASH_STATE,
            retryCount = 0,
            errorCode = ERROR_PREFIX + checkpoint,
            updatedAt = nowMillis,
        )
    }

    fun isCheckpoint(item: QueuedMedia): Boolean =
        item.state == QueueSourceChangePolicy.REHASH_STATE &&
            item.errorCode.orEmpty().startsWith(ERROR_PREFIX)

    fun unchanged(
        checkpoint: QueuedMedia,
        observedSha256: String,
        observedSize: Long,
    ): Boolean = checkpoint.sha256 != null &&
        checkpoint.sha256 == observedSha256 && checkpoint.byteSize == observedSize

    fun restoreUnchanged(checkpoint: QueuedMedia, nowMillis: Long): QueuedMedia {
        require(isCheckpoint(checkpoint))
        val prior = checkpoint.errorCode.orEmpty()
            .removePrefix(ERROR_PREFIX)
            .split("|", limit = 2)
        val priorState = prior.firstOrNull().orEmpty()
        require(priorState in RESTORABLE_STATES)
        val priorError = prior.getOrNull(1).orEmpty().ifBlank {
            if (priorState == "permanent_error") FALLBACK_ERROR else ""
        }
        return checkpoint.copy(
            state = priorState,
            retryCount = 0,
            errorCode = priorError.ifBlank { null },
            updatedAt = nowMillis,
        )
    }

    fun failureStateKey(queueId: String): String = FAILURE_STATE_PREFIX + queueId

    fun failureStateValue(attempts: Int, nowMillis: Long): String =
        "source_unreadable|${attempts.coerceAtLeast(1)}|${nowMillis.coerceAtLeast(0)}"
}

object QueueMergePolicy {
    fun normalizeObservationIdentity(
        existing: QueuedMedia?,
        observed: QueuedMedia,
    ): QueuedMedia = if (
        existing != null &&
        existing.sourceSignature.isNotBlank() &&
        existing.sourceSignature == observed.sourceSignature
    ) {
        observed.copy(queueId = existing.queueId)
    } else {
        observed
    }

    fun merge(existing: QueuedMedia?, observed: QueuedMedia): QueuedMedia {
        if (existing == null) return observed
        val sameSource = existing.sourceSignature.isBlank() ||
            (existing.sourceSignature == observed.sourceSignature &&
                existing.queueId == observed.queueId)
        if (!sameSource) return observed
        // Metadata and scan evidence may advance, but transport state survives
        // an ordinary repeat observation and the v1 legacy adoption scan.
        return existing.copy(
            contentUri = observed.contentUri,
            displayName = observed.displayName,
            mimeType = observed.mimeType,
            byteSize = observed.byteSize,
            captureTimestamp = observed.captureTimestamp,
            bucketName = observed.bucketName,
            scanGeneration = observed.scanGeneration,
            sourceSignature = observed.sourceSignature,
        )
    }

    fun resetRequired(existing: QueuedMedia, observed: QueuedMedia): Boolean =
        existing.sourceSignature.isBlank() ||
            existing.sourceSignature != observed.sourceSignature ||
            existing.queueId != observed.queueId
}

@Dao
abstract class BackupDao {
    @Query("UPDATE media_queue SET serverUploadId = NULL, acceptedOffset = 0, state = 'queued' WHERE state != 'permanent_error'")
    abstract suspend fun reconnectDevice()

    companion object {
        // Leaves ample room below SQLite's usual 999-variable ceiling for the
        // media type and future query predicates.
        const val OBSERVATION_BATCH_SIZE = 400
    }

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    abstract suspend fun enqueue(item: QueuedMedia): Long

    @Query("""SELECT * FROM media_queue
        WHERE state IN (
            'discovered','queued','uploading','retryable_error','source_changed','source_rehash'
        )
        ORDER BY CASE
                     WHEN state='source_changed' THEN 0
                     WHEN state='source_rehash' AND
                          errorCode LIKE 'integrity_revalidate:%' THEN 3
                     WHEN state IN ('retryable_error','source_rehash') THEN 2
                     ELSE 1
                 END,
                 retryCount, captureTimestamp, queueId LIMIT 1""")
    abstract suspend fun nextPending(): QueuedMedia?

    @Query("""SELECT COUNT(*) FROM media_queue
        WHERE state IN ('discovered','queued','uploading','retryable_error','source_changed')
           OR (state='source_rehash' AND
               (errorCode IS NULL OR errorCode NOT LIKE 'integrity_revalidate:%'))""")
    abstract suspend fun countNonIntegrityPending(): Int

    @Query("""SELECT * FROM media_queue
        WHERE state='source_rehash' AND errorCode LIKE 'integrity_revalidate:%'
        ORDER BY retryCount,captureTimestamp,queueId LIMIT 1""")
    abstract suspend fun nextIntegrityRevalidation(): QueuedMedia?

    @Query("SELECT * FROM media_queue WHERE queueId=:id")
    abstract suspend fun byId(id: String): QueuedMedia?

    @Query("SELECT * FROM media_queue WHERE mediaStoreId=:id AND mediaType=:type")
    abstract suspend fun byMediaStoreId(id: Long, type: String): QueuedMedia?

    @Query("SELECT * FROM media_queue WHERE mediaType=:type AND mediaStoreId IN (:ids)")
    abstract suspend fun byMediaStoreIds(ids: List<Long>, type: String): List<QueuedMedia>

    @Insert(onConflict = OnConflictStrategy.ABORT)
    abstract suspend fun enqueueBatch(items: List<QueuedMedia>): List<Long>

    @Update
    abstract suspend fun updateBatch(items: List<QueuedMedia>)

    @Delete
    abstract suspend fun deleteBatch(items: List<QueuedMedia>)

    @Update
    abstract suspend fun update(item: QueuedMedia)

    @Delete
    abstract suspend fun delete(item: QueuedMedia)

    @Query("DELETE FROM media_queue WHERE queueId=:id")
    abstract suspend fun deleteById(id: String)

    @Query("SELECT * FROM media_queue WHERE scanGeneration<>:generation")
    abstract suspend fun notObservedIn(generation: String): List<QueuedMedia>

    @Transaction
    open suspend fun replaceAfterSourceChange(oldId: String, replacement: QueuedMedia) {
        require(oldId != replacement.queueId) { "A changed source must use a new queue identity" }
        deleteById(oldId)
        check(enqueue(replacement) != -1L) { "Changed-source queue replacement failed" }
    }

    @Transaction
    open suspend fun retireUnseenAfterCompleteScan(generation: String) {
        val unseen = notObservedIn(generation)
        val cleanup = unseen.filter(QueueSourceChangePolicy::requiresServerCleanup).map {
            it.copy(
                state = QueueSourceChangePolicy.STATE,
                errorCode = QueueSourceChangePolicy.REMOVE,
                updatedAt = System.currentTimeMillis(),
            )
        }
        val removable = unseen.filterNot(QueueSourceChangePolicy::requiresServerCleanup)
        if (cleanup.isNotEmpty()) updateBatch(cleanup)
        if (removable.isNotEmpty()) deleteBatch(removable)
    }

    @Query("UPDATE media_queue SET state=:state, updatedAt=:now WHERE queueId=:id")
    abstract suspend fun setState(id: String, state: String, now: Long = System.currentTimeMillis())

    @Query("SELECT state, COUNT(*) AS count FROM media_queue GROUP BY state")
    abstract fun observeCounts(): Flow<List<StateCount>>

    @Query("SELECT COALESCE(SUM(byteSize),0) FROM media_queue WHERE state NOT IN ('primary_verified','secondary_pending','fully_protected','permanent_error')")
    abstract fun observePendingBytes(): Flow<Long>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    abstract suspend fun putState(state: SyncState)

    @Query("SELECT value FROM sync_state WHERE `key`=:key")
    abstract suspend fun getState(key: String): String?

    @Query("DELETE FROM sync_state WHERE `key`=:key")
    abstract suspend fun deleteState(key: String)

    @Transaction
    open suspend fun recordObservations(
        observations: List<QueuedMediaObservation>,
        revalidationBudget: IntegrityRevalidationBudget? = null,
        nowMillis: Long = System.currentTimeMillis(),
    ): List<QueueObservation> {
        require(observations.isNotEmpty()) { "An observation batch cannot be empty" }
        require(observations.size <= OBSERVATION_BATCH_SIZE) {
            "An observation batch exceeds the transaction bound"
        }
        val mediaType = observations.first().media.mediaType
        require(observations.all { it.media.mediaType == mediaType }) {
            "An observation batch must contain one media type"
        }
        val mediaStoreIds = observations.map { it.media.mediaStoreId }
        require(mediaStoreIds.distinct().size == mediaStoreIds.size) {
            "An observation batch contains duplicate MediaStore IDs"
        }
        val plan = QueueObservationPlanner.plan(
            observations,
            byMediaStoreIds(mediaStoreIds, mediaType),
            nowMillis,
            revalidationBudget,
        )

        if (plan.deletes.isNotEmpty()) deleteBatch(plan.deletes)
        if (plan.updates.isNotEmpty()) updateBatch(plan.updates)
        if (plan.inserts.isNotEmpty()) {
            val inserted = enqueueBatch(plan.inserts)
            check(inserted.size == plan.inserts.size && inserted.none { it == -1L }) {
                "Observation batch insertion failed"
            }
        }
        if (revalidationBudget != null) {
            putState(
                SyncState(
                    IntegrityRevalidationBudget.STATE_KEY,
                    revalidationBudget.checkpointCursor(),
                    nowMillis,
                )
            )
        }
        return plan.results
    }
}

data class StateCount(val state: String, val count: Int)

object BackupQueuePolicy {
    private val completedStates = setOf(
        "primary_verified", "secondary_pending", "fully_protected", "permanent_error"
    )
    fun isPending(state: String): Boolean = state !in completedStates
}

@Database(entities = [QueuedMedia::class, SyncState::class], version = 2, exportSchema = true)
abstract class BackupDatabase : RoomDatabase() {
    abstract fun dao(): BackupDao

    companion object {
        private val instances = mutableMapOf<String, BackupDatabase>()
        fun get(context: Context): BackupDatabase = synchronized(this) {
            val scope = com.davidpi.backup.net.DavidPiOrigin.sessionScope
            instances.getOrPut(scope) { Room.databaseBuilder(
                context.applicationContext, BackupDatabase::class.java, com.davidpi.backup.security.HouseholdStorage.databaseName(context, "david-pi-backup")
            ).addMigrations(MIGRATION_1_2).build() }
        }

        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL(
                    "ALTER TABLE media_queue ADD COLUMN scanGeneration " +
                        "TEXT NOT NULL DEFAULT 'legacy-existing'"
                )
                database.execSQL(
                    "ALTER TABLE media_queue ADD COLUMN sourceSignature " +
                        "TEXT NOT NULL DEFAULT ''"
                )
            }
        }
    }
}
