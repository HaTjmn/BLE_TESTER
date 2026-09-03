#ifndef BLE_CENTRAL_H
#define BLE_CENTRAL_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BLE_CENTRAL_MAX_SERVICES        12
#define BLE_CENTRAL_MAX_CHARACTERISTICS 24
#define BLE_CENTRAL_MAX_READ_LEN        512
#define BLE_CENTRAL_UUID_STR_LEN        37 /* 36 chars + NUL */

typedef enum {
    BLE_CENTRAL_STATE_IDLE = 0,
    BLE_CENTRAL_STATE_SCANNING,
    BLE_CENTRAL_STATE_CONNECTING,
    BLE_CENTRAL_STATE_CONNECTED,
    BLE_CENTRAL_STATE_DISCOVERING,
    BLE_CENTRAL_STATE_READY,
    BLE_CENTRAL_STATE_DISCONNECTING,
    BLE_CENTRAL_STATE_ERROR,
} ble_central_state_t;

typedef struct {
    char     uuid[BLE_CENTRAL_UUID_STR_LEN];
    uint16_t start_handle;
    uint16_t end_handle;
} ble_central_service_t;

typedef struct {
    char     uuid[BLE_CENTRAL_UUID_STR_LEN];
    uint16_t def_handle;
    uint16_t val_handle;
    uint8_t  properties; /* raw NimBLE ble_gatt_chr.properties bitmask */
} ble_central_characteristic_t;

/* One pending async operation at a time (matches the real HolterLEV
 * device's own V1 scope: one active connection, one thing in flight).
 * A second request while one is pending is rejected BUSY at the
 * dispatch layer, not queued -- deterministic, simple, testable. */
typedef enum {
    BLE_CENTRAL_OP_NONE = 0,
    BLE_CENTRAL_OP_SCAN,
    BLE_CENTRAL_OP_CONNECT,
    BLE_CENTRAL_OP_DISC_SERVICES,
    BLE_CENTRAL_OP_DISC_CHARACTERISTICS,
    BLE_CENTRAL_OP_READ,
    BLE_CENTRAL_OP_WRITE,
    BLE_CENTRAL_OP_SUBSCRIBE,
    BLE_CENTRAL_OP_UNSUBSCRIBE,
} ble_central_op_t;

/* Dispatcher-side notifications. Called from whatever context completes
 * the operation: immediately (a local validation failure), from the
 * NimBLE host task (a real GAP/GATT callback), or from ble_central's own
 * timeout timer. Never called with a stale/superseded result. */
void command_dispatcher_on_device_found(const char *addr, const char *name, int rssi);
void command_dispatcher_on_scan_done(int64_t request_id, int devices_found);
void command_dispatcher_on_connected(int64_t request_id, const char *addr, const char *name);
void command_dispatcher_on_connect_failed(int64_t request_id, const char *error_code, const char *detail);
void command_dispatcher_on_disconnected(const char *reason);
void command_dispatcher_on_svc_disc_done(int64_t request_id, const ble_central_service_t *svcs, int count);
void command_dispatcher_on_chr_disc_done(int64_t request_id, const ble_central_characteristic_t *chrs, int count);
void command_dispatcher_on_read_done(int64_t request_id, const uint8_t *data, size_t len);
void command_dispatcher_on_write_done(int64_t request_id);
void command_dispatcher_on_subscribe_done(int64_t request_id, bool subscribed);
void command_dispatcher_on_op_error(int64_t request_id, const char *error_code, const char *detail);
void command_dispatcher_on_op_timeout(int64_t request_id);
void command_dispatcher_on_notification(uint16_t val_handle, const char *char_uuid, const uint8_t *data, size_t len, bool is_indication);

void ble_central_init(void);

ble_central_state_t ble_central_get_state(void);
bool ble_central_is_connected(void);
const char *ble_central_peer_addr_str(void);
const char *ble_central_peer_name(void);
uint16_t ble_central_negotiated_mtu(void);

/* Every *_start below returns false immediately (no dispatcher_on_*
 * callback will ever fire for this request_id) if another operation is
 * already pending or the current state does not permit it -- caller
 * (command_dispatcher) is expected to have already checked state and
 * should treat false as its own immediate BUSY/INVALID_STATE response. */

bool ble_central_scan_start(int64_t request_id, uint32_t timeout_ms,
                             const char *name_filter, const char *addr_filter,
                             const char *service_uuid_filter);
bool ble_central_scan_stop(void);

bool ble_central_connect_start(int64_t request_id, uint32_t timeout_ms,
                                 const char *addr, const char *name);
bool ble_central_disconnect_start(int64_t request_id);

bool ble_central_disc_services_start(int64_t request_id, uint32_t timeout_ms);
bool ble_central_disc_characteristics_start(int64_t request_id, uint32_t timeout_ms,
                                              const char *service_uuid);

bool ble_central_read_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid);
bool ble_central_write_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid,
                               const uint8_t *data, size_t len, bool with_response);
bool ble_central_subscribe_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid,
                                   bool indicate);
bool ble_central_unsubscribe_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid);

void ble_central_clear_bonds(void);

/* Soft reset: cancels/forgets any pending operation and forces a
 * disconnect if connected, returning to IDLE. Does not reboot the chip. */
void ble_central_reset(void);

/* Looks up a previously-discovered characteristic's value handle by
 * UUID string. Returns 0xFFFF if not found (0 is never a valid ATT
 * handle). */
uint16_t ble_central_find_char_val_handle(const char *char_uuid);

#ifdef __cplusplus
}
#endif

#endif /* BLE_CENTRAL_H */
