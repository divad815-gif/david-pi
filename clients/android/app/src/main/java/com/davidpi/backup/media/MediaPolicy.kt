package com.davidpi.backup.media

import java.nio.charset.StandardCharsets
import java.util.UUID

object MediaPolicy {
    fun includeBucket(
        bucket: String?,
        includeScreenshots: Boolean,
        includeDownloads: Boolean,
        includeMessaging: Boolean = false
    ): Boolean {
        val value = bucket.orEmpty().lowercase()
        if (!includeScreenshots && "screenshot" in value) return false
        if (!includeDownloads && "download" in value) return false
        if (!includeMessaging && listOf("whatsapp", "messenger", "telegram").any { it in value }) return false
        return true
    }

    fun clientItemId(mediaType: String, mediaStoreId: Long) = "$mediaType:$mediaStoreId"

    fun queueId(mediaType: String, mediaStoreId: Long, sourceSignature: String): String =
        UUID.nameUUIDFromBytes(
            "${clientItemId(mediaType, mediaStoreId)}:$sourceSignature"
                .toByteArray(StandardCharsets.UTF_8)
        ).toString()

    fun recoveryQueueId(
        mediaType: String,
        mediaStoreId: Long,
        sourceSignature: String,
        contentSha256: String,
    ): String {
        require(contentSha256.matches(Regex("[0-9a-f]{64}")))
        // Older Android releases can reuse a MediaStore ID while exposing the
        // same coarse modified-time/size metadata. Bind the recovery identity
        // to the freshly observed bytes so a completed server record for the
        // former content can never satisfy the replacement item.
        return UUID.nameUUIDFromBytes(
            "${clientItemId(mediaType, mediaStoreId)}:$sourceSignature:content:$contentSha256"
                .toByteArray(StandardCharsets.UTF_8)
        ).toString()
    }
}
