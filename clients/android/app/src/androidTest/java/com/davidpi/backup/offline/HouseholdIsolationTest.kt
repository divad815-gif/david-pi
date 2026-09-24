package com.davidpi.backup.offline

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.davidpi.backup.net.DavidPiOrigin
import com.davidpi.backup.security.HouseholdScope
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class HouseholdIsolationTest {
    @Test fun reconnectRetainsAudioWhileAnotherPersonGetsASeparateLibrary() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val installation = UUID.randomUUID().toString()
        val john = HouseholdScope.key(installation, "john")
        val jane = HouseholdScope.key(installation, "jane")
        val id = "a".repeat(32)
        DavidPiOrigin.approve("https://john-pi.tail123456.ts.net", john)
        val johnDatabase = OfflineAudiobookDatabase.get(context)
        val original = OfflineAudiobook(id, "John's book", "Author", "", 200.0, 3,
            contentType = "audio/mpeg", remoteUrl = "", coverUrl = "", localFileName = "$id.audio",
            state = "ready", positionSeconds = 17.0)
        johnDatabase.dao().upsert(original)
        val audio = OfflineAudiobookFiles.final(context, id)
        audio.writeBytes(byteArrayOf(1, 2, 3))
        try {
            DavidPiOrigin.disconnect()
            DavidPiOrigin.approve("https://john-pi.tail123456.ts.net", jane)
            assertNull(OfflineAudiobookDatabase.get(context).dao().get(id))
            assertFalse(OfflineAudiobookFiles.final(context, id).exists())
            DavidPiOrigin.disconnect()
            DavidPiOrigin.approve("https://renamed-pi.tail654321.ts.net", john)
            assertEquals(original, OfflineAudiobookDatabase.get(context).dao().get(id))
            assertArrayEquals(byteArrayOf(1, 2, 3), OfflineAudiobookFiles.final(context, id).readBytes())
        } finally {
            audio.delete()
            johnDatabase.dao().delete(id)
            DavidPiOrigin.disconnect()
        }
    }
}
