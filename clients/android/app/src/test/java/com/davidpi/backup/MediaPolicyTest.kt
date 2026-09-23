package com.davidpi.backup

import com.davidpi.backup.media.MediaPolicy
import com.davidpi.backup.media.CompleteScanAccumulator
import com.davidpi.backup.media.IncompleteMediaScanException
import com.davidpi.backup.media.MediaAccessPolicy
import com.davidpi.backup.media.MediaAccessScope
import com.davidpi.backup.media.PendingReconciliationReceipt
import com.davidpi.backup.media.ReconciliationClockAnchor
import com.davidpi.backup.media.ReconciliationProtocol
import com.davidpi.backup.media.ScanObservationCollector
import com.davidpi.backup.media.SourceSignature
import com.davidpi.backup.media.TransactionBatcher
import com.davidpi.backup.media.UuidV7Generator
import com.davidpi.backup.data.BackupDao
import com.davidpi.backup.data.BackupQueuePolicy
import com.davidpi.backup.data.QueueMergePolicy
import com.davidpi.backup.data.QueueObservation
import com.davidpi.backup.data.QueueObservationPlanner
import com.davidpi.backup.data.QueueSourceChangePolicy
import com.davidpi.backup.data.IntegrityRevalidationBudget
import com.davidpi.backup.data.QueueIntegrityRevalidationPolicy
import com.davidpi.backup.data.QueuedMedia
import com.davidpi.backup.data.QueuedMediaObservation
import com.davidpi.backup.net.BackupApi
import com.davidpi.backup.net.ApiException
import com.davidpi.backup.net.BackupFailurePolicy
import com.davidpi.backup.net.IntegrityRecoveryPolicy
import com.davidpi.backup.net.ProtocolAmbiguityException
import com.davidpi.backup.net.SourceRecoveryContinuation
import com.davidpi.backup.net.parseSuccessfulJson
import com.davidpi.backup.work.BackupScheduler
import com.davidpi.backup.work.BackupFrequency
import com.davidpi.backup.work.BackupStatusText
import com.davidpi.backup.work.IntegrityReadFailurePolicy
import com.davidpi.backup.work.ReconciliationDisposition
import com.davidpi.backup.work.ReconciliationFailurePolicy
import androidx.work.WorkInfo
import kotlinx.coroutines.test.runTest
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID

class MediaPolicyTest {
    @Test fun protectionMetricsUseOnlyConsistentAuthoritativeServerCounts() {
        val counts = mapOf<String, Any?>(
            "secondary_pending" to 2,
            "secondary_error" to 1,
            "fully_protected" to 3,
        )
        assertEquals(
            ServerProtectionSummary(6, 3),
            ProtectionStatusPolicy.fromCounts(3, counts),
        )
        assertNull(ProtectionStatusPolicy.fromCounts(4, counts))
        assertNull(
            ProtectionStatusPolicy.fromCounts(-1, mapOf("fully_protected" to -1))
        )
        assertNull(ProtectionStatusPolicy.fromCounts(null, emptyMap()))
    }

    @Test fun completedAndTerminalRowsAreRevalidatedOnACadenceBehindFreshUploads() {
        val now = QueueIntegrityRevalidationPolicy.INTERVAL_MILLIS * 2
        val terminal = QueuedMedia(
            queueId = "terminal", mediaStoreId = 7, mediaType = "image",
            contentUri = "content://media/7", displayName = "old.jpg",
            mimeType = "image/jpeg", byteSize = 12, captureTimestamp = 1,
            bucketName = "Camera", sha256 = "a".repeat(64),
            state = "permanent_error", retryCount = 3,
            errorCode = "invalid_media", updatedAt = 0,
            sourceSignature = "coarse",
        )
        val observedTerminal = terminal.copy(
            contentUri = "content://media/current/7",
            displayName = "current.jpg",
            sha256 = null,
            state = "discovered",
            retryCount = 0,
            errorCode = null,
            scanGeneration = "current",
            updatedAt = now,
        )
        val fresh = observedTerminal.copy(
            queueId = "fresh", mediaStoreId = 8, sourceSignature = "fresh",
        )
        val plan = QueueObservationPlanner.plan(
            listOf(
                QueuedMediaObservation(observedTerminal, true),
                QueuedMediaObservation(fresh, true),
            ),
            listOf(terminal),
            now,
            IntegrityRevalidationBudget(null),
        )
        val scheduled = plan.updates.single()
        assertEquals(QueueSourceChangePolicy.REHASH_STATE, scheduled.state)
        assertTrue(QueueIntegrityRevalidationPolicy.isCheckpoint(scheduled))
        assertEquals("a".repeat(64), scheduled.sha256)
        assertEquals("content://media/current/7", scheduled.contentUri)
        assertEquals("fresh", plan.inserts.single().queueId)

        val ordinaryObservation = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(observedTerminal, true)),
            listOf(terminal),
            now,
        ).updates.single()
        assertEquals("permanent_error", ordinaryObservation.state)
        assertFalse(QueueIntegrityRevalidationPolicy.isCheckpoint(ordinaryObservation))

        assertTrue(
            QueueIntegrityRevalidationPolicy.unchanged(scheduled, "a".repeat(64), 12)
        )
        val restored = QueueIntegrityRevalidationPolicy.restoreUnchanged(scheduled, now + 1)
        assertEquals("permanent_error", restored.state)
        assertEquals("invalid_media", restored.errorCode)
        assertFalse(QueueIntegrityRevalidationPolicy.isDue(restored, now + 1))
        assertFalse(
            QueueIntegrityRevalidationPolicy.unchanged(scheduled, "b".repeat(64), 12)
        )

        listOf("primary_verified", "secondary_pending", "fully_protected").forEach { state ->
            val completed = terminal.copy(
                queueId = state,
                state = state,
                errorCode = null,
            )
            val observed = observedTerminal.copy(queueId = state)
            val completedPlan = QueueObservationPlanner.plan(
                listOf(QueuedMediaObservation(observed, true)),
                listOf(completed),
                now,
                IntegrityRevalidationBudget(null),
            )
            val checkpoint = completedPlan.updates.single()
            assertTrue(state, QueueIntegrityRevalidationPolicy.isCheckpoint(checkpoint))
            assertEquals(
                state,
                QueueIntegrityRevalidationPolicy.restoreUnchanged(checkpoint, now + 1).state,
            )
        }

        val maximumStoredError = "e".repeat(80)
        val maximumErrorItem = terminal.copy(errorCode = maximumStoredError)
        val maximumErrorCheckpoint = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(observedTerminal, true)),
            listOf(maximumErrorItem),
            now,
            IntegrityRevalidationBudget(null),
        ).updates.single()
        assertEquals(
            maximumStoredError,
            QueueIntegrityRevalidationPolicy.restoreUnchanged(
                maximumErrorCheckpoint,
                now + 1,
            ).errorCode,
        )
    }

    @Test fun integrityRevalidationUsesPersistedRoundRobinItemAndByteBounds() {
        val now = QueueIntegrityRevalidationPolicy.INTERVAL_MILLIS * 2
        fun completed(id: Long, type: String = "image") = QueuedMedia(
            queueId = "$type-$id", mediaStoreId = id, mediaType = type,
            contentUri = "content://media/$id", displayName = "$id.jpg",
            mimeType = "image/jpeg", byteSize = 10, captureTimestamp = id,
            bucketName = "Camera", sha256 = "a".repeat(64),
            state = "fully_protected", updatedAt = 0,
            sourceSignature = "signature-$id",
        )
        fun observed(item: QueuedMedia) = QueuedMediaObservation(
            item.copy(sha256 = null, state = "discovered", updatedAt = now),
            true,
        )
        val images = (1L..4L).map { completed(it) }
        val videos = (1L..2L).map { completed(it, "video") }

        val firstBudget = IntegrityRevalidationBudget(null, maxItems = 2, maxBytes = 15)
        assertEquals("image", firstBudget.activeMediaType())
        val first = QueueObservationPlanner.plan(
            images.map(::observed), images, now, firstBudget
        )
        assertEquals(1, first.updates.count(QueueIntegrityRevalidationPolicy::isCheckpoint))
        assertEquals(1, firstBudget.scheduledItems())
        assertEquals(10L, firstBudget.scheduledBytes())
        assertTrue(firstBudget.completedScanCursor().isNotBlank())

        val secondBudget = IntegrityRevalidationBudget(
            firstBudget.completedScanCursor(),
            maxItems = 2,
            maxBytes = 15,
        )
        assertEquals("video", secondBudget.activeMediaType())
        val second = QueueObservationPlanner.plan(
            videos.map(::observed), videos, now, secondBudget
        )
        assertEquals(
            listOf("video-1"),
            second.updates.filter(QueueIntegrityRevalidationPolicy::isCheckpoint)
                .map { it.queueId },
        )

        val thirdBudget = IntegrityRevalidationBudget(
            secondBudget.completedScanCursor(),
            maxItems = 2,
            maxBytes = 15,
        )
        assertEquals("image", thirdBudget.activeMediaType())
        val third = QueueObservationPlanner.plan(
            images.map(::observed), images, now, thirdBudget
        )
        assertEquals(
            listOf("image-2"),
            third.updates.filter(QueueIntegrityRevalidationPolicy::isCheckpoint)
                .map { it.queueId },
        )

        val itemBudget = IntegrityRevalidationBudget(null, maxItems = 2, maxBytes = 100)
        val itemBound = QueueObservationPlanner.plan(
            images.map(::observed), images, now, itemBudget
        )
        assertEquals(2, itemBound.updates.count(QueueIntegrityRevalidationPolicy::isCheckpoint))
        assertEquals(2, itemBudget.scheduledItems())
        assertEquals(20L, itemBudget.scheduledBytes())
    }

    @Test fun unreadableIntegrityProbePreservesPriorProtectionAndRecordsLocalFailure() {
        val now = QueueIntegrityRevalidationPolicy.INTERVAL_MILLIS * 2
        val protected = QueuedMedia(
            queueId = "protected", mediaStoreId = 7, mediaType = "image",
            contentUri = "content://media/7", displayName = "seven.jpg",
            mimeType = "image/jpeg", byteSize = 12, captureTimestamp = 1,
            bucketName = "Camera", sha256 = "a".repeat(64),
            state = "fully_protected", updatedAt = 0,
            sourceSignature = "coarse",
        )
        val checkpoint = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(protected.copy(state = "discovered"), true)),
            listOf(protected),
            now,
            IntegrityRevalidationBudget(null),
        ).updates.single()
        val first = IntegrityReadFailurePolicy.transition(checkpoint, now + 1)
        assertTrue(first.retryLater)
        assertTrue(QueueIntegrityRevalidationPolicy.isCheckpoint(first.item))
        val second = IntegrityReadFailurePolicy.transition(first.item, now + 2)
        assertTrue(second.retryLater)
        val third = IntegrityReadFailurePolicy.transition(second.item, now + 3)
        assertFalse(third.retryLater)
        assertEquals("fully_protected", third.item.state)
        assertNull(third.item.errorCode)
        assertEquals(
            "media_integrity_failure:protected",
            QueueIntegrityRevalidationPolicy.failureStateKey(protected.queueId),
        )
        assertEquals(
            "source_unreadable|3|$now",
            QueueIntegrityRevalidationPolicy.failureStateValue(3, now),
        )
    }

    @Test fun unchangedIntegrityRecoveryIsDoneWithoutTransportAndPreservesExactState() {
        val now = QueueIntegrityRevalidationPolicy.INTERVAL_MILLIS * 3
        listOf(
            "primary_verified" to null,
            "secondary_pending" to "secondary_waiting",
            "fully_protected" to null,
            "permanent_error" to "e".repeat(80),
        ).forEachIndexed { index, (state, error) ->
            listOf<String?>(null, "server-upload-$index").forEach { uploadId ->
                val completed = QueuedMedia(
                    queueId = "$state-$index-${uploadId.orEmpty()}",
                    mediaStoreId = index.toLong() + 1,
                    mediaType = "image",
                    contentUri = "content://media/$index",
                    displayName = "$index.jpg",
                    mimeType = "image/jpeg",
                    byteSize = 128,
                    captureTimestamp = 1,
                    bucketName = "Camera",
                    sha256 = "a".repeat(64),
                    serverUploadId = uploadId,
                    acceptedOffset = 128,
                    state = state,
                    retryCount = 4,
                    errorCode = error,
                    updatedAt = 0,
                    sourceSignature = "coarse-$index",
                )
                val checkpoint = QueueIntegrityRevalidationPolicy.schedule(
                    completed,
                    completed.copy(state = "discovered", sha256 = null),
                    now,
                )
                val decision = IntegrityRecoveryPolicy.afterDigest(
                    checkpoint,
                    "a".repeat(64),
                    128,
                    now + 1,
                )
                assertEquals(SourceRecoveryContinuation.DONE, decision.continuation)
                assertEquals(state, decision.item.state)
                assertEquals(error, decision.item.errorCode)
                assertEquals(uploadId, decision.item.serverUploadId)
                assertEquals(128, decision.item.acceptedOffset)
                assertEquals("a".repeat(64), decision.item.sha256)
            }
        }

        val changed = QueuedMedia(
            queueId = "changed", mediaStoreId = 99, mediaType = "video",
            contentUri = "content://media/99", displayName = "changed.mp4",
            mimeType = "video/mp4", byteSize = 128, captureTimestamp = 1,
            bucketName = "Camera", sha256 = "a".repeat(64),
            state = "fully_protected", updatedAt = 0,
            sourceSignature = "coarse",
        )
        val changedCheckpoint = QueueIntegrityRevalidationPolicy.schedule(
            changed,
            changed.copy(state = "discovered", sha256 = null),
            now,
        )
        val changedDecision = IntegrityRecoveryPolicy.afterDigest(
            changedCheckpoint,
            "b".repeat(64),
            128,
            now + 1,
        )
        assertEquals(SourceRecoveryContinuation.UPLOAD, changedDecision.continuation)
        assertEquals("queued", changedDecision.item.state)
        assertNotEquals(changed.queueId, changedDecision.item.queueId)
    }

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
        assertNotEquals(BackupScheduler.MANUAL_WORK, BackupScheduler.PERIODIC_WORK)
        assertNotEquals(BackupScheduler.PERIODIC_WORK, BackupScheduler.INTEGRITY_WORK)
        assertEquals(
            setOf(BackupScheduler.PERIODIC_WORK, BackupScheduler.INTEGRITY_WORK),
            BackupScheduler.AUTOMATIC_WORK_NAMES,
        )
        assertEquals(1L, BackupScheduler.INTEGRITY_INTERVAL_DAYS)
        assertEquals(4096, IntegrityRevalidationBudget.MAX_ITEMS_PER_SCAN)
        assertEquals(64L * 1024 * 1024 * 1024, IntegrityRevalidationBudget.MAX_BYTES_PER_SCAN)
        val countBoundDays = 2 * ((
            IntegrityRevalidationBudget.REFERENCE_LIBRARY_ITEMS +
                IntegrityRevalidationBudget.MAX_ITEMS_PER_SCAN - 1
            ) / IntegrityRevalidationBudget.MAX_ITEMS_PER_SCAN)
        assertEquals(IntegrityRevalidationBudget.COUNT_BOUND_CYCLE_DAYS, countBoundDays)
        assertTrue(countBoundDays <= 30)
        val volumeBoundDays = 2L * ((
            IntegrityRevalidationBudget.REFERENCE_LIBRARY_BYTES_PER_TYPE +
                IntegrityRevalidationBudget.MAX_BYTES_PER_SCAN - 1
            ) / IntegrityRevalidationBudget.MAX_BYTES_PER_SCAN)
        assertEquals(
            IntegrityRevalidationBudget.VOLUME_BOUND_CYCLE_DAYS,
            volumeBoundDays,
        )
        assertTrue(volumeBoundDays <= 30)
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
        assertTrue(
            BackupFailurePolicy.isPermanent(
                ApiException(413, "invalid_size", "Media exceeds the supported size")
            )
        )
        assertFalse(
            BackupFailurePolicy.isPermanent(
                ApiException(413, "payload_too_large", "Unexpected proxy response")
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
        assertTrue(BackupQueuePolicy.isPending("source_changed"))
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

    @Test fun onlyFullImageAndVideoPermissionCanProduceAReconciliation() {
        assertEquals(MediaAccessScope.FULL, MediaAccessPolicy.scope(true, true, false))
        assertEquals(MediaAccessScope.PARTIAL, MediaAccessPolicy.scope(true, false, false))
        assertEquals(MediaAccessScope.PARTIAL, MediaAccessPolicy.scope(false, false, true))
        assertEquals(MediaAccessScope.NONE, MediaAccessPolicy.scope(false, false, false))
    }

    @Test fun bothCollectionsMustFinishAndCancellationFailsClosed() {
        val complete = CompleteScanAccumulator("scan")
        complete.completeCollection("image", listOf("image:1"), 1)
        complete.completeCollection("video", listOf("video:2"), 1)
        assertEquals(listOf("image:1", "video:2"), complete.finish().visibleClientItemIds)

        val partial = CompleteScanAccumulator("partial")
        partial.completeCollection("image", listOf("image:1"), 1)
        assertThrows(IncompleteMediaScanException::class.java) { partial.finish() }

        val cancelled = CompleteScanAccumulator("cancelled")
        cancelled.completeCollection("image", listOf("image:1"), 1)
        cancelled.completeCollection("video", listOf("video:2"), 1)
        cancelled.cancel()
        assertThrows(IncompleteMediaScanException::class.java) { cancelled.finish() }
    }

    @Test fun completeReceiptCanonicalizesAndSupportsMoreThan5000WithoutTruncating() {
        val ids = (0..5000).map { "image:$it" }.reversed() + listOf("image:1")
        val envelope = ReconciliationProtocol.envelope(ids)
        assertEquals(5001, envelope.itemCount)
        assertEquals(5001, envelope.visibleClientItemIds.size)
        assertEquals(envelope.visibleClientItemIds.sorted(), envelope.visibleClientItemIds)
        assertEquals(64, envelope.idsSha256.length)
        val body = envelope.requestJson()
        assertTrue(body.contains("\"complete\":true"))
        assertTrue(body.contains("\"item_count\":5001"))
        assertEquals(5001, "\"image:".toRegex().findAll(body).count())
    }

    @Test fun receiptDigestMatchesTheServerAsciiJsonContract() {
        assertEquals(
            "4b9c23e5cf882e47c04dacccd817750a8a7b33d4e1cbb173f7fe13e1e9e48ccd",
            ReconciliationProtocol.digest(listOf("image:1", "video:2")),
        )
    }

    @Test fun ambiguousRetryReusesReceiptOnlyWhileDigestIsUnchanged() {
        val first = ReconciliationProtocol.envelope(listOf("image:1", "video:2"))
        val pending = PendingReconciliationReceipt.from(first).json()
        val retried = ReconciliationProtocol.envelope(
            listOf("video:2", "image:1", "image:1"), pending
        )
        val changed = ReconciliationProtocol.envelope(listOf("image:1"), pending)
        assertEquals(first.scanId, retried.scanId)
        assertEquals(first.idsSha256, retried.idsSha256)
        assertNotEquals(first.scanId, changed.scanId)
        assertTrue(changed.scanId > first.scanId)
    }

    @Test fun serverClockAnchorRetiresOnlyTheRejectedFutureReceipt() {
        val serverTime = 1_700_000_000_000L
        val badPhoneTime = serverTime + 10L * 365 * 24 * 60 * 60 * 1000
        val bad = ReconciliationProtocol.envelope(
            listOf("image:1"), nowMillis = badPhoneTime
        )
        val anchor = requireNotNull(
            ReconciliationClockAnchor.fromServer(bad.scanId, serverTime)
        )
        val recovered = ReconciliationProtocol.envelope(
            listOf("image:1"),
            pendingReceipt = PendingReconciliationReceipt.from(bad).json(),
            clockAnchor = anchor.json(),
            nowMillis = badPhoneTime,
        )
        assertNotEquals(bad.scanId, recovered.scanId)
        assertEquals(serverTime, UuidV7Generator.timestampMillis(recovered.scanId))

        val ambiguousRetry = ReconciliationProtocol.envelope(
            listOf("image:1"),
            pendingReceipt = PendingReconciliationReceipt.from(recovered).json(),
            clockAnchor = anchor.json(),
            nowMillis = badPhoneTime,
        )
        assertEquals(recovered.scanId, ambiguousRetry.scanId)

        val changed = ReconciliationProtocol.envelope(
            listOf("video:2"),
            pendingReceipt = PendingReconciliationReceipt.from(recovered).json(),
            clockAnchor = anchor.json(),
            nowMillis = badPhoneTime,
        )
        assertTrue(changed.scanId > recovered.scanId)
        assertEquals(serverTime + 1, UuidV7Generator.timestampMillis(changed.scanId))
        assertNull(
            ReconciliationClockAnchor.fromServer(
                "01890f3a-1234-7abc-8def-000000000099", serverTime
            )
        )
    }

    @Test fun malformedSuccessfulReceiptIsRetryableWithTheSamePendingId() {
        val envelope = ReconciliationProtocol.envelope(listOf("image:1"))
        val pending = PendingReconciliationReceipt.from(envelope).json()
        val failure = assertThrows(ProtocolAmbiguityException::class.java) {
            parseSuccessfulJson("{not-json") {
                throw IllegalArgumentException("malformed successful JSON")
            }
        }
        assertEquals(
            ReconciliationDisposition.RETRY_SAME_RECEIPT,
            ReconciliationFailurePolicy.disposition(failure, envelope.scanId),
        )
        assertEquals(
            envelope.scanId,
            ReconciliationProtocol.envelope(listOf("image:1"), pending).scanId,
        )
    }

    @Test fun excludedPhysicalItemWithoutQueueIsVisibleButNotQueued() {
        val signature = SourceSignature.fromMetadata(
            "image", 42, 100, "image/jpeg", 17
        )
        val observed = queued(
            queueId = MediaPolicy.queueId("image", 42, signature),
            signature = signature,
            generation = "scan",
        ).copy(bucketName = "Screenshots")
        assertFalse(MediaPolicy.includeBucket(observed.bucketName, false, false))

        val plan = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(observed, enqueueIfMissing = false)),
            existingRows = emptyList(),
        )
        val collector = ScanObservationCollector().also { it.collect(plan.results) }

        assertEquals(listOf(observed.queueId), collector.visibleClientItemIds)
        assertEquals(listOf(false), plan.results.map { it.newlyQueued })
        assertEquals(0, collector.newlyQueuedCount)
        assertTrue(plan.inserts.isEmpty())
        assertTrue(plan.updates.isEmpty())
        assertTrue(plan.deletes.isEmpty())
    }

    @Test fun appDataLossReconstructsTheUnchangedOpaqueSourceId() {
        val signature = SourceSignature.fromMetadata(
            "image", 42, 100, "image/jpeg", 17
        )
        val beforeLoss = MediaPolicy.queueId("image", 42, signature)
        val afterLoss = MediaPolicy.queueId("image", 42, signature)

        assertEquals(beforeLoss, afterLoss)
        assertEquals(beforeLoss, UUID.fromString(beforeLoss).toString())
        assertNotEquals(MediaPolicy.clientItemId("image", 42), beforeLoss)
    }

    @Test fun exactMatchPreservesDurableIdWithoutRequeueingExcludedContent() {
        val oldSignature = "a".repeat(64)
        val existing = queued(
            queueId = MediaPolicy.queueId("image", 42, oldSignature),
            signature = oldSignature,
            generation = "old",
        ).copy(state = "fully_protected")
        val unchanged = queued(
            queueId = "newly-derived-id",
            signature = existing.sourceSignature,
            generation = "new",
        ).copy(bucketName = "Download")
        val unchangedPlan = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(unchanged, enqueueIfMissing = false)),
            listOf(existing),
        )
        assertEquals(existing.queueId, unchangedPlan.results.single().clientItemId)
        assertEquals(existing.queueId, unchangedPlan.updates.single().queueId)
        assertTrue(unchangedPlan.inserts.isEmpty())
        assertTrue(unchangedPlan.deletes.isEmpty())
    }

    @Test fun changedExcludedItemReportsNewIdAndDeletesStaleQueueIdentity() {
        val existing = queued(
            queueId = MediaPolicy.queueId("image", 42, "a".repeat(64)),
            signature = "a".repeat(64),
            generation = "old",
        ).copy(state = "fully_protected")
        val newSignature = "c".repeat(64)
        val changed = queued(
            queueId = MediaPolicy.queueId("image", 42, newSignature),
            signature = newSignature,
            generation = "new",
        ).copy(bucketName = "Download")
        assertFalse(MediaPolicy.includeBucket(changed.bucketName, false, false))
        val changedPlan = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(changed, enqueueIfMissing = false)),
            listOf(existing),
        )
        val collector = ScanObservationCollector().also { it.collect(changedPlan.results) }
        assertEquals(listOf(changed.queueId), collector.visibleClientItemIds)
        assertFalse(changedPlan.results.single().newlyQueued)
        assertEquals(listOf(existing), changedPlan.deletes)
        assertTrue(changedPlan.inserts.isEmpty())
        assertTrue(changedPlan.updates.isEmpty())
    }

    @Test fun changedSourceDefersReplacementUntilActiveServerSessionIsRetired() {
        val existing = queued(
            queueId = "old-id", signature = "a".repeat(64), generation = "old"
        ).copy(
            sha256 = "b".repeat(64),
            serverUploadId = "active-session",
            acceptedOffset = 91,
            state = "uploading",
        )
        val changed = queued(
            queueId = "replacement-id", signature = "c".repeat(64), generation = "new"
        )

        val replace = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(changed, enqueueIfMissing = true)),
            listOf(existing),
        )
        val deferred = replace.updates.single()
        assertEquals("old-id", deferred.queueId)
        assertEquals("replacement-id", replace.results.single().clientItemId)
        assertEquals("active-session", deferred.serverUploadId)
        assertEquals(QueueSourceChangePolicy.STATE, deferred.state)
        assertEquals(QueueSourceChangePolicy.REPLACE, deferred.errorCode)
        assertTrue(replace.deletes.isEmpty())
        assertTrue(replace.inserts.isEmpty())

        val remove = QueueObservationPlanner.plan(
            listOf(QueuedMediaObservation(changed, enqueueIfMissing = false)),
            listOf(existing),
        )
        assertEquals(QueueSourceChangePolicy.REMOVE, remove.updates.single().errorCode)
        assertTrue(remove.deletes.isEmpty())
        assertTrue(remove.inserts.isEmpty())

        val afterDelete = QueueSourceChangePolicy.checkpointAfterServerCleanup(deferred)
        assertEquals(QueueSourceChangePolicy.REHASH_STATE, afterDelete.state)
        assertNull(afterDelete.serverUploadId)
        assertEquals(0, afterDelete.acceptedOffset)
        assertEquals(QueueSourceChangePolicy.REPLACE, afterDelete.errorCode)
        assertFalse(QueueSourceChangePolicy.shouldQuarantineUnreadable(2))
        assertTrue(QueueSourceChangePolicy.shouldQuarantineUnreadable(3))
        assertEquals(
            QueueSourceChangePolicy.STATE,
            QueueSourceChangePolicy.stateAfterTemporaryFailure(QueueSourceChangePolicy.STATE),
        )
        assertEquals(
            QueueSourceChangePolicy.REHASH_STATE,
            QueueSourceChangePolicy.stateAfterTemporaryFailure(
                QueueSourceChangePolicy.REHASH_STATE
            ),
        )
        assertEquals(
            "retryable_error",
            QueueSourceChangePolicy.stateAfterTemporaryFailure("uploading"),
        )
    }

    @Test fun fiftyThousandObservationsUseBoundedBatchTransactions() = runTest {
        var transactions = 0
        val sizes = mutableListOf<Int>()
        val collector = ScanObservationCollector()
        val batcher = TransactionBatcher<QueuedMediaObservation, QueueObservation>(
            BackupDao.OBSERVATION_BATCH_SIZE
        ) {
            transactions++
            sizes += it.size
            QueueObservationPlanner.plan(it, existingRows = emptyList()).also { plan ->
                assertTrue(plan.inserts.isEmpty())
                assertTrue(plan.updates.isEmpty())
                assertTrue(plan.deletes.isEmpty())
            }.results
        }
        repeat(ReconciliationProtocol.MAX_ITEMS) { index ->
            collector.collect(batcher.add(
                QueuedMediaObservation(
                    queued(
                        queueId = "opaque-$index",
                        signature = "a".repeat(64),
                        generation = "scan",
                    ).copy(mediaStoreId = index.toLong(), bucketName = "Screenshots"),
                    enqueueIfMissing = false,
                )
            ))
        }
        collector.collect(batcher.finish())

        assertEquals(ReconciliationProtocol.MAX_ITEMS, collector.visibleClientItemIds.size)
        assertEquals("opaque-0", collector.visibleClientItemIds.first())
        assertEquals("opaque-49999", collector.visibleClientItemIds.last())
        assertEquals(0, collector.newlyQueuedCount)
        assertEquals(125, transactions)
        assertTrue(sizes.all { it in 1..BackupDao.OBSERVATION_BATCH_SIZE })
    }

    @Test fun uuidV7ReceiptsAreCanonicalAndMonotonicWithinOneProcess() {
        val first = UuidV7Generator.next(1_700_000_000_000)
        val second = UuidV7Generator.next(1_700_000_000_000)
        val afterClockRollback = UuidV7Generator.nextAfter(first, 1_600_000_000_000)
        assertEquals(7, UUID.fromString(first).version())
        assertTrue(second > first)
        assertTrue(afterClockRollback > first)
    }

    @Test fun repeatObservationPreservesTransportButSourceDriftResetsIt() {
        val existing = queued(
            queueId = "old-id", signature = "a".repeat(64), generation = "old"
        ).copy(
            sha256 = "b".repeat(64), serverUploadId = "upload-1", acceptedOffset = 91,
            state = "retryable_error", retryCount = 4, errorCode = "offline",
        )
        val repeated = queued(
            queueId = "newly-derived-id", signature = "a".repeat(64), generation = "new"
        )
        val normalized = QueueMergePolicy.normalizeObservationIdentity(existing, repeated)
        assertEquals("old-id", normalized.queueId)
        val preserved = QueueMergePolicy.merge(existing, normalized)
        assertEquals("upload-1", preserved.serverUploadId)
        assertEquals(91, preserved.acceptedOffset)
        assertEquals(4, preserved.retryCount)
        assertEquals("new", preserved.scanGeneration)
        assertFalse(QueueMergePolicy.resetRequired(existing, normalized))

        val legacy = existing.copy(sourceSignature = "")
        val adopted = QueueMergePolicy.normalizeObservationIdentity(legacy, repeated)
        assertEquals("newly-derived-id", adopted.queueId)
        assertEquals(repeated.sourceSignature, adopted.sourceSignature)
        assertTrue(QueueMergePolicy.resetRequired(legacy, adopted))

        val drifted = queued(
            queueId = "replacement-id", signature = "c".repeat(64), generation = "new"
        )
        assertTrue(QueueMergePolicy.resetRequired(existing, drifted))
        val reset = QueueMergePolicy.merge(existing, drifted)
        assertNull(reset.sha256)
        assertNull(reset.serverUploadId)
        assertEquals(0, reset.acceptedOffset)
        assertEquals(0, reset.retryCount)
        assertEquals("discovered", reset.state)
    }

    @Test fun reconciliationErrorsRetrySafelyOrFailClosed() {
        assertEquals(
            ReconciliationDisposition.RETRY_SAME_RECEIPT,
            ReconciliationFailurePolicy.disposition(java.io.IOException("ambiguous")),
        )
        assertEquals(
            ReconciliationDisposition.RETRY_FRESH_RECEIPT,
            ReconciliationFailurePolicy.disposition(
                ApiException(409, "stale_scan", "newer receipt exists")
            ),
        )
        assertEquals(
            ReconciliationDisposition.RETRY_WITH_SERVER_CLOCK,
            ReconciliationFailurePolicy.disposition(
                ApiException(
                    422,
                    "scan_clock_skew",
                    "clock ahead",
                    1_700_000_000_000L,
                    "ffffffff-ffff-7abc-8def-000000000099",
                ),
                "ffffffff-ffff-7abc-8def-000000000099",
            ),
        )
        assertEquals(
            ReconciliationDisposition.RETRY_SAME_RECEIPT,
            ReconciliationFailurePolicy.disposition(
                ApiException(
                    422,
                    "scan_clock_skew",
                    "unbound clock response",
                    1_700_000_000_000L,
                    "01890f3a-1234-7abc-8def-000000000098",
                ),
                "01890f3a-1234-7abc-8def-000000000099",
            ),
        )
        assertEquals(
            ReconciliationDisposition.FAIL_CLOSED,
            ReconciliationFailurePolicy.disposition(
                ApiException(409, "scan_replay_conflict", "conflict")
            ),
        )
        assertEquals(
            ReconciliationDisposition.REVOKED,
            ReconciliationFailurePolicy.disposition(ApiException(401, message = "revoked")),
        )
        assertEquals(
            ReconciliationDisposition.FAIL_CLOSED,
            ReconciliationFailurePolicy.disposition(
                ApiException(403, "android_complete_scan_required", "wrong platform")
            ),
        )
    }

    private fun queued(queueId: String, signature: String, generation: String) = QueuedMedia(
        queueId = queueId,
        mediaStoreId = 42,
        mediaType = "image",
        contentUri = "content://media/42",
        displayName = "one.jpg",
        mimeType = "image/jpeg",
        byteSize = 100,
        captureTimestamp = 1,
        bucketName = "Camera",
        scanGeneration = generation,
        sourceSignature = signature,
    )
}
