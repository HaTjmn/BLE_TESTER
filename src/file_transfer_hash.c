#include "file_transfer_hash.h"
#include "ble_central.h"

#include <string.h>
#include <stdio.h>

#include "mbedtls/sha256.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define CHUNK_HEADER_LEN 12

static bool     s_enabled = false;
static bool     s_have_transfer_id = false;
static uint16_t s_transfer_id = 0;
static uint32_t s_expected_offset = 0;
static bool     s_canonical_valid = false;

static uint32_t s_notifications = 0;
static uint32_t s_bytes = 0;
static bool     s_has_first_offset = false;
static uint32_t s_first_offset = 0;
static uint32_t s_last_offset = 0;
static bool     s_has_gap = false;
static uint32_t s_first_gap_offset = 0;
static uint32_t s_gap_count = 0;
static uint32_t s_duplicate_count = 0;
static uint32_t s_out_of_order_count = 0;
static uint32_t s_reconnect_count = 0;

static uint32_t s_crc32 = 0xFFFFFFFFU;
static mbedtls_sha256_context s_sha_ctx;
static bool     s_digest_valid = false;
static uint8_t  s_digest[32];

static uint32_t s_boot_generation_start = 0;
static int64_t  s_start_tick_ms = 0;

/* Standard zlib-compatible CRC-32 (poly 0xEDB88320, reflected), computed
 * byte-at-a-time -- deliberately NOT esp_rom_crc32_le(), whose init/xorout
 * convention isn't guaranteed to match Python's zlib.crc32() without
 * separate verification, and getting that wrong would fabricate a false
 * "corruption" finding. This matches zlib.crc32() bit-for-bit. */
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

static int64_t now_ms_local(void)
{
    return (int64_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
}

void file_transfer_hash_init(void)
{
    mbedtls_sha256_init(&s_sha_ctx);
}

void file_transfer_hash_enable(bool enable)
{
    if (enable) {
        s_have_transfer_id = false;
        s_transfer_id = 0;
        s_expected_offset = 0;
        s_notifications = 0;
        s_bytes = 0;
        s_has_first_offset = false;
        s_first_offset = 0;
        s_last_offset = 0;
        s_has_gap = false;
        s_first_gap_offset = 0;
        s_gap_count = 0;
        s_duplicate_count = 0;
        s_out_of_order_count = 0;
        s_reconnect_count = 0;
        s_canonical_valid = true;
        s_crc32 = 0xFFFFFFFFU;

        mbedtls_sha256_free(&s_sha_ctx);
        mbedtls_sha256_init(&s_sha_ctx);
        mbedtls_sha256_starts(&s_sha_ctx, 0 /* SHA-256, not SHA-224 */);
        s_digest_valid = false;

        ble_central_boot_info_t b;
        ble_central_get_boot_info(&b);
        s_boot_generation_start = b.boot_generation;
        s_start_tick_ms = now_ms_local();
    }
    s_enabled = enable;
}

bool file_transfer_hash_is_enabled(void) { return s_enabled; }

void file_transfer_hash_ingest(const uint8_t *data, size_t len)
{
    if (!s_enabled) return;
    if (s_digest_valid) return; /* a summary was already finalized -- see get_summary()'s doc comment */
    if (len < CHUNK_HEADER_LEN) return; /* malformed/truncated -- ignore, don't crash on it */

    uint16_t transfer_id     = (uint16_t)(data[0] | (data[1] << 8));
    uint32_t chunk_offset    = (uint32_t)(data[2] | (data[3] << 8) | (data[4] << 16) | ((uint32_t)data[5] << 24));
    /* total_length (bytes[6..9]) isn't needed for hashing/counting here. */
    uint16_t fragment_length = (uint16_t)(data[10] | (data[11] << 8));

    size_t available = len - CHUNK_HEADER_LEN;
    if (fragment_length > available) fragment_length = (uint16_t)available;

    s_notifications++;
    s_last_offset = chunk_offset;
    if (!s_has_first_offset) {
        s_has_first_offset = true;
        s_first_offset = chunk_offset;
    }

    /* BLE-D4.0I Phase 4: this codebase's own resume convention issues a
     * new transfer_id when a download continues after a reconnect -- that
     * is NOT itself a break in canonical byte order (the file is still
     * the same logical file, picking up where it left off). Track the
     * change for visibility only; the offset check below is the ONLY
     * thing allowed to gate hashing or invalidate the digest, so a
     * reconnect that resumes exactly at next_expected_offset must not
     * stop the hash. */
    if (!s_have_transfer_id) {
        s_have_transfer_id = true;
        s_transfer_id = transfer_id;
    } else if (transfer_id != s_transfer_id) {
        s_transfer_id = transfer_id;
        s_out_of_order_count++;
        s_reconnect_count++;
    }

    if (!s_canonical_valid) {
        /* Order was already broken earlier in this attempt -- a streaming
         * hash can never recover a lost position, so every subsequent
         * chunk (even a perfectly in-order one from here on) is still
         * counted for observability but never hashed again. */
        if (chunk_offset < s_expected_offset) s_duplicate_count++;
        else if (chunk_offset > s_expected_offset) s_gap_count++;
        return;
    }

    if (chunk_offset != s_expected_offset) {
        /* Strictly BEFORE or AFTER next_expected_offset -- either way the
         * digest can no longer represent "the file by position" from a
         * pure streaming pass, so it is marked invalid HERE, permanently,
         * rather than either silently skipping (which could hide a lost
         * tail-of-chunk on a partial overlap) or silently hashing anyway
         * (the bug this replaces: a forward gap used to be hashed as if
         * contiguous, corrupting the digest without any signal). */
        if (chunk_offset < s_expected_offset) {
            s_duplicate_count++;
        } else {
            s_gap_count++;
            if (!s_has_gap) {
                s_has_gap = true;
                s_first_gap_offset = chunk_offset;
            }
        }
        s_canonical_valid = false;
        return;
    }

    const uint8_t *fragment = data + CHUNK_HEADER_LEN;
    s_crc32 = crc32_update(s_crc32, fragment, fragment_length);
    mbedtls_sha256_update(&s_sha_ctx, fragment, fragment_length);
    s_bytes += fragment_length;
    s_expected_offset = chunk_offset + fragment_length;
}

void file_transfer_hash_get_summary(file_transfer_hash_summary_t *out)
{
    memset(out, 0, sizeof(*out));
    out->boot_generation_start = s_boot_generation_start;
    ble_central_boot_info_t b;
    ble_central_get_boot_info(&b);
    out->boot_generation_end = b.boot_generation;
    out->notifications_received = s_notifications;
    out->bytes_received = s_bytes;
    out->next_expected_offset = s_expected_offset;
    out->first_offset = s_first_offset;
    out->has_first_offset = s_has_first_offset;
    out->last_offset = s_last_offset;
    out->first_gap_offset = s_first_gap_offset;
    out->has_gap = s_has_gap;
    out->gap_count = s_gap_count;
    out->duplicate_count = s_duplicate_count;
    out->out_of_order_count = s_out_of_order_count;
    out->reconnect_count = s_reconnect_count;
    out->canonical_valid = s_canonical_valid;
    out->crc32 = s_crc32 ^ 0xFFFFFFFFU;

    /* mbedtls_sha256_context on this target may wrap a hardware SHA engine
     * that cannot be safely struct-copied to take a "snapshot" digest
     * mid-stream -- so finishing is a one-way operation: the first call
     * finalizes and caches the digest; ingest() then refuses further
     * updates (checked above) so the cached value stays correct. Call
     * this only once a test attempt is actually done observing. */
    if (!s_digest_valid) {
        mbedtls_sha256_finish(&s_sha_ctx, s_digest);
        s_digest_valid = true;
    }
    for (int i = 0; i < 32; i++) {
        snprintf(&out->sha256_hex[i * 2], 3, "%02x", s_digest[i]);
    }

    out->elapsed_ms = (uint32_t)(now_ms_local() - s_start_tick_ms);
}
