package com.davidpi.backup.work

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.pm.ServiceInfo
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.work.*
import com.davidpi.backup.data.BackupDatabase
import com.davidpi.backup.data.QueueSourceChangePolicy
import com.davidpi.backup.data.QueueIntegrityRevalidationPolicy
import com.davidpi.backup.data.SyncState
import com.davidpi.backup.media.MediaScanner
import com.davidpi.backup.media.IncompleteMediaScanException
import com.davidpi.backup.media.ReconciliationEnvelopeStore
import com.davidpi.backup.net.BackupApi
import com.davidpi.backup.net.ApiException
import com.davidpi.backup.net.BackupFailurePolicy
import com.davidpi.backup.net.SourceUnreadableException
import com.davidpi.backup.security.CredentialStore
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException
import java.util.concurrent.TimeUnit

class BackupWorker(context: Context, parameters: WorkerParameters) :
    CoroutineWorker(context, parameters) {
    private var executionScope: String? = null
    private var executionDeviceId: String? = null
    override suspend fun doWork(): Result = runMutex.withLock { doWorkExclusive() }

    private suspend fun doWorkExclusive(): Result {
        try {
            setForeground(progress("Checking your library…"))
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            return Result.failure(
                workDataOf("error" to "Android could not start the backup notification.")
            )
        }
        val scope = com.davidpi.backup.net.DavidPiOrigin.sessionScope
        executionScope = scope
        executionDeviceId = CredentialStore(applicationContext).deviceId
        if (scope == "unpaired" || "device_backup" !in CredentialStore(applicationContext).enabledModules) return Result.failure()
        val database = BackupDatabase.get(applicationContext)
        val api = BackupApi(applicationContext.contentResolver, CredentialStore(applicationContext), database.dao())
        try {
            api.status()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            return if (error is ApiException && error.status == 401) revokedAccessFailure() else Result.retry()
        }
        if ("device_backup" !in CredentialStore(applicationContext).enabledModules) {
            BackupScheduler.cancelAutomatic(applicationContext)
            return Result.success()
        }
        val scanner = MediaScanner(applicationContext, database.dao())
        val integrityOnly = inputData.getBoolean("integrity_only", false)
        if (!scanner.hasFullAccess()) return Result.failure(
            workDataOf("error" to "Full photo and video permission is required.")
        )
        val completeScan = try {
            scanner.scan(
                includeScreenshots = inputData.getBoolean("include_screenshots", false),
                includeDownloads = inputData.getBoolean("include_downloads", false),
                // Only the independent charging-constrained cadence may turn
                // completed records into local content-integrity checkpoints.
                scheduleIntegrityRevalidation = integrityOnly,
            )
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (incomplete: IncompleteMediaScanException) {
            return Result.failure(workDataOf("error" to incomplete.message.orEmpty()))
        }
        if (scope != com.davidpi.backup.net.DavidPiOrigin.sessionScope || isStopped) return Result.failure()

        if (isStopped) return Result.retry()
        val envelope = try {
            ReconciliationEnvelopeStore.prepare(
                database.dao(), completeScan.visibleClientItemIds
            )
        } catch (invalid: IllegalArgumentException) {
            return Result.failure(workDataOf("error" to (invalid.message ?: "Invalid complete scan.")))
        }
        try {
            api.reconcile(envelope)
            ReconciliationEnvelopeStore.acknowledge(database.dao(), envelope)
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            when (ReconciliationFailurePolicy.disposition(error, envelope.scanId)) {
                ReconciliationDisposition.RETRY_SAME_RECEIPT -> return Result.retry()
                ReconciliationDisposition.RETRY_FRESH_RECEIPT -> {
                    ReconciliationEnvelopeStore.discard(database.dao(), envelope.scanId)
                    return Result.retry()
                }
                ReconciliationDisposition.RETRY_WITH_SERVER_CLOCK -> {
                    val apiError = error as ApiException
                    try {
                        ReconciliationEnvelopeStore.recoverClockSkew(
                            database.dao(),
                            envelope.scanId,
                            requireNotNull(apiError.serverTimeMillis),
                        )
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (_: Exception) {
                        return Result.retry()
                    }
                    // A malformed anchor leaves the pending ID intact. A valid
                    // one is durably saved before only its bound ID is retired.
                    return Result.retry()
                }
                ReconciliationDisposition.REVOKED -> {
                    return revokedAccessFailure()
                }
                ReconciliationDisposition.FAIL_CLOSED -> return Result.failure(
                    workDataOf("error" to "The complete library scan was rejected without changes.")
                )
            }
        }
        var completed = 0
        while (!isStopped) {
            val item = if (integrityOnly) {
                // Integrity sampling is deliberately lower priority than every
                // new/retrying upload and server-session cleanup. The normal
                // backup run will clear those first, while this daily
                // charging-only job remains tightly bounded.
                if (database.dao().countNonIntegrityPending() > 0) break
                database.dao().nextIntegrityRevalidation() ?: break
            } else {
                // A checkpoint left by a stopped integrity run stays dormant
                // until the next charging-constrained integrity invocation.
                // Manual and user-frequency jobs never perform the bounded
                // content rehash off-charge.
                database.dao().nextPending()
                    ?.takeUnless(QueueIntegrityRevalidationPolicy::isCheckpoint)
                    ?: break
            }
            try {
                setForeground(progress("Backing up ${item.displayName}", completed))
                setProgress(workDataOf("status" to "Backing up media to your household…"))
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                return Result.retry()
            }
            try {
                api.upload(item, inputData.getLong("rate_bytes", BackupApi.DEFAULT_RATE_BYTES))
                completed++
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                if (error is ApiException && error.status == 401) {
                    return revokedAccessFailure()
                }
                if (error is SourceUnreadableException) {
                    val persisted = database.dao().byId(item.queueId)
                        ?: database.dao().byMediaStoreId(item.mediaStoreId, item.mediaType)
                        ?: item
                    val attempts = persisted.retryCount + 1
                    if (QueueIntegrityRevalidationPolicy.isCheckpoint(persisted)) {
                        val now = System.currentTimeMillis()
                        database.dao().putState(
                            SyncState(
                                QueueIntegrityRevalidationPolicy.failureStateKey(
                                    persisted.queueId
                                ),
                                QueueIntegrityRevalidationPolicy.failureStateValue(
                                    attempts,
                                    now,
                                ),
                                now,
                            )
                        )
                        val transition = IntegrityReadFailurePolicy.transition(
                            persisted,
                            now,
                        )
                        if (transition.retryLater) {
                            database.dao().update(transition.item)
                            return Result.retry()
                        } else {
                            // A failed local re-read cannot revoke the server's
                            // last authoritative protection state. Preserve it
                            // and retry on the next bounded cadence; the local
                            // failure remains separately recorded in sync_state.
                            database.dao().update(transition.item)
                            setProgress(workDataOf(
                                "status" to "Kept the prior protected status for one temporarily unavailable item."
                            ))
                            continue
                        }
                    }
                    if (QueueSourceChangePolicy.shouldQuarantineUnreadable(attempts)) {
                        database.dao().update(
                            persisted.copy(
                                state = "permanent_error",
                                retryCount = attempts,
                                errorCode = "source_unreadable",
                                updatedAt = System.currentTimeMillis(),
                            )
                        )
                        setProgress(workDataOf(
                            "status" to "Skipped one unavailable item and kept going."
                        ))
                        continue
                    }
                }
                if (BackupFailurePolicy.isPermanent(error)) {
                    val apiError = error as ApiException
                    val persisted = database.dao().byId(item.queueId)
                        ?: database.dao().byMediaStoreId(item.mediaStoreId, item.mediaType)
                        ?: item
                    database.dao().update(
                        persisted.copy(
                            state = "permanent_error",
                            retryCount = persisted.retryCount + 1,
                            errorCode = apiError.errorCode.take(80),
                            updatedAt = System.currentTimeMillis()
                        )
                    )
                    setProgress(workDataOf(
                        "status" to "Skipped one unreadable item and kept going."
                    ))
                    continue
                }
                val persisted = database.dao().byId(item.queueId)
                    ?: database.dao().byMediaStoreId(item.mediaStoreId, item.mediaType)
                    ?: item
                val recoveringChangedSource = persisted.state in setOf(
                    QueueSourceChangePolicy.STATE,
                    QueueSourceChangePolicy.REHASH_STATE,
                )
                database.dao().update(
                    persisted.copy(
                        state = QueueSourceChangePolicy.stateAfterTemporaryFailure(
                            persisted.state
                        ),
                        retryCount = persisted.retryCount + 1,
                        errorCode = if (recoveringChangedSource) {
                            persisted.errorCode
                        } else {
                            error.javaClass.simpleName.take(80)
                        },
                        updatedAt = System.currentTimeMillis()
                    )
                )
                setProgress(workDataOf("status" to BackupStatusText.retryMessage(error)))
                return Result.retry()
            }
        }
        return Result.success(workDataOf("completed" to completed))
    }

    private fun revokedAccessFailure(): Result {
        // Clearing credentials and canceling both automatic schedules is one
        // operation at every authenticated endpoint. A revoked token cannot
        // leave a daily or user-frequency job waking in the background.
        val credentials = CredentialStore(applicationContext)
        if (executionScope != null && credentials.scope == executionScope && credentials.deviceId == executionDeviceId) {
            credentials.clear(executionScope, executionDeviceId)
            BackupScheduler.cancelAutomatic(applicationContext)
        }
        return Result.failure(workDataOf(
            "error" to "Backup access was removed. Open the app and pair this phone again."
        ))
    }

    private fun progress(message: String, completed: Int = 0): ForegroundInfo {
        val manager = applicationContext.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel("backup", "Photo backup", NotificationManager.IMPORTANCE_LOW)
        )
        val notification = NotificationCompat.Builder(applicationContext, "backup")
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("${CredentialStore(applicationContext).displayName} backup")
            .setContentText(message)
            .setOngoing(true)
            .setProgress(0, completed, true)
            .build()
        return ForegroundInfo(
            8201,
            notification,
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
        )
    }

    companion object {
        // Manual and periodic unique work have different names; serialize them
        // so their complete observations and pending receipt state cannot race.
        private val runMutex = Mutex()
    }
}

enum class ReconciliationDisposition {
    RETRY_SAME_RECEIPT,
    RETRY_FRESH_RECEIPT,
    RETRY_WITH_SERVER_CLOCK,
    REVOKED,
    FAIL_CLOSED,
}

object ReconciliationFailurePolicy {
    fun disposition(
        error: Throwable,
        expectedScanId: String? = null,
    ): ReconciliationDisposition = when {
        error is ApiException && error.status == 401 -> ReconciliationDisposition.REVOKED
        error is ApiException && error.status == 409 && error.errorCode == "stale_scan" ->
            ReconciliationDisposition.RETRY_FRESH_RECEIPT
        error is ApiException && error.status == 422 &&
            error.errorCode == "scan_clock_skew" &&
            expectedScanId != null &&
            error.serverTimeMillis != null &&
            error.scanId == expectedScanId ->
            ReconciliationDisposition.RETRY_WITH_SERVER_CLOCK
        error is ApiException && error.errorCode == "scan_clock_skew" ->
            ReconciliationDisposition.RETRY_SAME_RECEIPT
        error is ApiException && error.status in setOf(409, 413, 422) ->
            ReconciliationDisposition.FAIL_CLOSED
        error is ApiException && (error.status == 408 || error.status == 425 ||
            error.status == 429 || error.status in 500..599) ->
            ReconciliationDisposition.RETRY_SAME_RECEIPT
        error is ApiException -> ReconciliationDisposition.FAIL_CLOSED
        error is IOException -> ReconciliationDisposition.RETRY_SAME_RECEIPT
        else -> ReconciliationDisposition.FAIL_CLOSED
    }
}

object BackupStatusText {
    fun describe(
        state: WorkInfo.State?,
        runAttemptCount: Int = 0,
        progress: String = "",
    ): String = when {
        progress.isNotBlank() -> progress
        state == WorkInfo.State.RUNNING -> "Backing up media to your household…"
        state == WorkInfo.State.ENQUEUED && runAttemptCount > 0 ->
            "Retry scheduled after a temporary connection or server problem."
        state == WorkInfo.State.ENQUEUED || state == WorkInfo.State.BLOCKED ->
            "Waiting for Wi-Fi or charging requirements."
        state == WorkInfo.State.SUCCEEDED -> "Last backup run finished."
        state == WorkInfo.State.FAILED -> "Backup stopped and needs attention."
        state == WorkInfo.State.CANCELLED -> "Backup paused."
        else -> "Ready to back up."
    }

    fun retryMessage(error: Exception): String = when (error) {
        is ApiException -> when (error.status) {
            429 -> "David-Pi is busy; retry scheduled."
            503 -> "David-Pi is temporarily unavailable; retry scheduled."
            else -> "The server returned a temporary problem; retry scheduled."
        }
        is java.io.IOException -> "Connection interrupted; retry scheduled."
        else -> "Temporary problem; retry scheduled."
    }
}

internal data class IntegrityReadFailureTransition(
    val item: com.davidpi.backup.data.QueuedMedia,
    val retryLater: Boolean,
)

internal object IntegrityReadFailurePolicy {
    fun transition(
        item: com.davidpi.backup.data.QueuedMedia,
        nowMillis: Long,
    ): IntegrityReadFailureTransition {
        require(QueueIntegrityRevalidationPolicy.isCheckpoint(item))
        val attempts = item.retryCount + 1
        return if (QueueSourceChangePolicy.shouldQuarantineUnreadable(attempts)) {
            IntegrityReadFailureTransition(
                QueueIntegrityRevalidationPolicy.restoreUnchanged(item, nowMillis),
                retryLater = false,
            )
        } else {
            IntegrityReadFailureTransition(
                item.copy(retryCount = attempts, updatedAt = nowMillis),
                retryLater = true,
            )
        }
    }
}

object BackupScheduler {
    const val MANUAL_WORK = "david-pi-manual-backup"
    const val PERIODIC_WORK = "david-pi-weekly-reconciliation"
    const val INTEGRITY_WORK = "david-pi-daily-integrity-revalidation"
    const val INTEGRITY_INTERVAL_DAYS = 1L
    internal val AUTOMATIC_WORK_NAMES = setOf(PERIODIC_WORK, INTEGRITY_WORK)
    @Deprecated("Use PERIODIC_WORK")
    const val WEEKLY_WORK = PERIODIC_WORK

    fun startNow(
        context: Context,
        rateBytes: Long = BackupApi.DEFAULT_RATE_BYTES,
        wifiOnly: Boolean = true,
        chargingOnly: Boolean = false,
        includeScreenshots: Boolean = false,
        includeDownloads: Boolean = false,
    ) {
        val request = OneTimeWorkRequestBuilder<BackupWorker>()
            .setInputData(workDataOf(
                "rate_bytes" to rateBytes,
                "include_screenshots" to includeScreenshots,
                "include_downloads" to includeDownloads,
            ))
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(if (wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
                    .setRequiresCharging(chargingOnly)
                    .build()
            )
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .addTag("david-pi-backup")
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            MANUAL_WORK, ExistingWorkPolicy.KEEP, request
        )
    }

    fun schedulePeriodic(context: Context, settings: BackupSettings) {
        if ("device_backup" !in CredentialStore(context).enabledModules) { cancelAll(context); return }
        val frequency = settings.frequency
        if (frequency == BackupFrequency.OFF) {
            val manager = WorkManager.getInstance(context)
            manager.cancelUniqueWork(PERIODIC_WORK)
            manager.cancelUniqueWork(INTEGRITY_WORK)
            return
        }
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(if (settings.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
            .setRequiresCharging(settings.chargingOnly)
            .build()
        val request = PeriodicWorkRequestBuilder<BackupWorker>(
            requireNotNull(frequency.repeatInterval),
            requireNotNull(frequency.repeatUnit),
        )
            .setInputData(workDataOf(
                "rate_bytes" to BackupPreferences.rateBytes(settings),
                "include_screenshots" to settings.includeScreenshots,
                "include_downloads" to settings.includeDownloads,
            ))
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .addTag("david-pi-backup")
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            PERIODIC_WORK, ExistingPeriodicWorkPolicy.UPDATE, request
        )
        val integrityRequest = PeriodicWorkRequestBuilder<BackupWorker>(
            INTEGRITY_INTERVAL_DAYS,
            TimeUnit.DAYS,
        )
            .setInputData(workDataOf(
                "integrity_only" to true,
                "rate_bytes" to BackupPreferences.rateBytes(settings),
                "include_screenshots" to settings.includeScreenshots,
                "include_downloads" to settings.includeDownloads,
            ))
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(
                        if (settings.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED
                    )
                    .setRequiresCharging(true)
                    .build()
            )
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .addTag("david-pi-integrity-revalidation")
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            INTEGRITY_WORK,
            ExistingPeriodicWorkPolicy.UPDATE,
            integrityRequest,
        )
    }

    fun pause(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(MANUAL_WORK)
    }

    fun cancelAutomatic(context: Context) {
        val manager = WorkManager.getInstance(context)
        AUTOMATIC_WORK_NAMES.forEach(manager::cancelUniqueWork)
    }

    fun cancelAll(context: Context) {
        val manager = WorkManager.getInstance(context)
        manager.cancelUniqueWork(MANUAL_WORK)
        cancelAutomatic(context)
    }
}
