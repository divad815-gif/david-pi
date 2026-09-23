package com.davidpi.backup.net

import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.net.URI

enum class PortalNavigationDecision { ALLOW_IN_WEBVIEW, OPEN_EXTERNAL, BLOCK }

/** Keep bridge installation fail-closed if the second WebView registration fails. */
object PortalBridgeInstallPolicy {
    fun <T> install(
        addListener: () -> Unit,
        addScript: () -> T,
        removeListener: () -> Unit,
    ): T? {
        var listenerInstalled = false
        return try {
            addListener()
            listenerInstalled = true
            addScript()
        } catch (_: RuntimeException) {
            if (listenerInstalled) runCatching(removeListener)
            null
        }
    }
}

/** The one network origin trusted by the native David-Pi application. */
object DavidPiOrigin {
    @Volatile private var approvedOrigin: String? = null
    @Volatile var sessionScope: String = "unpaired"
        private set
    val ORIGIN: String get() = approvedOrigin ?: "https://unpaired.invalid"
    val HOST: String get() = URI(ORIGIN).host

    /** Candidate validation is not trust: only an explicit pairing or saved credential may approve it. */
    fun approve(value: String, scope: String) {
        approvedOrigin = requireNotNull(canonicalPairingOrigin(value))
        sessionScope = scope
    }
    fun disconnect() { approvedOrigin = null; sessionScope = "unpaired" }
    fun isApproved(value: String): Boolean = approvedOrigin == value


    private fun exactOriginUrl(value: String): Pair<HttpUrl, URI>? {
        if (approvedOrigin == null) return null
        val raw = runCatching { URI(value) }.getOrNull() ?: return null
        val parsed = value.toHttpUrlOrNull() ?: return null
        if (
            parsed.scheme != "https" ||
            parsed.host != HOST ||
            parsed.port != 443 ||
            parsed.username.isNotEmpty() ||
            parsed.password.isNotEmpty() ||
            raw.scheme != "https" ||
            raw.rawAuthority != HOST ||
            raw.rawUserInfo != null ||
            raw.port != -1 ||
            raw.host != HOST ||
            raw.isOpaque
        ) return null
        return parsed to raw
    }

    /** Accept the canonical pairing origin, optionally with its single root slash. */
    fun canonicalPairingOrigin(value: String?): String? {
        if (value == null) return null
        val raw = runCatching { URI(value) }.getOrNull() ?: return null
        val parsed = value.toHttpUrlOrNull() ?: return null
        val host = raw.host ?: return null
        val label = "[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if (!Regex("$label\\.$label\\.ts\\.net").matches(host)) return null
        if (raw.scheme != "https" || raw.rawAuthority != host || raw.port != -1 ||
            raw.rawUserInfo != null || raw.isOpaque || raw.rawPath !in setOf("", "/") ||
            raw.rawQuery != null || raw.rawFragment != null || parsed.host != host ||
            parsed.scheme != "https" || parsed.port != 443 || parsed.encodedPath != "/") return null
        return "https://$host"
    }

    /** Canonicalize a portal URL while retaining only exact-origin paths and parameters. */
    fun canonicalPortalUrl(value: String): String? {
        val (parsed, raw) = exactOriginUrl(value) ?: return null
        if (
            raw.rawPath.isNotEmpty() && !raw.rawPath.startsWith("/") ||
            raw.normalize().rawPath != raw.rawPath
        ) return null
        return parsed.toString()
    }

    /** Limit native file transfers to exact-origin API GET endpoints. */
    fun canonicalPortalDownloadUrl(value: String): String? {
        val canonical = canonicalPortalUrl(value) ?: return null
        val parsed = canonical.toHttpUrlOrNull() ?: return null
        if (!parsed.encodedPath.startsWith("/api/") || parsed.fragment != null) return null
        return canonical
    }

    fun portalUrl(path: String): String {
        val suffix = if (path.startsWith('/')) path else "/$path"
        return canonicalPortalUrl("$ORIGIN$suffix") ?: ORIGIN
    }

    fun apiUrl(path: String): String {
        require(path.startsWith("/api/") && '?' !in path && '#' !in path && ".." !in path) {
            "David-Pi API path is invalid."
        }
        return "$ORIGIN$path"
    }

    fun canonicalAudiobookDownloadUrl(value: String, bookId: String): String? {
        val (parsed, raw) = exactOriginUrl(value) ?: return null
        val expectedPath = "/api/audiobooks/$bookId/download"
        if (
            raw.rawPath != expectedPath ||
            parsed.encodedPath != expectedPath ||
            raw.rawQuery != null ||
            raw.rawFragment != null ||
            parsed.query != null ||
            parsed.fragment != null
        ) return null
        return "$ORIGIN$expectedPath"
    }

    fun canonicalAudiobookCoverUrl(value: String, bookId: String): String? {
        if (value.isBlank()) return ""
        val (parsed, raw) = exactOriginUrl(value) ?: return null
        val expectedPath = "/api/audiobooks/$bookId/cover"
        if (
            raw.rawPath != expectedPath ||
            parsed.encodedPath != expectedPath ||
            raw.rawQuery != null ||
            raw.rawFragment != null ||
            parsed.query != null ||
            parsed.fragment != null
        ) return null
        return "$ORIGIN$expectedPath"
    }
}

/** Rebuild even injected clients so no caller can silently re-enable redirects. */
object DavidPiHttp {
    /** Capture identity when the client is constructed; stale work may never cross household boundaries. */
    fun forSession(client: OkHttpClient): OkHttpClient {
        val scope = DavidPiOrigin.sessionScope
        val origin = DavidPiOrigin.ORIGIN
        return noRedirects(client).newBuilder().addInterceptor { chain ->
            if (DavidPiOrigin.sessionScope != scope || !DavidPiOrigin.isApproved(origin) ||
                DavidPiOrigin.canonicalPortalUrl(chain.request().url.toString()) == null) {
                throw java.io.IOException("The household connection changed. Reopen this operation.")
            }
            chain.proceed(chain.request())
        }.build()
    }

    fun noRedirects(client: OkHttpClient): OkHttpClient = client.newBuilder()
        .followRedirects(false)
        .followSslRedirects(false)
        .build()
}

/** Pure WebView navigation policy: native launches require a deliberate top-level web link. */
object PortalNavigationPolicy {
    fun decide(
        value: String,
        isMainFrame: Boolean,
        hasUserGesture: Boolean,
    ): PortalNavigationDecision {
        if (DavidPiOrigin.canonicalPortalUrl(value) != null) {
            return PortalNavigationDecision.ALLOW_IN_WEBVIEW
        }
        if (!isMainFrame || !hasUserGesture) return PortalNavigationDecision.BLOCK
        val uri = runCatching { URI(value) }.getOrNull() ?: return PortalNavigationDecision.BLOCK
        if (
            uri.scheme !in setOf("http", "https") ||
            uri.host.isNullOrBlank() ||
            uri.rawAuthority.isNullOrBlank() ||
            uri.rawUserInfo != null ||
            uri.isOpaque
        ) return PortalNavigationDecision.BLOCK
        return PortalNavigationDecision.OPEN_EXTERNAL
    }

    fun isBackupShortcut(value: String, isMainFrame: Boolean, hasUserGesture: Boolean): Boolean =
        isMainFrame && hasUserGesture && value == "davidpi://backup"
}

data class PairingRequest(val server: String, val token: String)

data class PairingDeepLink(
    val scheme: String?,
    val host: String?,
    val port: Int,
    val userInfo: String?,
    val path: String?,
    val fragment: String?,
    val queryNames: Set<String>,
    val serverValues: List<String>,
    val tokenValues: List<String>,
)

/** Pure policy shared by manual pairing and the exported custom-scheme entry point. */
object PairingRequestPolicy {
    private val tokenPattern = Regex("^[A-Za-z0-9_-]{6,128}$")

    fun manual(server: String?, token: String?, alreadyPaired: Boolean): PairingRequest? {
        if (alreadyPaired) return null
        val canonical = DavidPiOrigin.canonicalPairingOrigin(server) ?: return null
        val safeToken = token?.takeIf(tokenPattern::matches) ?: return null
        return PairingRequest(canonical, safeToken)
    }

    fun deepLink(link: PairingDeepLink, alreadyPaired: Boolean): PairingRequest? {
        if (
            link.scheme != "davidpibackup" ||
            link.host != "pair" ||
            link.port != -1 ||
            link.userInfo != null ||
            !link.path.isNullOrEmpty() ||
            link.fragment != null ||
            link.queryNames != setOf("server", "token") ||
            link.serverValues.size != 1 ||
            link.tokenValues.size != 1
        ) return null
        return manual(link.serverValues.single(), link.tokenValues.single(), alreadyPaired)
    }
}
