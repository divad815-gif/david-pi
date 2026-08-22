package com.davidpi.backup.media

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
}
