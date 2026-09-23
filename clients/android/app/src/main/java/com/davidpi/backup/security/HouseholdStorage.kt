package com.davidpi.backup.security

import android.content.Context
import com.davidpi.backup.net.DavidPiOrigin

/** Legacy files are adopted only after the old bearer credential authenticates their owner. */
object HouseholdStorage {
    private fun bindings(context: Context) = context.getSharedPreferences("household_data_bindings", Context.MODE_PRIVATE)
    fun isLegacy(context: Context): Boolean = bindings(context).getString("legacy_owner", null) == DavidPiOrigin.sessionScope
    fun databaseName(context: Context, base: String): String =
        if (isLegacy(context)) "$base.db" else "$base-${DavidPiOrigin.sessionScope}.db"
    fun preferencesName(context: Context, base: String): String =
        if (isLegacy(context)) base else "$base-${DavidPiOrigin.sessionScope}"
    fun offlineDirectory(context: Context): java.io.File = java.io.File(context.filesDir,
        if (isLegacy(context)) "offline-audiobooks" else "households/${DavidPiOrigin.sessionScope}/offline-audiobooks")

    fun adoptLegacy(context: Context, instance: String, member: String, origin: String) {
        val scope = HouseholdScope.key(instance, member)
        val prefs = bindings(context)
        val existing = prefs.getString("legacy_owner", null)
        check(existing == null || existing == scope) { "The existing offline library belongs to a different person." }
        check(prefs.edit().putString("legacy_owner", scope).putString("origin_$scope", origin).commit())
    }

    suspend fun reconnect(context: Context, origin: String) {
        val prefs = bindings(context)
        val scope = DavidPiOrigin.sessionScope
        val previous = prefs.getString("origin_$scope", null)
        if (previous != null && previous != origin) {
            com.davidpi.backup.offline.OfflineAudiobookDatabase.get(context).dao().reconnectOrigin(previous, origin)
        }
        // Sessions and receipts belong to a device credential. Recheck preserved rows through owner-bound deduplication; never upload a second stored copy.
        com.davidpi.backup.data.BackupDatabase.get(context).dao().reconnectDevice()
        check(prefs.edit().putString("origin_$scope", origin).commit())
    }
}
