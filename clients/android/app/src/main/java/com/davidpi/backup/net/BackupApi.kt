package com.davidpi.backup.net

import android.content.ContentResolver
import android.net.Uri
import com.davidpi.backup.data.BackupDao
import com.davidpi.backup.data.QueuedMedia
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
import java.security.MessageDigest
import java.time.Instant
import java.util.concurrent.TimeUnit

class BackupApi(
    private val resolver: ContentResolver,
    private val credentials: CredentialStore,
    private val dao: BackupDao,
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .writeTimeout(90, TimeUnit.SECONDS)
        .build()
) {
    companion object {
        const val CHUNK_SIZE = 8 * 1024 * 1024
        const val DEFAULT_RATE_BYTES = 2 * 1024 * 1024L
        internal val JSON = "application/json".toMediaType()
        internal val CHUNK = "application/offset+octet-stream".toMediaType()
    }

    suspend fun pair(serverUrl: String, token: String, deviceName: String): JSONObject {
        require(serverUrl.startsWith("https://")) { "Use the private HTTPS David-Pi address." }
        val body = JSONObject()
            .put("pairing_token", token)
            .put("device_name", deviceName)
            .toString().toRequestBody(JSON)
        val request = Request.Builder()
            .url("${serverUrl.trimEnd('/')}/api/v1/device-backup/pair")
            .post(body).build()
        return executeJson(request).also {
            credentials.serverUrl = serverUrl
            credentials.deviceId = it.getString("device_id")
            credentials.deviceName = deviceName
            credentials.ownerName = it.optString("owner_name")
            credentials.saveCredential(it.getString("device_credential"))
        }
    }

    suspend fun upload(item: QueuedMedia, rateBytesPerSecond: Long = DEFAULT_RATE_BYTES): QueuedMedia {
        val sha = item.sha256 ?: sha256(Uri.parse(item.contentUri))
        var current = item.copy(sha256 = sha, state = "queued", updatedAt = System.currentTimeMillis())
        dao.update(current)
        val session = if (current.serverUploadId == null) createSession(current) else {
            JSONObject().put("upload_id", current.serverUploadId)
                .put("offset", serverOffset(current.serverUploadId))
        }
        if (session.optString("state") == "already_present") {
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
                "${baseUrl()}/api/v1/device-backup/uploads/$uploadId"
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
                        throw ApiException(response.code, parsed.first, parsed.second)
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
        val completed = authenticated(
            "${baseUrl()}/api/v1/device-backup/uploads/$uploadId/complete"
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

    suspend fun status(): JSONObject = executeJson(
        authenticated("${baseUrl()}/api/v1/device-backup/status").get().build()
    )

    suspend fun reconcile(visibleIds: List<String>) {
        val body = JSONObject().put("visible_client_item_ids", visibleIds).toString().toRequestBody(JSON)
        executeJson(authenticated("${baseUrl()}/api/v1/device-backup/reconcile").post(body).build())
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
            authenticated("${baseUrl()}/api/v1/device-backup/uploads")
                .post(payload.toString().toRequestBody(JSON)).build()
        )
    }

    private suspend fun serverOffset(uploadId: String): Long = withContext(Dispatchers.IO) {
        client.newCall(
            authenticated("${baseUrl()}/api/v1/device-backup/uploads/$uploadId").head().build()
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

    private suspend fun sha256(uri: Uri): String = withContext(Dispatchers.IO) {
        val digest = MessageDigest.getInstance("SHA-256")
        resolver.openInputStream(uri)?.use { input ->
            val buffer = ByteArray(1024 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
        } ?: throw IOException("The local media item is no longer readable.")
        digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun authenticated(url: String): Request.Builder {
        val token = credentials.credential() ?: throw ApiException(
            401, message = "Pair this phone first."
        )
        return Request.Builder().url(url).header("Authorization", "Bearer $token")
            .header("User-Agent", "David-Pi-Backup/${com.davidpi.backup.BuildConfig.VERSION_NAME}")
    }

    private fun baseUrl() = credentials.serverUrl ?: throw ApiException(
        401, message = "Pair this phone first."
    )

    private suspend fun executeJson(request: Request): JSONObject = withContext(Dispatchers.IO) {
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                val parsed = safeError(response)
                throw ApiException(response.code, parsed.first, parsed.second)
            }
            JSONObject(response.body?.string().orEmpty().ifBlank { "{}" })
        }
    }

    private fun safeError(response: Response): Pair<String, String> = runCatching {
        val error = JSONObject(response.body?.string().orEmpty()).optJSONObject("error")
        Pair(
            error?.optString("code").orEmpty(),
            error?.optString("message").orEmpty()
        )
    }.getOrNull().let { parsed ->
        Pair(
            parsed?.first.orEmpty(),
            parsed?.second.orEmpty().ifBlank { "David-Pi returned ${response.code}." }
        )
    }
}

class ApiException(
    val status: Int,
    val errorCode: String = "",
    message: String
) : IOException(message)

object BackupFailurePolicy {
    private val permanentCodes = setOf("invalid_media", "invalid_size", "invalid_hash")

    fun isPermanent(error: Throwable): Boolean =
        error is ApiException && error.status == 422 && error.errorCode in permanentCodes
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
        resolver.openInputStream(uri)?.use { input ->
            var skipped = 0L
            while (skipped < offset) {
                val amount = input.skip(offset - skipped)
                if (amount <= 0) throw IOException("Could not resume the local file.")
                skipped += amount
            }
            val buffer = ByteArray(256 * 1024)
            var remaining = length
            while (remaining > 0) {
                val read = input.read(buffer, 0, minOf(buffer.size, remaining))
                if (read < 0) throw IOException("The local file ended unexpectedly.")
                sink.write(buffer, 0, read)
                remaining -= read
            }
        } ?: throw IOException("The local media item is unavailable.")
    }
}
