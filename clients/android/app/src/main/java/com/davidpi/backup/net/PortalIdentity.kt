package com.davidpi.backup.net

import android.content.Context
import com.davidpi.backup.security.CredentialStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Tailscale's browser identity must match the person who explicitly paired this app. */
object PortalIdentity {
    suspend fun verify(context: Context) = withContext(Dispatchers.IO) {
        val credentials = CredentialStore(context)
        val expectedScope = credentials.scope
        if (expectedScope == "unpaired" || expectedScope != DavidPiOrigin.sessionScope) {
            throw IOException("Pair this household first.")
        }
        val client = DavidPiHttp.forSession(OkHttpClient.Builder()
            .callTimeout(20, TimeUnit.SECONDS).build())
        client.newCall(Request.Builder().url(DavidPiOrigin.apiUrl("/api/installation")).get().build())
            .execute().use { response ->
                if (!response.isSuccessful) throw IOException("Open Tailscale with the person who paired this app.")
                val identity = JSONObject(response.body?.string() ?: "{}")
                if (identity.optString("installation_id") != credentials.instanceId ||
                    identity.optString("member_id") != credentials.memberId ||
                    expectedScope != DavidPiOrigin.sessionScope) {
                    throw IOException("Tailscale is connected as a different household or person. Reconnect explicitly before saving content.")
                }
            }
    }
}
