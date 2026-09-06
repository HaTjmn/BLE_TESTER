#ifndef FILE_TRANSFER_HASH_H
#define FILE_TRANSFER_HASH_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * BLE-D4.0C Step 6 -- a diagnostic receive mode for the HolterLEV File
 * Transfer Data characteristic that hashes the payload stream LOCALLY on
 * BLE_TESTER, without forwarding per-notification JSON over UART. This is
 * how Test A removes UART serialization and Python entirely from the data
 * path, to determine whether BLE notification reception alone is what
 * crashes BLE_TESTER.
 *
 * Purely additive: normal notification forwarding (command_dispatcher_on_
 * notification) is untouched and still runs whenever this mode is
 * disabled -- see file_transfer_hash_enable()'s own doc comment for how
 * the two interact.
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
    uint32_t bytes_received;
    uint32_t first_offset;
    bool     has_first_offset;
    uint32_t last_offset;
    uint32_t first_gap_offset;
    bool     has_gap;
    uint32_t gap_count;
    uint32_t duplicate_count;
    uint32_t out_of_order_count;   /* chunk from a different transfer_id than the one first locked in */
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
