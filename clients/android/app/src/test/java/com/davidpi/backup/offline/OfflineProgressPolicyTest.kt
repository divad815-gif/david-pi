package com.davidpi.backup.offline

import org.junit.Assert.*
import org.junit.Test

class OfflineProgressPolicyTest {
    private fun book(sequence: Long = 2) = OfflineAudiobook(
        id = "a".repeat(32), title = "Book", author = "Author", series = "",
        durationSeconds = 300.0, byteSize = 100, contentType = "audio/mpeg",
        remoteUrl = "", coverUrl = "", localFileName = "book.audio", state = "ready",
        positionSeconds = 80.0, progressScope = "b".repeat(32), progressRevision = 4,
        progressSession = "c".repeat(32), progressSequence = sequence, progressDirty = true,
    )

    @Test fun acknowledgementPreservesNewerListeningEventAndAdvancesItsBase() {
        val current = book(3)
        val acknowledged = OfflineProgressPolicy.acknowledge(current, current.progressSession, 2, 4, 5)
        assertTrue(acknowledged.progressDirty)
        assertEquals(3, acknowledged.progressSequence)
        assertEquals(5, acknowledged.progressRevision)
        assertEquals(current.positionSeconds, acknowledged.positionSeconds, 0.0)
    }

    @Test fun acknowledgementClearsOnlyTheExactLastEvent() {
        val current = book()
        val acknowledged = OfflineProgressPolicy.acknowledge(current, current.progressSession, 2, 4, 5)
        assertFalse(acknowledged.progressDirty)
        assertEquals(5, acknowledged.progressRevision)
        assertEquals(acknowledged, OfflineProgressPolicy.acknowledge(acknowledged, current.progressSession, 2, 4, 5))
    }

    @Test fun staleOrForgedAcknowledgementsCannotEraseListeningProgress() {
        val current = book()
        assertEquals(current, OfflineProgressPolicy.acknowledge(current, "d".repeat(32), 2, 4, 5))
        assertEquals(current, OfflineProgressPolicy.acknowledge(current, current.progressSession, 3, 4, 5))
        assertEquals(current, OfflineProgressPolicy.acknowledge(current, current.progressSession, 2, 3, 4))
        assertEquals(current, OfflineProgressPolicy.acknowledge(current, current.progressSession, 2, 4, 10))
    }
}
