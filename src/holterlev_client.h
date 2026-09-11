#ifndef HOLTERLEV_CLIENT_H
#define HOLTERLEV_CLIENT_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Thin semantic layer over ble_central's raw GATT primitives, built
 * strictly against the frozen contract in holterlev_protocol_ref.h
 * (mirroring the real firmware's docs/ble/PROTOCOL.md). Only implements
 * commands whose BLE contract is already live in that firmware: AUTH,
 * GET_STATUS (as a raw Status read, which the protocol documents as
 * always current with no authorization gate), START, STOP, and Device
 * Info.
 */

void holterlev_client_init(void);

/* Each returns false immediately if a semantic or raw operation is
 * already in flight -- caller sends its own BUSY response in that case. */
bool holterlev_client_connect(int64_t request_id, uint32_t timeout_ms, const char *addr, const char *name);
bool holterlev_client_authenticate(int64_t request_id, uint32_t timeout_ms, const char *code);
bool holterlev_client_get_status(int64_t request_id, uint32_t timeout_ms);
bool holterlev_client_get_device_info(int64_t request_id, uint32_t timeout_ms);
bool holterlev_client_get_session_status(int64_t request_id, uint32_t timeout_ms);
bool holterlev_client_start_session(int64_t request_id, uint32_t timeout_ms, uint32_t duration_seconds);
bool holterlev_client_stop_session(int64_t request_id, uint32_t timeout_ms);
bool holterlev_client_wait_state(int64_t request_id, uint32_t timeout_ms, uint8_t target_session_state);

/* True if `request_id` belongs to a semantic operation still in flight
 * -- command_dispatcher routes ble_central completion callbacks here
 * instead of finalizing them itself when this returns true. */
bool holterlev_client_owns_request(int64_t request_id);

void holterlev_client_handle_connected(int64_t request_id);
void holterlev_client_handle_connect_failed(int64_t request_id, const char *error_code, const char *detail);
void holterlev_client_handle_svc_disc_done(int64_t request_id);
void holterlev_client_handle_chr_disc_done(int64_t request_id);
void holterlev_client_handle_write_done(int64_t request_id);
void holterlev_client_handle_read_done(int64_t request_id, const uint8_t *data, size_t len);
void holterlev_client_handle_op_error(int64_t request_id, const char *error_code, const char *detail);
void holterlev_client_handle_op_timeout(int64_t request_id);

#ifdef __cplusplus
}
#endif

#endif /* HOLTERLEV_CLIENT_H */
