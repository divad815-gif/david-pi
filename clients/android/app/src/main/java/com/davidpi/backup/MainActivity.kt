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
import com.davidpi.backup.media.MediaScanner
import com.davidpi.backup.security.CredentialStore
import com.davidpi.backup.work.BackupScheduler
import com.davidpi.backup.work.BackupFrequency
import com.davidpi.backup.work.BackupPreferences
import com.davidpi.backup.work.BackupSettings
import com.davidpi.backup.work.BackupStatusText
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

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
    val permissionGranted: Boolean = false,
    val frequency: BackupFrequency = BackupFrequency.WEEKLY,
    val wifiOnly: Boolean = true,
    val chargingOnly: Boolean = false,
    val includeScreenshots: Boolean = false,
    val includeDownloads: Boolean = false,
    val rateMiB: Int = 2,
)

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val store = CredentialStore(application)
    private val database = BackupDatabase.get(application)
    private val savedSettings = BackupPreferences.load(application)
    private val mutable = MutableStateFlow(
        UiState(
            paired = store.credential() != null,
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
        if (mutable.value.paired) refreshStatus()
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
                BackupApi(getApplication<Application>().contentResolver, store, database.dao()).status()
            }.onSuccess { status ->
                val device = status.optJSONObject("device")
                mutable.update {
                    it.copy(
                        serverReachable = true,
                        lastContact = device?.optString("last_contact_at").orEmpty(),
                        message = "Primary David-Pi storage is reachable."
                    )
                }
            }.onFailure { error ->
                if (error is ApiException && error.status == 401) {
                    BackupScheduler.cancelAll(getApplication())
                    store.clear()
                    mutable.update { value ->
                        value.copy(
                            paired = false,
                            server = "",
                            owner = "",
                            serverReachable = false,
                            lastContact = "",
                            message = "Backup access was removed. Pair this phone with David-Pi again."
                        )
                    }
                } else {
                    mutable.update { value ->
                        value.copy(
                            serverReachable = false,
                            message = "David-Pi could not be reached. Check Tailscale and try again."
                        )
                    }
                }
            }
        }
    }
    fun pair(server: String, token: String) {
        viewModelScope.launch {
            runCatching {
                BackupApi(getApplication<Application>().contentResolver, store, database.dao())
                    .pair(server, token, Build.MODEL)
            }.onSuccess {
                mutable.update {
                    it.copy(
                        paired = true, server = store.serverUrl.orEmpty(),
                        owner = store.ownerName.orEmpty(), message = "Phone paired successfully."
                    )
                }
                BackupScheduler.schedulePeriodic(getApplication(), BackupPreferences.load(getApplication()))
                refreshStatus()
            }.onFailure { error -> message(error.message ?: "Pairing failed.") }
        }
    }
}

class MainActivity : ComponentActivity() {
    private val model by viewModels<MainViewModel>()
    private val notificationPath = mutableStateOf("")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleIntent(intent)
        setContent { DavidPiApp(model, notificationPath.value) }
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
        if (uri.scheme != "davidpibackup" || uri.host != "pair") return
        val token = uri.getQueryParameter("token") ?: return
        val server = uri.getQueryParameter("server") ?: return
        model.pair(server, token)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DavidPiBackupScreen(model: MainViewModel, outerPadding: PaddingValues = PaddingValues(0.dp)) {
    val state by model.state.collectAsStateWithLifecycle()
    var server by remember { mutableStateOf("") }
    var code by remember { mutableStateOf("") }
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
                Text("DAVID-PI", color = MaterialTheme.colorScheme.primary)
                Text("Phone backup", style = MaterialTheme.typography.displaySmall, fontFamily = FontFamily.Serif)
                Text("Original photos and videos, sent privately home through Tailscale.")
                if (!state.paired) {
                    OutlinedTextField(
                        server, { server = it },
                        label = { Text("Private HTTPS server address") },
                        placeholder = { Text("https://your-private-name.ts.net") },
                        modifier = Modifier.fillMaxWidth()
                    )
                    OutlinedTextField(code, { code = it.take(64) }, label = { Text("Pairing code") }, modifier = Modifier.fillMaxWidth())
                    Button(
                        onClick = { model.pair(server, code) },
                        enabled = server.startsWith("https://") && code.isNotBlank(),
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
                    if (!state.permissionGranted) {
                        Button(onClick = { permissionLauncher.launch(permissions) }, modifier = Modifier.fillMaxWidth()) {
                            Text("Allow all photos and videos")
                        }
                        Text("Selected-photo access is not enough for automatic full-library backup.")
                    }
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                        Metric("Pending", state.counts.filterKeys { BackupQueuePolicy.isPending(it) }.values.sum().toString())
                        Metric("On Pi", state.counts["secondary_pending"].orEmpty().toString())
                        Metric("Protected", state.counts["fully_protected"].orEmpty().toString())
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
                    Text("Primary-verified files are on David-Pi. They are not fully protected until a separate backup disk verifies another copy.")
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
