package com.davidpi.backup

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Environment
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
import android.webkit.JavascriptInterface
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.app.NotificationManagerCompat
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
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
import androidx.lifecycle.compose.collectAsStateWithLifecycle

private const val DEFAULT_PORTAL = "https://david-pi.example-tailnet.ts.net"

private class ChatPushBridge(private val webView: WebView) {
    @JavascriptInterface fun isConfigured(): Boolean =
        FirebaseApp.getApps(webView.context).isNotEmpty()

    @JavascriptInterface fun notificationsEnabled(): Boolean =
        NotificationManagerCompat.from(webView.context).areNotificationsEnabled()

    @JavascriptInterface fun openNotificationSettings() {
        val intent = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).apply {
            putExtra(Settings.EXTRA_APP_PACKAGE, webView.context.packageName)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        webView.context.startActivity(intent)
    }

    @JavascriptInterface fun requestToken() {
        if (FirebaseApp.getApps(webView.context).isEmpty()) return
        FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
            val encoded = org.json.JSONObject.quote(token)
            webView.post { webView.evaluateJavascript("window.davidPiRegisterAndroidPush&&window.davidPiRegisterAndroidPush($encoded)", null) }
        }
    }
}

private enum class Destination(val label: String) {
    HOME("Home"), MEDIA("Media"), BACKUP("Backup"), MORE("More"), PAGE("More")
}

@Composable
fun DavidPiApp(model: MainViewModel, notificationPath: String = "") {
    val state by model.state.collectAsStateWithLifecycle()
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
                    Destination.entries.filter { it != Destination.PAGE }.forEach { item ->
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
                            }) },
                            label = { Text(item.label) }
                        )
                    }
                }
            }
        ) { padding ->
            when (destination) {
                Destination.BACKUP -> DavidPiBackupScreen(model, padding)
                Destination.MORE -> MoreScreen(padding) { path ->
                    open(path, Destination.PAGE)
                }
                else -> PortalScreen(
                    baseUrl = state.server.ifBlank { DEFAULT_PORTAL },
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
private fun MoreScreen(padding: PaddingValues, open: (String) -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(padding).padding(22.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text("DAVID-PI", color = MaterialTheme.colorScheme.primary)
        Text("Everything at home", style = MaterialTheme.typography.headlineMedium)
        Text("App ${BuildConfig.VERSION_NAME}", color = MaterialTheme.colorScheme.secondary)
        listOf(
            "Assistant" to "/assistant", "Notes" to "/notes",
            "Chat" to "/chat",
            "Movie Night" to "/movies", "Recipes" to "/recipes",
            "Files" to "/files", "Audiobooks" to "/audiobooks",
            "Games" to "/games",
            "Date Night" to "/places", "Server status" to "/status"
        ).forEach { (label, path) ->
            Button(onClick = { open(path) }, modifier = Modifier.fillMaxWidth()) {
                Text(label)
            }
        }
    }
}

@Composable
private fun PortalScreen(
    baseUrl: String,
    path: String,
    navigationRequest: Int,
    padding: PaddingValues,
    onOpenBackup: () -> Unit
) {
    val context = LocalContext.current
    var webView by remember { mutableStateOf<WebView?>(null) }
    var mediaBridge by remember { mutableStateOf<NativeMediaBridge?>(null) }
    var failed by remember { mutableStateOf(false) }
    var pendingFileCallback by remember { mutableStateOf<ValueCallback<Array<Uri>>?>(null) }
    val filePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        pendingFileCallback?.onReceiveValue(uris.toTypedArray())
        pendingFileCallback = null
    }
    val allowedBase = remember(baseUrl) {
        Uri.parse(baseUrl.trimEnd('/')).takeIf {
            it.scheme == "https" && !it.host.isNullOrBlank()
        }?.toString() ?: DEFAULT_PORTAL
    }
    val allowedHost = Uri.parse(allowedBase).host.orEmpty()
    val target = allowedBase + if (path.startsWith('/')) path else "/$path"
    var appliedNavigationRequest by remember { mutableIntStateOf(navigationRequest) }

    BackHandler(enabled = webView?.canGoBack() == true) { webView?.goBack() }
    DisposableEffect(Unit) {
        onDispose {
            mediaBridge?.release()
            // Android may keep one renderer process behind several WebViews. An
            // immediate destroy while changing native tabs was terminating that
            // renderer just as the next portal page opened, producing a blank
            // Chat page. Stop work and detach the bridge; AndroidView owns the
            // actual view lifetime and will release it after composition removal.
            webView?.stopLoading()
            webView?.removeJavascriptInterface("DavidPiMedia")
            webView?.removeJavascriptInterface("DavidPiPush")
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
                    mediaBridge = NativeMediaBridge(ctx, this).also { addJavascriptInterface(it, "DavidPiMedia") }
                    addJavascriptInterface(ChatPushBridge(this), "DavidPiPush")
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    // A lock-screen MediaSession callback is not a touchscreen
                    // gesture. Permit it to resume an already-selected book.
                    settings.mediaPlaybackRequiresUserGesture = false
                    // The portal already versions its assets. The embedded WebView must
                    // not resurrect an older organizer script from its private HTTP cache.
                    settings.cacheMode = WebSettings.LOAD_NO_CACHE
                    clearCache(true)
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
                            if (uri.scheme == "davidpi" && uri.host == "backup") {
                                onOpenBackup()
                                return true
                            }
                            if (uri.scheme == "https" && uri.host == allowedHost) return false
                            runCatching { ctx.startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                            return true
                        }

                        override fun onPageFinished(view: WebView, url: String) {
                            // Assets are versioned and LOAD_NO_CACHE is active. Do not force
                            // a JavaScript reload here: navigation away from this WebView can
                            // otherwise destroy its renderer while that reload is in flight.
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
                    setDownloadListener(PortalDownloadListener(ctx, this, allowedHost))
                    loadUrl(target)
                }
            },
            update = { view ->
                // Only native tab changes should replace the current page. Internal portal
                // navigation, dialogs, history, and form flows must be left alone.
                if (appliedNavigationRequest != navigationRequest) {
                    appliedNavigationRequest = navigationRequest
                    view.loadUrl(target)
                }
            }
        )
        if (failed) {
            Card(Modifier.fillMaxWidth().padding(20.dp)) {
                Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("David-Pi is unavailable")
                    Text("Check that Tailscale is connected, then try again.")
                    Button(onClick = { failed = false; webView?.reload() }) { Text("Try again") }
                }
            }
        }
    }
}

private class PortalDownloadListener(
    private val context: Context,
    private val webView: WebView,
    private val allowedHost: String
) : DownloadListener {
    override fun onDownloadStart(
        url: String,
        userAgent: String,
        contentDisposition: String,
        mimetype: String,
        contentLength: Long
    ) {
        val uri = Uri.parse(url)
        if (uri.scheme != "https" || uri.host != allowedHost) {
            context.startActivity(Intent(Intent.ACTION_VIEW, uri))
            return
        }
        val name = android.webkit.URLUtil.guessFileName(url, contentDisposition, mimetype)
        val request = DownloadManager.Request(uri)
            .setMimeType(mimetype)
            .addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url).orEmpty())
            .addRequestHeader("User-Agent", userAgent)
            .setTitle(name)
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
        (context.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(request)
    }
}
