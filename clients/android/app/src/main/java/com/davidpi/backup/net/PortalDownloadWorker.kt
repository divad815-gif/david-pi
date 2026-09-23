package com.davidpi.backup.net

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.TimeUnit

/**
 * Exact-origin portal downloader. It deliberately does not import WebView
 * cookies and never follows redirects, so an API response cannot replay a
 * browser credential or move a private response to another origin.
 */
class PortalDownloadWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {
    private val client by lazy { DavidPiHttp.forSession(
        OkHttpClient.Builder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(5, TimeUnit.MINUTES)
            .build()
    ) }

    override suspend fun doWork(): Result {
        val url = DavidPiOrigin.canonicalPortalDownloadUrl(
            inputData.getString(KEY_URL).orEmpty()
        ) ?: return Result.failure()
        val requestedName = safeName(inputData.getString(KEY_NAME).orEmpty())
        val mimeType = inputData.getString(KEY_MIME).orEmpty().take(160)
            .ifBlank { "application/octet-stream" }
        val request = Request.Builder()
            .url(url)
            .header(
                "User-Agent",
                inputData.getString(KEY_USER_AGENT).orEmpty().take(300)
                    .ifBlank { "David-Pi/${com.davidpi.backup.BuildConfig.VERSION_NAME}" }
            )
            .get()
            .build()

        return try {
            PortalIdentity.verify(applicationContext)
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return Result.failure()
                val body = response.body ?: return Result.failure()
                if (!PortalDownloadPolicy.declaredLengthAllowed(body.contentLength())) {
                    return Result.failure()
                }
                if (Build.VERSION.SDK_INT >= 29) {
                    val values = ContentValues().apply {
                        put(MediaStore.Downloads.DISPLAY_NAME, requestedName)
                        put(MediaStore.Downloads.MIME_TYPE, mimeType)
                        put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                        put(MediaStore.Downloads.IS_PENDING, 1)
                    }
                    val resolver = applicationContext.contentResolver
                    val destination = resolver.insert(
                        MediaStore.Downloads.EXTERNAL_CONTENT_URI, values
                    ) ?: return Result.failure()
                    try {
                        val outputStream = resolver.openOutputStream(destination, "w")
                            ?: throw IOException("Download destination is unavailable.")
                        outputStream.use { output ->
                            body.byteStream().use { input ->
                                PortalDownloadPolicy.copyBounded(input, output)
                            }
                        }
                        resolver.update(
                            destination,
                            ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) },
                            null,
                            null,
                        )
                    } catch (error: Exception) {
                        resolver.delete(destination, null, null)
                        throw error
                    }
                } else {
                    val directory = applicationContext.getExternalFilesDir(
                        Environment.DIRECTORY_DOWNLOADS
                    ) ?: return Result.failure()
                    directory.mkdirs()
                    val destination = uniqueFile(directory, requestedName)
                    try {
                        FileOutputStream(destination).use { output ->
                            body.byteStream().use { input ->
                                PortalDownloadPolicy.copyBounded(input, output)
                            }
                            output.fd.sync()
                        }
                    } catch (error: Exception) {
                        destination.delete()
                        throw error
                    }
                }
            }
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        private const val KEY_URL = "url"
        private const val KEY_NAME = "name"
        private const val KEY_MIME = "mime"
        private const val KEY_USER_AGENT = "user_agent"
        private val unsafeName = Regex("[^A-Za-z0-9._() -]+")

        fun enqueue(
            context: Context,
            url: String,
            name: String,
            mimeType: String,
            userAgent: String,
        ): Boolean {
            val canonical = DavidPiOrigin.canonicalPortalDownloadUrl(url) ?: return false
            val request = OneTimeWorkRequestBuilder<PortalDownloadWorker>()
                .setInputData(
                    Data.Builder()
                        .putString(KEY_URL, canonical)
                        .putString(KEY_NAME, safeName(name))
                        .putString(KEY_MIME, mimeType.take(160))
                        .putString(KEY_USER_AGENT, userAgent.take(300))
                        .build()
                )
                .build()
            WorkManager.getInstance(context.applicationContext).enqueue(request)
            return true
        }

        internal fun safeName(value: String): String = unsafeName
            .replace(value.substringAfterLast('/').substringAfterLast('\\'), "_")
            .trim(' ', '.')
            .take(180)
            .ifBlank { "David-Pi-download" }

        private fun uniqueFile(directory: File, requestedName: String): File {
            val original = File(directory, requestedName)
            if (!original.exists()) return original
            val extension = original.extension.takeIf { it.isNotBlank() }?.let { ".$it" }.orEmpty()
            val stem = original.name.removeSuffix(extension)
            for (suffix in 2..10_000) {
                val candidate = File(directory, "$stem ($suffix)$extension")
                if (!candidate.exists()) return candidate
            }
            throw IllegalStateException("No safe download name is available.")
        }
    }
}

internal object PortalDownloadPolicy {
    const val MAX_DOWNLOAD_BYTES = 2L * 1024 * 1024 * 1024

    fun declaredLengthAllowed(length: Long): Boolean =
        length < 0 || length <= MAX_DOWNLOAD_BYTES

    fun copyBounded(
        input: InputStream,
        output: OutputStream,
        maximumBytes: Long = MAX_DOWNLOAD_BYTES,
    ): Long {
        require(maximumBytes >= 0)
        val buffer = ByteArray(256 * 1024)
        var total = 0L
        while (true) {
            val read = input.read(buffer)
            if (read < 0) return total
            if (read == 0) continue
            if (read.toLong() > maximumBytes - total) {
                throw DownloadTooLargeException()
            }
            output.write(buffer, 0, read)
            total += read
        }
    }
}

internal class DownloadTooLargeException : IOException(
    "The download exceeds David-Pi's 2 GiB safety limit."
)
