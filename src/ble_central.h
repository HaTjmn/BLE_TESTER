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

/* -----------------------------------------------------------------------
 * BLE-D4.0B diagnostics -- WHY a *_start() call returned false, the real
 * HCI disconnect reason, and a bounded state-transition trace. Purely
 * additive: no *_start() signature or externally-visible success path
 * changes; command_dispatcher.c uses these to turn one generic
 * INVALID_STATE into the specific rejected condition.
 * --------------------------------------------------------------------- */

typedef enum {
    BLE_CENTRAL_REJECT_NONE = 0,
    BLE_CENTRAL_REJECT_LINK_NOT_CONNECTED,             /* no GAP connection at all */
    BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING,         /* s_pending.active */
    BLE_CENTRAL_REJECT_CENTRAL_NOT_READY,              /* connected but not yet READY (mid-discovery, etc.) */
    BLE_CENTRAL_REJECT_CHARACTERISTIC_NOT_DISCOVERED,  /* val handle lookup failed */
    BLE_CENTRAL_REJECT_SERVICE_NOT_DISCOVERED,         /* service_uuid lookup failed */
    BLE_CENTRAL_REJECT_PAYLOAD_TOO_LARGE,
    BLE_CENTRAL_REJECT_INVALID_ARGUMENT,
} ble_central_reject_reason_t;

typedef struct {
    ble_central_reject_reason_t reason;
    ble_central_state_t         state;
    uint16_t                    conn_handle;
    bool                        pending_active;
    ble_central_op_t            pending_op;
    int64_t                     pending_request_id;
    char                        characteristic_uuid[BLE_CENTRAL_UUID_STR_LEN];
    bool                        characteristic_cached;
} ble_central_reject_info_t;

/* Snapshot of the most recent *_start() rejection (single global slot --
 * safe here because exactly one UART command is dispatched/processed at a
 * time, matching this codebase's existing one-pending-op design). */
const ble_central_reject_info_t *ble_central_last_reject_info(void);

typedef struct {
    bool     valid;               /* false until the first disconnect is ever observed */
    int32_t  reason_code;         /* raw HCI/NimBLE reason, preserved even if unrecognized */
    char     reason_name[28];     /* best-effort symbolic name, "UNKNOWN_0xNN" if not recognized */
    bool     locally_initiated;   /* true if THIS side called ble_gap_terminate/disc_cancel */
    uint16_t conn_handle;
    uint8_t  previous_state;      /* ble_central_state_t at the moment of disconnect */
    uint8_t  pending_op_at_disconnect;
    int64_t  pending_request_id_at_disconnect;
    bool     had_pending_op;
    uint32_t connection_duration_ms;
    int64_t  timestamp_ms;
} ble_central_disconnect_info_t;

const ble_central_disconnect_info_t *ble_central_last_disconnect_info(void);

#define BLE_CENTRAL_TRACE_DEPTH 64

typedef struct {
    int64_t  timestamp_ms;
    uint8_t  prev_state;
    uint8_t  new_state;
    char     event[24];
    uint16_t conn_handle;
    uint8_t  pending_op;
    int64_t  pending_request_id;
    int32_t  result_code;   /* 0 = n/a, else an HCI/NimBLE status or reason */
} ble_central_trace_entry_t;

/* Total transitions recorded since boot (monotonically increasing, may
 * exceed BLE_CENTRAL_TRACE_DEPTH -- only the most recent DEPTH are kept). */
uint32_t ble_central_trace_total_count(void);

/* Fetches the trace entry `logical_index` slots back from the most recent
 * (0 = most recent). Returns false if that far back is no longer retained
 * or was never recorded. */
bool ble_central_trace_get(uint32_t logical_index, ble_central_trace_entry_t *out);

/* -----------------------------------------------------------------------
 * BLE-D4.0C boot/reset and runtime-health diagnostics.
 * --------------------------------------------------------------------- */

typedef struct {
    int32_t  reset_reason_code;          /* raw esp_reset_reason_t value */
    char     reset_reason_name[20];      /* symbolic name, "UNKNOWN_N" if unrecognized */
    uint32_t boot_generation;            /* survives SW/panic/watchdog reset; power-on/brownout resets it to 1 */
    uint32_t uptime_ms;                  /* since THIS boot, not cumulative */
} ble_central_boot_info_t;

void ble_central_get_boot_info(ble_central_boot_info_t *out);

typedef struct {
    uint32_t free_internal_heap;
    uint32_t minimum_free_internal_heap;   /* lowest ever observed since this boot */
    uint32_t largest_free_internal_block;
    uint32_t notifications_received;       /* all characteristics, since this boot */
    uint32_t notification_bytes_received;
    uint32_t nimble_host_task_stack_highwater_words;  /* uxTaskGetStackHighWaterMark units */
    bool     pending_operation_active;
    uint32_t state_transition_total;
} ble_central_health_t;

void ble_central_get_health(ble_central_health_t *out);

#ifdef __cplusplus
}
#endif

#endif /* BLE_CENTRAL_H */
