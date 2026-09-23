package com.davidpi.backup.offline

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class OfflineAudiobookBridgeTest {
    @org.junit.Before fun approveTestOrigin() {
        com.davidpi.backup.net.DavidPiOrigin.approve("https://john-pi.tail123456.ts.net", "test-scope")
    }

    @Test
    fun bridgeRecoversAtEntryAndRepliesOnlyAfterDurableEnqueue() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val repository = MemoryRepository()
        val persistedBeforeEnqueue = CompletableDeferred<String>()
        val allowEnqueueConfirmation = CompletableDeferred<Unit>()
        val reply = CompletableDeferred<String>()
        var recoveryCalls = 0
        val bridge = OfflineAudiobookBridge(
            context = context,
            repository = repository,
            scope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
            recoverQueue = { recoveryCalls += 1 },
            verifyIdentity = {},
            enqueue = { _, id ->
                assertNotNull(repository.book)
                persistedBeforeEnqueue.complete(id)
                allowEnqueueConfirmation.await()
            },
        )
        val bookId = "0123456789abcdef0123456789abcdef"
        val payload = JSONObject()
            .put("book_id", bookId)
            .put("title", "The Book")
            .put("byte_size", 4096)
            .put("content_sha256", "a".repeat(64))
            .put("progress_scope", "b".repeat(32))
            .put("progress_revision", 0)
            .put(
                "download_url",
                "https://john-pi.tail123456.ts.net/api/audiobooks/$bookId/download",
            )
            .toString()

        try {
            assertEquals(1, recoveryCalls)
            bridge.saveAudiobook(payload) { reply.complete(it) }
            assertEquals(bookId, withTimeout(5_000) { persistedBeforeEnqueue.await() })
            assertEquals(OfflineDownloadWorker.STATE_QUEUED, repository.book?.state)
            assertFalse(reply.isCompleted)

            allowEnqueueConfirmation.complete(Unit)
            assertEquals("queued", withTimeout(5_000) { reply.await() })
        } finally {
            bridge.release()
        }
    }

    private class MemoryRepository : OfflineAudiobookRepository {
        @Volatile var book: OfflineAudiobook? = null

        override suspend fun get(id: String): OfflineAudiobook? = book?.takeIf { it.id == id }

        override suspend fun upsert(book: OfflineAudiobook) {
            this.book = book
        }
    }
}
