package com.davidpi.backup

import android.app.Application
import com.davidpi.backup.security.CredentialStore
import com.davidpi.backup.work.BackupPreferences
import com.davidpi.backup.work.BackupScheduler

class DavidPiBackupApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (CredentialStore(this).restoreOrigin()) {
            BackupScheduler.schedulePeriodic(this, BackupPreferences.load(this))
        } else {
            BackupScheduler.cancelAll(this)
        }
    }
}
