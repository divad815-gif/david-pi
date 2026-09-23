package com.davidpi.backup

import com.davidpi.backup.net.DavidPiHttp
import com.davidpi.backup.net.DavidPiOrigin
import com.davidpi.backup.net.PairingDeepLink
import com.davidpi.backup.net.PairingRequestPolicy
import com.davidpi.backup.net.PortalBridgeInstallPolicy
import com.davidpi.backup.net.PortalNavigationDecision
import com.davidpi.backup.net.PortalNavigationPolicy
import com.davidpi.backup.net.UploadRecoveryPolicy
import com.davidpi.backup.net.UploadRecoveryReason
import com.davidpi.backup.net.UploadSessionConflictPolicy
import com.davidpi.backup.net.ApiException
import com.davidpi.backup.net.AbandonSessionPolicy
import com.davidpi.backup.net.AlreadyPresentPolicy
import com.davidpi.backup.net.BackupFailurePolicy
import com.davidpi.backup.net.DownloadTooLargeException
import com.davidpi.backup.net.PortalDownloadPolicy
import com.davidpi.backup.net.SourceUnreadableException
import com.davidpi.backup.net.writeSourceRange
import com.davidpi.backup.data.QueuedMedia
import com.davidpi.backup.data.QueueSourceChangePolicy
import okio.Buffer
import okio.ForwardingSink
import okio.buffer
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream

class NetworkOriginPolicyTest {
    @org.junit.Before fun approveTestOrigin() {
        com.davidpi.backup.net.DavidPiOrigin.approve("https://john-pi.tail123456.ts.net", "test-scope")
    }

    @Test fun abandonCleanupAcceptsOnlyTheExplicitTerminalConflict() {
        assertTrue(AbandonSessionPolicy.isComplete(204, ""))
        assertTrue(AbandonSessionPolicy.isComplete(404, ""))
        assertTrue(AbandonSessionPolicy.isComplete(409, "upload_not_abandonable"))

        assertFalse(AbandonSessionPolicy.isComplete(409, "upload_busy"))
        assertFalse(AbandonSessionPolicy.isComplete(409, ""))
        assertFalse(AbandonSessionPolicy.isComplete(503, "upload_busy"))
    }

    @Test fun partialBridgeInstallAlwaysRemovesTheListener() {
        val events = mutableListOf<String>()
        val result = PortalBridgeInstallPolicy.install(
            addListener = { events += "listener-added" },
            addScript = {
                events += "script-failed"
                throw IllegalStateException("fixture")
            },
            removeListener = { events += "listener-removed" },
        )
        assertNull(result)
        assertEquals(
            listOf("listener-added", "script-failed", "listener-removed"),
            events,
        )
    }

    @Test fun changedSourceGetsBoundedFreshSessionsThenQuarantinesWithoutBlockingQueue() {
        val mismatch = ApiException(422, "sha256_mismatch", "changed")
        assertEquals(
            UploadRecoveryReason.SOURCE_MISMATCH,
            UploadRecoveryPolicy.reason(mismatch, true),
        )
        assertTrue(UploadRecoveryPolicy.mayRestart(0))
        assertTrue(UploadRecoveryPolicy.mayRestart(1))
        assertFalse(UploadRecoveryPolicy.mayRestart(2))
        val terminal = ApiException(
            422,
            UploadRecoveryPolicy.terminalErrorCode(UploadRecoveryReason.SOURCE_MISMATCH),
            "unstable",
        )
        assertTrue(BackupFailurePolicy.isPermanent(terminal))
        assertEquals(
            UploadRecoveryReason.SESSION_MISSING,
            UploadRecoveryPolicy.reason(ApiException(404, "", "gone"), true),
        )
        assertNull(UploadRecoveryPolicy.reason(ApiException(404, "", "gone"), false))
    }

    @Test fun uploadRestartCheckpointCannotReuseTheOldDigestAfterProcessDeath() {
        val old = QueuedMedia(
            queueId = "image:7", mediaStoreId = 7, mediaType = "image",
            contentUri = "content://media/7", displayName = "changed.jpg",
            mimeType = "image/jpeg", byteSize = 123, captureTimestamp = 1,
            bucketName = "Camera", sha256 = "a".repeat(64),
            serverUploadId = "old-session", acceptedOffset = 123,
            state = "retryable_error",
        )
        val checkpoint = UploadRecoveryPolicy.checkpointForFreshDigest(old)

        assertEquals(QueueSourceChangePolicy.REHASH_STATE, checkpoint.state)
        assertEquals(UploadRecoveryPolicy.REHASH_ERROR_CODE, checkpoint.errorCode)
        assertNull(checkpoint.sha256)
        assertEquals(0L, checkpoint.byteSize)
        assertNull(checkpoint.serverUploadId)
        assertEquals(0L, checkpoint.acceptedOffset)
    }

    @Test fun activeSessionConflictRequiresOneCanonicalServerUploadIdentity() {
        val id = "123e4567-e89b-42d3-a456-426614174000"
        assertEquals(
            id,
            UploadSessionConflictPolicy.recoveryUploadId(
                409,
                "upload_session_conflict",
                listOf(id),
            ),
        )
        assertNull(
            UploadSessionConflictPolicy.recoveryUploadId(
                409,
                "upload_session_conflict",
                emptyList(),
            )
        )
        assertNull(
            UploadSessionConflictPolicy.recoveryUploadId(
                409,
                "upload_session_conflict",
                listOf(id, id),
            )
        )
        listOf(
            "123e4567-e89b-12d3-a456-426614174000",
            "123E4567-E89B-42D3-A456-426614174000",
            "../$id",
            " $id",
        ).forEach {
            assertNull(
                UploadSessionConflictPolicy.recoveryUploadId(
                    409,
                    "upload_session_conflict",
                    listOf(it),
                )
            )
        }
        assertNull(
            UploadSessionConflictPolicy.recoveryUploadId(
                422,
                "upload_session_conflict",
                listOf(id),
            )
        )
        assertNull(
            UploadSessionConflictPolicy.recoveryUploadId(
                409,
                "upload_busy",
                listOf(id),
            )
        )
    }

    @Test fun activeSessionConflictIsDurablyCleanedAndRekeyedToFreshBytes() {
        val id = "123e4567-e89b-42d3-a456-426614174000"
        val old = QueuedMedia(
            queueId = "old-client-item", mediaStoreId = 7, mediaType = "image",
            contentUri = "content://media/7", displayName = "changed.jpg",
            mimeType = "image/jpeg", byteSize = 123, captureTimestamp = 1,
            bucketName = "Camera", sha256 = "a".repeat(64),
            state = "queued", sourceSignature = "coarse-metadata",
        )
        val cleanup = UploadSessionConflictPolicy.cleanupCheckpoint(old, id)
        assertEquals(id, cleanup.serverUploadId)
        assertEquals(QueueSourceChangePolicy.STATE, cleanup.state)
        assertEquals(UploadRecoveryPolicy.REHASH_ERROR_CODE, cleanup.errorCode)
        assertEquals(
            UploadRecoveryReason.SOURCE_MISMATCH,
            UploadRecoveryPolicy.reason(
                ApiException(
                    409,
                    "upload_session_conflict",
                    "changed",
                    recoveryUploadId = id,
                ),
                true,
            ),
        )

        val postDelete = QueueSourceChangePolicy.checkpointAfterServerCleanup(cleanup)
        val replacement = UploadRecoveryPolicy.itemAfterFreshDigest(
            postDelete,
            "b".repeat(64),
            456,
        )
        assertEquals(QueueSourceChangePolicy.REHASH_STATE, postDelete.state)
        assertNull(postDelete.serverUploadId)
        assertTrue(replacement.queueId != old.queueId)
        assertEquals("b".repeat(64), replacement.sha256)
        assertEquals(456L, replacement.byteSize)
        assertEquals("queued", replacement.state)
        assertNull(replacement.serverUploadId)
        assertEquals(
            replacement.queueId,
            UploadRecoveryPolicy.itemAfterFreshDigest(
                postDelete,
                "b".repeat(64),
                456,
            ).queueId,
        )
        assertTrue(
            replacement.queueId != UploadRecoveryPolicy.itemAfterFreshDigest(
                postDelete,
                "c".repeat(64),
                456,
            ).queueId,
        )
    }

    @Test fun dedupeAndFullOffsetCompletionRequireASecondExactLocalSourceObservation() {
        val digest = "a".repeat(64)
        // The same predicate gates both an already-present response and the
        // completion call for a resumed session whose server offset is full.
        assertTrue(AlreadyPresentPolicy.matchesCurrentSource(digest, 12, digest, 12))
        assertFalse(
            AlreadyPresentPolicy.matchesCurrentSource(digest, 12, "b".repeat(64), 12)
        )
        assertFalse(AlreadyPresentPolicy.matchesCurrentSource(digest, 12, digest, 13))
        assertFalse(AlreadyPresentPolicy.matchesCurrentSource(null, 12, digest, 12))
        assertEquals(
            UploadRecoveryReason.SOURCE_MISMATCH,
            UploadRecoveryPolicy.reason(
                ApiException(422, "local_source_changed", "changed"), false
            ),
        )
    }

    @Test fun sourceBodyFailuresAreTypedWithoutMisclassifyingNetworkSinkFailures() {
        assertThrows(SourceUnreadableException::class.java) {
            writeSourceRange({ null }, Buffer(), 0, 1)
        }
        assertThrows(SourceUnreadableException::class.java) {
            writeSourceRange({ throw SecurityException("revoked") }, Buffer(), 0, 1)
        }
        assertThrows(SourceUnreadableException::class.java) {
            writeSourceRange({ ByteArrayInputStream(byteArrayOf(1)) }, Buffer(), 2, 1)
        }
        assertThrows(SourceUnreadableException::class.java) {
            writeSourceRange({ ByteArrayInputStream(byteArrayOf(1)) }, Buffer(), 0, 2)
        }
        assertThrows(SourceUnreadableException::class.java) {
            writeSourceRange(
                {
                    object : InputStream() {
                        override fun read(): Int = 0
                        override fun read(buffer: ByteArray, offset: Int, length: Int): Int = 0
                    }
                },
                Buffer(),
                0,
                1,
            )
        }

        val failedSink = object : ForwardingSink(Buffer()) {
            override fun write(source: Buffer, byteCount: Long) {
                throw IOException("network failed")
            }
        }.buffer()
        val sinkFailure = assertThrows(IOException::class.java) {
            writeSourceRange(
                { ByteArrayInputStream(ByteArray(256 * 1024)) },
                failedSink,
                0,
                256 * 1024,
            )
        }
        assertFalse(sinkFailure is SourceUnreadableException)
    }

    @Test fun portalDownloadsEnforceDeclaredAndStreamingSizeBounds() {
        assertTrue(PortalDownloadPolicy.declaredLengthAllowed(-1))
        assertTrue(
            PortalDownloadPolicy.declaredLengthAllowed(PortalDownloadPolicy.MAX_DOWNLOAD_BYTES)
        )
        assertFalse(
            PortalDownloadPolicy.declaredLengthAllowed(
                PortalDownloadPolicy.MAX_DOWNLOAD_BYTES + 1
            )
        )
        val exact = ByteArrayOutputStream()
        assertEquals(
            3L,
            PortalDownloadPolicy.copyBounded(
                ByteArrayInputStream(byteArrayOf(1, 2, 3)), exact, 3
            ),
        )
        assertEquals(3, exact.size())
        val partial = ByteArrayOutputStream()
        assertThrows(DownloadTooLargeException::class.java) {
            PortalDownloadPolicy.copyBounded(
                ByteArrayInputStream(byteArrayOf(1, 2, 3, 4)), partial, 3
            )
        }
        assertTrue(partial.size() <= 3)
    }

    @Test fun pairingOriginIsExactlyCanonical() {
        assertEquals(DavidPiOrigin.ORIGIN, DavidPiOrigin.canonicalPairingOrigin(DavidPiOrigin.ORIGIN))
        assertEquals(DavidPiOrigin.ORIGIN, DavidPiOrigin.canonicalPairingOrigin("${DavidPiOrigin.ORIGIN}/"))

        listOf(
            "http://john-pi.tail123456.ts.net",
            "https://JOHN-PI.tail123456.ts.net",
            "https://john-pi.tail123456.ts.net:443",
            "https://john-pi.tail123456.ts.net:8443",
            "https://user@john-pi.tail123456.ts.net",
            "https://john-pi.tail123456.ts.net.evil.example",
            "https://evil.example/john-pi.tail123456.ts.net",
            "https://john-pi.tail123456.ts.net/api",
            "https://john-pi.tail123456.ts.net/?next=evil",
            "https://john-pi.tail123456.ts.net/#fragment",
            " https://john-pi.tail123456.ts.net",
        ).forEach { assertNull(it, DavidPiOrigin.canonicalPairingOrigin(it)) }
    }

    @Test fun portalAndAudiobookUrlsUseTheSameExactOriginPolicy() {
        assertEquals("${DavidPiOrigin.ORIGIN}/", DavidPiOrigin.canonicalPortalUrl(DavidPiOrigin.ORIGIN))
        assertEquals(
            "${DavidPiOrigin.ORIGIN}/photos?page=2",
            DavidPiOrigin.canonicalPortalUrl("${DavidPiOrigin.ORIGIN}/photos?page=2"),
        )
        assertNull(DavidPiOrigin.canonicalPortalUrl("${DavidPiOrigin.ORIGIN}:444/photos"))
        assertNull(DavidPiOrigin.canonicalPortalUrl("https://user@${DavidPiOrigin.HOST}/photos"))

        val id = "0123456789abcdef0123456789abcdef"
        val exactDownload = "${DavidPiOrigin.ORIGIN}/api/audiobooks/$id/download"
        val exactCover = "${DavidPiOrigin.ORIGIN}/api/audiobooks/$id/cover"
        assertEquals(exactDownload, DavidPiOrigin.canonicalAudiobookDownloadUrl(exactDownload, id))
        assertEquals(exactCover, DavidPiOrigin.canonicalAudiobookCoverUrl(exactCover, id))
        assertEquals("", DavidPiOrigin.canonicalAudiobookCoverUrl("", id))
        assertNull(
            DavidPiOrigin.canonicalAudiobookDownloadUrl(
                "${DavidPiOrigin.ORIGIN}/api/audiobooks/${"f".repeat(32)}/download", id
            )
        )
        for (suffix in listOf("?next=1", "#fragment")) {
            assertNull(DavidPiOrigin.canonicalAudiobookDownloadUrl(exactDownload + suffix, id))
        }
        assertNull(
            DavidPiOrigin.canonicalAudiobookDownloadUrl(
                "https://${DavidPiOrigin.HOST}:443/api/audiobooks/$id/download", id
            )
        )
    }

    @Test fun directManualAndDeepLinkPairingFailClosed() {
        val valid = PairingRequestPolicy.manual(DavidPiOrigin.ORIGIN, "123456", false)
        assertEquals(DavidPiOrigin.ORIGIN, valid?.server)
        assertEquals("123456", valid?.token)
        assertNull(PairingRequestPolicy.manual("https://evil.example", "123456", false))
        assertNull(PairingRequestPolicy.manual(DavidPiOrigin.ORIGIN, "bad token", false))
        assertNull(PairingRequestPolicy.manual(DavidPiOrigin.ORIGIN, "123456", true))

        val canonicalLink = PairingDeepLink(
            scheme = "davidpibackup",
            host = "pair",
            port = -1,
            userInfo = null,
            path = "",
            fragment = null,
            queryNames = setOf("server", "token"),
            serverValues = listOf(DavidPiOrigin.ORIGIN),
            tokenValues = listOf("valid_token_123"),
        )
        assertEquals(
            "valid_token_123",
            PairingRequestPolicy.deepLink(canonicalLink, false)?.token,
        )
        assertNull(PairingRequestPolicy.deepLink(canonicalLink, true))
        assertNull(
            PairingRequestPolicy.deepLink(
                canonicalLink.copy(serverValues = listOf("https://evil.example")), false
            )
        )
        assertNull(
            PairingRequestPolicy.deepLink(
                canonicalLink.copy(serverValues = listOf(DavidPiOrigin.ORIGIN, DavidPiOrigin.ORIGIN)),
                false,
            )
        )
        assertNull(PairingRequestPolicy.deepLink(canonicalLink.copy(path = "/extra"), false))
        assertNull(PairingRequestPolicy.deepLink(canonicalLink.copy(userInfo = "attacker"), false))
        assertNull(PairingRequestPolicy.deepLink(canonicalLink.copy(port = 443), false))
    }

    @Test fun hardenedClientNeverFollowsBodyRedirectsEvenWhenInjectedClientWould() {
        val source = MockWebServer()
        val attacker = MockWebServer()
        try {
            source.start()
            attacker.start()
            val injected = OkHttpClient.Builder()
                .followRedirects(true)
                .followSslRedirects(true)
                .build()
            val hardened = DavidPiHttp.noRedirects(injected)
            assertFalse(hardened.followRedirects)
            assertFalse(hardened.followSslRedirects)
            listOf(301, 302, 303, 307, 308).forEach { redirectStatus ->
                source.enqueue(
                    MockResponse()
                        .setResponseCode(redirectStatus)
                        .setHeader("Location", attacker.url("/capture-$redirectStatus"))
                )
                val request = Request.Builder()
                    .url(source.url("/private-upload-$redirectStatus"))
                    .post("private body".toRequestBody("text/plain".toMediaType()))
                    .build()
                hardened.newCall(request).execute().use { response ->
                    assertEquals(redirectStatus, response.code)
                }
            }
            assertEquals(5, source.requestCount)
            assertEquals(0, attacker.requestCount)
        } finally {
            runCatching { source.shutdown() }
            runCatching { attacker.shutdown() }
        }
    }

    @Test fun portalDownloadPolicyRejectsLegacyAndOffOriginRows() {
        val safe = "${DavidPiOrigin.ORIGIN}/api/files/abc/content?download=1"
        assertEquals(safe, DavidPiOrigin.canonicalPortalDownloadUrl(safe))
        assertNull(DavidPiOrigin.canonicalPortalDownloadUrl("${DavidPiOrigin.ORIGIN}/photos"))
        assertNull(
            DavidPiOrigin.canonicalPortalDownloadUrl(
                "https://${DavidPiOrigin.HOST}:443/api/files/abc/content"
            )
        )
        assertNull(DavidPiOrigin.canonicalPortalDownloadUrl("https://evil.example/api/files/abc"))
        assertTrue(DavidPiOrigin.portalUrl("/").startsWith(DavidPiOrigin.ORIGIN))
    }

    @Test fun portalNavigationRequiresTopLevelUserGestureAndWebScheme() {
        assertEquals(
            PortalNavigationDecision.ALLOW_IN_WEBVIEW,
            PortalNavigationPolicy.decide("${DavidPiOrigin.ORIGIN}/photos", true, false),
        )
        assertEquals(
            PortalNavigationDecision.ALLOW_IN_WEBVIEW,
            PortalNavigationPolicy.decide("${DavidPiOrigin.ORIGIN}/embed", false, false),
        )
        for (scheme in listOf("https", "http")) {
            val external = "$scheme://example.com/help"
            assertEquals(
                PortalNavigationDecision.OPEN_EXTERNAL,
                PortalNavigationPolicy.decide(external, true, true),
            )
            assertEquals(
                PortalNavigationDecision.BLOCK,
                PortalNavigationPolicy.decide(external, true, false),
            )
            assertEquals(
                PortalNavigationDecision.BLOCK,
                PortalNavigationPolicy.decide(external, false, true),
            )
        }
        listOf(
            "file:///etc/passwd",
            "content://attacker/private",
            "javascript:alert(1)",
            "intent://attacker/#Intent;scheme=https;end",
            "davidpibackup://pair?server=${DavidPiOrigin.ORIGIN}",
            "custom://example.com/path",
            "https://user@example.com/path",
        ).forEach {
            assertEquals(
                it,
                PortalNavigationDecision.BLOCK,
                PortalNavigationPolicy.decide(it, true, true),
            )
        }
        assertTrue(PortalNavigationPolicy.isBackupShortcut("davidpi://backup", true, true))
        assertFalse(PortalNavigationPolicy.isBackupShortcut("davidpi://backup", false, true))
        assertFalse(PortalNavigationPolicy.isBackupShortcut("davidpi://backup", true, false))
        assertFalse(PortalNavigationPolicy.isBackupShortcut("davidpi://backup/path", true, true))
    }
}
