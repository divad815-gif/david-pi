package com.davidpi.backup

import android.Manifest
import android.app.Application
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.asFlow
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.work.WorkInfo
import androidx.work.WorkManager
import com.davidpi.backup.data.BackupDatabase
import com.davidpi.backup.data.BackupQueuePolicy
import com.davidpi.backup.net.BackupApi
import com.davidpi.backup.net.ApiException
import com.davidpi.backup.net.DavidPiOrigin
import com.davidpi.backup.net.PairingDeepLink
import com.davidpi.backup.net.PairingRequestPolicy
import com.davidpi.backup.media.MediaScanner
import com.davidpi.backup.security.CredentialStore
import com.davidpi.backup.work.BackupScheduler
import com.davidpi.backup.work.BackupFrequency
import com.davidpi.backup.work.BackupPreferences
import com.davidpi.backup.work.BackupSettings
import com.davidpi.backup.work.BackupStatusText
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.IOException

data class ServerProtectionSummary(val onPi: Int, val fullyProtected: Int)

internal object ProtectionStatusPolicy {
    fun parse(status: JSONObject): ServerProtectionSummary? {
        val protection = status.optJSONObject("protection") ?: return null
        val counts = mutableMapOf<String, Any?>()
        val keys = protection.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            counts[key] = protection.opt(key)
        }
        return fromCounts(status.opt("fully_protected"), counts)
    }

    fun fromCounts(
        declaredValue: Any?,
        counts: Map<String, Any?>,
    ): ServerProtectionSummary? {
        val declared = nonNegativeInt(declaredValue) ?: return null
        var total = 0L
        var protectedFromMap: Int? = null
        counts.forEach { (key, value) ->
            val count = nonNegativeInt(value) ?: return null
            total += count.toLong()
            if (total > Int.MAX_VALUE) return null
            if (key == "fully_protected") protectedFromMap = count
        }
        if ((protectedFromMap ?: 0) != declared || declared > total) return null
        return ServerProtectionSummary(total.toInt(), declared)
    }

    private fun nonNegativeInt(value: Any?): Int? {
        val number = value as? Number ?: return null
        val long = number.toLong()
        if (long < 0 || long > Int.MAX_VALUE || number.toDouble() != long.toDouble()) return null
        return long.toInt()
    }
}

data class UiState(
    val paired: Boolean = false,
    val server: String = "",
    val owner: String = "",
    val counts: Map<String, Int> = emptyMap(),
    val pendingBytes: Long = 0,
    val running: Boolean = false,
    val workStatus: String = "Ready to back up.",
    val message: String = "",
    val serverReachable: Boolean = false,
    val lastContact: String = "",
    val serverOnPi: Int? = null,
    val serverFullyProtected: Int? = null,
    val permissionGranted: Boolean = false,
    val frequency: BackupFrequency = BackupFrequency.WEEKLY,
    val wifiOnly: Boolean = true,
    val chargingOnly: Boolean = false,
    val includeScreenshots: Boolean = false,
    val includeDownloads: Boolean = false,
    val rateMiB: Int = 2,
    val pendingPairingToken: String = "",
    val pendingPairingServer: String = "",
    val restartSession: Boolean = false,
    val displayName: String = "David-Pi",
    val busy: Boolean = false,
    val enabledModules: Set<String> = emptySet(),
)

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val store = CredentialStore(application)
    private val restoredIdentity = store.restoreOrigin()
    private val boundScope = store.scope
    private val boundDeviceId = store.deviceId
    private val database = BackupDatabase.get(application)
    private val savedSettings = BackupPreferences.load(application)
    private val mutable = MutableStateFlow(
        UiState(
            paired = store.restoreOrigin(),
            displayName = store.displayName,
            enabledModules = store.enabledModules,
            server = store.serverUrl.orEmpty(),
            owner = store.ownerName.orEmpty(),
            frequency = savedSettings.frequency,
            wifiOnly = savedSettings.wifiOnly,
            chargingOnly = savedSettings.chargingOnly,
            includeScreenshots = savedSettings.includeScreenshots,
            includeDownloads = savedSettings.includeDownloads,
            rateMiB = savedSettings.rateMiB,
        )
    )
    val state: StateFlow<UiState> = combine(
        mutable,
        database.dao().observeCounts(),
        database.dao().observePendingBytes(),
        WorkManager.getInstance(application)
            .getWorkInfosForUniqueWorkLiveData(BackupScheduler.MANUAL_WORK)
            .asFlow()
    ) { base, counts, bytes, work ->
        val currentWork = work.firstOrNull()
        base.copy(
            counts = counts.associate { it.state to it.count },
            pendingBytes = bytes,
            running = work.any { !it.state.isFinished },
            workStatus = BackupStatusText.describe(
                currentWork?.state,
                currentWork?.runAttemptCount ?: 0,
                currentWork?.progress?.getString("status").orEmpty(),
            )
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), mutable.value)

    init {
        mutable.update {
            it.copy(permissionGranted = MediaScanner(application, database.dao()).hasFullAccess())
        }
        if (!restoredIdentity && store.credential() != null && store.instanceId == null) {
            mutable.update { it.copy(busy = true, message = "Verifying your existing household before reconnecting saved data…") }
            viewModelScope.launch {
                runCatching {
                    BackupApi(application.contentResolver, store, database.dao()).restoreLegacyIdentity(application)
                }.onSuccess { mutable.update { it.copy(restartSession = true) } }
                    .onFailure { error -> mutable.update { it.copy(busy = false, message = error.message ?: "Your previous data is preserved. Connect to the updated server and reopen the app, or disconnect to pair another household.") } }
            }
        }
        if (mutable.value.paired) {
            // App upgrades must install the independent integrity cadence even
            // when the phone was paired before this release.
            BackupScheduler.schedulePeriodic(application, savedSettings)
            refreshStatus()
        }
    }

    fun permission(granted: Boolean) { mutable.update { it.copy(permissionGranted = granted) } }
    fun message(value: String) { mutable.update { it.copy(message = value) } }
    fun settings(
        frequency: BackupFrequency? = null,
        wifiOnly: Boolean? = null,
        charging: Boolean? = null,
        screenshots: Boolean? = null,
        downloads: Boolean? = null,
        rate: Int? = null
    ) {
        mutable.update {
            it.copy(
                frequency = frequency ?: it.frequency,
                wifiOnly = wifiOnly ?: it.wifiOnly,
                chargingOnly = charging ?: it.chargingOnly,
                includeScreenshots = screenshots ?: it.includeScreenshots,
                includeDownloads = downloads ?: it.includeDownloads,
                rateMiB = rate ?: it.rateMiB
            )
        }
        val current = mutable.value
        val settings = BackupSettings(
            frequency = current.frequency,
            wifiOnly = current.wifiOnly,
            chargingOnly = current.chargingOnly,
            includeScreenshots = current.includeScreenshots,
            includeDownloads = current.includeDownloads,
            rateMiB = current.rateMiB,
        )
        BackupPreferences.save(getApplication(), settings)
        BackupScheduler.schedulePeriodic(getApplication(), settings)
    }
    fun start() {
        if (!mutable.value.permissionGranted) {
            message("Grant full photo and video access first.")
            return
        }
        BackupScheduler.startNow(
            getApplication(), mutable.value.rateMiB * 1024L * 1024L,
            mutable.value.wifiOnly, mutable.value.chargingOnly,
            mutable.value.includeScreenshots, mutable.value.includeDownloads
        )
    }
    fun pause() = BackupScheduler.pause(getApplication())
    fun refreshStatus() {
        viewModelScope.launch {
            runCatching {
                val status = BackupApi(
                    getApplication<Application>().contentResolver,
                    store,
                    database.dao(),
                ).status()
                status to (ProtectionStatusPolicy.parse(status) ?: throw IOException(
                    "David-Pi returned incomplete protection status."
                ))
            }.onSuccess { (status, protection) ->
                BackupScheduler.schedulePeriodic(getApplication(), BackupPreferences.load(getApplication()))
                val device = status.optJSONObject("device")
                mutable.update {
                    it.copy(
                        serverReachable = true,
                        displayName = store.displayName,
                        enabledModules = store.enabledModules,
                        lastContact = device?.optString("last_contact_at").orEmpty(),
                        serverOnPi = protection.onPi,
                        serverFullyProtected = protection.fullyProtected,
                        message = "${store.displayName} is reachable."
                    )
                }
            }.onFailure { error ->
                if (error is ApiException && error.status == 401 && store.scope == boundScope && store.deviceId == boundDeviceId) {
                    BackupScheduler.cancelAll(getApplication())
                    store.clear(boundScope, boundDeviceId)
                    mutable.update { value ->
                        value.copy(
                            paired = false,
                            server = "",
                            owner = "",
                            serverReachable = false,
                            lastContact = "",
                            serverOnPi = null,
                            serverFullyProtected = null,
                            message = "Backup access was removed. Pair this phone with David-Pi again."
                        )
                    }
                } else {
                    mutable.update { value ->
                        value.copy(
                            serverReachable = false,
                            serverOnPi = null,
                            serverFullyProtected = null,
                            message = "David-Pi could not be reached. Check Tailscale and try again."
                        )
                    }
                }
            }
        }
    }
    fun pair(server: String, token: String) {
        val request = PairingRequestPolicy.manual(server, token, mutable.value.paired)
        if (request == null) {
            message(
                if (mutable.value.paired) {
                    "This phone is already paired. Remove its existing access before pairing again."
                } else {
                    "Enter the household’s private HTTPS address and a valid pairing code."
                }
            )
            return
        }
        if (mutable.value.busy) return
        mutable.update { it.copy(busy = true) }
        viewModelScope.launch {
            runCatching {
                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                    WorkManager.getInstance(getApplication()).cancelAllWork().result.get(20, java.util.concurrent.TimeUnit.SECONDS)
                }
                BackupApi(getApplication<Application>().contentResolver, store, database.dao())
                    .pair(request.server, request.token, Build.MODEL)
                com.davidpi.backup.security.HouseholdStorage.reconnect(getApplication(), request.server)
            }.onSuccess {
                mutable.update { it.copy(restartSession = true, busy = false) }
            }.onFailure { error ->
                mutable.update { it.copy(busy = false, message = error.message ?: "Pairing failed.") }
            }
        }
    }

    fun disconnect() {
        if (mutable.value.busy) return
        mutable.update { it.copy(busy = true) }
        viewModelScope.launch {
            runCatching {
                // Invalidate the old credentials before asynchronous worker cancellation.
                store.clear()
                mutable.update { it.copy(paired = false, server = "", owner = "") }
                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                    WorkManager.getInstance(getApplication()).cancelAllWork().result.get(20, java.util.concurrent.TimeUnit.SECONDS)
                }
                val context = getApplication<Application>()
                context.stopService(Intent(context, com.davidpi.backup.offline.OfflinePlaybackService::class.java))
                context.stopService(Intent(context, AudiobookKeepAliveService::class.java))
                android.webkit.CookieManager.getInstance().removeAllCookies(null)
                android.webkit.WebStorage.getInstance().deleteAllData()
                mutable.update { it.copy(restartSession = true) }
            }.onFailure { mutable.update { it.copy(restartSession = true) } }
        }
    }

    fun offerPairingLink(link: PairingDeepLink) {
        val request = PairingRequestPolicy.deepLink(link, mutable.value.paired)
        if (request == null) {
            message(
                if (mutable.value.paired) {
                    "This phone is already paired. The pairing link was not applied."
                } else {
                    "That pairing link is not a canonical David-Pi link."
                }
            )
            return
        }
        mutable.update {
            it.copy(
                pendingPairingToken = request.token,
                pendingPairingServer = request.server,
                message = "Pairing link received. Confirm Pair this phone to continue.",
            )
        }
    }
}

class MainActivity : ComponentActivity() {
    private val model by viewModels<MainViewModel>()
    private val notificationPath = mutableStateOf("")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleIntent(intent)
        setContent {
            val state by model.state.collectAsStateWithLifecycle()
            LaunchedEffect(state.restartSession) {
                if (state.restartSession) {
                    startActivity(Intent(this@MainActivity, MainActivity::class.java)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
                    finish()
                }
            }
            DavidPiApp(model, notificationPath.value)
        }
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }
    private fun handleIntent(intent: Intent) {
        intent.getStringExtra("david_pi_chat_path")?.takeIf { it.startsWith("/chat") }?.let {
            notificationPath.value = it
        }
        handlePairing(intent)
    }
    private fun handlePairing(intent: Intent) {
        val uri = intent.data ?: return
        if (uri.scheme == "davidpi" && uri.host == "backup") return
        val link = runCatching {
            PairingDeepLink(
                scheme = uri.scheme,
                host = uri.host,
                port = uri.port,
                userInfo = uri.userInfo,
                path = uri.path,
                fragment = uri.fragment,
                queryNames = uri.queryParameterNames,
                serverValues = uri.getQueryParameters("server"),
                tokenValues = uri.getQueryParameters("token"),
            )
        }.getOrNull()
        if (link == null) {
            model.message("That pairing link is not a canonical David-Pi link.")
            return
        }
        model.offerPairingLink(link)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DavidPiBackupScreen(model: MainViewModel, outerPadding: PaddingValues = PaddingValues(0.dp)) {
    val state by model.state.collectAsStateWithLifecycle()
    var code by remember { mutableStateOf("") }
    var server by remember { mutableStateOf("") }
    var showDisconnect by remember { mutableStateOf(false) }
    var frequencyMenuOpen by remember { mutableStateOf(false) }
    val permissions = buildList {
        if (Build.VERSION.SDK_INT >= 33) {
            add(Manifest.permission.READ_MEDIA_IMAGES)
            add(Manifest.permission.READ_MEDIA_VIDEO)
        } else add(Manifest.permission.READ_EXTERNAL_STORAGE)
        if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
    }.toTypedArray()
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        val media = result.filterKeys { "READ_MEDIA" in it || "READ_EXTERNAL" in it }
        model.permission(media.isNotEmpty() && media.values.all { it })
    }
    LaunchedEffect(state.pendingPairingToken) {
        if (state.pendingPairingToken.isNotBlank()) code = state.pendingPairingToken
        if (state.pendingPairingServer.isNotBlank()) server = state.pendingPairingServer
    }

    if (showDisconnect) {
        AlertDialog(onDismissRequest = { showDisconnect = false },
            title = { Text("Disconnect this household?") },
            text = { Text("Uploads and playback stop. Saved books, listening progress, and queues stay in this household’s private profile. Pairing a different server or person opens a separate profile. Remove this phone in the server’s Phone backup page to revoke its server access.") },
            confirmButton = { TextButton(onClick = { showDisconnect = false; model.disconnect() }) { Text("Disconnect") } },
            dismissButton = { TextButton(onClick = { showDisconnect = false }) { Text("Keep connected") } })
    }
    MaterialTheme(
        colorScheme = lightColorScheme(
            primary = Color(0xFFC95D43), secondary = Color(0xFF4DA86A),
            background = Color(0xFFFFF9EF), surface = Color(0xFFFFF9EF),
            onBackground = Color(0xFF241F19)
        )
    ) {
        Scaffold(
            modifier = Modifier.padding(outerPadding),
            containerColor = MaterialTheme.colorScheme.background
        ) { padding ->
            Column(
                Modifier.padding(padding).padding(24.dp).verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(18.dp)
            ) {
                Text(state.displayName, color = MaterialTheme.colorScheme.primary)
                Text(if (state.paired && "device_backup" in state.enabledModules) "Phone backup" else "Connect household", style = MaterialTheme.typography.displaySmall, fontFamily = FontFamily.Serif)
                Text("Original photos and videos, sent privately home through Tailscale.")
                if (!state.paired) {
                    OutlinedTextField(
                        value = server,
                        onValueChange = { server = it.take(255) },
                        singleLine = true,
                        label = { Text("Private server address (https://name.tailnet.ts.net)") },
                        modifier = Modifier.fillMaxWidth()
                    )
                    if (state.server.isNotBlank()) {
                        OutlinedButton(onClick = { showDisconnect = true }, enabled = !state.busy) { Text("Disconnect previous household…") }
                    }
                    Text("Confirm that this HTTPS address belongs to your household before pairing. Keep Tailscale connected. Native chat push alerts are not available in this release.")
                    OutlinedTextField(code, { code = it.take(64) }, label = { Text("Pairing code") }, modifier = Modifier.fillMaxWidth())
                    Button(
                        onClick = { model.pair(server.trim(), code) },
                        enabled = code.isNotBlank() && server.isNotBlank() && !state.busy,
                        modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp)
                    ) { Text("Pair this phone") }
                } else {
                    ElevatedCard(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text("Paired for ${state.owner.ifBlank { "home" }}", style = MaterialTheme.typography.titleLarge)
                            Text(state.server)
                            Text(if (state.serverReachable) "Primary server reachable" else "Primary server not confirmed")
                            if (state.lastContact.isNotBlank()) Text("Last contact: ${state.lastContact}")
                            Text(if (state.permissionGranted) "Full media access ready" else "Media permission needed")
                        }
                    }
                    if ("device_backup" in state.enabledModules) {
                    if (!state.permissionGranted) {
                        Button(onClick = { permissionLauncher.launch(permissions) }, modifier = Modifier.fillMaxWidth()) {
                            Text("Allow all photos and videos")
                        }
                        Text("Selected-photo access is not enough for automatic full-library backup.")
                    }
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Metric("Pending", state.counts.filterKeys { BackupQueuePolicy.isPending(it) }.values.sum().toString())
                        Metric("On Pi", state.serverOnPi?.toString() ?: "—")
                        Metric("Protected", state.serverFullyProtected?.toString() ?: "—")
                    }
                    val skipped = state.counts["permanent_error"].orEmpty()
                    if (skipped > 0) {
                        Text("$skipped unreadable phone item${if (skipped == 1) "" else "s"} skipped safely")
                    }
                    Text("${state.pendingBytes / (1024 * 1024)} MiB estimated remaining")
                    Button(
                        onClick = model::start,
                        enabled = !state.running,
                        modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)
                    ) { Text(if (state.running) "Backup running…" else "Start Initial Backup / Back Up Now") }
                    Text(state.workStatus)
                    OutlinedButton(onClick = model::pause, enabled = state.running, modifier = Modifier.fillMaxWidth()) {
                        Text("Pause")
                    }
                    Text("Speed limit: ${state.rateMiB} MiB/s")
                    Slider(
                        value = state.rateMiB.toFloat(), onValueChange = { model.settings(rate = it.toInt()) },
                        valueRange = 1f..20f, steps = 18
                    )
                    Row(
                        Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text("Automatic backup")
                            Text(
                                if (state.frequency == BackupFrequency.OFF)
                                    "Automatic backups are off."
                                else
                                    "Runs approximately ${state.frequency.label.lowercase()}.",
                                style = MaterialTheme.typography.bodySmall
                            )
                        }
                        Box {
                            OutlinedButton(onClick = { frequencyMenuOpen = true }) {
                                Text(state.frequency.label)
                            }
                            DropdownMenu(
                                expanded = frequencyMenuOpen,
                                onDismissRequest = { frequencyMenuOpen = false }
                            ) {
                                BackupFrequency.entries.forEach { frequency ->
                                    DropdownMenuItem(
                                        text = { Text(frequency.label) },
                                        onClick = {
                                            frequencyMenuOpen = false
                                            model.settings(frequency = frequency)
                                        }
                                    )
                                }
                            }
                        }
                    }
                    SettingSwitch("Wi-Fi / unmetered only", state.wifiOnly) { model.settings(wifiOnly = it) }
                    SettingSwitch("Charging only", state.chargingOnly) { model.settings(charging = it) }
                    SettingSwitch("Include screenshots", state.includeScreenshots) { model.settings(screenshots = it) }
                    SettingSwitch("Include Downloads", state.includeDownloads) { model.settings(downloads = it) }
                    } else { Text("Automatic phone backups are disabled on this server. Open Home for your enabled modules.") }
                    OutlinedButton(onClick = { showDisconnect = true }, enabled = !state.busy) { Text("Disconnect household…") }
                    Text("Primary-verified files are on your server. They are not fully protected until a separate backup disk verifies another copy.")
                    Text("While charging, a daily integrity pass alternates photos and videos and rechecks up to 4,096 items / 64 GiB without delaying new backups. Larger libraries continue from a saved cursor.")
                }
                if (state.message.isNotBlank()) {
                    Text(state.message, color = MaterialTheme.colorScheme.primary)
                }
                Spacer(Modifier.height(32.dp))
            }
        }
    }
}

@Composable private fun Metric(label: String, value: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(value, style = MaterialTheme.typography.headlineMedium)
        Text(label, style = MaterialTheme.typography.labelMedium)
    }
}
@Composable private fun SettingSwitch(label: String, checked: Boolean, changed: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label); Switch(checked, changed)
    }
}
private fun Int?.orEmpty() = this ?: 0
