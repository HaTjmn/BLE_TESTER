#ifndef FILE_INTEGRITY_DIAG_H
#define FILE_INTEGRITY_DIAG_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * BLE-D4.0F -- absolute-offset canonical reconstruction of the File
 * Transfer Data stream, with byte-level duplicate/conflict detection.
 * Unlike file_transfer_hash.c (a streaming hash over the arrival-order
 * accepted stream, built for BLE-D4.0C's crash-isolation purpose), this
 * reconstructs into a buffer addressed by ABSOLUTE FILE OFFSET so a
 * retry re-delivering an already-received range doesn't disturb the
 * canonical result, and so a conflicting redelivery (same offset,
 * different bytes) is caught and counted rather than silently
 * overwritten. This is what lets the resulting hash represent "the file
 * by position," not "whatever happened to arrive last."
 */

void file_integrity_diag_init(void);

/* Allocates a buffer sized to expected_size and resets all counters.
 * Returns false (and starts nothing) if expected_size is 0 or too large
 * for a deliberately-bounded diagnostic buffer (this task's own
 * restriction: one small file only) or allocation fails. */
bool file_integrity_diag_start(uint32_t expected_size);
void file_integrity_diag_stop(void);   /* frees the buffer; safe to call when not started */
bool file_integrity_diag_is_active(void);

/* Called from the BLE_GAP_EVENT_NOTIFY_RX path for File Transfer Data
 * notifications while active. Cheap: fixed-size buffer writes and byte
 * compares only, no allocation, no logging -- still runs on the NimBLE
 * host task. `connection_generation` is ble_central's own connection
 * counter (bumped on every successful GAP connect), recorded per-byte on
 * first arrival so a later conflict can report which connection each
 * side of it came from. */
void file_integrity_diag_ingest(const uint8_t *data, size_t len, uint32_t connection_generation);

typedef struct {
    uint32_t offset;
    uint8_t  old_byte;
    uint8_t  new_byte;
    uint32_t old_generation;
    uint32_t new_generation;
} file_integrity_conflict_t;

typedef struct {
    uint32_t expected_size;
    uint32_t notifications_received;
    uint32_t unique_bytes_received;
    uint32_t identical_duplicate_bytes;
    uint32_t conflicting_duplicate_bytes;
    uint32_t out_of_range_chunks;
    bool     has_conflict;
    file_integrity_conflict_t first_conflict;
    bool     complete;              /* every byte 0..expected_size-1 received */
    bool     has_missing;
    uint32_t first_missing_offset;  /* only meaningful if !complete */
    uint32_t crc32;                 /* only meaningful if complete -- computed from the canonical buffer */
    char     sha256_hex[65];        /* only meaningful if complete */
} file_integrity_diag_summary_t;

/* Computes canonical CRC32/SHA-256 from the offset-addressed buffer
 * (fields are only meaningful once `complete`) and returns the full
 * summary. Safe to call repeatedly -- this hashes a stable buffer on
 * demand, not an incremental streaming context. */
void file_integrity_diag_get_summary(file_integrity_diag_summary_t *out);

#ifdef __cplusplus
}
#endif

#endif /* FILE_INTEGRITY_DIAG_H */
