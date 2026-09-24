package com.davidpi.backup.security

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.davidpi.backup.net.DavidPiOrigin
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class CredentialStore(context: Context) {
    companion object { private val clearingLock = Any() }
    private val prefs = context.getSharedPreferences("paired_server", Context.MODE_PRIVATE)
    private val alias = "david_pi_backup_device_credential"

    var serverUrl: String?
        get() {
            val raw = prefs.getString("server_url", null) ?: return null
            return DavidPiOrigin.canonicalPairingOrigin(raw) ?: run {
                clear()
                null
            }
        }
        set(value) {
            if (value == null) {
                prefs.edit().remove("server_url").apply()
                return
            }
            val canonical = requireNotNull(DavidPiOrigin.canonicalPairingOrigin(value)) {
                "Only a canonical private Tailscale HTTPS origin can be stored."
            }
            prefs.edit().putString("server_url", canonical).apply()
        }
    val instanceId: String? get() = prefs.getString("instance_id", null)
    val memberId: String? get() = prefs.getString("member_id", null)
    val enabledModules: Set<String> get() = prefs.getStringSet("enabled_modules", emptySet())?.toSet() ?: emptySet()
    fun updateMetadata(identity: org.json.JSONObject) {
        val modules = identity.optJSONArray("enabled_modules") ?: return
        val names = (0 until modules.length()).map { modules.getString(it) }.toSet()
        prefs.edit().putStringSet("enabled_modules", names)
            .putString("display_name", identity.optString("display_name", "David-Pi").take(100)).apply()
    }
    val displayName: String get() = prefs.getString("display_name", "David-Pi") ?: "David-Pi"
    val scope: String get() = if (instanceId != null && memberId != null)
        HouseholdScope.key(instanceId!!, memberId!!) else "unpaired"

    fun bindIdentity(instance: String, member: String, display: String) {
        require(HouseholdScope.validIdentity(instance, member)) { "Server identity is missing or invalid." }
        check(prefs.edit().putString("instance_id", instance).putString("member_id", member)
            .putString("display_name", display.take(100)).commit())
        DavidPiOrigin.approve(requireNotNull(serverUrl), scope)
    }

    fun restoreOrigin(): Boolean {
        val server = serverUrl ?: return false
        if (instanceId == null || memberId == null || credential() == null) return false
        DavidPiOrigin.approve(server, scope)
        return true
    }

    var deviceId: String?
        get() = prefs.getString("device_id", null)
        set(value) { prefs.edit().putString("device_id", value).apply() }
    var deviceName: String?
        get() = prefs.getString("device_name", null)
        set(value) { prefs.edit().putString("device_name", value).apply() }
    var ownerName: String?
        get() = prefs.getString("owner_name", null)
        set(value) { prefs.edit().putString("owner_name", value).apply() }

    fun saveCredential(value: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        prefs.edit()
            .putString("credential", Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .putString("credential_iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .apply()
    }

    fun credential(): String? {
        val storedServer = prefs.getString("server_url", null)
        if (DavidPiOrigin.canonicalPairingOrigin(storedServer) == null) {
            if (
                storedServer != null ||
                prefs.contains("credential") ||
                prefs.contains("credential_iv")
            ) clear()
            return null
        }
        val encrypted = prefs.getString("credential", null) ?: return null
        val iv = prefs.getString("credential_iv", null) ?: return null
        return runCatching {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(
                Cipher.DECRYPT_MODE, key(),
                GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP))
            )
            String(
                cipher.doFinal(Base64.decode(encrypted, Base64.NO_WRAP)),
                Charsets.UTF_8
            )
        }.getOrNull()
    }

    fun clear(expectedScope: String? = null, expectedDeviceId: String? = null) = synchronized(clearingLock) {
        if (expectedScope != null && expectedScope != scope) return@synchronized
        if (expectedDeviceId != null && expectedDeviceId != deviceId) return@synchronized
        prefs.edit().clear().commit()
        DavidPiOrigin.disconnect()
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (store.containsAlias(alias)) store.deleteEntry(alias)
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build()
        )
        return generator.generateKey()
    }
}

/** Installation and person identities are independent of mutable names or hostnames. */
object HouseholdScope {
    fun validIdentity(instance: String, member: String): Boolean =
        runCatching { java.util.UUID.fromString(instance).toString() == instance }.getOrDefault(false) &&
            member.isNotBlank() && member.length <= 200 && member.none { it.isISOControl() }
    fun key(instance: String, member: String): String {
        require(validIdentity(instance, member))
        return java.security.MessageDigest.getInstance("SHA-256")
            .digest("$instance\n$member".toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }
}
