package com.davidpi.backup.offline

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.davidpi.backup.security.CredentialStore
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class OfflineProgressOutboxTest {
    @Test fun durableOutboxKeepsNewerPlaybackAndRequiresExplicitConflictResolution(): Unit = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val credentials = CredentialStore(context)
        val member = "john@example.test"
        val listeningScope = java.security.MessageDigest.getInstance("SHA-256")
            .digest("david-pi:audiobook-progress:v3\u0000$member".toByteArray())
            .joinToString("") { "%02x".format(it) }.take(32)
        credentials.clear()
        credentials.serverUrl = "https://john-pi.tail123456.ts.net"
        credentials.saveCredential("instrumentation-fixture-only")
        credentials.bindIdentity(UUID.randomUUID().toString(), member, "John's home")
        val database = OfflineAudiobookDatabase.get(context)
        val dao = database.dao()
        val id = "d".repeat(32)
        try {
            dao.upsert(OfflineAudiobook(id, "Book", "Author", "", 120.0, 3,
                contentType = "audio/mpeg", remoteUrl = "", coverUrl = "", localFileName = "$id.audio",
                state = "ready", positionSeconds = 20.0, progressScope = listeningScope,
                progressRevision = 4, progressSession = "c".repeat(32), progressSequence = 1, progressDirty = true))
            val outbox = OfflineProgressStore(context)
            val pending = outbox.request(JSONObject().put("action", "pending").put("scope", listeningScope))
                .getJSONArray("entries").getJSONObject(0)
            assertEquals(20.0, pending.getDouble("position"), 0.0)
            dao.updatePosition(id, 30.0, false, System.currentTimeMillis())
            outbox.request(JSONObject(pending.toString()).put("action", "ack").put("progress_revision", 5))
            val newer = requireNotNull(dao.get(id))
            assertEquals(30.0, newer.positionSeconds, 0.0)
            assertEquals(5, newer.progressRevision)
            assertTrue(newer.progressDirty)
            val candidate = outbox.request(JSONObject().put("action", "pending").put("scope", listeningScope))
                .getJSONArray("entries").getJSONObject(0)
            outbox.request(candidate.put("action", "resolve").put("keep_candidate", false)
                .put("progress_revision", 7).put("position_seconds", 60.0).put("completed", false))
            val resolved = requireNotNull(dao.get(id))
            assertEquals(60.0, resolved.positionSeconds, 0.0)
            assertEquals(7, resolved.progressRevision)
            assertFalse(resolved.progressDirty)
            assertEquals(0, outbox.request(JSONObject().put("action", "pending").put("scope", listeningScope)).getJSONArray("entries").length())
        } finally {
            dao.delete(id)
            credentials.clear()
        }
    }
}
