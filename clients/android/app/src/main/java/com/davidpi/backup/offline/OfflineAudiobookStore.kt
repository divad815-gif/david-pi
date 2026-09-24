package com.davidpi.backup.offline

import android.content.Context
import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.Upsert
import androidx.room.Transaction
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

@Entity(tableName = "offline_audiobooks")
data class OfflineAudiobook(
    @PrimaryKey val id: String,
    val title: String,
    val author: String,
    val series: String,
    val durationSeconds: Double,
    val byteSize: Long,
    val contentSha256: String? = null,
    val contentType: String,
    val remoteUrl: String,
    val coverUrl: String,
    val localFileName: String,
    val state: String,
    val bytesDownloaded: Long = 0,
    val etag: String? = null,
    val positionSeconds: Double = 0.0,
    val completed: Boolean = false,
    val updatedAt: Long = System.currentTimeMillis(),
    val integrityVerifiedAt: Long? = null,
    val error: String = "",
    val progressScope: String? = null,
    @ColumnInfo(defaultValue = "0") val progressRevision: Long = 0,
    @ColumnInfo(defaultValue = "''") val progressSession: String = "",
    @ColumnInfo(defaultValue = "0") val progressSequence: Long = 0,
    @ColumnInfo(defaultValue = "0") val progressDirty: Boolean = false,
)

interface OfflineAudiobookRepository {
    suspend fun get(id: String): OfflineAudiobook?
    suspend fun upsert(book: OfflineAudiobook)
    suspend fun saveDownload(book: OfflineAudiobook) { upsert(book) }
}

@Dao
interface OfflineAudiobookDao : OfflineAudiobookRepository {
    @Query("SELECT * FROM offline_audiobooks WHERE progressDirty = 1 AND progressScope = :scope ORDER BY updatedAt")
    suspend fun pendingProgress(scope: String): List<OfflineAudiobook>
    @Query("SELECT * FROM offline_audiobooks WHERE progressScope IS NULL")
    suspend fun unboundProgress(): List<OfflineAudiobook>

    @Query("UPDATE offline_audiobooks SET remoteUrl = :newOrigin || substr(remoteUrl, length(:oldOrigin) + 1), coverUrl = CASE WHEN substr(coverUrl, 1, length(:oldOrigin) + 1) = :oldOrigin || '/' THEN :newOrigin || substr(coverUrl, length(:oldOrigin) + 1) ELSE coverUrl END WHERE substr(remoteUrl, 1, length(:oldOrigin) + 1) = :oldOrigin || '/'")
    suspend fun reconnectOrigin(oldOrigin: String, newOrigin: String)

    @Query("SELECT * FROM offline_audiobooks ORDER BY updatedAt DESC")
    fun observeAll(): Flow<List<OfflineAudiobook>>

    @Query("SELECT * FROM offline_audiobooks WHERE id = :id LIMIT 1")
    override suspend fun get(id: String): OfflineAudiobook?

    @Upsert
    override suspend fun upsert(book: OfflineAudiobook)

    @Transaction
    override suspend fun saveDownload(book: OfflineAudiobook) {
        val current = get(book.id)
        val preserve = current != null && (current.progressDirty || current.progressScope == null || current.progressSequence > book.progressSequence)
        upsert(if (!preserve) book else book.copy(
            positionSeconds = current!!.positionSeconds, completed = current.completed,
            progressScope = current.progressScope, progressRevision = current.progressRevision,
            progressSession = current.progressSession, progressSequence = current.progressSequence,
            progressDirty = current.progressDirty))
    }


    @Query("UPDATE offline_audiobooks SET state = :state, bytesDownloaded = :bytesDownloaded, etag = :etag, error = :error, updatedAt = :updatedAt, integrityVerifiedAt = :integrityVerifiedAt WHERE id = :id")
    suspend fun updateDownload(
        id: String,
        state: String,
        bytesDownloaded: Long,
        etag: String?,
        error: String,
        updatedAt: Long,
        integrityVerifiedAt: Long?,
    ): Int

    @Query("UPDATE offline_audiobooks SET positionSeconds = :position, completed = :completed, updatedAt = :updatedAt, progressSequence = progressSequence + 1, progressDirty = 1 WHERE id = :id")
    suspend fun updatePosition(id: String, position: Double, completed: Boolean, updatedAt: Long)

    @Query("DELETE FROM offline_audiobooks WHERE id = :id")
    suspend fun delete(id: String)

    @Query("SELECT id FROM offline_audiobooks WHERE state IN ('queued','downloading') ORDER BY updatedAt, id")
    suspend fun recoverableIds(): List<String>

    @Query("""SELECT id FROM offline_audiobooks
        WHERE state='ready' AND contentSha256 IS NOT NULL
          AND (integrityVerifiedAt IS NULL OR integrityVerifiedAt<=:cutoff)
        ORDER BY COALESCE(integrityVerifiedAt,0), id LIMIT :limit""")
    suspend fun dueIntegrityIds(cutoff: Long, limit: Int): List<String>
}

@Database(entities = [OfflineAudiobook::class], version = 3, exportSchema = true)
abstract class OfflineAudiobookDatabase : RoomDatabase() {
    abstract fun dao(): OfflineAudiobookDao

    companion object {
        private val instances = mutableMapOf<String, OfflineAudiobookDatabase>()
        fun get(context: Context): OfflineAudiobookDatabase = synchronized(this) {
            val scope = com.davidpi.backup.net.DavidPiOrigin.sessionScope
            instances.getOrPut(scope) { Room.databaseBuilder(
                context.applicationContext, OfflineAudiobookDatabase::class.java, com.davidpi.backup.security.HouseholdStorage.databaseName(context, "offline_audiobooks")
            ).addMigrations(MIGRATION_1_2, MIGRATION_2_3).build() }
        }

        val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL("ALTER TABLE offline_audiobooks ADD COLUMN progressScope TEXT")
                database.execSQL("ALTER TABLE offline_audiobooks ADD COLUMN progressRevision INTEGER NOT NULL DEFAULT 0")
                database.execSQL("ALTER TABLE offline_audiobooks ADD COLUMN progressSession TEXT NOT NULL DEFAULT ''")
                database.execSQL("ALTER TABLE offline_audiobooks ADD COLUMN progressSequence INTEGER NOT NULL DEFAULT 0")
                database.execSQL("ALTER TABLE offline_audiobooks ADD COLUMN progressDirty INTEGER NOT NULL DEFAULT 0")
            }
        }

        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(database: SupportSQLiteDatabase) {
                // Existing app-private copies remain playable. Their expected
                // digest is learned the next time the user saves the book from
                // the authenticated shelf; no row or local audio is discarded.
                database.execSQL(
                    "ALTER TABLE offline_audiobooks ADD COLUMN contentSha256 TEXT"
                )
                database.execSQL(
                    "ALTER TABLE offline_audiobooks ADD COLUMN integrityVerifiedAt INTEGER"
                )
            }
        }
    }
}

object OfflineDownloadScheduler {
    private const val RECOVERY_WORK = "offline-audiobook-queue-recovery-v1"
    private const val ENQUEUE_TIMEOUT_SECONDS = 20L

    fun recover(context: Context) {
        val request = OneTimeWorkRequestBuilder<OfflineQueueRecoveryWorker>().build()
        WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
            RECOVERY_WORK,
            ExistingWorkPolicy.KEEP,
            request,
        )
    }

    suspend fun enqueueAndConfirm(context: Context, id: String) = withContext(Dispatchers.IO) {
        val request = OneTimeWorkRequestBuilder<OfflineDownloadWorker>()
            .setInputData(Data.Builder().putString(OfflineDownloadWorker.KEY_BOOK_ID, id).build())
            .setConstraints(
                Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
            )
            .addTag(OfflineDownloadWorker.tag(id))
            .build()
        WorkManager.getInstance(context.applicationContext).enqueueUniqueWork(
            OfflineDownloadWorker.workName(id),
            ExistingWorkPolicy.KEEP,
            request,
        ).result.get(ENQUEUE_TIMEOUT_SECONDS, TimeUnit.SECONDS)
    }
}

class OfflineQueueRecoveryWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val dao = OfflineAudiobookDatabase.get(applicationContext).dao()
        val candidates = OfflineDownloadPolicy.recoveryCandidates(
            dao.recoverableIds(),
            dao.dueIntegrityIds(
                System.currentTimeMillis() - OfflineDownloadPolicy.INTEGRITY_INTERVAL_MILLIS,
                OfflineDownloadPolicy.MAX_INTEGRITY_CHECKS_PER_RECOVERY,
            ),
        )
        return try {
            for (id in candidates) {
                OfflineDownloadScheduler.enqueueAndConfirm(applicationContext, id)
            }
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }
}

object OfflineAudiobookFiles {
    fun directory(context: Context): File =
        com.davidpi.backup.security.HouseholdStorage.offlineDirectory(context).also { it.mkdirs() }

    fun final(context: Context, id: String): File = File(directory(context), "$id.audio")
    fun partial(context: Context, id: String): File = File(directory(context), "$id.part")
}

object OfflineAudiobookFileLocks {
    private val locks = ConcurrentHashMap<String, Mutex>()

    suspend fun <T> withLock(id: String, action: suspend () -> T): T =
        locks.computeIfAbsent(id) { Mutex() }.withLock { action() }
}

object OfflineDownloadPolicy {
    private val idPattern = Regex("^[0-9a-f]{32}$")
    private val contentRangePattern = Regex("^bytes ([0-9]+)-([0-9]+)/([0-9]+)$")
    const val MAX_BOOK_BYTES = 4L * 1024 * 1024 * 1024
    const val FREE_SPACE_RESERVE = 512L * 1024 * 1024
    const val INTEGRITY_INTERVAL_MILLIS = 7L * 24 * 60 * 60 * 1000
    // A rolling pair caps one portal/app entry at at most 8 GiB of local reads.
    // Oldest-first selection advances on later entries after each successful check.
    const val MAX_INTEGRITY_CHECKS_PER_RECOVERY = 2
    private val sha256Pattern = Regex("^[0-9a-f]{64}$")

    fun validBookId(id: String): Boolean = idPattern.matches(id)

    fun validSha256(value: String?): Boolean = value != null && sha256Pattern.matches(value)

    fun digestMatches(expected: String?, observed: String): Boolean =
        validSha256(expected) && validSha256(observed) && expected == observed

    fun recoveryCandidates(recoverable: List<String>, dueIntegrity: List<String>): List<String> =
        (recoverable + dueIntegrity.take(MAX_INTEGRITY_CHECKS_PER_RECOVERY)).distinct()

    fun localCopyAction(
        expectedBytes: Long,
        actualBytes: Long,
        expectedSha256: String?,
        observedSha256: String?,
    ): LocalCopyAction = when {
        actualBytes != expectedBytes -> LocalCopyAction.REPAIR
        expectedSha256 == null -> LocalCopyAction.ACCEPT_LEGACY
        observedSha256 != null && digestMatches(expectedSha256, observedSha256) ->
            LocalCopyAction.ACCEPT_VERIFIED
        else -> LocalCopyAction.REPAIR
    }

    fun responseMode(status: Int, existingBytes: Long): ResponseMode = when {
        status == 206 && existingBytes > 0 -> ResponseMode.APPEND
        status == 200 || (status == 206 && existingBytes == 0L) -> ResponseMode.REPLACE
        else -> ResponseMode.REJECT
    }

    fun strongEtag(value: String?): String? {
        val candidate = value?.trim() ?: return null
        if (candidate.startsWith("W/", ignoreCase = true) || candidate.length < 2) return null
        if (candidate.first() != '"' || candidate.last() != '"') return null
        val opaque = candidate.substring(1, candidate.lastIndex)
        if (opaque.any { it == '"' || it.code < 0x21 || it.code == 0x7f }) return null
        return candidate
    }

    fun resumableBytes(existingBytes: Long, expectedTotal: Long, storedEtag: String?): Long =
        if (existingBytes in 1 until expectedTotal && strongEtag(storedEtag) != null) {
            existingBytes
        } else {
            0L
        }

    fun responseContract(
        status: Int,
        existingBytes: Long,
        expectedTotal: Long,
        storedEtag: String?,
        responseEtag: String?,
        contentRange: String?,
        contentLength: Long,
    ): ResponseContract? {
        if (expectedTotal !in 1..MAX_BOOK_BYTES || existingBytes !in 0 until expectedTotal) {
            return null
        }
        val mode = responseMode(status, existingBytes)
        if (mode == ResponseMode.REJECT || contentLength < -1L) return null
        val responseValidator = strongEtag(responseEtag)
        if (mode == ResponseMode.REPLACE && status == 200) {
            if (contentRange != null || (contentLength >= 0L && contentLength != expectedTotal)) {
                return null
            }
            return ResponseContract(mode, expectedTotal, responseValidator)
        }

        val match = contentRange?.let(contentRangePattern::matchEntire) ?: return null
        val start = match.groupValues[1].toLongOrNull() ?: return null
        val end = match.groupValues[2].toLongOrNull() ?: return null
        val total = match.groupValues[3].toLongOrNull() ?: return null
        if (start != existingBytes || end < start || end >= total || total != expectedTotal) {
            return null
        }
        val segmentBytes = end - start + 1L
        if (contentLength >= 0L && contentLength != segmentBytes) return null
        if (mode == ResponseMode.APPEND) {
            val storedValidator = strongEtag(storedEtag) ?: return null
            if (responseValidator != storedValidator) return null
        }
        return ResponseContract(mode, segmentBytes, responseValidator)
    }

    fun checkedSegmentBytes(
        copiedBytes: Long,
        nextCount: Int,
        expectedSegmentBytes: Long,
        existingBytes: Long,
        expectedTotal: Long,
    ): Long {
        if (nextCount <= 0 || copiedBytes < 0L || expectedSegmentBytes < 0L) {
            throw DownloadIntegrityException("Download stream made no progress")
        }
        val updated = try {
            Math.addExact(copiedBytes, nextCount.toLong())
        } catch (_: ArithmeticException) {
            throw DownloadIntegrityException("Download response exceeded its declared size")
        }
        if (updated > expectedSegmentBytes || existingBytes > expectedTotal - updated) {
            throw DownloadIntegrityException("Download response exceeded its declared size")
        }
        return updated
    }

    data class ResponseContract(
        val mode: ResponseMode,
        val expectedSegmentBytes: Long,
        val responseEtag: String?,
    )

    enum class ResponseMode { APPEND, REPLACE, REJECT }
    enum class LocalCopyAction { ACCEPT_LEGACY, ACCEPT_VERIFIED, REPAIR }
}

class DownloadIntegrityException(message: String) : Exception(message)
