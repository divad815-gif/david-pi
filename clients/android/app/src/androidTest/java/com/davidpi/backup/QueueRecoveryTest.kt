package com.davidpi.backup

import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.davidpi.backup.data.BackupDatabase
import com.davidpi.backup.data.QueuedMedia
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class QueueRecoveryTest {
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
        assertEquals(64, recovered?.acceptedOffset)
        database.close()
    }
}
