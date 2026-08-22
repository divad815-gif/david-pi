package com.davidpi.backup

import com.davidpi.backup.media.MediaPolicy
import com.davidpi.backup.data.BackupQueuePolicy
import com.davidpi.backup.net.BackupApi
import com.davidpi.backup.net.ApiException
import com.davidpi.backup.net.BackupFailurePolicy
import com.davidpi.backup.work.BackupScheduler
import com.davidpi.backup.work.BackupFrequency
import com.davidpi.backup.work.BackupStatusText
import androidx.work.WorkInfo
import org.junit.Assert.*
import org.junit.Test

class MediaPolicyTest {
    @Test fun cameraIncludedButOptionalFoldersExcluded() {
        assertTrue(MediaPolicy.includeBucket("Camera", false, false))
        assertFalse(MediaPolicy.includeBucket("Screenshots", false, false))
        assertFalse(MediaPolicy.includeBucket("Download", false, false))
        assertFalse(MediaPolicy.includeBucket("WhatsApp Images", true, true))
        assertTrue(MediaPolicy.includeBucket("Screenshots", true, false))
    }

    @Test fun mappingIsStableAcrossScans() {
        assertEquals("image:42", MediaPolicy.clientItemId("image", 42))
        assertNotEquals(MediaPolicy.clientItemId("image", 42), MediaPolicy.clientItemId("video", 42))
    }

    @Test fun protocolAndUniqueWorkDefaultsAreBounded() {
        assertEquals(8 * 1024 * 1024, BackupApi.CHUNK_SIZE)
        assertEquals(2 * 1024 * 1024L, BackupApi.DEFAULT_RATE_BYTES)
        assertNotEquals(BackupScheduler.MANUAL_WORK, BackupScheduler.WEEKLY_WORK)
    }

    @Test fun automaticBackupFrequenciesAreExplicitAndBounded() {
        assertEquals(BackupFrequency.WEEKLY, BackupFrequency.fromStored(null))
        assertEquals(BackupFrequency.HOURLY, BackupFrequency.fromStored("hourly"))
        assertEquals(1L, BackupFrequency.HOURLY.repeatInterval)
        assertEquals(1L, BackupFrequency.DAILY.repeatInterval)
        assertEquals(7L, BackupFrequency.WEEKLY.repeatInterval)
        assertEquals(30L, BackupFrequency.MONTHLY.repeatInterval)
        assertNull(BackupFrequency.OFF.repeatInterval)
    }

    @Test fun malformedServerMediaIsSkippedButTemporaryFailuresRetry() {
        assertTrue(
            BackupFailurePolicy.isPermanent(
                ApiException(422, "invalid_media", "Unreadable media")
            )
        )
        assertFalse(
            BackupFailurePolicy.isPermanent(
                ApiException(503, "primary_storage_unavailable", "Try again")
            )
        )
        assertFalse(BackupFailurePolicy.isPermanent(java.io.IOException("Offline")))
    }

    @Test fun uploadedItemsAreNotCountedAsPending() {
        assertTrue(BackupQueuePolicy.isPending("discovered"))
        assertTrue(BackupQueuePolicy.isPending("retryable_error"))
        assertFalse(BackupQueuePolicy.isPending("secondary_pending"))
        assertFalse(BackupQueuePolicy.isPending("fully_protected"))
        assertFalse(BackupQueuePolicy.isPending("permanent_error"))
    }

    @Test fun workStatusExplainsWaitingRetryAndSkippedItems() {
        assertEquals(
            "Waiting for Wi-Fi or charging requirements.",
            BackupStatusText.describe(WorkInfo.State.ENQUEUED)
        )
        assertEquals(
            "Retry scheduled after a temporary connection or server problem.",
            BackupStatusText.describe(WorkInfo.State.ENQUEUED, 2)
        )
        assertEquals(
            "Skipped one unreadable item and kept going.",
            BackupStatusText.describe(
                WorkInfo.State.RUNNING,
                progress = "Skipped one unreadable item and kept going."
            )
        )
    }
}
