package com.davidpi.backup

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TrustedPortalBridgePolicyTest {
    @Test
    fun offlineRequestUsesBoundedAsyncReplyInsteadOfOptimisticQueuedClaim() {
        val script = trustedBridgeBootstrap(
            pushConfigured = false,
            notificationsEnabled = false,
        )

        assertTrue(script.contains("pending.size >= 16"))
        assertTrue(script.contains("pending.set(requestId"))
        assertTrue(script.contains("response.request_id"))
        assertTrue(script.contains("entry.resolve(response.result)"))
        assertTrue(script.contains("saveAudiobook: value => request('offline', 'saveAudiobook'"))
        assertFalse(script.contains("saveAudiobook: value => {"))
        assertFalse(script.contains("return 'queued'"))
    }
}
