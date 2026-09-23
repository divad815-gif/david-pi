package com.davidpi.backup.offline

import android.content.Context
import androidx.room.withTransaction
import com.davidpi.backup.security.CredentialStore
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** Acknowledging an older upload never discards a newer local listening event. */
object OfflineProgressPolicy {
    fun acknowledge(book: OfflineAudiobook, session: String, sequence: Long, base: Long, revision: Long): OfflineAudiobook {
        if (book.progressSession != session || sequence > book.progressSequence || sequence < 1 ||
            base != book.progressRevision || revision != base + 1) return book
        return book.copy(progressRevision = revision, progressDirty = book.progressSequence != sequence)
    }
}

class OfflineProgressStore(context: Context) {
    private val database = OfflineAudiobookDatabase.get(context)
    private val dao = database.dao()
    private val credentials = CredentialStore(context)
    private val householdScope = credentials.scope

    suspend fun request(payload: JSONObject): JSONObject = database.withTransaction {
        require(householdScope != "unpaired" && householdScope == com.davidpi.backup.net.DavidPiOrigin.sessionScope)
        val owner = requireNotNull(credentials.memberId).trim().lowercase(java.util.Locale.ROOT)
        val expected = java.security.MessageDigest.getInstance("SHA-256")
            .digest("david-pi:audiobook-progress:v3\u0000$owner".toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }.take(32)
        val scope = payload.optString("scope")
        require(scope == expected) { "Listening identity changed. Reopen Audiobooks." }
        when (payload.optString("action")) {
            "pending" -> JSONObject().put("unbound_count", dao.unboundProgress().size)
                .put("entries", JSONArray().apply {
                    dao.pendingProgress(scope).forEach { book -> put(JSONObject()
                        .put("book_id", book.id).put("scope", scope)
                        .put("position", book.positionSeconds).put("completed", book.completed)
                        .put("base_revision", book.progressRevision)
                        .put("session_id", book.progressSession).put("sequence", book.progressSequence)) }
                })
            "ack" -> {
                val book = requireNotNull(dao.get(payload.getString("book_id")))
                require(book.progressScope == scope)
                dao.upsert(OfflineProgressPolicy.acknowledge(book, payload.getString("session_id"),
                    payload.getLong("sequence"), payload.getLong("base_revision"), payload.getLong("progress_revision")))
                JSONObject().put("ok", true)
            }
            "bind_legacy" -> {
                dao.unboundProgress().forEach { book -> dao.upsert(book.copy(
                    progressScope = scope, progressRevision = -1,
                    progressSession = UUID.randomUUID().toString().replace("-", ""),
                    progressSequence = 1, progressDirty = true)) }
                JSONObject().put("ok", true)
            }
            "resolve" -> {
                val book = requireNotNull(dao.get(payload.getString("book_id")))
                require(book.progressScope == scope && book.progressSequence == payload.getLong("sequence") &&
                    book.progressSession == payload.getString("session_id")) { "Playback advanced. Pause and refresh the listening choices." }
                val keep = payload.getBoolean("keep_candidate")
                val revision = payload.getLong("progress_revision")
                require(revision >= 0)
                val position = if (keep) book.positionSeconds else payload.getDouble("position_seconds")
                require(position.isFinite() && position >= 0)
                dao.upsert(book.copy(positionSeconds = position,
                    completed = if (keep) book.completed else payload.optBoolean("completed"),
                    progressRevision = revision, progressSession = UUID.randomUUID().toString().replace("-", ""),
                    progressSequence = if (keep) 1 else 0, progressDirty = keep))
                JSONObject().put("ok", true)
            }
            else -> throw IllegalArgumentException("Unsupported listening progress action.")
        }
    }
}
