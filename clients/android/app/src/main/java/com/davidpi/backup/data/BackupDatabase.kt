package com.davidpi.backup.data

import android.content.Context
import androidx.room.*
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
    val updatedAt: Long = System.currentTimeMillis()
)

@Entity(tableName = "sync_state")
data class SyncState(
    @PrimaryKey val key: String,
    val value: String,
    val updatedAt: Long = System.currentTimeMillis()
)

@Dao
interface BackupDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun enqueue(item: QueuedMedia): Long

    @Query("SELECT * FROM media_queue WHERE state IN ('discovered','queued','uploading','retryable_error') ORDER BY captureTimestamp, queueId LIMIT 1")
    suspend fun nextPending(): QueuedMedia?

    @Query("SELECT * FROM media_queue WHERE queueId=:id")
    suspend fun byId(id: String): QueuedMedia?

    @Update
    suspend fun update(item: QueuedMedia)

    @Query("UPDATE media_queue SET state=:state, updatedAt=:now WHERE queueId=:id")
    suspend fun setState(id: String, state: String, now: Long = System.currentTimeMillis())

    @Query("SELECT state, COUNT(*) AS count FROM media_queue GROUP BY state")
    fun observeCounts(): Flow<List<StateCount>>

    @Query("SELECT COALESCE(SUM(byteSize),0) FROM media_queue WHERE state NOT IN ('primary_verified','secondary_pending','fully_protected','permanent_error')")
    fun observePendingBytes(): Flow<Long>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putState(state: SyncState)

    @Query("SELECT value FROM sync_state WHERE `key`=:key")
    suspend fun getState(key: String): String?
}

data class StateCount(val state: String, val count: Int)

object BackupQueuePolicy {
    private val completedStates = setOf(
        "primary_verified", "secondary_pending", "fully_protected", "permanent_error"
    )
    fun isPending(state: String): Boolean = state !in completedStates
}

@Database(entities = [QueuedMedia::class, SyncState::class], version = 1, exportSchema = true)
abstract class BackupDatabase : RoomDatabase() {
    abstract fun dao(): BackupDao

    companion object {
        @Volatile private var instance: BackupDatabase? = null
        fun get(context: Context): BackupDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(
                context.applicationContext, BackupDatabase::class.java, "david-pi-backup.db"
            ).build().also { instance = it }
        }
    }
}
