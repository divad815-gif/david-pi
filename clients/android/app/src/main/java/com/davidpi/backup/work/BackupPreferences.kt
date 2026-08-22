package com.davidpi.backup.work

import android.content.Context
import com.davidpi.backup.net.BackupApi
import java.util.concurrent.TimeUnit

enum class BackupFrequency(
    val storedValue: String,
    val label: String,
    val repeatInterval: Long?,
    val repeatUnit: TimeUnit?,
) {
    OFF("off", "Manual only", null, null),
    HOURLY("hourly", "Every hour", 1, TimeUnit.HOURS),
    DAILY("daily", "Every day", 1, TimeUnit.DAYS),
    WEEKLY("weekly", "Every week", 7, TimeUnit.DAYS),
    MONTHLY("monthly", "Every month", 30, TimeUnit.DAYS);

    companion object {
        fun fromStored(value: String?): BackupFrequency =
            entries.firstOrNull { it.storedValue == value } ?: WEEKLY
    }
}

data class BackupSettings(
    val frequency: BackupFrequency = BackupFrequency.WEEKLY,
    val wifiOnly: Boolean = true,
    val chargingOnly: Boolean = false,
    val includeScreenshots: Boolean = false,
    val includeDownloads: Boolean = false,
    val rateMiB: Int = 2,
)

object BackupPreferences {
    private const val FILE = "backup_settings"
    private const val FREQUENCY = "frequency"
    private const val WIFI_ONLY = "wifi_only"
    private const val CHARGING_ONLY = "charging_only"
    private const val INCLUDE_SCREENSHOTS = "include_screenshots"
    private const val INCLUDE_DOWNLOADS = "include_downloads"
    private const val RATE_MIB = "rate_mib"

    fun load(context: Context): BackupSettings {
        val values = context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        return BackupSettings(
            frequency = BackupFrequency.fromStored(values.getString(FREQUENCY, null)),
            wifiOnly = values.getBoolean(WIFI_ONLY, true),
            chargingOnly = values.getBoolean(CHARGING_ONLY, false),
            includeScreenshots = values.getBoolean(INCLUDE_SCREENSHOTS, false),
            includeDownloads = values.getBoolean(INCLUDE_DOWNLOADS, false),
            rateMiB = values.getInt(RATE_MIB, 2).coerceIn(1, 20),
        )
    }

    fun save(context: Context, settings: BackupSettings) {
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
            .putString(FREQUENCY, settings.frequency.storedValue)
            .putBoolean(WIFI_ONLY, settings.wifiOnly)
            .putBoolean(CHARGING_ONLY, settings.chargingOnly)
            .putBoolean(INCLUDE_SCREENSHOTS, settings.includeScreenshots)
            .putBoolean(INCLUDE_DOWNLOADS, settings.includeDownloads)
            .putInt(RATE_MIB, settings.rateMiB.coerceIn(1, 20))
            .apply()
    }

    fun rateBytes(settings: BackupSettings): Long =
        settings.rateMiB.coerceIn(1, 20) * 1024L * 1024L
}
