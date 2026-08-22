package com.davidpi.backup.media

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import com.davidpi.backup.data.BackupDao
import com.davidpi.backup.data.QueuedMedia
import java.util.UUID

class MediaScanner(private val context: Context, private val dao: BackupDao) {
    fun hasFullAccess(): Boolean {
        val imagePermission = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_IMAGES
            else Manifest.permission.READ_EXTERNAL_STORAGE
        val videoPermission = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_VIDEO
            else Manifest.permission.READ_EXTERNAL_STORAGE
        return ContextCompat.checkSelfPermission(context, imagePermission) == PackageManager.PERMISSION_GRANTED &&
            ContextCompat.checkSelfPermission(context, videoPermission) == PackageManager.PERMISSION_GRANTED
    }

    suspend fun scan(includeScreenshots: Boolean = false, includeDownloads: Boolean = false): Int {
        if (!hasFullAccess()) return 0
        var discovered = 0
        discovered += scanCollection(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI, "image",
            includeScreenshots, includeDownloads
        )
        discovered += scanCollection(
            MediaStore.Video.Media.EXTERNAL_CONTENT_URI, "video",
            includeScreenshots, includeDownloads
        )
        return discovered
    }

    private suspend fun scanCollection(
        collection: android.net.Uri,
        type: String,
        includeScreenshots: Boolean,
        includeDownloads: Boolean
    ): Int {
        val columns = arrayOf(
            MediaStore.MediaColumns._ID,
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.SIZE,
            MediaStore.MediaColumns.DATE_TAKEN,
            MediaStore.MediaColumns.DATE_ADDED,
            MediaStore.MediaColumns.BUCKET_DISPLAY_NAME,
        )
        var count = 0
        context.contentResolver.query(
            collection, columns, null, null,
            "${MediaStore.MediaColumns.DATE_ADDED} ASC"
        )?.use { cursor ->
            val idIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
            val nameIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
            val mimeIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.MIME_TYPE)
            val sizeIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)
            val takenIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_TAKEN)
            val addedIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_ADDED)
            val bucketIndex = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.BUCKET_DISPLAY_NAME)
            while (cursor.moveToNext()) {
                val id = cursor.getLong(idIndex)
                val bucket = cursor.getString(bucketIndex)
                if (!MediaPolicy.includeBucket(bucket, includeScreenshots, includeDownloads)) continue
                val uri = ContentUris.withAppendedId(collection, id)
                val taken = cursor.getLong(takenIndex).takeIf { it > 0 }
                    ?: cursor.getLong(addedIndex).takeIf { it > 0 }?.times(1000)
                val inserted = dao.enqueue(
                    QueuedMedia(
                        queueId = UUID.nameUUIDFromBytes(
                            MediaPolicy.clientItemId(type, id).toByteArray()
                        ).toString(),
                        mediaStoreId = id,
                        mediaType = type,
                        contentUri = uri.toString(),
                        displayName = cursor.getString(nameIndex) ?: "$type-$id",
                        mimeType = cursor.getString(mimeIndex) ?: "application/octet-stream",
                        byteSize = cursor.getLong(sizeIndex),
                        captureTimestamp = taken,
                        bucketName = bucket,
                    )
                )
                if (inserted != -1L) count++
            }
        }
        return count
    }
}
