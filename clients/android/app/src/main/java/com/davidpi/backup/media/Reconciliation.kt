package com.davidpi.backup.media

import com.davidpi.backup.data.BackupDao
import com.davidpi.backup.data.SyncState
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.UUID

data class ReconciliationEnvelope(
    val scanId: String,
    val itemCount: Int,
    val idsSha256: String,
    val visibleClientItemIds: List<String>,
) {
    fun requestJson(): String =
        "{\"scan_id\":\"$scanId\",\"complete\":true," +
            "\"item_count\":$itemCount,\"ids_sha256\":\"$idsSha256\"," +
            "\"visible_client_item_ids\":${ReconciliationProtocol.canonicalJsonArray(visibleClientItemIds)}}"
}

object ReconciliationProtocol {
    /** Matches the server's documented practical complete-scan ceiling. */
    const val MAX_ITEMS = 50_000
    private val portableId = Regex("[A-Za-z0-9._:-]{1,200}")

    fun canonicalIds(values: Collection<String>): List<String> {
        require(values.size <= MAX_ITEMS) {
            "A complete scan supports at most $MAX_ITEMS items; nothing was truncated."
        }
        val canonical = values.onEach {
            require(portableId.matches(it)) { "The complete scan contains an invalid item ID." }
        }.toSortedSet().toList()
        require(canonical.size <= MAX_ITEMS) {
            "A complete scan supports at most $MAX_ITEMS items; nothing was truncated."
        }
        return canonical
    }

    fun digest(canonicalIds: List<String>): String {
        require(canonicalIds == canonicalIds.distinct().sorted()) {
            "IDs must be canonical before digesting."
        }
        val bytes = canonicalJsonArray(canonicalIds).toByteArray(Charsets.US_ASCII)
        return MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }
    }

    fun canonicalJsonArray(canonicalIds: List<String>): String =
        canonicalIds.joinToString(prefix = "[", postfix = "]", separator = ",") {
            "\"$it\""
        }

    fun envelope(
        values: Collection<String>,
        pendingReceipt: String? = null,
        notBeforeScanId: String? = null,
        clockAnchor: String? = null,
        nowMillis: Long = System.currentTimeMillis(),
    ): ReconciliationEnvelope {
        val ids = canonicalIds(values)
        val digest = digest(ids)
        val anchor = ReconciliationClockAnchor.parse(clockAnchor)
        val pending = PendingReconciliationReceipt.parse(pendingReceipt)
            ?.takeUnless { it.scanId == anchor?.rejectedScanId }
        val reusable = pending
            ?.takeIf { it.itemCount == ids.size && it.idsSha256 == digest }
        val receiptFloor = listOfNotNull(notBeforeScanId, pending?.scanId).maxOrNull()
        return ReconciliationEnvelope(
            // Even if an ambiguous request used different content, its ID may
            // already be accepted by the server. A replacement receipt must
            // therefore sort after both acknowledged and pending IDs.
            scanId = reusable?.scanId ?: UuidV7Generator.nextAfter(
                receiptFloor,
                nowMillis = anchor?.serverTimeMillis ?: nowMillis,
                allowClockReset = anchor != null,
            ),
            itemCount = ids.size,
            idsSha256 = digest,
            visibleClientItemIds = ids,
        )
    }
}

data class ReconciliationClockAnchor(
    val rejectedScanId: String,
    val serverTimeMillis: Long,
) {
    fun json(): String = "$rejectedScanId|$serverTimeMillis"

    companion object {
        private const val MAX_UUID_TIMESTAMP_MILLIS = 0x0000ffffffffffffL
        private const val MAX_FUTURE_SKEW_MILLIS = 5 * 60 * 1000L

        fun fromServer(rejectedScanId: String, serverTimeMillis: Long): ReconciliationClockAnchor? {
            val rejectedTimestamp = UuidV7Generator.timestampMillis(rejectedScanId) ?: return null
            if (serverTimeMillis !in 1..MAX_UUID_TIMESTAMP_MILLIS) return null
            if (rejectedTimestamp <= serverTimeMillis + MAX_FUTURE_SKEW_MILLIS) return null
            return ReconciliationClockAnchor(rejectedScanId, serverTimeMillis)
        }

        fun parse(raw: String?): ReconciliationClockAnchor? = runCatching {
            val fields = (raw ?: return null).split('|')
            require(fields.size == 2)
            requireNotNull(fromServer(fields[0], fields[1].toLong()))
        }.getOrNull()
    }
}

data class PendingReconciliationReceipt(
    val scanId: String,
    val itemCount: Int,
    val idsSha256: String,
) {
    fun json(): String = "$scanId|$itemCount|$idsSha256"

    companion object {
        fun parse(raw: String?): PendingReconciliationReceipt? = runCatching {
            val fields = (raw ?: return null).split('|')
            require(fields.size == 3)
            val scanId = fields[0]
            val uuid = UUID.fromString(scanId)
            require(uuid.version() == 7 && uuid.toString() == scanId)
            val itemCount = fields[1].toInt()
            val digest = fields[2]
            require(itemCount in 0..ReconciliationProtocol.MAX_ITEMS)
            require(Regex("[0-9a-f]{64}").matches(digest))
            PendingReconciliationReceipt(scanId, itemCount, digest)
        }.getOrNull()

        fun from(envelope: ReconciliationEnvelope) = PendingReconciliationReceipt(
            envelope.scanId, envelope.itemCount, envelope.idsSha256
        )
    }
}

object ReconciliationEnvelopeStore {
    private const val PENDING_KEY = "pending_complete_reconciliation"
    private const val LAST_ACCEPTED_KEY = "last_accepted_reconciliation"
    private const val CLOCK_ANCHOR_KEY = "reconciliation_server_clock_anchor"

    suspend fun prepare(dao: BackupDao, visibleIds: Collection<String>): ReconciliationEnvelope {
        val envelope = ReconciliationProtocol.envelope(
            visibleIds,
            pendingReceipt = dao.getState(PENDING_KEY),
            notBeforeScanId = PendingReconciliationReceipt.parse(
                dao.getState(LAST_ACCEPTED_KEY)
            )?.scanId,
            clockAnchor = dao.getState(CLOCK_ANCHOR_KEY),
        )
        dao.putState(
            SyncState(PENDING_KEY, PendingReconciliationReceipt.from(envelope).json())
        )
        return envelope
    }

    suspend fun acknowledge(dao: BackupDao, envelope: ReconciliationEnvelope) {
        val current = PendingReconciliationReceipt.parse(dao.getState(PENDING_KEY))
        if (current?.scanId == envelope.scanId && current.idsSha256 == envelope.idsSha256) {
            dao.putState(
                SyncState(LAST_ACCEPTED_KEY, PendingReconciliationReceipt.from(envelope).json())
            )
            dao.deleteState(PENDING_KEY)
        }
    }

    suspend fun discard(dao: BackupDao, scanId: String) {
        val current = PendingReconciliationReceipt.parse(dao.getState(PENDING_KEY))
        if (current?.scanId == scanId) dao.deleteState(PENDING_KEY)
    }

    suspend fun recoverClockSkew(
        dao: BackupDao,
        rejectedScanId: String,
        serverTimeMillis: Long,
    ): Boolean {
        val anchor = ReconciliationClockAnchor.fromServer(
            rejectedScanId, serverTimeMillis
        ) ?: return false
        // Persist the rejected ID first. If the process stops before deleting
        // pending state, prepare() will still exclude that exact bad floor.
        dao.putState(SyncState(CLOCK_ANCHOR_KEY, anchor.json()))
        val current = PendingReconciliationReceipt.parse(dao.getState(PENDING_KEY))
        if (current?.scanId == rejectedScanId) dao.deleteState(PENDING_KEY)
        return true
    }
}

object UuidV7Generator {
    private val random = SecureRandom()
    private var lastTimestamp = -1L
    private var sequence = 0

    @Synchronized
    fun next(nowMillis: Long = System.currentTimeMillis()): String {
        if (nowMillis > lastTimestamp) {
            lastTimestamp = nowMillis
            sequence = random.nextInt(4096)
        } else {
            sequence++
            if (sequence > 4095) {
                lastTimestamp++
                sequence = 0
            }
        }
        val bytes = ByteArray(16).also(random::nextBytes)
        var timestamp = lastTimestamp and 0x0000ffffffffffffL
        for (index in 5 downTo 0) {
            bytes[index] = (timestamp and 0xff).toByte()
            timestamp = timestamp ushr 8
        }
        bytes[6] = (0x70 or ((sequence ushr 8) and 0x0f)).toByte()
        bytes[7] = (sequence and 0xff).toByte()
        bytes[8] = ((bytes[8].toInt() and 0x3f) or 0x80).toByte()
        val buffer = ByteBuffer.wrap(bytes)
        return UUID(buffer.long, buffer.long).toString()
    }

    fun nextAfter(
        previousScanId: String?,
        nowMillis: Long = System.currentTimeMillis(),
        allowClockReset: Boolean = false,
    ): String {
        val previousTimestamp = timestampMillis(previousScanId)
        val targetTimestamp = if (previousTimestamp == null) {
            nowMillis
        } else {
            maxOf(nowMillis, previousTimestamp + 1)
        }
        synchronized(this) {
            // Only a persisted, authenticated server clock anchor may reset a
            // process-local timestamp poisoned by the phone's bad wall clock.
            // The durable accepted/pending receipt remains the ordering floor.
            if (allowClockReset && lastTimestamp > targetTimestamp) {
                lastTimestamp = -1L
                sequence = 0
            }
            return next(targetTimestamp)
        }
    }

    fun timestampMillis(scanId: String?): Long? = runCatching {
        val parsed = UUID.fromString(scanId)
        require(parsed.version() == 7 && parsed.toString() == scanId)
        parsed.toString().replace("-", "").substring(0, 12).toLong(16)
    }.getOrNull()
}
