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

    if (!s_have_transfer_id) {
        s_have_transfer_id = true;
        s_transfer_id = transfer_id;
    } else if (transfer_id != s_transfer_id) {
        s_out_of_order_count++;
        return;
    }

    if (chunk_offset < s_expected_offset) {
        s_duplicate_count++;
        return;
    }
    if (chunk_offset > s_expected_offset) {
        s_gap_count++;
        if (!s_has_gap) {
            s_has_gap = true;
            s_first_gap_offset = chunk_offset;
        }
        /* Diagnostic mode: keep observing forward rather than stalling the
         * whole session on one gap -- Test A cares about whether BLE_TESTER
         * survives sustained reception, not about producing a valid file. */
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
    out->first_offset = s_first_offset;
    out->has_first_offset = s_has_first_offset;
    out->last_offset = s_last_offset;
    out->first_gap_offset = s_first_gap_offset;
    out->has_gap = s_has_gap;
    out->gap_count = s_gap_count;
    out->duplicate_count = s_duplicate_count;
    out->out_of_order_count = s_out_of_order_count;
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
