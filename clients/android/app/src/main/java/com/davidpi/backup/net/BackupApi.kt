package com.davidpi.backup.net

import android.content.ContentResolver
import android.net.Uri
import com.davidpi.backup.data.BackupDao
import com.davidpi.backup.data.QueueSourceChangePolicy
import com.davidpi.backup.data.QueueIntegrityRevalidationPolicy
import com.davidpi.backup.data.QueuedMedia
import com.davidpi.backup.media.MediaPolicy
import com.davidpi.backup.media.ReconciliationEnvelope
import com.davidpi.backup.security.CredentialStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID
import java.util.concurrent.TimeUnit

class BackupApi(
    private val resolver: ContentResolver,
    private val credentials: CredentialStore,
    private val dao: BackupDao,
    client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .writeTimeout(90, TimeUnit.SECONDS)
        .build()
) {
    private val pairingClient = DavidPiHttp.noRedirects(client)
    private val sessionScope = DavidPiOrigin.sessionScope
    private val client by lazy { DavidPiHttp.forSession(client) }

    companion object {
        const val CHUNK_SIZE = 8 * 1024 * 1024
        const val DEFAULT_RATE_BYTES = 2 * 1024 * 1024L
        internal val JSON = "application/json".toMediaType()
        internal val CHUNK = "application/offset+octet-stream".toMediaType()
    }

    suspend fun restoreLegacyIdentity(context: android.content.Context) {
        check(credentials.instanceId == null)
        val server = requireNotNull(credentials.serverUrl)
        val token = requireNotNull(credentials.credential())
        val identity = withContext(Dispatchers.IO) {
            pairingClient.newCall(Request.Builder().url("$server/api/v1/device-backup/status")
                .header("Authorization", "Bearer $token").get().build()).execute().use { response ->
                if (!response.isSuccessful) throw IOException("Connect to your updated home server to verify the existing offline library.")
                JSONObject(response.body?.string() ?: throw IOException("Empty identity response."))
            }
        }
        val instance = identity.getString("installation_id")
        val member = identity.getString("member_id")
        require(identity.getString("server_url") == server)
        require(com.davidpi.backup.security.HouseholdScope.validIdentity(instance, member))
        com.davidpi.backup.security.HouseholdStorage.adoptLegacy(context, instance, member, server)
        credentials.bindIdentity(instance, member, identity.optString("display_name", "David-Pi"))
        credentials.updateMetadata(identity)
    }

    suspend fun pair(serverUrl: String, token: String, deviceName: String): JSONObject {
        val canonicalServer = requireNotNull(
            DavidPiOrigin.canonicalPairingOrigin(serverUrl)
        ) { "Use the canonical private HTTPS David-Pi address." }
        if (credentials.credential() != null) {
            throw ApiException(
                409,
                message = "This phone is already paired. Remove its existing access before pairing again."
            )
        }
        val body = JSONObject()
            .put("pairing_token", token)
            .put("device_name", deviceName)
            .toString().toRequestBody(JSON)
        val request = Request.Builder()
            .url("$canonicalServer/api/v1/device-backup/pair")
            .post(body).build()
        return withContext(Dispatchers.IO) {
            pairingClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw ApiException(response.code, message = "Pairing failed. Check the code and try again.")
                JSONObject(response.body?.string() ?: throw IOException("Empty pairing response."))
            }
        }.also {
            val instance = it.getString("installation_id")
            val member = it.getString("member_id")
            require(com.davidpi.backup.security.HouseholdScope.validIdentity(instance, member)) {
                "This server cannot verify its installation and household identity. Update it before pairing."
            }
            require(it.getString("server_url") == canonicalServer) { "The server address did not match the approved address." }
            credentials.serverUrl = canonicalServer
            credentials.deviceId = it.getString("device_id")
            credentials.deviceName = deviceName
            credentials.ownerName = it.optString("owner_name")
            credentials.saveCredential(it.getString("device_credential"))
            credentials.bindIdentity(instance, member, it.optString("display_name", "David-Pi"))
            credentials.updateMetadata(it)
        }
    }

    suspend fun upload(item: QueuedMedia, rateBytesPerSecond: Long = DEFAULT_RATE_BYTES): QueuedMedia {
        val recovery = recoverChangedSource(item)
        var current = recovery.item ?: return item.copy(
            serverUploadId = null,
            acceptedOffset = 0,
            state = "source_removed",
        )
        if (recovery.continuation == SourceRecoveryContinuation.DONE) return current
        if (current.state == "permanent_error") return current
        var restarts = 0
        while (true) {
            try {
                return uploadOnce(current, rateBytesPerSecond)
            } catch (error: ApiException) {
                var persisted = dao.byId(current.queueId) ?: current
                if (
                    error.status == 409 &&
                    error.errorCode == UploadSessionConflictPolicy.ERROR_CODE
                ) {
                    val recoveryUploadId = error.recoveryUploadId ?: throw ApiException(
                        502,
                        "upload_session_conflict_invalid",
                        "David-Pi returned an invalid upload recovery response.",
                    )
                    persisted = UploadSessionConflictPolicy.cleanupCheckpoint(
                        persisted,
                        recoveryUploadId,
                    )
                    // Persist the authenticated server identity before DELETE.
                    // A process death at this boundary resumes through
                    // recoverChangedSource() and cannot lose the cleanup target.
                    dao.update(persisted)
                }
                val reason = UploadRecoveryPolicy.reason(error, persisted.serverUploadId != null)
                    ?: throw error
                if (!UploadRecoveryPolicy.mayRestart(restarts)) {
                    current = resetUploadSession(persisted, reason)
                    throw ApiException(
                        422,
                        UploadRecoveryPolicy.terminalErrorCode(reason),
                        "This local media item changed repeatedly while it was being backed up."
                    )
                }
                current = resetUploadSession(persisted, reason)
                restarts += 1
            }
        }
    }

    private suspend fun recoverChangedSource(item: QueuedMedia): SourceRecoveryOutcome {
        if (item.state !in setOf(
                QueueSourceChangePolicy.STATE,
                QueueSourceChangePolicy.REHASH_STATE,
            )
        ) return SourceRecoveryOutcome(item, SourceRecoveryContinuation.UPLOAD)
        var checkpoint = item
        if (item.state == QueueSourceChangePolicy.STATE) {
            item.serverUploadId?.let { abandonSession(it) }
            checkpoint = QueueSourceChangePolicy.checkpointAfterServerCleanup(item)
            // This durable boundary prevents a successful DELETE from being
            // repeated and lets unreadable local content rotate behind fresh
            // queue work after a process death or provider failure.
            dao.update(checkpoint)
        }
        if (checkpoint.errorCode == QueueSourceChangePolicy.REMOVE) {
            dao.delete(checkpoint)
            return SourceRecoveryOutcome(null, SourceRecoveryContinuation.DONE)
        }
        if (QueueIntegrityRevalidationPolicy.isCheckpoint(checkpoint)) {
            val source = sha256AndSize(Uri.parse(checkpoint.contentUri))
            val decision = IntegrityRecoveryPolicy.afterDigest(
                checkpoint,
                source.sha256,
                source.byteSize,
                System.currentTimeMillis(),
            )
            if (decision.continuation == SourceRecoveryContinuation.DONE) {
                dao.update(decision.item)
                dao.deleteState(
                    QueueIntegrityRevalidationPolicy.failureStateKey(checkpoint.queueId)
                )
                return SourceRecoveryOutcome(decision.item, decision.continuation)
            }
            val recovered = decision.item
            if (recovered.queueId == checkpoint.queueId) {
                dao.update(recovered)
            } else {
                dao.replaceAfterSourceChange(checkpoint.queueId, recovered)
            }
            dao.deleteState(
                QueueIntegrityRevalidationPolicy.failureStateKey(checkpoint.queueId)
            )
            return SourceRecoveryOutcome(recovered, decision.continuation)
        }
        if (checkpoint.errorCode == UploadRecoveryPolicy.REHASH_ERROR_CODE) {
            val source = sha256AndSize(Uri.parse(checkpoint.contentUri))
            val recovered = UploadRecoveryPolicy.itemAfterFreshDigest(
                checkpoint,
                source.sha256,
                source.byteSize,
            )
            if (recovered.queueId == checkpoint.queueId) {
                dao.update(recovered)
            } else {
                dao.replaceAfterSourceChange(checkpoint.queueId, recovered)
            }
            return SourceRecoveryOutcome(recovered, SourceRecoveryContinuation.UPLOAD)
        }
        if (checkpoint.errorCode != QueueSourceChangePolicy.REPLACE) {
            throw ApiException(
                422,
                "source_recovery_invalid",
                "This changed media item has invalid recovery state.",
            )
        }
        val source = sha256AndSize(Uri.parse(checkpoint.contentUri))
        val replacement = checkpoint.copy(
            queueId = MediaPolicy.queueId(
                checkpoint.mediaType,
                checkpoint.mediaStoreId,
                checkpoint.sourceSignature,
            ),
            byteSize = source.byteSize,
            sha256 = source.sha256,
            serverUploadId = null,
            acceptedOffset = 0,
            state = "queued",
            retryCount = 0,
            errorCode = null,
            updatedAt = System.currentTimeMillis(),
        )
        dao.replaceAfterSourceChange(checkpoint.queueId, replacement)
        return SourceRecoveryOutcome(replacement, SourceRecoveryContinuation.UPLOAD)
    }

    private suspend fun uploadOnce(
        item: QueuedMedia,
        rateBytesPerSecond: Long,
    ): QueuedMedia {
        val source = if (item.sha256 == null) {
            sha256AndSize(Uri.parse(item.contentUri))
        } else {
            SourceDigest(item.sha256, item.byteSize)
        }
        var current = item.copy(
            sha256 = source.sha256,
            byteSize = source.byteSize,
            state = "queued",
            updatedAt = System.currentTimeMillis(),
        )
        dao.update(current)
        val session = if (current.serverUploadId == null) createSession(current) else {
            JSONObject().put("upload_id", current.serverUploadId)
                .put("offset", serverOffset(current.serverUploadId))
        }
        if (session.optString("state") == "already_present") {
            val confirmed = sha256AndSize(Uri.parse(current.contentUri))
            if (!AlreadyPresentPolicy.matchesCurrentSource(
                    current.sha256,
                    current.byteSize,
                    confirmed.sha256,
                    confirmed.byteSize,
                )
            ) {
                throw ApiException(
                    422,
                    "local_source_changed",
                    "The local media item changed before deduplication completed.",
                )
            }
            current = current.copy(
                state = "primary_verified",
                acceptedOffset = current.byteSize,
                updatedAt = System.currentTimeMillis()
            )
            dao.update(current)
            return current
        }
        val uploadId = session.getString("upload_id")
        var offset = session.optLong("offset", current.acceptedOffset)
        current = current.copy(serverUploadId = uploadId, acceptedOffset = offset, state = "uploading")
        dao.update(current)
        while (offset < current.byteSize) {
            val length = minOf(CHUNK_SIZE.toLong(), current.byteSize - offset).toInt()
            val started = System.nanoTime()
            val body = ContentRangeRequestBody(resolver, Uri.parse(current.contentUri), offset, length)
            val request = authenticated(
                DavidPiOrigin.apiUrl("/api/v1/device-backup/uploads/$uploadId")
            ).header("Upload-Offset", offset.toString()).patch(body).build()
            var retryChunk = false
            client.newCall(request).execute().use { response ->
                if (response.code == 409) {
                    offset = response.header("Upload-Offset")?.toLongOrNull() ?: serverOffset(uploadId)
                    current = current.copy(acceptedOffset = offset)
                    dao.update(current)
                    retryChunk = true
                } else if (response.code == 429 || response.code == 503) {
                    delay((response.header("Retry-After")?.toLongOrNull() ?: 30) * 1000)
                    retryChunk = true
                } else {
                    if (!response.isSuccessful) {
                        val parsed = safeError(response)
                        throw ApiException(
                            response.code,
                            parsed.code,
                            parsed.message,
                            parsed.serverTimeMillis,
                            parsed.scanId,
                        )
                    }
                    offset = response.header("Upload-Offset")?.toLongOrNull() ?: offset + length
                }
            }
            if (retryChunk) continue
            current = current.copy(acceptedOffset = offset, updatedAt = System.currentTimeMillis())
            dao.update(current)
            val expectedMillis = (length * 1000L) / maxOf(rateBytesPerSecond, 1)
            val elapsedMillis = (System.nanoTime() - started) / 1_000_000
            if (elapsedMillis < expectedMillis) delay(expectedMillis - elapsedMillis)
        }
        // A resumed server session may already have every byte while the local
        // MediaStore item has since changed without a reliable metadata signal
        // (notably on older Android releases). Re-read the whole source before
        // every completion so zero-byte resumes cannot protect stale content.
        val completionSource = sha256AndSize(Uri.parse(current.contentUri))
        if (!AlreadyPresentPolicy.matchesCurrentSource(
                current.sha256,
                current.byteSize,
                completionSource.sha256,
                completionSource.byteSize,
            )
        ) {
            throw ApiException(
                422,
                "local_source_changed",
                "The local media item changed before upload completion.",
            )
        }
        val completed = authenticated(
            DavidPiOrigin.apiUrl("/api/v1/device-backup/uploads/$uploadId/complete")
        ).post(ByteArray(0).toRequestBody(null)).build()
        val result = executeJson(completed)
        current = current.copy(
            state = result.optString("secondary_verification_state", "primary_verified"),
            acceptedOffset = current.byteSize,
            updatedAt = System.currentTimeMillis()
        )
        dao.update(current)
        return current
    }

    private suspend fun resetUploadSession(
        item: QueuedMedia,
        reason: UploadRecoveryReason,
    ): QueuedMedia {
        if (reason == UploadRecoveryReason.SOURCE_MISMATCH) {
            item.serverUploadId?.let { abandonSession(it) }
        }
        val checkpoint = UploadRecoveryPolicy.checkpointForFreshDigest(item)
            .also { dao.update(it) }
        val source = sha256AndSize(Uri.parse(checkpoint.contentUri))
        val recovered = UploadRecoveryPolicy.itemAfterFreshDigest(
            checkpoint,
            source.sha256,
            source.byteSize,
        )
        if (recovered.queueId == checkpoint.queueId) {
            dao.update(recovered)
        } else {
            dao.replaceAfterSourceChange(checkpoint.queueId, recovered)
        }
        return recovered
    }

    suspend fun status(): JSONObject = executeJson(
        authenticated(DavidPiOrigin.apiUrl("/api/v1/device-backup/status")).get().build()
    ).also { response ->
        if (response.optString("installation_id") != credentials.instanceId ||
            response.optString("member_id") != credentials.memberId) {
            throw ApiException(401, message = "The server identity changed. Reconnect explicitly.")
        }
        credentials.updateMetadata(response)
    }

    suspend fun reconcile(envelope: ReconciliationEnvelope): JSONObject {
        val result = executeJson(
            authenticated(DavidPiOrigin.apiUrl("/api/v1/device-backup/reconcile"))
            .post(envelope.requestJson().toRequestBody(JSON))
            .build()
        )
        if (
            !result.optBoolean("ok") ||
            result.optString("scan_id") != envelope.scanId ||
            result.optInt("item_count", -1) != envelope.itemCount ||
            result.optString("state") !in setOf("accepted", "replayed")
        ) {
            throw IOException("The reconciliation response was ambiguous.")
        }
        return result
    }

    private suspend fun createSession(item: QueuedMedia): JSONObject {
        val payload = JSONObject()
            .put("client_item_id", item.queueId)
            .put("original_filename", item.displayName)
            .put("byte_size", item.byteSize)
            .put("sha256", item.sha256)
            .put("mime_type", item.mimeType)
            .put("capture_timestamp", item.captureTimestamp?.let { Instant.ofEpochMilli(it).toString() })
        return executeJson(
            authenticated(DavidPiOrigin.apiUrl("/api/v1/device-backup/uploads"))
                .post(payload.toString().toRequestBody(JSON)).build()
        )
    }

    private suspend fun serverOffset(uploadId: String): Long = withContext(Dispatchers.IO) {
        client.newCall(
            authenticated(DavidPiOrigin.apiUrl("/api/v1/device-backup/uploads/$uploadId")).head().build()
        ).execute().use { response ->
            if (response.code == 422) throw ApiException(
                422,
                response.header("Upload-Error-Code").orEmpty().ifBlank { "invalid_media" },
                "The phone item is not readable media."
            )
            if (!response.isSuccessful) throw ApiException(
                response.code, message = "Upload session is unavailable."
            )
            response.header("Upload-Offset")?.toLongOrNull() ?: 0
        }
    }

    private suspend fun abandonSession(uploadId: String) = withContext(Dispatchers.IO) {
        client.newCall(
            authenticated(DavidPiOrigin.apiUrl("/api/v1/device-backup/uploads/$uploadId"))
                .delete()
                .build()
        ).execute().use { response ->
            if (AbandonSessionPolicy.isComplete(response.code, "")) {
                return@use
            }
            val parsed = safeError(response)
            if (AbandonSessionPolicy.isComplete(response.code, parsed.code)) {
                // Only this explicit conflict proves the old upload can no
                // longer consume an active slot. A generic 409 (notably
                // upload_busy) remains retryable so local recovery cannot race
                // an in-flight append or completion lease.
                return@use
            }
            throw ApiException(response.code, parsed.code, parsed.message)
        }
    }

    private data class SourceDigest(val sha256: String, val byteSize: Long)

    private suspend fun sha256AndSize(uri: Uri): SourceDigest = withContext(Dispatchers.IO) {
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            var byteSize = 0L
            resolver.openInputStream(uri)?.use { input ->
                val buffer = ByteArray(1024 * 1024)
                while (true) {
                    val read = input.read(buffer)
                    if (read < 0) break
                    digest.update(buffer, 0, read)
                    byteSize += read
                }
            } ?: throw SourceUnreadableException()
            SourceDigest(
                digest.digest().joinToString("") { "%02x".format(it) },
                byteSize,
            )
        } catch (error: SourceUnreadableException) {
            throw error
        } catch (error: IOException) {
            throw SourceUnreadableException(error)
        } catch (error: SecurityException) {
            throw SourceUnreadableException(error)
        }
    }

    private fun authenticated(url: String): Request.Builder {
        if (sessionScope != DavidPiOrigin.sessionScope || sessionScope != credentials.scope) {
            throw ApiException(401, message = "The household connection changed. Reopen the app.")
        }
        val token = credentials.credential() ?: throw ApiException(
            401, message = "Pair this phone first."
        )
        return Request.Builder().url(url).header("Authorization", "Bearer $token")
            .header("User-Agent", "David-Pi-Backup/${com.davidpi.backup.BuildConfig.VERSION_NAME}")
    }

    private suspend fun executeJson(request: Request): JSONObject = withContext(Dispatchers.IO) {
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                val parsed = safeError(response)
                val recoveryUploadId = UploadSessionConflictPolicy.recoveryUploadId(
                    response.code,
                    parsed.code,
                    response.headers.values("Upload-Id"),
                )
                throw ApiException(
                    response.code,
                    parsed.code,
                    parsed.message,
                    parsed.serverTimeMillis,
                    parsed.scanId,
                    recoveryUploadId,
                )
            }
            parseSuccessfulJson(response.body?.string().orEmpty())
        }
    }

    private data class ParsedApiError(
        val code: String = "",
        val message: String = "",
        val serverTimeMillis: Long? = null,
        val scanId: String? = null,
    )

    private fun safeError(response: Response): ParsedApiError = runCatching {
        val error = JSONObject(response.body?.string().orEmpty()).optJSONObject("error")
        val rawServerTime = error?.opt("server_time_ms")
        ParsedApiError(
            code = error?.optString("code").orEmpty(),
            message = error?.optString("message").orEmpty(),
            serverTimeMillis = (rawServerTime as? Number)?.toLong()?.takeIf { it > 0 },
            scanId = error?.optString("scan_id").orEmpty().ifBlank { null },
        )
    }.getOrNull().let { parsed ->
        ParsedApiError(
            code = parsed?.code.orEmpty(),
            message = parsed?.message.orEmpty().ifBlank {
                "David-Pi returned ${response.code}."
            },
            serverTimeMillis = parsed?.serverTimeMillis,
            scanId = parsed?.scanId,
        )
    }
}

internal fun parseSuccessfulJson(
    body: String,
    parser: (String) -> JSONObject = { JSONObject(it) },
): JSONObject = try {
    parser(body.ifBlank { "{}" })
} catch (error: Exception) {
    // A 2xx response may have committed the receipt even if its representation
    // is malformed or truncated. Surface protocol ambiguity as IOException so
    // reconciliation retries the durable pending UUID instead of minting one.
    throw ProtocolAmbiguityException(
        "The successful David-Pi response was not valid JSON.", error
    )
}

class ProtocolAmbiguityException(message: String, cause: Throwable? = null) :
    IOException(message, cause)

class SourceUnreadableException(cause: Throwable? = null) :
    IOException("The local media item is no longer readable.", cause)

class ApiException(
    val status: Int,
    val errorCode: String = "",
    message: String,
    val serverTimeMillis: Long? = null,
    val scanId: String? = null,
    val recoveryUploadId: String? = null,
) : IOException(message)

internal enum class SourceRecoveryContinuation { DONE, UPLOAD }

internal data class SourceRecoveryOutcome(
    val item: QueuedMedia?,
    val continuation: SourceRecoveryContinuation,
)

internal data class IntegrityRecoveryDecision(
    val item: QueuedMedia,
    val continuation: SourceRecoveryContinuation,
)

internal object IntegrityRecoveryPolicy {
    fun afterDigest(
        checkpoint: QueuedMedia,
        observedSha256: String,
        observedSize: Long,
        nowMillis: Long,
    ): IntegrityRecoveryDecision {
        require(QueueIntegrityRevalidationPolicy.isCheckpoint(checkpoint))
        return if (QueueIntegrityRevalidationPolicy.unchanged(
                checkpoint,
                observedSha256,
                observedSize,
            )
        ) {
            // This is a local integrity observation, not an upload. Preserve
            // the exact authoritative server state/session and make the caller
            // return without issuing create, HEAD, PATCH, or complete.
            IntegrityRecoveryDecision(
                QueueIntegrityRevalidationPolicy.restoreUnchanged(checkpoint, nowMillis),
                SourceRecoveryContinuation.DONE,
            )
        } else {
            IntegrityRecoveryDecision(
                UploadRecoveryPolicy.itemAfterFreshDigest(
                    checkpoint,
                    observedSha256,
                    observedSize,
                ),
                SourceRecoveryContinuation.UPLOAD,
            )
        }
    }
}

object BackupFailurePolicy {
    private val permanentCodes = setOf(
        "invalid_media", "invalid_size", "invalid_hash", "source_unstable",
        "upload_session_unstable", "source_recovery_invalid",
    )

    fun isPermanent(error: Throwable): Boolean =
        error is ApiException && (
            (error.status == 422 && error.errorCode in permanentCodes) ||
                // The server's explicit upload limit uses 413. Only its typed
                // invalid_size response is a local terminal item; an arbitrary
                // 413 remains retryable/fail-closed rather than being skipped.
                (error.status == 413 && error.errorCode == "invalid_size")
            )
}

enum class UploadRecoveryReason { SOURCE_MISMATCH, SESSION_MISSING }

object UploadRecoveryPolicy {
    const val MAX_RESTARTS = 2
    const val REHASH_ERROR_CODE = "upload_restart_rehash"

    fun checkpointForFreshDigest(item: QueuedMedia): QueuedMedia = item.copy(
        // Clearing both fields at the same durable boundary is essential: a
        // crash must never let a replacement source complete through an
        // already-present response for the old digest.
        byteSize = 0,
        sha256 = null,
        serverUploadId = null,
        acceptedOffset = 0,
        state = QueueSourceChangePolicy.REHASH_STATE,
        errorCode = REHASH_ERROR_CODE,
        updatedAt = System.currentTimeMillis(),
    )

    fun itemAfterFreshDigest(
        checkpoint: QueuedMedia,
        sha256: String,
        byteSize: Long,
    ): QueuedMedia = checkpoint.copy(
        queueId = MediaPolicy.recoveryQueueId(
            checkpoint.mediaType,
            checkpoint.mediaStoreId,
            checkpoint.sourceSignature,
            sha256,
        ),
        byteSize = byteSize,
        sha256 = sha256,
        serverUploadId = null,
        acceptedOffset = 0,
        state = "queued",
        retryCount = 0,
        errorCode = null,
        updatedAt = System.currentTimeMillis(),
    )

    fun reason(error: ApiException, hasPersistedSession: Boolean): UploadRecoveryReason? = when {
        error.status == 422 && error.errorCode in setOf(
            "sha256_mismatch", "local_source_changed"
        ) ->
            UploadRecoveryReason.SOURCE_MISMATCH
        error.status == 409 &&
            error.errorCode == UploadSessionConflictPolicy.ERROR_CODE &&
            error.recoveryUploadId != null -> UploadRecoveryReason.SOURCE_MISMATCH
        hasPersistedSession && error.status == 404 -> UploadRecoveryReason.SESSION_MISSING
        else -> null
    }

    fun mayRestart(completedRestarts: Int): Boolean = completedRestarts < MAX_RESTARTS

    fun terminalErrorCode(reason: UploadRecoveryReason): String = when (reason) {
        UploadRecoveryReason.SOURCE_MISMATCH -> "source_unstable"
        UploadRecoveryReason.SESSION_MISSING -> "upload_session_unstable"
    }
}

internal object UploadSessionConflictPolicy {
    const val ERROR_CODE = "upload_session_conflict"

    fun recoveryUploadId(
        status: Int,
        errorCode: String,
        headerValues: List<String>,
    ): String? {
        if (status != 409 || errorCode != ERROR_CODE || headerValues.size != 1) return null
        val value = headerValues.single()
        val parsed = runCatching { UUID.fromString(value) }.getOrNull() ?: return null
        return value.takeIf { parsed.version() == 4 && parsed.toString() == value }
    }

    fun cleanupCheckpoint(item: QueuedMedia, uploadId: String): QueuedMedia {
        require(recoveryUploadId(409, ERROR_CODE, listOf(uploadId)) == uploadId)
        return item.copy(
            serverUploadId = uploadId,
            acceptedOffset = 0,
            state = QueueSourceChangePolicy.STATE,
            errorCode = UploadRecoveryPolicy.REHASH_ERROR_CODE,
            updatedAt = System.currentTimeMillis(),
        )
    }
}

internal object AlreadyPresentPolicy {
    fun matchesCurrentSource(
        expectedSha256: String?,
        expectedSize: Long,
        observedSha256: String,
        observedSize: Long,
    ): Boolean = expectedSha256 != null &&
        expectedSha256 == observedSha256 && expectedSize == observedSize
}

internal object AbandonSessionPolicy {
    fun isComplete(status: Int, errorCode: String): Boolean =
        status == 204 || status == 404 ||
            (status == 409 && errorCode == "upload_not_abandonable")
}

private class ContentRangeRequestBody(
    private val resolver: ContentResolver,
    private val uri: Uri,
    private val offset: Long,
    private val length: Int
) : RequestBody() {
    override fun contentType(): MediaType = BackupApi.CHUNK
    override fun contentLength(): Long = length.toLong()
    override fun writeTo(sink: BufferedSink) {
        writeSourceRange(
            openSource = { resolver.openInputStream(uri) },
            sink = sink,
            offset = offset,
            length = length,
        )
    }
}

internal fun writeSourceRange(
    openSource: () -> InputStream?,
    sink: BufferedSink,
    offset: Long,
    length: Int,
) {
    val input = try {
        openSource()
    } catch (error: IOException) {
        throw SourceUnreadableException(error)
    } catch (error: SecurityException) {
        throw SourceUnreadableException(error)
    } ?: throw SourceUnreadableException()

    var bodyFailure: Throwable? = null
    try {
        var skipped = 0L
        while (skipped < offset) {
            val amount = try {
                input.skip(offset - skipped)
            } catch (error: IOException) {
                throw SourceUnreadableException(error)
            } catch (error: SecurityException) {
                throw SourceUnreadableException(error)
            }
            if (amount <= 0) throw SourceUnreadableException(
                IOException("Could not resume the local file.")
            )
            skipped += amount
        }
        val buffer = ByteArray(256 * 1024)
        var remaining = length
        while (remaining > 0) {
            val read = try {
                input.read(buffer, 0, minOf(buffer.size, remaining))
            } catch (error: IOException) {
                throw SourceUnreadableException(error)
            } catch (error: SecurityException) {
                throw SourceUnreadableException(error)
            }
            if (read <= 0) throw SourceUnreadableException(
                IOException("The local file ended unexpectedly.")
            )
            // Deliberately outside the source-read catch: an IOException here
            // is a transport/sink failure and remains retryable, rather than
            // quarantining healthy local media.
            sink.write(buffer, 0, read)
            remaining -= read
        }
    } catch (error: Throwable) {
        bodyFailure = error
        throw error
    } finally {
        try {
            input.close()
        } catch (error: IOException) {
            if (bodyFailure == null) throw SourceUnreadableException(error)
        } catch (error: SecurityException) {
            if (bodyFailure == null) throw SourceUnreadableException(error)
        }
    }
}
