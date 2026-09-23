package com.davidpi.backup

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.davidpi.backup.security.CredentialStore
import com.davidpi.backup.security.HouseholdScope
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class HouseholdCredentialTest {
    @Test fun oldSessionCannotClearNewlyPairedPersonsEncryptedCredential() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val store = CredentialStore(context)
        val installation = UUID.randomUUID().toString()
        store.clear()
        try {
            store.serverUrl = "https://john-pi.tail123456.ts.net"
            store.deviceId = "new-device"
            store.saveCredential("instrumentation-fixture-only")
            store.bindIdentity(installation, "new-person", "John's home")
            store.clear(HouseholdScope.key(installation, "previous-person"))
            assertEquals("instrumentation-fixture-only", store.credential())
            store.clear(HouseholdScope.key(installation, "new-person"), "old-device")
            assertEquals("instrumentation-fixture-only", store.credential())
            assertTrue(store.restoreOrigin())
            assertEquals("John's home", store.displayName)
            store.clear(HouseholdScope.key(installation, "new-person"))
            assertNull(store.credential())
            assertFalse(store.restoreOrigin())
        } finally { store.clear() }
    }
}
