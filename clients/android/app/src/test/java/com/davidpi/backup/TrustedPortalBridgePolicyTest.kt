package com.davidpi.backup

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test

class TrustedPortalBridgePolicyTest {
    @Test
    fun householdPageWaitsForActualJavascriptAndSecureBridgeSupport() {
        var reply: ((String) -> Unit)? = null
        var opened = 0
        var guidance = 0
        var bridgeSupported = true
        fun check() = checkPortalWebView(
            evaluate = { _, callback -> reply = callback },
            bridgeSupported = { bridgeSupported },
            ready = { opened += 1 },
            updateRequired = { guidance += 1 },
        )

        check()
        assertEquals(0, opened)
        assertEquals(0, guidance)
        requireNotNull(reply)("false") // Old JavaScript engine, even if bridge APIs exist.
        assertEquals(0, opened)
        assertEquals(1, guidance)

        bridgeSupported = false
        check()
        requireNotNull(reply)("true")
        assertEquals(0, opened)
        assertEquals(2, guidance)

        bridgeSupported = true
        check()
        requireNotNull(reply)("null") // A missing/failed probe never opens the page.
        assertEquals(0, opened)
        assertEquals(3, guidance)

        check()
        requireNotNull(reply)("true")
        assertEquals(1, opened)
        assertEquals(3, guidance)
    }

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
