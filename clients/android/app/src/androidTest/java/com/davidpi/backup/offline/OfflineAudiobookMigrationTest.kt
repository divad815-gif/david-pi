package com.davidpi.backup.offline

import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class OfflineAudiobookMigrationTest {
    @Test
    fun migrationPreservesReadyRowProgressAndLocalAudio() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val databaseName = "offline-audiobook-migration-${System.nanoTime()}.db"
        val databasePath = context.getDatabasePath(databaseName)
        val bookId = UUID.randomUUID().toString().replace("-", "")
        val audio = byteArrayOf(1, 3, 3, 7, 9, 2)
        val localFile = OfflineAudiobookFiles.final(context, bookId)
        localFile.parentFile?.mkdirs()
        localFile.writeBytes(audio)
        databasePath.parentFile?.mkdirs()

        try {
            SQLiteDatabase.openOrCreateDatabase(databasePath, null).use { legacy ->
                legacy.execSQL(
                    """CREATE TABLE offline_audiobooks (
                        id TEXT NOT NULL PRIMARY KEY, title TEXT NOT NULL,
                        author TEXT NOT NULL, series TEXT NOT NULL,
                        durationSeconds REAL NOT NULL, byteSize INTEGER NOT NULL,
                        contentType TEXT NOT NULL, remoteUrl TEXT NOT NULL,
                        coverUrl TEXT NOT NULL, localFileName TEXT NOT NULL,
                        state TEXT NOT NULL, bytesDownloaded INTEGER NOT NULL,
                        etag TEXT, positionSeconds REAL NOT NULL,
                        completed INTEGER NOT NULL, updatedAt INTEGER NOT NULL,
                        error TEXT NOT NULL)
                    """.trimIndent()
                )
                legacy.execSQL(
                    """INSERT INTO offline_audiobooks VALUES (
                        ?, 'The Book', 'The Author', 'Series', 3600.0, ?,
                        'audio/mpeg', 'https://david-pi.example/audiobooks/$bookId/download',
                        'https://david-pi.example/audiobooks/$bookId/cover', '$bookId.audio',
                        'ready', ?, '"v1"', 913.5, 0, 123456, '')
                    """.trimIndent(),
                    arrayOf(bookId, audio.size, audio.size),
                )
                legacy.version = 1
            }

            val database = Room.databaseBuilder(
                context,
                OfflineAudiobookDatabase::class.java,
                databaseName,
            ).addMigrations(OfflineAudiobookDatabase.MIGRATION_1_2, OfflineAudiobookDatabase.MIGRATION_2_3).build()
            try {
                val migrated = database.dao().get(bookId)
                assertEquals("ready", migrated?.state)
                assertEquals(audio.size.toLong(), migrated?.bytesDownloaded)
                assertEquals(913.5, migrated?.positionSeconds ?: -1.0, 0.0)
                assertEquals("\"v1\"", migrated?.etag)
                assertNull(migrated?.progressScope)
                assertEquals(false, migrated?.progressDirty)
                assertNull(migrated?.contentSha256)
                assertNull(migrated?.integrityVerifiedAt)
                assertArrayEquals(audio, localFile.readBytes())
            } finally {
                database.close()
            }
        } finally {
            context.deleteDatabase(databaseName)
            localFile.delete()
        }
    }
}
