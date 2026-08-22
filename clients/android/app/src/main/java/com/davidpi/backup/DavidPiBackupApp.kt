package com.davidpi.backup

import android.app.Application
import com.davidpi.backup.security.CredentialStore
import com.davidpi.backup.work.BackupPreferences
import com.davidpi.backup.work.BackupScheduler
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions

class DavidPiBackupApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (BuildConfig.FIREBASE_APPLICATION_ID.isNotBlank() && FirebaseApp.getApps(this).isEmpty()) {
            FirebaseApp.initializeApp(this, FirebaseOptions.Builder()
                .setApplicationId(BuildConfig.FIREBASE_APPLICATION_ID)
                .setApiKey(BuildConfig.FIREBASE_API_KEY)
                .setProjectId(BuildConfig.FIREBASE_PROJECT_ID)
                .setGcmSenderId(BuildConfig.FIREBASE_SENDER_ID)
                .build())
        }
        if (CredentialStore(this).credential() != null) {
            BackupScheduler.schedulePeriodic(this, BackupPreferences.load(this))
        } else {
            BackupScheduler.cancelAll(this)
        }
    }
}
