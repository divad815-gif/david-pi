package com.davidpi.backup

import androidx.lifecycle.compose.collectAsStateWithLifecycle

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.provider.Settings
import android.webkit.CookieManager
import android.webkit.DownloadListener
import android.webkit.SslErrorHandler
import android.net.http.SslError
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.app.NotificationManagerCompat
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.davidpi.backup.offline.OfflineAudiobookBridge
import com.davidpi.backup.offline.OfflineAudiobookScreen
import com.davidpi.backup.net.DavidPiOrigin
import com.davidpi.backup.net.PortalBridgeInstallPolicy
import com.davidpi.backup.net.PortalNavigationDecision
import com.davidpi.backup.net.PortalNavigationPolicy
import com.davidpi.backup.net.PortalDownloadWorker
import org.json.JSONArray
import org.json.JSONObject

private const val TRUSTED_BRIDGE_NAME = "DavidPiNativeBridge"

internal fun checkPortalWebView(
    evaluate: (String, (String) -> Unit) -> Unit,
    bridgeSupported: () -> Boolean,
    ready: () -> Unit,
    updateRequired: () -> Unit,
) {
    // Probe the actual engine on about:blank before loading household content.
    // The outer script also parses in older engines, which report false safely.
    evaluate("""(function(){try{return new Function("return globalThis && (({value:1})?.value ?? 0) === 1;")();}catch(error){return false;}})()""") { result ->
        if (result == "true" && bridgeSupported()) ready() else updateRequired()
    }
}

private enum class PortalWebViewState { CHECKING, READY, UPDATE_REQUIRED }

private class TrustedPortalBridgeHandle(
    private val webView: WebView,
    private val script: androidx.webkit.ScriptHandler,
    private val offline: OfflineAudiobookBridge,
) {
    fun release() {
        offline.release()
        runCatching { script.remove() }
        runCatching { WebViewCompat.removeWebMessageListener(webView, TRUSTED_BRIDGE_NAME) }
    }
}

private class ChatPushBridge(private val webView: WebView) {
    private fun evaluate(script: String) {
        webView.post {
            if (DavidPiOrigin.canonicalPortalUrl(webView.url.orEmpty()) != null) {
                webView.evaluateJavascript(script, null)
            }
        }
    }

    fun isConfigured(): Boolean = false
    fun notificationsEnabled(): Boolean = false
    fun openNotificationSettings() = Unit
    fun requestToken() = Unit
    fun retireToken(expectedToken: String) = Unit

}

private class TrustedPortalMessageListener(
    private val media: NativeMediaBridge,
    private val push: ChatPushBridge,
    private val offline: OfflineAudiobookBridge,
) : WebViewCompat.WebMessageListener {
    override fun onPostMessage(
        view: WebView,
        message: WebMessageCompat,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
        replyProxy: JavaScriptReplyProxy,
    ) {
        if (
            !isMainFrame ||
            !DavidPiOrigin.isApproved(DavidPiOrigin.canonicalPairingOrigin(sourceOrigin.toString()).orEmpty())
        ) return
        val raw = message.data ?: return
        if (raw.length > 3_000_000) return
        val payload = runCatching { JSONObject(raw) }.getOrNull() ?: return
        val channel = payload.optString("channel")
        val method = payload.optString("method")
        val arguments = payload.optJSONArray("args") ?: JSONArray()
        fun string(index: Int, limit: Int): String =
            arguments.optString(index, "").take(limit)
        fun number(index: Int, fallback: Double = 0.0): Double =
            arguments.optDouble(index, fallback).takeIf(Double::isFinite) ?: fallback

        when (channel to method) {
            "media" to "setMetadata" -> media.setMetadata(string(0, 180), string(1, 180))
            "media" to "setArtworkDataUrl" -> media.setArtworkDataUrl(string(0, 2_800_000))
            "media" to "clearArtwork" -> media.clearArtwork()
            "media" to "updatePlayback" -> media.updatePlayback(
                arguments.optBoolean(0, false),
                number(1),
                number(2),
                number(3, 1.0),
            )
            "media" to "clear" -> media.clear()
            "push" to "openNotificationSettings" -> push.openNotificationSettings()
            "push" to "requestToken" -> push.requestToken()
            "push" to "retireToken" -> push.retireToken(string(0, 4096))
            "offline" to "saveAudiobook", "offline" to "progress" -> {
                val request = string(0, 32_000)
                val requestId = payload.optString("request_id")
                    .takeIf { it.matches(Regex("[A-Za-z0-9-]{1,80}")) }
                if (request.isNotBlank() && requestId != null) {
                    val respond: (String) -> Unit = { result ->
                        val response = JSONObject()
                            .put("request_id", requestId)
                            .put("result", result)
                            .toString()
                        view.post { runCatching { replyProxy.postMessage(response) } }
                    }
                    if (method == "progress") offline.progress(request, respond)
                    else offline.saveAudiobook(request, respond)
                }
            }
        }
    }
}

internal fun trustedBridgeBootstrap(
    pushConfigured: Boolean,
    notificationsEnabled: Boolean,
): String {
    val configured = pushConfigured.toString()
    val notifications = notificationsEnabled.toString()
    return """
        (() => {
          const bridge = globalThis.$TRUSTED_BRIDGE_NAME;
          if (!bridge || typeof bridge.postMessage !== 'function') return;
          let requestSequence = 0;
          const pending = new Map();
          bridge.onmessage = event => {
            try {
              const response = JSON.parse(String(event.data || ''));
              const entry = pending.get(response.request_id);
              if (!entry || typeof response.result !== 'string') return;
              pending.delete(response.request_id);
              clearTimeout(entry.timer);
              entry.resolve(response.result);
            } catch (_error) {}
          };
          const send = (channel, method, args = [], requestId = null) => {
            const payload = {channel, method, args};
            if (requestId) payload.request_id = requestId;
            bridge.postMessage(JSON.stringify(payload));
          };
          const request = (channel, method, args = []) => {
            if (pending.size >= 16) return Promise.reject(new Error('Android is busy.'));
            const requestId = `offline-${'$'}{Date.now()}-${'$'}{++requestSequence}`;
            return new Promise((resolve, reject) => {
              const timer = setTimeout(() => {
                pending.delete(requestId);
                reject(new Error('Android did not confirm the request.'));
              }, 30000);
              pending.set(requestId, {resolve, timer});
              try { send(channel, method, args, requestId); }
              catch (error) { clearTimeout(timer); pending.delete(requestId); reject(error); }
            });
          };
          Object.defineProperty(globalThis, 'DavidPiMedia', {value: Object.freeze({
            setMetadata: (title, author) => send('media', 'setMetadata', [title, author]),
            setArtworkDataUrl: value => send('media', 'setArtworkDataUrl', [value]),
            clearArtwork: () => send('media', 'clearArtwork'),
            updatePlayback: (playing, seconds, duration, rate) =>
              send('media', 'updatePlayback', [playing, seconds, duration, rate]),
            clear: () => send('media', 'clear')
          }), configurable: false});
          Object.defineProperty(globalThis, 'DavidPiPush', {value: Object.freeze({
            isConfigured: () => $configured,
            notificationsEnabled: () => $notifications,
            openNotificationSettings: () => send('push', 'openNotificationSettings'),
            requestToken: () => send('push', 'requestToken'),
            retireToken: token => send('push', 'retireToken', [token])
          }), configurable: false});
          Object.defineProperty(globalThis, 'DavidPiOffline', {value: Object.freeze({
            saveAudiobook: value => request('offline', 'saveAudiobook', [value]),
            progress: value => request('offline', 'progress', [value])
          }), configurable: false});
        })();
    """.trimIndent()
}

private fun installTrustedPortalBridge(
    webView: WebView,
    media: NativeMediaBridge,
    push: ChatPushBridge,
    offline: OfflineAudiobookBridge,
): TrustedPortalBridgeHandle? {
    if (
        !WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) ||
        !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)
    ) return null
    val origins = setOf(DavidPiOrigin.ORIGIN)
    val script = PortalBridgeInstallPolicy.install(
        addListener = {
            WebViewCompat.addWebMessageListener(
                webView,
                TRUSTED_BRIDGE_NAME,
                origins,
                TrustedPortalMessageListener(media, push, offline),
            )
        },
        addScript = {
            WebViewCompat.addDocumentStartJavaScript(
                webView,
                trustedBridgeBootstrap(push.isConfigured(), push.notificationsEnabled()),
                origins,
            )
        },
        removeListener = {
            WebViewCompat.removeWebMessageListener(webView, TRUSTED_BRIDGE_NAME)
        },
    ) ?: return null
    return TrustedPortalBridgeHandle(webView, script, offline)
}

private enum class Destination(val label: String) {
    HOME("Home"), MEDIA("Media"), BACKUP("Backup"), MORE("More"), PAGE("More"), OFFLINE("Offline")
}

@Composable
fun DavidPiApp(model: MainViewModel, notificationPath: String = "") {
    val session by model.state.collectAsStateWithLifecycle()
    if (!session.paired) {
        DavidPiBackupScreen(model)
        return
    }
    var destination by remember { mutableStateOf(Destination.HOME) }
    var portalPath by remember { mutableStateOf("/") }
    var navigationRequest by remember { mutableIntStateOf(0) }

    fun open(path: String, tab: Destination) {
        portalPath = path
        destination = tab
        // A portal link can move the WebView away from the native tab's last
        // target without changing Compose state. Incrementing this request on
        // every native navigation makes Home/Media/More deterministic even
        // when the requested path string itself has not changed.
        navigationRequest += 1
    }

    LaunchedEffect(notificationPath) {
        if (notificationPath.startsWith("/chat")) open(notificationPath, Destination.PAGE)
    }

    MaterialTheme(
        colorScheme = androidx.compose.material3.lightColorScheme(
            primary = Color(0xFFC95D43), secondary = Color(0xFF4DA86A),
            background = Color(0xFFFFF9EF), surface = Color(0xFFFFF9EF),
            onBackground = Color(0xFF241F19)
        )
    ) {
        Scaffold(
            containerColor = MaterialTheme.colorScheme.background,
            bottomBar = {
                NavigationBar {
                    listOf(Destination.HOME, Destination.MEDIA, Destination.BACKUP, Destination.MORE).filter { it != Destination.MEDIA || "media" in session.enabledModules }.forEach { item ->
                        NavigationBarItem(
                            selected = destination == item ||
                                (item == Destination.MORE && destination == Destination.PAGE),
                            onClick = {
                                when (item) {
                                    Destination.HOME -> open("/", item)
                                    Destination.MEDIA -> open("/photos", item)
                                    else -> destination = item
                                }
                            },
                            icon = { Text(when (item) {
                                Destination.HOME -> "⌂"
                                Destination.MEDIA -> "▦"
                                Destination.BACKUP -> "⇧"
                                Destination.MORE -> "•••"
                                Destination.PAGE -> "•••"
                                Destination.OFFLINE -> "▶"
                            }) },
                            label = { Text(if (item == Destination.BACKUP && "device_backup" !in session.enabledModules) "Connection" else item.label) }
                        )
                    }
                }
            }
        ) { padding ->
            when (destination) {
                Destination.BACKUP -> DavidPiBackupScreen(model, padding)
                Destination.MORE -> MoreScreen(
                    padding = padding,
                    enabledModules = session.enabledModules,
                    open = { path -> open(path, Destination.PAGE) },
                    openOffline = { destination = Destination.OFFLINE },
                )
                Destination.OFFLINE -> OfflineAudiobookScreen(padding)
                else -> PortalScreen(
                    path = portalPath,
                    navigationRequest = navigationRequest,
                    padding = padding,
                    onOpenBackup = { destination = Destination.BACKUP }
                )
            }
        }
    }
}

@Composable
private fun MoreScreen(
    padding: PaddingValues,
    enabledModules: Set<String>,
    open: (String) -> Unit,
    openOffline: () -> Unit,
) {
    Column(
        Modifier.fillMaxSize().padding(padding).verticalScroll(rememberScrollState()).padding(22.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text(com.davidpi.backup.security.CredentialStore(androidx.compose.ui.platform.LocalContext.current).displayName, color = MaterialTheme.colorScheme.primary)
        Text("Everything at home", style = MaterialTheme.typography.headlineMedium)
        Text("App ${BuildConfig.VERSION_NAME}", color = MaterialTheme.colorScheme.secondary)
        if ("audiobooks" in enabledModules) Button(onClick = openOffline, modifier = Modifier.fillMaxWidth()) {
            Text("Offline audiobooks")
        }
        listOf(
            "Assistant" to "/assistant", "Notes" to "/notes",
            "Chat" to "/chat",
            "Movie Night" to "/movies", "Recipes" to "/recipes",
            "Files" to "/files", "Audiobooks" to "/audiobooks",
            "Games" to "/games",
            "Date Night" to "/places", "Server status" to "/status"
        ).filter { (_, path) -> path == "/status" || path.removePrefix("/") in enabledModules }.forEach { (label, path) ->
            Button(onClick = { open(path) }, modifier = Modifier.fillMaxWidth()) {
                Text(label)
            }
        }
    }
}

@Composable
private fun PortalScreen(
    path: String,
    navigationRequest: Int,
    padding: PaddingValues,
    onOpenBackup: () -> Unit
) {
    val context = LocalContext.current
    var webView by remember { mutableStateOf<WebView?>(null) }
    var mediaBridge by remember { mutableStateOf<NativeMediaBridge?>(null) }
    var trustedBridge by remember { mutableStateOf<TrustedPortalBridgeHandle?>(null) }
    var compatibility by remember { mutableStateOf(PortalWebViewState.CHECKING) }
    var failed by remember { mutableStateOf(false) }
    var pendingFileCallback by remember { mutableStateOf<ValueCallback<Array<Uri>>?>(null) }
    val filePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        pendingFileCallback?.onReceiveValue(uris.toTypedArray())
        pendingFileCallback = null
    }
    val target = remember(path) { DavidPiOrigin.portalUrl(path) }
    var appliedNavigationRequest by remember { mutableIntStateOf(navigationRequest) }

    BackHandler(enabled = webView?.canGoBack() == true) { webView?.goBack() }
    DisposableEffect(Unit) {
        onDispose {
            trustedBridge?.release()
            trustedBridge = null
            mediaBridge?.release()
            // Android may keep one renderer process behind several WebViews. An
            // immediate destroy while changing native tabs was terminating that
            // renderer just as the next portal page opened, producing a blank
            // Chat page. Stop work and detach the bridge; AndroidView owns the
            // actual view lifetime and will release it after composition removal.
            webView?.stopLoading()
            webView = null
        }
    }

    // Android 15's edge-to-edge window can overlay the IME even when the
    // manifest requests adjustResize. Apply Compose's live IME inset to the
    // WebView itself so page composers remain above the keyboard.
    Box(Modifier.fillMaxSize().padding(padding).imePadding()) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                WebView(ctx).apply {
                    webView = this
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    // A lock-screen MediaSession callback is not a touchscreen
                    // gesture. Permit it to resume an already-selected book.
                    settings.mediaPlaybackRequiresUserGesture = false
                    // Portal assets carry release versions, so normal validation keeps repeat
                    // visits quick without making APIs, media, or private catalogs persistent.
                    settings.cacheMode = WebSettings.LOAD_DEFAULT
                    settings.allowFileAccess = false
                    settings.allowContentAccess = false
                    settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                    settings.setSupportMultipleWindows(false)
                    if (android.os.Build.VERSION.SDK_INT >= 26) settings.safeBrowsingEnabled = true
                    CookieManager.getInstance().setAcceptCookie(true)
                    CookieManager.getInstance().setAcceptThirdPartyCookies(this, false)
                    webViewClient = object : WebViewClient() {
                        override fun shouldOverrideUrlLoading(
                            view: WebView,
                            request: WebResourceRequest
                        ): Boolean {
                            val uri = request.url
                            if (PortalNavigationPolicy.isBackupShortcut(
                                uri.toString(), request.isForMainFrame, request.hasGesture()
                            )) {
                                onOpenBackup()
                                return true
                            }
                            return when (PortalNavigationPolicy.decide(
                                uri.toString(), request.isForMainFrame, request.hasGesture()
                            )) {
                                PortalNavigationDecision.ALLOW_IN_WEBVIEW -> false
                                PortalNavigationDecision.OPEN_EXTERNAL -> {
                                    runCatching { ctx.startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                                    true
                                }
                                PortalNavigationDecision.BLOCK -> true
                            }
                        }

                        override fun onPageStarted(
                            view: WebView,
                            url: String,
                            favicon: Bitmap?,
                        ) {
                            if (compatibility == PortalWebViewState.CHECKING && url == "about:blank") return
                            if (DavidPiOrigin.canonicalPortalUrl(url) == null) {
                                view.stopLoading()
                                failed = true
                                return
                            }
                            super.onPageStarted(view, url, favicon)
                        }

                        override fun onPageFinished(view: WebView, url: String) {
                            if (compatibility == PortalWebViewState.CHECKING && url == "about:blank") {
                                checkPortalWebView(
                                    evaluate = { script, callback -> view.evaluateJavascript(script, callback) },
                                    bridgeSupported = {
                                        WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) &&
                                            WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)
                                    },
                                    ready = {
                                        if (webView !== view) return@checkPortalWebView
                                        val nativeMedia = NativeMediaBridge(ctx, view)
                                        val nativeOffline = OfflineAudiobookBridge(ctx)
                                        val installed = installTrustedPortalBridge(view, nativeMedia, ChatPushBridge(view), nativeOffline)
                                        if (installed == null) {
                                            nativeMedia.release()
                                            nativeOffline.release()
                                            compatibility = PortalWebViewState.UPDATE_REQUIRED
                                        } else {
                                            trustedBridge = installed
                                            mediaBridge = nativeMedia
                                            compatibility = PortalWebViewState.READY
                                            view.loadUrl(target)
                                        }
                                    },
                                    updateRequired = {
                                        if (webView === view) compatibility = PortalWebViewState.UPDATE_REQUIRED
                                    },
                                )
                                return
                            }
                            if (DavidPiOrigin.canonicalPortalUrl(url) == null) {
                                view.stopLoading()
                                failed = true
                                return
                            }
                            // Do not force a JavaScript reload here: navigation away from this
                            // WebView can otherwise destroy its renderer while it is in flight.
                            failed = false
                        }

                        override fun onReceivedError(
                            view: WebView,
                            request: WebResourceRequest,
                            error: WebResourceError
                        ) {
                            if (request.isForMainFrame) failed = true
                        }

                        override fun onReceivedSslError(
                            view: WebView,
                            handler: SslErrorHandler,
                            error: SslError
                        ) {
                            handler.cancel()
                            failed = true
                        }
                    }
                    webChromeClient = object : WebChromeClient() {
                        override fun onShowFileChooser(
                            webView: WebView,
                            filePathCallback: ValueCallback<Array<Uri>>,
                            fileChooserParams: FileChooserParams
                        ): Boolean {
                            pendingFileCallback?.onReceiveValue(null)
                            pendingFileCallback = filePathCallback
                            val requested = fileChooserParams.acceptTypes
                                .flatMap { it.split(',') }
                                .map { it.trim() }
                                .filter { it.contains('/') }
                                .distinct()
                                .toTypedArray()
                            filePicker.launch(
                                requested.ifEmpty {
                                    arrayOf(
                                        "image/*", "video/*", "audio/*",
                                        "application/pdf", "text/*",
                                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                    )
                                }
                            )
                            return true
                        }
                    }
                    setDownloadListener(PortalDownloadListener(ctx))
                    loadUrl("about:blank")
                }
            },
            update = { view ->
                // Only native tab changes should replace the current page. Internal portal
                // navigation, dialogs, history, and form flows must be left alone.
                if (compatibility == PortalWebViewState.READY && appliedNavigationRequest != navigationRequest) {
                    appliedNavigationRequest = navigationRequest
                    view.loadUrl(target)
                }
            }
        )
        if (compatibility != PortalWebViewState.READY) {
            Card(Modifier.fillMaxWidth().padding(20.dp)) {
                Column(Modifier.padding(20.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    if (compatibility == PortalWebViewState.CHECKING) {
                        Text("Opening your home…")
                    } else {
                        Text("Update your Android web component")
                        Text("Update Android System WebView and Chrome in your phone’s app store, then close and reopen David-Pi. This web component cannot display your home with the features this app needs.")
                        Text("Your pairing and saved data are kept. Backup and saved offline books remain available from the tabs below.")
                        Button(onClick = {
                            val provider = WebViewCompat.getCurrentWebViewPackage(context)?.packageName
                            val intent = if (provider == null) Intent(Settings.ACTION_SETTINGS)
                                else Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$provider"))
                            runCatching { context.startActivity(intent) }
                        }) { Text("Open web component settings") }
                    }
                }
            }
        } else if (failed) {
            Card(Modifier.fillMaxWidth().padding(20.dp)) {
                Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("${com.davidpi.backup.security.CredentialStore(context).displayName} is unavailable")
                    Text("Check that Tailscale is connected, then try again.")
                    Button(onClick = { failed = false; webView?.reload() }) { Text("Try again") }
                }
            }
        }
    }
}

private class PortalDownloadListener(
    private val context: Context,
) : DownloadListener {
    override fun onDownloadStart(
        url: String,
        userAgent: String,
        contentDisposition: String,
        mimetype: String,
        contentLength: Long
    ) {
        val canonical = DavidPiOrigin.canonicalPortalDownloadUrl(url) ?: return
        val name = android.webkit.URLUtil.guessFileName(url, contentDisposition, mimetype)
        PortalDownloadWorker.enqueue(context, canonical, name, mimetype, userAgent)
    }
}
