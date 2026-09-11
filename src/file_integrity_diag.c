#include "file_integrity_diag.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#include "mbedtls/sha256.h"

#define CHUNK_HEADER_LEN 12

/* BLE-D4.0F: "one small file only" -- this is a diagnostic buffer, not a
 * general download path. 256KB comfortably covers this task's 17,280-byte
 * file with a large margin while still refusing anything that would eat
 * an unreasonable share of BLE_TESTER's ~240KB free heap. */
#define MAX_DIAG_FILE_SIZE (256U * 1024U)

static uint8_t  *s_buf = NULL;
static bool     *s_received = NULL;
static uint16_t *s_gen_at_offset = NULL;
static uint32_t  s_expected_size = 0;
static bool      s_active = false;

static bool     s_have_transfer_id = false;
static uint16_t s_transfer_id_locked = 0;

static uint32_t s_notifications = 0;
static uint32_t s_unique_bytes = 0;
static uint32_t s_identical_duplicates = 0;
static uint32_t s_conflicting_duplicates = 0;
static uint32_t s_out_of_range_chunks = 0;
static bool     s_has_conflict = false;
static file_integrity_conflict_t s_first_conflict;

static uint32_t crc32_update(uint32_t crc, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            uint32_t mask = -(crc & 1U);
            crc = (crc >> 1) ^ (0xEDB88320U & mask);
        }
    }
    return crc;
}

void file_integrity_diag_init(void)
{
    /* Nothing to do at boot -- buffers are allocated per-attempt by
     * file_integrity_diag_start(), not held for the process lifetime. */
}

bool file_integrity_diag_start(uint32_t expected_size)
{
    file_integrity_diag_stop();

    if (expected_size == 0 || expected_size > MAX_DIAG_FILE_SIZE) {
        return false;
    }

    s_buf = malloc(expected_size);
    s_received = calloc(expected_size, sizeof(bool));
    s_gen_at_offset = calloc(expected_size, sizeof(uint16_t));
    if (s_buf == NULL || s_received == NULL || s_gen_at_offset == NULL) {
        file_integrity_diag_stop();
        return false;
    }

    s_expected_size = expected_size;
    s_have_transfer_id = false;
    s_transfer_id_locked = 0;
    s_notifications = 0;
    s_unique_bytes = 0;
    s_identical_duplicates = 0;
    s_conflicting_duplicates = 0;
    s_out_of_range_chunks = 0;
    s_has_conflict = false;
    memset(&s_first_conflict, 0, sizeof(s_first_conflict));

    s_active = true;
    return true;
}

void file_integrity_diag_stop(void)
{
    free(s_buf);
    free(s_received);
    free(s_gen_at_offset);
    s_buf = NULL;
    s_received = NULL;
    s_gen_at_offset = NULL;
    s_expected_size = 0;
    s_active = false;
}

bool file_integrity_diag_is_active(void) { return s_active; }

void file_integrity_diag_ingest(const uint8_t *data, size_t len, uint32_t connection_generation)
{
    if (!s_active) return;
    if (len < CHUNK_HEADER_LEN) return;

    uint16_t transfer_id     = (uint16_t)(data[0] | (data[1] << 8));
    uint32_t chunk_offset    = (uint32_t)(data[2] | (data[3] << 8) | (data[4] << 16) | ((uint32_t)data[5] << 24));
    uint16_t fragment_length = (uint16_t)(data[10] | (data[11] << 8));

    size_t available = len - CHUNK_HEADER_LEN;
    if (fragment_length > available) fragment_length = (uint16_t)available;

    s_notifications++;

    if (!s_have_transfer_id) {
        s_have_transfer_id = true;
        s_transfer_id_locked = transfer_id;
    }
    /* A different transfer_id is still the SAME logical file resuming
     * from wherever it left off (this codebase's own resume design) --
     * not itself an out-of-range condition. Only the absolute offset
     * range matters for this task's own validation rule. */

    /* Step 1/2: validate offset and length against the expected file
     * size; reject and count anything out of range rather than writing
     * past the canonical buffer. */
    if (chunk_offset >= s_expected_size ||
        (uint64_t)chunk_offset + fragment_length > (uint64_t)s_expected_size) {
        s_out_of_range_chunks++;
        return;
    }

    const uint8_t *fragment = data + CHUNK_HEADER_LEN;
    uint16_t gen16 = (uint16_t)(connection_generation & 0xFFFFU);

    for (uint16_t i = 0; i < fragment_length; i++) {
        uint32_t off = chunk_offset + i;
        uint8_t byte = fragment[i];
        if (!s_received[off]) {
            s_buf[off] = byte;
            s_received[off] = true;
            s_gen_at_offset[off] = gen16;
            s_unique_bytes++;
        } else if (s_buf[off] == byte) {
            s_identical_duplicates++;
        } else {
            /* Step 4/5: a conflicting redelivery -- never silently
             * overwrite. Count it, and record the FIRST one in full
             * detail (offset, both byte values, both generations). */
            s_conflicting_duplicates++;
            if (!s_has_conflict) {
                s_has_conflict = true;
                s_first_conflict.offset = off;
                s_first_conflict.old_byte = s_buf[off];
                s_first_conflict.new_byte = byte;
                s_first_conflict.old_generation = s_gen_at_offset[off];
                s_first_conflict.new_generation = gen16;
            }
            /* Buffer keeps the FIRST-seen byte at this offset -- a
             * conflict is evidence to report, not a value to silently
             * replace with whichever copy arrived more recently. */
        }
    }
}

void file_integrity_diag_get_summary(file_integrity_diag_summary_t *out)
{
    memset(out, 0, sizeof(*out));
    out->expected_size = s_expected_size;
    out->notifications_received = s_notifications;
    out->unique_bytes_received = s_unique_bytes;
    out->identical_duplicate_bytes = s_identical_duplicates;
    out->conflicting_duplicate_bytes = s_conflicting_duplicates;
    out->out_of_range_chunks = s_out_of_range_chunks;
    out->has_conflict = s_has_conflict;
    out->first_conflict = s_first_conflict;

    if (!s_active || s_expected_size == 0) return;

    out->complete = (s_unique_bytes >= s_expected_size);
    if (!out->complete) {
        for (uint32_t i = 0; i < s_expected_size; i++) {
            if (!s_received[i]) {
                out->has_missing = true;
                out->first_missing_offset = i;
                break;
            }
        }
        return; /* CRC32/SHA-256 only meaningful once complete */
    }

    uint32_t crc = crc32_update(0xFFFFFFFFU, s_buf, s_expected_size);
    out->crc32 = crc ^ 0xFFFFFFFFU;

    uint8_t digest[32];
    mbedtls_sha256(s_buf, s_expected_size, digest, 0);
    for (int i = 0; i < 32; i++) {
        snprintf(&out->sha256_hex[i * 2], 3, "%02x", digest[i]);
    }
}
