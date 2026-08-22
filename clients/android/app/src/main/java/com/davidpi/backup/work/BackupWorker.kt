package com.davidpi.backup.work

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.pm.ServiceInfo
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.work.*
import com.davidpi.backup.data.BackupDatabase
import com.davidpi.backup.media.MediaScanner
import com.davidpi.backup.net.BackupApi
import com.davidpi.backup.net.ApiException
import com.davidpi.backup.net.BackupFailurePolicy
import com.davidpi.backup.security.CredentialStore
import java.util.concurrent.TimeUnit

class BackupWorker(context: Context, parameters: WorkerParameters) :
    CoroutineWorker(context, parameters) {
    override suspend fun doWork(): Result {
        try {
            setForeground(progress("Checking your library…"))
        } catch (error: Exception) {
            return Result.failure(
                workDataOf("error" to "Android could not start the backup notification.")
            )
        }
        val database = BackupDatabase.get(applicationContext)
        val scanner = MediaScanner(applicationContext, database.dao())
        if (!scanner.hasFullAccess()) return Result.failure(
            workDataOf("error" to "Full photo and video permission is required.")
        )
        scanner.scan(
            includeScreenshots = inputData.getBoolean("include_screenshots", false),
            includeDownloads = inputData.getBoolean("include_downloads", false)
        )
        val api = BackupApi(
            applicationContext.contentResolver,
            CredentialStore(applicationContext),
            database.dao()
        )
        var completed = 0
        while (!isStopped) {
            val item = database.dao().nextPending() ?: break
            try {
                setForeground(progress("Backing up ${item.displayName}", completed))
                setProgress(workDataOf("status" to "Backing up media to David-Pi…"))
            } catch (error: Exception) {
                return Result.retry()
            }
            try {
                api.upload(item, inputData.getLong("rate_bytes", BackupApi.DEFAULT_RATE_BYTES))
                completed++
            } catch (error: Exception) {
                if (error is ApiException && error.status == 401) {
                    CredentialStore(applicationContext).clear()
                    return Result.failure(workDataOf(
                        "error" to "Backup access was removed. Open the app and pair this phone again."
                    ))
                }
                if (BackupFailurePolicy.isPermanent(error)) {
                    val apiError = error as ApiException
                    database.dao().update(
                        item.copy(
                            state = "permanent_error",
                            retryCount = item.retryCount + 1,
                            errorCode = apiError.errorCode.take(80),
                            updatedAt = System.currentTimeMillis()
                        )
                    )
                    setProgress(workDataOf(
                        "status" to "Skipped one unreadable item and kept going."
                    ))
                    continue
                }
                database.dao().update(
                    item.copy(
                        state = "retryable_error",
                        retryCount = item.retryCount + 1,
                        errorCode = error.javaClass.simpleName.take(80),
                        updatedAt = System.currentTimeMillis()
                    )
                )
                setProgress(workDataOf("status" to BackupStatusText.retryMessage(error)))
                return Result.retry()
            }
        }
        return Result.success(workDataOf("completed" to completed))
    }

    private fun progress(message: String, completed: Int = 0): ForegroundInfo {
        val manager = applicationContext.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel("backup", "Photo backup", NotificationManager.IMPORTANCE_LOW)
        )
        val notification = NotificationCompat.Builder(applicationContext, "backup")
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("David-Pi Backup")
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
}

object BackupStatusText {
    fun describe(
        state: WorkInfo.State?,
        runAttemptCount: Int = 0,
        progress: String = "",
    ): String = when {
        progress.isNotBlank() -> progress
        state == WorkInfo.State.RUNNING -> "Backing up media to David-Pi…"
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

object BackupScheduler {
    const val MANUAL_WORK = "david-pi-manual-backup"
    const val PERIODIC_WORK = "david-pi-weekly-reconciliation"
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
        val frequency = settings.frequency
        if (frequency == BackupFrequency.OFF) {
            WorkManager.getInstance(context).cancelUniqueWork(PERIODIC_WORK)
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
    }

    fun pause(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(MANUAL_WORK)
    }

    fun cancelAll(context: Context) {
        val manager = WorkManager.getInstance(context)
        manager.cancelUniqueWork(MANUAL_WORK)
        manager.cancelUniqueWork(PERIODIC_WORK)
    }
}
