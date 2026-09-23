package com.davidpi.backup

import androidx.room.Room
import android.database.sqlite.SQLiteDatabase
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.davidpi.backup.data.BackupDatabase
import com.davidpi.backup.data.QueueSourceChangePolicy
import com.davidpi.backup.data.IntegrityRevalidationBudget
import com.davidpi.backup.data.QueueIntegrityRevalidationPolicy
import com.davidpi.backup.data.QueuedMedia
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class QueueRecoveryTest {
    @Test fun dueTerminalRevalidationWaitsBehindFreshAndRotatesChangedContent() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val database = Room.inMemoryDatabaseBuilder(context, BackupDatabase::class.java).build()
        val now = System.currentTimeMillis()
        val terminal = QueuedMedia(
            queueId = "old-terminal-id", mediaStoreId = 7, mediaType = "image",
            contentUri = "content://media/7", displayName = "changed.jpg",
            mimeType = "image/jpeg", byteSize = 100, captureTimestamp = 1,
            bucketName = "Camera", sha256 = "a".repeat(64),
            state = "permanent_error", retryCount = 3,
            errorCode = "invalid_media",
            updatedAt = now - QueueIntegrityRevalidationPolicy.INTERVAL_MILLIS - 1,
            scanGeneration = "prior", sourceSignature = "coarse",
        )
        database.dao().enqueue(terminal)
        val revalidationBudget = IntegrityRevalidationBudget(null)
        database.dao().recordObservations(
            listOf(
                com.davidpi.backup.data.QueuedMediaObservation(
                    terminal.copy(
                        sha256 = null,
                        state = "discovered",
                        retryCount = 0,
                        errorCode = null,
                        updatedAt = now,
                        scanGeneration = "current",
                    ),
                    true,
                )
            ),
            revalidationBudget,
            now,
        )
        assertEquals(
            revalidationBudget.checkpointCursor(),
            database.dao().getState(IntegrityRevalidationBudget.STATE_KEY),
        )
        assertEquals(0, database.dao().countNonIntegrityPending())
        assertEquals("old-terminal-id", database.dao().nextIntegrityRevalidation()?.queueId)
        val fresh = terminal.copy(
            queueId = "fresh", mediaStoreId = 8, sha256 = null,
            state = "discovered", retryCount = 0, errorCode = null,
            updatedAt = now, sourceSignature = "fresh",
        )
        database.dao().enqueue(fresh)

        assertEquals(1, database.dao().countNonIntegrityPending())
        assertEquals("fresh", database.dao().nextPending()?.queueId)
        database.dao().update(
            fresh.copy(state = "retryable_error", retryCount = 4)
        )
        assertEquals(
            "fresh",
            database.dao().nextPending()?.queueId,
        )
        database.dao().update(fresh.copy(state = "primary_verified"))
        val scheduled = requireNotNull(database.dao().nextPending())
        assertEquals("old-terminal-id", scheduled.queueId)
        assertTrue(QueueIntegrityRevalidationPolicy.isCheckpoint(scheduled))

        val replacement = com.davidpi.backup.net.UploadRecoveryPolicy.itemAfterFreshDigest(
            scheduled,
            "b".repeat(64),
            100,
        )
        database.dao().replaceAfterSourceChange(scheduled.queueId, replacement)
        assertEquals(null, database.dao().byId("old-terminal-id"))
        assertEquals(replacement, database.dao().byId(replacement.queueId))
        assertEquals(replacement.queueId, database.dao().nextPending()?.queueId)
        database.close()
    }

    @Test fun quarantinedUnstableSourceDoesNotBlockLaterQueueItems() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val database = Room.inMemoryDatabaseBuilder(context, BackupDatabase::class.java).build()
        val oldest = QueuedMedia(
            queueId = "image:old", mediaStoreId = 1, mediaType = "image",
            contentUri = "content://media/old", displayName = "old.jpg",
            mimeType = "image/jpeg", byteSize = 100, captureTimestamp = 1,
            bucketName = "Camera", state = "permanent_error", retryCount = 3,
            errorCode = "source_unstable",
        )
        val later = QueuedMedia(
            queueId = "image:later", mediaStoreId = 2, mediaType = "image",
            contentUri = "content://media/later", displayName = "later.jpg",
            mimeType = "image/jpeg", byteSize = 200, captureTimestamp = 2,
            bucketName = "Camera", state = "discovered",
        )
        database.dao().enqueue(oldest)
        database.dao().enqueue(later)

        assertEquals("image:later", database.dao().nextPending()?.queueId)
        assertEquals("source_unstable", database.dao().byId("image:old")?.errorCode)
        database.close()
    }

    @Test fun oversizedTerminalItemCannotBlockFreshOrDailyIntegrityWork() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val database = Room.inMemoryDatabaseBuilder(context, BackupDatabase::class.java).build()
        val now = System.currentTimeMillis()
        val oversized = QueuedMedia(
            queueId = "oversized-video", mediaStoreId = 1, mediaType = "video",
            contentUri = "content://media/oversized", displayName = "oversized.mp4",
            mimeType = "video/mp4", byteSize = 2L * 1024 * 1024 * 1024 + 1,
            captureTimestamp = 1, bucketName = "Camera",
            sha256 = "a".repeat(64), state = "permanent_error",
            retryCount = 1, errorCode = "invalid_size", updatedAt = now,
            sourceSignature = "oversized",
        )
        val dueProtected = oversized.copy(
            queueId = "due-protected", mediaStoreId = 2, mediaType = "image",
            contentUri = "content://media/protected", displayName = "protected.jpg",
            mimeType = "image/jpeg", byteSize = 10, state = "fully_protected",
            retryCount = 0, errorCode = null,
            updatedAt = now - QueueIntegrityRevalidationPolicy.INTERVAL_MILLIS - 1,
            sourceSignature = "protected",
        )
        val fresh = oversized.copy(
            queueId = "fresh", mediaStoreId = 3, mediaType = "image",
            contentUri = "content://media/fresh", displayName = "fresh.jpg",
            mimeType = "image/jpeg", byteSize = 10, sha256 = null,
            state = "discovered", retryCount = 0, errorCode = null,
            sourceSignature = "fresh",
        )
        database.dao().enqueue(oversized)
        database.dao().enqueue(dueProtected)
        database.dao().recordObservations(
            listOf(
                com.davidpi.backup.data.QueuedMediaObservation(
                    dueProtected.copy(state = "discovered", sha256 = null, updatedAt = now),
                    true,
                )
            ),
            IntegrityRevalidationBudget(null),
            now,
        )
        database.dao().enqueue(fresh)

        assertEquals(1, database.dao().countNonIntegrityPending())
        assertEquals("fresh", database.dao().nextPending()?.queueId)
        database.dao().update(fresh.copy(state = "primary_verified"))
        assertEquals(0, database.dao().countNonIntegrityPending())
        assertEquals(
            "due-protected",
            database.dao().nextIntegrityRevalidation()?.queueId,
        )
        assertEquals("permanent_error", database.dao().byId("oversized-video")?.state)
        assertEquals("invalid_size", database.dao().byId("oversized-video")?.errorCode)
        database.close()
    }

    @Test fun changedSourceIdentityIsReplacedOnlyByExplicitRecoveryTransaction() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val database = Room.inMemoryDatabaseBuilder(context, BackupDatabase::class.java).build()
        val old = QueuedMedia(
            queueId = "old-id", mediaStoreId = 7, mediaType = "image",
            contentUri = "content://media/7", displayName = "changed.jpg",
            mimeType = "image/jpeg", byteSize = 100, captureTimestamp = 1,
            bucketName = "Camera", serverUploadId = "retired-server-session",
            acceptedOffset = 64, state = "source_changed",
            errorCode = "source_changed_replace", sourceSignature = "a".repeat(64),
        )
        database.dao().enqueue(old)
        val replacement = old.copy(
            queueId = "new-id", byteSize = 200, sha256 = "b".repeat(64),
            serverUploadId = null, acceptedOffset = 0, state = "queued",
            retryCount = 0, errorCode = null, sourceSignature = "c".repeat(64),
        )

        database.dao().replaceAfterSourceChange(old.queueId, replacement)

        assertEquals(null, database.dao().byId("old-id"))
        assertEquals(replacement, database.dao().byId("new-id"))
        database.close()
    }

    @Test fun completeScanPrioritizesUnseenActiveSessionCleanupAheadOfFreshUploads() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val database = Room.inMemoryDatabaseBuilder(context, BackupDatabase::class.java).build()
        val vanishedActive = QueuedMedia(
            queueId = "vanished-active", mediaStoreId = 8, mediaType = "image",
            contentUri = "content://media/8", displayName = "gone.jpg",
            mimeType = "image/jpeg", byteSize = 100, captureTimestamp = 1,
            bucketName = "Camera", serverUploadId = "orphan-server-session",
            state = "uploading", scanGeneration = "previous",
        )
        val vanishedLocalOnly = vanishedActive.copy(
            queueId = "vanished-local", mediaStoreId = 9,
            serverUploadId = null, state = "queued",
        )
        val currentFresh = vanishedActive.copy(
            queueId = "current-fresh", mediaStoreId = 10,
            serverUploadId = null, state = "discovered", scanGeneration = "current",
        )
        database.dao().enqueue(vanishedActive)
        database.dao().enqueue(vanishedLocalOnly)
        database.dao().enqueue(currentFresh)

        database.dao().retireUnseenAfterCompleteScan("current")

        val cleanup = database.dao().byId("vanished-active")
        assertEquals("source_changed", cleanup?.state)
        assertEquals("source_changed_remove", cleanup?.errorCode)
        assertEquals("orphan-server-session", cleanup?.serverUploadId)
        assertEquals(null, database.dao().byId("vanished-local"))
        assertEquals("vanished-active", database.dao().nextPending()?.queueId)
        val checkpoint = QueueSourceChangePolicy.checkpointAfterServerCleanup(
            requireNotNull(cleanup)
        )
        val afterFirstUnreadable = checkpoint.copy(
            state = QueueSourceChangePolicy.stateAfterTemporaryFailure(checkpoint.state),
            retryCount = checkpoint.retryCount + 1,
        )
        database.dao().update(afterFirstUnreadable)
        // Once authenticated cleanup is durably checkpointed, a provider that
        // stays unreadable cannot starve an unrelated fresh upload.
        assertEquals("source_rehash", database.dao().byId("vanished-active")?.state)
        assertEquals("current-fresh", database.dao().nextPending()?.queueId)
        database.close()
    }

    @Test fun interruptedOffsetAndUploadIdRemainDurable() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val database = Room.inMemoryDatabaseBuilder(context, BackupDatabase::class.java).build()
        val item = QueuedMedia(
            queueId = "image:1", mediaStoreId = 1, mediaType = "image",
            contentUri = "content://media/1", displayName = "one.jpg",
            mimeType = "image/jpeg", byteSize = 100, captureTimestamp = 1,
            bucketName = "Camera", serverUploadId = "session-1",
            acceptedOffset = 64, state = "uploading"
        )
        database.dao().enqueue(item)
        val recovered = database.dao().nextPending()
        assertEquals("session-1", recovered?.serverUploadId)
        assertEquals(64L, recovered?.acceptedOffset)
        database.close()
    }

    @Test fun migrationMarksExistingRowsSeenWithoutLosingRecoveryState(): Unit = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val name = "backup-migration-${System.nanoTime()}.db"
        val path = context.getDatabasePath(name)
        path.parentFile?.mkdirs()
        SQLiteDatabase.openOrCreateDatabase(path, null).use { legacy ->
            legacy.execSQL(
                """CREATE TABLE media_queue (
                    queueId TEXT NOT NULL PRIMARY KEY, mediaStoreId INTEGER NOT NULL,
                    mediaType TEXT NOT NULL, contentUri TEXT NOT NULL, displayName TEXT NOT NULL,
                    mimeType TEXT NOT NULL, byteSize INTEGER NOT NULL, captureTimestamp INTEGER,
                    bucketName TEXT, sha256 TEXT, serverUploadId TEXT, acceptedOffset INTEGER NOT NULL,
                    state TEXT NOT NULL, retryCount INTEGER NOT NULL, errorCode TEXT,
                    updatedAt INTEGER NOT NULL)"""
            )
            legacy.execSQL(
                "CREATE UNIQUE INDEX index_media_queue_mediaStoreId_mediaType " +
                    "ON media_queue(mediaStoreId,mediaType)"
            )
            legacy.execSQL("CREATE INDEX index_media_queue_state ON media_queue(state)")
            legacy.execSQL(
                "CREATE TABLE sync_state (`key` TEXT NOT NULL PRIMARY KEY, " +
                    "value TEXT NOT NULL, updatedAt INTEGER NOT NULL)"
            )
            legacy.execSQL(
                """INSERT INTO media_queue VALUES (
                    'legacy-id',7,'image','content://media/7','seven.jpg','image/jpeg',100,1,
                    'Camera','${"a".repeat(64)}','session-7',73,'uploading',5,'offline',9)"""
            )
            legacy.version = 1
        }
        val database = Room.databaseBuilder(context, BackupDatabase::class.java, name)
            .addMigrations(BackupDatabase.MIGRATION_1_2)
            .build()
        val migrated = database.dao().byId("legacy-id")
        assertEquals("legacy-existing", migrated?.scanGeneration)
        assertEquals("", migrated?.sourceSignature)
        assertEquals("a".repeat(64), migrated?.sha256)
        assertEquals("session-7", migrated?.serverUploadId)
        assertEquals(73L, migrated?.acceptedOffset)
        assertEquals("uploading", migrated?.state)
        assertEquals(5, migrated?.retryCount)
        assertEquals("offline", migrated?.errorCode)
        database.close()
        context.deleteDatabase(name)
    }
}
