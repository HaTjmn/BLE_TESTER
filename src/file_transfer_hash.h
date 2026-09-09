#ifndef FILE_TRANSFER_HASH_H
#define FILE_TRANSFER_HASH_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * BLE-D4.0C Step 6 -- a diagnostic O(1)-memory streaming hash over the
 * HolterLEV File Transfer Data characteristic's payload stream, computed
 * LOCALLY on BLE_TESTER. Originally (Test A) this REPLACED per-
 * notification forwarding entirely, to isolate whether BLE reception
 * alone crashed BLE_TESTER. As of BLE-D4.0I Phase 4, forwarding to
 * Python is the streaming architecture's own required data path, so
 * this mode is purely additive now -- normal notification forwarding
 * (command_dispatcher_on_notification) always runs, hashed or not.
 *
 * BLE-D4.0I Phase 4: the digest is only "canonical" (represents the true
 * file by position, from offset 0) while every accepted chunk satisfied
 * chunk_offset == next_expected_offset with no exception. The first
 * chunk that arrives before or after that offset permanently sets
 * canonical_valid=false for the rest of the attempt (a lost position can
 * never be un-lost by a pure streaming pass) -- callers MUST check that
 * flag (file_transfer_hash_summary_t.canonical_valid, JSON field
 * "valid") before treating crc32/sha256_hex as the file's real digest.
 * A reconnect's new transfer_id does NOT by itself invalidate the
 * digest -- only the offset check does.
 */

void file_transfer_hash_init(void);

/* Enabling always resets all counters/hash state first. Disabling leaves
 * the last summary available via file_transfer_hash_get_summary() until
 * the next enable. */
void file_transfer_hash_enable(bool enable);
bool file_transfer_hash_is_enabled(void);

/* Called from the BLE_GAP_EVENT_NOTIFY_RX path (ble_central.c) ONLY for
 * notifications on the File Transfer Data characteristic, and only while
 * enabled. Must stay cheap -- no logging, no UART -- this still runs on
 * the NimBLE host task. */
void file_transfer_hash_ingest(const uint8_t *data, size_t len);

typedef struct {
    uint32_t boot_generation_start;
    uint32_t boot_generation_end;
    uint32_t notifications_received;
    uint32_t bytes_received;        /* == hashed_byte_count: only bytes actually folded into crc32/sha256 */
    uint32_t next_expected_offset;  /* BLE-D4.0I Phase 4: the one offset that would still extend the canonical digest */
    uint32_t first_offset;
    bool     has_first_offset;
    uint32_t last_offset;
    uint32_t first_gap_offset;
    bool     has_gap;
    uint32_t gap_count;
    uint32_t duplicate_count;       /* any chunk whose offset != next_expected_offset, whether behind or ahead */
    uint32_t out_of_order_count;    /* chunk from a different transfer_id than the one first locked in (informational only -- see canonical_valid) */
    uint32_t reconnect_count;       /* transfer_id changes observed -- a resume convention, not itself a break in order */
    /* BLE-D4.0I Phase 4: true only if every accepted chunk since
     * file_transfer_hash_enable() satisfied chunk_offset ==
     * next_expected_offset, with no exception -- i.e. crc32/sha256_hex
     * represent the file's true canonical byte sequence from offset 0.
     * Once false, stays false for the rest of this attempt (a lost
     * position can never be un-lost by a streaming-only hash). A caller
     * MUST check this before treating crc32/sha256_hex as the file's
     * real digest -- they are still computed and returned when false,
     * but they cover only whatever prefix/fragments were actually
     * hashed before/around the break, not the whole file. */
    bool     canonical_valid;
    uint32_t crc32;
    char     sha256_hex[65];
    uint32_t elapsed_ms;
} file_transfer_hash_summary_t;

/* Finalizes the SHA-256 digest and returns the summary. The FIRST call
 * finalizes and caches the digest; ingest() silently stops accepting
 * further data after that (a hardware-backed SHA context may not support
 * a safe "snapshot" mid-stream, so finishing is one-way). Call this only
 * once a test attempt is done observing -- not repeatedly mid-transfer. */
void file_transfer_hash_get_summary(file_transfer_hash_summary_t *out);

#ifdef __cplusplus
}
#endif

#endif /* FILE_TRANSFER_HASH_H */
