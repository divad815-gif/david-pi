package com.davidpi.backup.offline

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertNull
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertThrows
import org.junit.Test

class OfflineDownloadPolicyTest {
    @Test fun acceptsOnlyServerBookIds() {
        assertTrue(OfflineDownloadPolicy.validBookId("0123456789abcdef0123456789abcdef"))
        assertFalse(OfflineDownloadPolicy.validBookId("../private"))
        assertFalse(OfflineDownloadPolicy.validBookId("ABCDEF0123456789ABCDEF0123456789"))
    }

    @Test fun sameLengthCopiesStillRequireTheAuthoritativeDigest() {
        val expected = "a".repeat(64)
        assertTrue(OfflineDownloadPolicy.validSha256(expected))
        assertFalse(OfflineDownloadPolicy.validSha256("A".repeat(64)))
        assertTrue(OfflineDownloadPolicy.digestMatches(expected, expected))
        assertFalse(OfflineDownloadPolicy.digestMatches(expected, "b".repeat(64)))
        assertFalse(OfflineDownloadPolicy.digestMatches(null, expected))
        assertEquals(
            OfflineDownloadPolicy.LocalCopyAction.REPAIR,
            OfflineDownloadPolicy.localCopyAction(
                expectedBytes = 8_192,
                actualBytes = 8_192,
                expectedSha256 = expected,
                observedSha256 = "b".repeat(64),
            ),
        )
        assertEquals(
            OfflineDownloadPolicy.LocalCopyAction.ACCEPT_VERIFIED,
            OfflineDownloadPolicy.localCopyAction(8_192, 8_192, expected, expected),
        )
    }

    @Test fun backgroundIntegrityChecksStayBounded() {
        assertTrue(OfflineDownloadPolicy.MAX_INTEGRITY_CHECKS_PER_RECOVERY in 1..8)
        assertTrue(OfflineDownloadPolicy.INTEGRITY_INTERVAL_MILLIS >= 24L * 60 * 60 * 1000)
        assertEquals(
            listOf("queued-a", "queued-b", "due-a", "due-b"),
            OfflineDownloadPolicy.recoveryCandidates(
                listOf("queued-a", "queued-b"),
                listOf("due-a", "due-b", "due-c"),
            ),
        )
    }

    @Test fun removalFenceWaitsAcrossOpenAndRenameCrashWindows() = runBlocking {
        for (window in listOf("before-open", "before-rename")) {
            val id = "book-$window"
            val workerAtWindow = CompletableDeferred<Unit>()
            val letWorkerFinish = CompletableDeferred<Unit>()
            val removalEntered = CompletableDeferred<Unit>()
            val events = mutableListOf<String>()
            val worker = async(Dispatchers.Default) {
                OfflineAudiobookFileLocks.withLock(id) {
                    events += window
                    workerAtWindow.complete(Unit)
                    letWorkerFinish.await()
                    events += "worker-finished"
                }
            }
            withTimeout(2_000) { workerAtWindow.await() }
            val remover = async(Dispatchers.Default) {
                OfflineAudiobookFileLocks.withLock(id) {
                    events += "final-cleanup"
                    removalEntered.complete(Unit)
                }
            }

            assertNull(withTimeoutOrNull(100) { removalEntered.await() })
            letWorkerFinish.complete(Unit)
            worker.await()
            remover.await()
            assertEquals(listOf(window, "worker-finished", "final-cleanup"), events)
        }
    }

    @Test fun appendsOnlyToAResumeResponse() {
        assertEquals(OfflineDownloadPolicy.ResponseMode.APPEND, OfflineDownloadPolicy.responseMode(206, 1024))
        assertEquals(OfflineDownloadPolicy.ResponseMode.REPLACE, OfflineDownloadPolicy.responseMode(200, 1024))
        assertEquals(OfflineDownloadPolicy.ResponseMode.REPLACE, OfflineDownloadPolicy.responseMode(206, 0))
        assertEquals(OfflineDownloadPolicy.ResponseMode.REJECT, OfflineDownloadPolicy.responseMode(403, 1024))
    }

    @Test fun resumesOnlyWithAValidStrongValidatorAndPartialPrefix() {
        assertEquals(1024L, OfflineDownloadPolicy.resumableBytes(1024, 4096, "\"v1\""))
        assertEquals(0L, OfflineDownloadPolicy.resumableBytes(1024, 4096, "W/\"v1\""))
        assertEquals(0L, OfflineDownloadPolicy.resumableBytes(1024, 4096, null))
        assertEquals(0L, OfflineDownloadPolicy.resumableBytes(4096, 4096, "\"v1\""))
    }

    @Test fun resumedResponseBindsRangeLengthTotalAndStrongValidator() {
        val accepted = OfflineDownloadPolicy.responseContract(
            status = 206,
            existingBytes = 1024,
            expectedTotal = 4096,
            storedEtag = "\"v1\"",
            responseEtag = "\"v1\"",
            contentRange = "bytes 1024-4095/4096",
            contentLength = 3072,
        )
        assertNotNull(accepted)
        assertEquals(3072L, accepted!!.expectedSegmentBytes)

        fun rejected(
            range: String? = "bytes 1024-4095/4096",
            length: Long = 3072,
            etag: String? = "\"v1\"",
        ) = OfflineDownloadPolicy.responseContract(
            206, 1024, 4096, "\"v1\"", etag, range, length,
        )
        assertNull(rejected(range = "bytes 0-3071/4096"))
        assertNull(rejected(range = "bytes 1024-4096/4096"))
        assertNull(rejected(range = "bytes 1024-4095/8192"))
        assertNull(rejected(length = 3071))
        assertNull(rejected(etag = "\"replacement\""))
        assertNull(rejected(etag = "W/\"v1\""))
        assertNull(OfflineDownloadPolicy.responseContract(
            206, 1024, 4096, null, "\"v1\"", "bytes 1024-4095/4096", 3072,
        ))
    }

    @Test fun replacementResponseCannotClaimAnUnexpectedSizeOrRange() {
        assertNotNull(OfflineDownloadPolicy.responseContract(
            200, 1024, 4096, "\"old\"", "\"new\"", null, 4096,
        ))
        assertNull(OfflineDownloadPolicy.responseContract(
            200, 1024, 4096, "\"old\"", "\"new\"", null, 4097,
        ))
        assertNull(OfflineDownloadPolicy.responseContract(
            200, 1024, 4096, "\"old\"", "\"new\"", "bytes 0-4095/4096", 4096,
        ))
    }

    @Test fun chunkedBodiesCannotExceedTheirDeclaredRangeOrWholeBook() {
        assertEquals(3072L, OfflineDownloadPolicy.checkedSegmentBytes(0, 3072, 3072, 1024, 4096))
        assertThrows(DownloadIntegrityException::class.java) {
            OfflineDownloadPolicy.checkedSegmentBytes(3072, 1, 3072, 1024, 4096)
        }
        assertThrows(DownloadIntegrityException::class.java) {
            OfflineDownloadPolicy.checkedSegmentBytes(0, 0, 3072, 1024, 4096)
        }
    }
}
