#ifndef PROTOCOL_JSON_H
#define PROTOCOL_JSON_H

#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * NDJSON framing for the UART automation protocol: one complete JSON
 * object per line. All send functions are safe to call from any task
 * (NimBLE host task included) -- the actual UART write is serialized by
 * uart_command_server so a response and an unsolicited event can never
 * interleave into one corrupted line.
 */

/* Parse one complete line (no trailing '\n') into a cJSON tree.
 * Returns NULL if `line` is not valid JSON or is not a JSON object. */
cJSON *protocol_json_parse_line(const char *line);

/* Sends {"type":"response","id":id,"status":"ok", <extra's own fields
 * merged in>}. Takes ownership of `extra` (may be NULL) -- deletes it. */
void protocol_json_send_ok(int64_t id, cJSON *extra);

/* Sends {"type":"response","id":id,"status":"error","error":error_code,
 * "detail":detail}. `detail` may be NULL. */
void protocol_json_send_error(int64_t id, const char *error_code, const char *detail);

/* Same as protocol_json_send_error(), plus extra's own fields merged in
 * (BLE-D4.0B Step 3 structured diagnostics). Takes ownership of `extra`
 * (may be NULL) -- deletes it. */
void protocol_json_send_error_ex(int64_t id, const char *error_code, const char *detail, cJSON *extra);

/* Sends {"type":"response","id":null,"status":"error","error":
 * "PARSE_ERROR","detail":detail} -- used only when the request's own id
 * could not be determined at all (unparseable line, or id field missing
 * or not a number). */
void protocol_json_send_parse_error(const char *detail);

/* Sends {"type":"event","event":event_name, <extra's own fields merged
 * in>}. Takes ownership of `extra` (may be NULL) -- deletes it. */
void protocol_json_send_event(const char *event_name, cJSON *extra);

/* Encodes `len` bytes as a lowercase hex string into `out` (out_cap must
 * be >= 2*len + 1). Returns true on success. */
bool protocol_json_hex_encode(const uint8_t *data, size_t len, char *out, size_t out_cap);

/* Decodes a lowercase/uppercase hex string (even length) into `out`
 * (out_cap bytes). Returns decoded byte count, or -1 on malformed input
 * or insufficient out_cap. */
int protocol_json_hex_decode(const char *hex, uint8_t *out, size_t out_cap);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_JSON_H */
