package com.davidpi.backup

import com.davidpi.backup.net.DavidPiOrigin
import com.davidpi.backup.net.DavidPiHttp
import com.davidpi.backup.security.HouseholdScope
import okhttp3.OkHttpClient
import okhttp3.Request
import org.junit.Assert.*
import org.junit.Test

class HouseholdSessionPolicyTest {
    @Test fun candidateServerNeverGrantsPortalTrust() {
        DavidPiOrigin.disconnect()
        val origin = "https://john-pi.tail123456.ts.net"
        assertEquals(origin, DavidPiOrigin.canonicalPairingOrigin(origin))
        assertNull(DavidPiOrigin.canonicalPortalUrl("$origin/api/files"))
        DavidPiOrigin.approve(origin, "john")
        assertNotNull(DavidPiOrigin.canonicalPortalUrl("$origin/api/files"))
        assertNull(DavidPiOrigin.canonicalPortalUrl("https://jane-pi.tail123456.ts.net/api/files"))
        assertNull(DavidPiOrigin.canonicalPortalUrl("https://john-pi.tail999999.ts.net/api/files"))
        DavidPiOrigin.disconnect()
        assertNull(DavidPiOrigin.canonicalPortalUrl("$origin/api/files"))
    }

    @Test fun hostnameDoesNotDetermineHouseholdDataIdentity() {
        val installation = "ba7d3d57-9844-4053-bbf0-8ee2aaf5b1d1"
        assertEquals(HouseholdScope.key(installation, "member-a"), HouseholdScope.key(installation, "member-a"))
        assertNotEquals(HouseholdScope.key(installation, "member-a"), HouseholdScope.key(installation, "member-b"))
        assertNotEquals(HouseholdScope.key(installation, "member-a"), HouseholdScope.key("ba7d3d57-9844-4053-bbf0-8ee2aaf5b1d2", "member-a"))
        assertFalse(HouseholdScope.validIdentity("", "member-a"))
        assertFalse(HouseholdScope.validIdentity(installation, "a\nb"))
    }

    @Test fun staleClientRejectsNewPersonBeforeAnyNetworkRequest() {
        val origin = "https://john-pi.tail123456.ts.net"
        DavidPiOrigin.approve(origin, "old-person")
        val old = DavidPiHttp.forSession(OkHttpClient())
        DavidPiOrigin.approve(origin, "new-person")
        assertThrows(java.io.IOException::class.java) {
            old.newCall(Request.Builder().url("$origin/api/private").build()).execute()
        }
        assertFalse(old.followRedirects)
        assertFalse(old.followSslRedirects)
    }
}
