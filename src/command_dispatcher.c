#include "command_dispatcher.h"
#include "uart_command_server.h"
#include "protocol_json.h"
#include "ble_central.h"
#include "holterlev_client.h"
#include "holterlev_protocol_ref.h"

#include <string.h>
#include <stdlib.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#define FIRMWARE_VERSION      "0.1.0"
#define UART_PROTOCOL_VERSION "1.0"

#define MIRROR_DATA_MAX   128
#define EVENT_QUEUE_DEPTH 16

typedef struct {
    char event[24];
    char characteristic[BLE_CENTRAL_UUID_STR_LEN];
    char data_hex[2 * MIRROR_DATA_MAX + 1];
} mirrored_event_t;

static QueueHandle_t s_event_queue;

/* ---------------------------------------------------------------------
 * Small JSON field helpers
 * ------------------------------------------------------------------- */

static const char *get_string(cJSON *root, const char *key)
{
    cJSON *v = cJSON_GetObjectItemCaseSensitive(root, key);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

static bool get_bool(cJSON *root, const char *key, bool def)
{
    cJSON *v = cJSON_GetObjectItemCaseSensitive(root, key);
    if (cJSON_IsBool(v)) return cJSON_IsTrue(v);
    return def;
}

static bool require_uint32(int64_t id, cJSON *root, const char *key, uint32_t *out)
{
    cJSON *v = cJSON_GetObjectItemCaseSensitive(root, key);
    if (!cJSON_IsNumber(v) || v->valuedouble < 0) {
        protocol_json_send_error(id, "MISSING_FIELD", key);
        return false;
    }
    *out = (uint32_t)v->valuedouble;
    return true;
}

static uint32_t optional_uint32(cJSON *root, const char *key, uint32_t def)
{
    cJSON *v = cJSON_GetObjectItemCaseSensitive(root, key);
    if (cJSON_IsNumber(v) && v->valuedouble >= 0) return (uint32_t)v->valuedouble;
    return def;
}

static bool require_string(int64_t id, cJSON *root, const char *key, const char **out)
{
    const char *v = get_string(root, key);
    if (v == NULL) {
        protocol_json_send_error(id, "MISSING_FIELD", key);
        return false;
    }
    *out = v;
    return true;
}

static const char *state_str(ble_central_state_t s)
{
    switch (s) {
    case BLE_CENTRAL_STATE_IDLE: return "IDLE";
    case BLE_CENTRAL_STATE_SCANNING: return "SCANNING";
    case BLE_CENTRAL_STATE_CONNECTING: return "CONNECTING";
    case BLE_CENTRAL_STATE_CONNECTED: return "CONNECTED";
    case BLE_CENTRAL_STATE_DISCOVERING: return "DISCOVERING";
    case BLE_CENTRAL_STATE_READY: return "READY";
    case BLE_CENTRAL_STATE_DISCONNECTING: return "DISCONNECTING";
    case BLE_CENTRAL_STATE_ERROR: return "ERROR";
    default: return "UNKNOWN";
    }
}

static bool parse_session_state(const char *s, uint8_t *out)
{
    if (strcasecmp(s, "INITIALIZING") == 0) { *out = HLV_SESSION_INITIALIZING; return true; }
    if (strcasecmp(s, "READY") == 0)        { *out = HLV_SESSION_READY; return true; }
    if (strcasecmp(s, "PREPARING") == 0)    { *out = HLV_SESSION_PREPARING; return true; }
    if (strcasecmp(s, "RECORDING") == 0)    { *out = HLV_SESSION_RECORDING; return true; }
    if (strcasecmp(s, "STOPPING") == 0)     { *out = HLV_SESSION_STOPPING; return true; }
    if (strcasecmp(s, "ERROR") == 0)        { *out = HLV_SESSION_ERROR; return true; }
    return false;
}

static bool is_all_ascii_digits(const char *s, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        if (s[i] < '0' || s[i] > '9') return false;
    }
    return true;
}

/* ---------------------------------------------------------------------
 * Event mirroring for wait_event
 * ------------------------------------------------------------------- */

static void mirror_push(const char *event, const char *characteristic, const uint8_t *data, size_t len)
{
    mirrored_event_t me;
    memset(&me, 0, sizeof(me));
    strlcpy(me.event, event, sizeof(me.event));
    if (characteristic != NULL) {
        strlcpy(me.characteristic, characteristic, sizeof(me.characteristic));
    }
    if (data != NULL && len > 0) {
        size_t capped = len > MIRROR_DATA_MAX ? MIRROR_DATA_MAX : len;
        protocol_json_hex_encode(data, capped, me.data_hex, sizeof(me.data_hex));
    }
    xQueueSend(s_event_queue, &me, 0);
}

/* ---------------------------------------------------------------------
 * ble_central completion callbacks (declared in ble_central.h)
 * ------------------------------------------------------------------- */

void command_dispatcher_on_device_found(const char *addr, const char *name, int rssi)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "addr", addr);
    cJSON_AddStringToObject(j, "name", name);
    cJSON_AddNumberToObject(j, "rssi", rssi);
    protocol_json_send_event("device_found", j);
    mirror_push("device_found", NULL, NULL, 0);
}

void command_dispatcher_on_scan_done(int64_t request_id, int devices_found)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddNumberToObject(j, "devices_found", devices_found);
    protocol_json_send_ok(request_id, j);
    mirror_push("scan_complete", NULL, NULL, 0);
}

void command_dispatcher_on_connected(int64_t request_id, const char *addr, const char *name)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_connected(request_id);
        return;
    }
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "addr", addr);
    cJSON_AddStringToObject(j, "name", name);
    protocol_json_send_ok(request_id, j);
}

void command_dispatcher_on_connect_failed(int64_t request_id, const char *error_code, const char *detail)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_connect_failed(request_id, error_code, detail);
        return;
    }
    protocol_json_send_error(request_id, error_code, detail);
}

void command_dispatcher_on_disconnected(const char *reason)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "reason", reason);
    protocol_json_send_event("disconnected", j);
    mirror_push("disconnected", NULL, NULL, 0);
}

void command_dispatcher_on_svc_disc_done(int64_t request_id, const ble_central_service_t *svcs, int count)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_svc_disc_done(request_id);
        return;
    }
    cJSON *arr = cJSON_CreateArray();
    for (int i = 0; i < count; i++) {
        cJSON *s = cJSON_CreateObject();
        cJSON_AddStringToObject(s, "uuid", svcs[i].uuid);
        cJSON_AddNumberToObject(s, "start_handle", svcs[i].start_handle);
        cJSON_AddNumberToObject(s, "end_handle", svcs[i].end_handle);
        cJSON_AddItemToArray(arr, s);
    }
    cJSON *j = cJSON_CreateObject();
    cJSON_AddItemToObject(j, "services", arr);
    cJSON_AddNumberToObject(j, "count", count);
    protocol_json_send_ok(request_id, j);
}

void command_dispatcher_on_chr_disc_done(int64_t request_id, const ble_central_characteristic_t *chrs, int count)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_chr_disc_done(request_id);
        return;
    }
    cJSON *arr = cJSON_CreateArray();
    for (int i = 0; i < count; i++) {
        cJSON *c = cJSON_CreateObject();
        cJSON_AddStringToObject(c, "uuid", chrs[i].uuid);
        cJSON_AddNumberToObject(c, "def_handle", chrs[i].def_handle);
        cJSON_AddNumberToObject(c, "val_handle", chrs[i].val_handle);
        cJSON_AddNumberToObject(c, "properties", chrs[i].properties);
        cJSON_AddItemToArray(arr, c);
    }
    cJSON *j = cJSON_CreateObject();
    cJSON_AddItemToObject(j, "characteristics", arr);
    cJSON_AddNumberToObject(j, "count", count);
    protocol_json_send_ok(request_id, j);
}

void command_dispatcher_on_read_done(int64_t request_id, const uint8_t *data, size_t len)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_read_done(request_id, data, len);
        return;
    }
    char hex[2 * BLE_CENTRAL_MAX_READ_LEN + 1];
    protocol_json_hex_encode(data, len, hex, sizeof(hex));
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "data_hex", hex);
    cJSON_AddNumberToObject(j, "length", (double)len);
    protocol_json_send_ok(request_id, j);
}

void command_dispatcher_on_write_done(int64_t request_id)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_write_done(request_id);
        return;
    }
    protocol_json_send_ok(request_id, NULL);
}

void command_dispatcher_on_subscribe_done(int64_t request_id, bool subscribed)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "subscribed", subscribed);
    protocol_json_send_ok(request_id, j);
}

void command_dispatcher_on_op_error(int64_t request_id, const char *error_code, const char *detail)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_op_error(request_id, error_code, detail);
        return;
    }
    protocol_json_send_error(request_id, error_code, detail);
}

void command_dispatcher_on_op_timeout(int64_t request_id)
{
    if (holterlev_client_owns_request(request_id)) {
        holterlev_client_handle_op_timeout(request_id);
        return;
    }
    protocol_json_send_error(request_id, "TIMEOUT", "operation exceeded timeout_ms");
}

void command_dispatcher_on_notification(uint16_t val_handle, const char *char_uuid,
                                         const uint8_t *data, size_t len, bool is_indication)
{
    char hex[2 * BLE_CENTRAL_MAX_READ_LEN + 1];
    protocol_json_hex_encode(data, len, hex, sizeof(hex));
    cJSON *j = cJSON_CreateObject();
    if (char_uuid != NULL) cJSON_AddStringToObject(j, "characteristic", char_uuid);
    cJSON_AddNumberToObject(j, "handle", val_handle);
    cJSON_AddStringToObject(j, "data_hex", hex);
    cJSON_AddBoolToObject(j, "indication", is_indication);
    protocol_json_send_event("notification", j);
    mirror_push("notification", char_uuid, data, len);
}

/* ---------------------------------------------------------------------
 * Command handlers
 * ------------------------------------------------------------------- */

static void cmd_ping(int64_t id, cJSON *root)
{
    (void)root;
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "pong", true);
    protocol_json_send_ok(id, j);
}

static void cmd_version(int64_t id, cJSON *root)
{
    (void)root;
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "firmware_version", FIRMWARE_VERSION);
    cJSON_AddStringToObject(j, "uart_protocol_version", UART_PROTOCOL_VERSION);
    cJSON_AddNumberToObject(j, "holterlev_ble_protocol_major", HLV_PROTOCOL_VERSION_MAJOR);
    cJSON_AddNumberToObject(j, "holterlev_ble_protocol_minor", HLV_PROTOCOL_VERSION_MINOR);
    cJSON_AddStringToObject(j, "expected_advertised_name", HLV_ADVERTISED_NAME);
    protocol_json_send_ok(id, j);
}

static void cmd_controller_status(int64_t id, cJSON *root)
{
    (void)root;
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "state", state_str(ble_central_get_state()));
    cJSON_AddBoolToObject(j, "connected", ble_central_is_connected());
    if (ble_central_is_connected()) {
        cJSON_AddStringToObject(j, "peer_addr", ble_central_peer_addr_str());
        cJSON_AddStringToObject(j, "peer_name", ble_central_peer_name());
        cJSON_AddNumberToObject(j, "mtu", ble_central_negotiated_mtu());
    }
    protocol_json_send_ok(id, j);
}

static void cmd_scan(int64_t id, cJSON *root)
{
    uint32_t timeout_ms;
    if (!require_uint32(id, root, "timeout_ms", &timeout_ms)) return;
    if (ble_central_get_state() != BLE_CENTRAL_STATE_IDLE) {
        protocol_json_send_error(id, "INVALID_STATE", "controller is not IDLE");
        return;
    }
    const char *name = get_string(root, "name");
    const char *addr = get_string(root, "addr");
    const char *svc = get_string(root, "service_uuid");
    if (!ble_central_scan_start(id, timeout_ms, name, addr, svc)) {
        protocol_json_send_error(id, "SCAN_FAILED", "could not start scan");
    }
    /* else: terminal response arrives later via command_dispatcher_on_scan_done */
}

static void cmd_scan_stop(int64_t id, cJSON *root)
{
    (void)root;
    if (ble_central_scan_stop()) {
        protocol_json_send_ok(id, NULL);
    } else {
        protocol_json_send_error(id, "INVALID_STATE", "not currently scanning");
    }
}

static void cmd_connect(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 10000);
    const char *name = get_string(root, "name");
    const char *addr = get_string(root, "addr");
    if (name == NULL && addr == NULL) {
        protocol_json_send_error(id, "MISSING_FIELD", "name or addr");
        return;
    }
    if (ble_central_get_state() != BLE_CENTRAL_STATE_IDLE) {
        protocol_json_send_error(id, "INVALID_STATE", "controller is not IDLE");
        return;
    }
    if (!ble_central_connect_start(id, timeout_ms, addr, name)) {
        protocol_json_send_error(id, "CONNECT_FAILED", "could not start connect");
    }
}

static void cmd_disconnect(int64_t id, cJSON *root)
{
    (void)root;
    if (ble_central_disconnect_start(id)) {
        protocol_json_send_ok(id, NULL);
    } else {
        protocol_json_send_error(id, "INVALID_STATE", "not connected or connecting");
    }
}

static void cmd_discover_services(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 8000);
    if (!ble_central_disc_services_start(id, timeout_ms)) {
        protocol_json_send_error(id, "INVALID_STATE", "not connected");
    }
}

static void cmd_discover_characteristics(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 8000);
    const char *svc = get_string(root, "service_uuid");
    if (!ble_central_disc_characteristics_start(id, timeout_ms, svc)) {
        protocol_json_send_error(id, "INVALID_STATE", "not connected, or service_uuid not found");
    }
}

static void cmd_read(int64_t id, cJSON *root)
{
    const char *uuid;
    if (!require_string(id, root, "characteristic", &uuid)) return;
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!ble_central_read_start(id, timeout_ms, uuid)) {
        protocol_json_send_error(id, "INVALID_STATE", "not READY, or characteristic not discovered");
    }
}

static void cmd_write(int64_t id, cJSON *root)
{
    const char *uuid;
    if (!require_string(id, root, "characteristic", &uuid)) return;
    const char *data_hex;
    if (!require_string(id, root, "data_hex", &data_hex)) return;
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    bool with_response = get_bool(root, "with_response", true);

    uint8_t buf[BLE_CENTRAL_MAX_READ_LEN];
    int len = protocol_json_hex_decode(data_hex, buf, sizeof(buf));
    if (len < 0) {
        protocol_json_send_error(id, "INVALID_ARGUMENT", "data_hex is not valid hex");
        return;
    }
    if (!ble_central_write_start(id, timeout_ms, uuid, buf, (size_t)len, with_response)) {
        protocol_json_send_error(id, "INVALID_STATE", "not READY, or characteristic not discovered");
    }
}

static void cmd_subscribe(int64_t id, cJSON *root)
{
    const char *uuid;
    if (!require_string(id, root, "characteristic", &uuid)) return;
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    const char *mode = get_string(root, "mode");
    bool indicate = (mode != NULL && strcasecmp(mode, "indicate") == 0);
    if (!ble_central_subscribe_start(id, timeout_ms, uuid, indicate)) {
        protocol_json_send_error(id, "INVALID_STATE", "not READY, or characteristic not discovered");
    }
}

static void cmd_unsubscribe(int64_t id, cJSON *root)
{
    const char *uuid;
    if (!require_string(id, root, "characteristic", &uuid)) return;
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!ble_central_unsubscribe_start(id, timeout_ms, uuid)) {
        protocol_json_send_error(id, "INVALID_STATE", "not READY, or characteristic not discovered");
    }
}

static void cmd_wait_event(int64_t id, cJSON *root)
{
    uint32_t timeout_ms;
    if (!require_uint32(id, root, "timeout_ms", &timeout_ms)) return;
    const char *filter = get_string(root, "event");

    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    mirrored_event_t me;
    while (xTaskGetTickCount() < deadline) {
        TickType_t remaining = deadline - xTaskGetTickCount();
        if (xQueueReceive(s_event_queue, &me, remaining) != pdTRUE) {
            break;
        }
        if (filter == NULL || strcmp(filter, me.event) == 0) {
            cJSON *j = cJSON_CreateObject();
            cJSON_AddStringToObject(j, "event", me.event);
            if (me.characteristic[0] != '\0') cJSON_AddStringToObject(j, "characteristic", me.characteristic);
            if (me.data_hex[0] != '\0') cJSON_AddStringToObject(j, "data_hex", me.data_hex);
            protocol_json_send_ok(id, j);
            return;
        }
    }
    protocol_json_send_error(id, "TIMEOUT", "no matching event within timeout_ms");
}

static void cmd_clear_bonds(int64_t id, cJSON *root)
{
    (void)root;
    ble_central_clear_bonds();
    protocol_json_send_ok(id, NULL);
}

static void cmd_reset_controller(int64_t id, cJSON *root)
{
    (void)root;
    ble_central_reset();
    holterlev_client_init();
    protocol_json_send_ok(id, NULL);
}

static void cmd_holter_connect(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 10000);
    const char *name = get_string(root, "name");
    const char *addr = get_string(root, "addr");
    if (name == NULL && addr == NULL) {
        protocol_json_send_error(id, "MISSING_FIELD", "name or addr");
        return;
    }
    if (!holterlev_client_connect(id, timeout_ms, addr, name)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress");
    }
}

static void cmd_holter_authenticate(int64_t id, cJSON *root)
{
    const char *code = get_string(root, "code");
    if (code == NULL) code = HLV_DEV_ACCESS_CODE;
    if (strlen(code) != 4 || !is_all_ascii_digits(code, 4)) {
        protocol_json_send_error(id, "INVALID_ARGUMENT", "code must be exactly 4 ASCII digits");
        return;
    }
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!holterlev_client_authenticate(id, timeout_ms, code)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress, or not READY");
    }
}

static void cmd_holter_get_status(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!holterlev_client_get_status(id, timeout_ms)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress, or not READY");
    }
}

static void cmd_holter_get_device_info(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!holterlev_client_get_device_info(id, timeout_ms)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress, or not READY");
    }
}

static void cmd_holter_get_session_status(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!holterlev_client_get_session_status(id, timeout_ms)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress, or not READY");
    }
}

static void cmd_holter_start_session(int64_t id, cJSON *root)
{
    uint32_t duration_seconds;
    if (!require_uint32(id, root, "duration_seconds", &duration_seconds)) return;
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!holterlev_client_start_session(id, timeout_ms, duration_seconds)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress, or not READY");
    }
}

static void cmd_holter_stop_session(int64_t id, cJSON *root)
{
    uint32_t timeout_ms = optional_uint32(root, "timeout_ms", 5000);
    if (!holterlev_client_stop_session(id, timeout_ms)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress, or not READY");
    }
}

static void cmd_holter_wait_state(int64_t id, cJSON *root)
{
    uint32_t timeout_ms;
    if (!require_uint32(id, root, "timeout_ms", &timeout_ms)) return;
    const char *target_str = get_string(root, "target_state");
    uint8_t target;
    if (target_str != NULL) {
        if (!parse_session_state(target_str, &target)) {
            protocol_json_send_error(id, "INVALID_ARGUMENT", "unrecognized target_state name");
            return;
        }
    } else {
        cJSON *v = cJSON_GetObjectItemCaseSensitive(root, "target_state");
        if (!cJSON_IsNumber(v)) {
            protocol_json_send_error(id, "MISSING_FIELD", "target_state");
            return;
        }
        target = (uint8_t)v->valuedouble;
    }
    if (!holterlev_client_wait_state(id, timeout_ms, target)) {
        protocol_json_send_error(id, "BUSY", "another operation is already in progress, or not READY");
    }
}

/* ---------------------------------------------------------------------
 * Line entry point
 * ------------------------------------------------------------------- */

void command_dispatcher_init(void)
{
    s_event_queue = xQueueCreate(EVENT_QUEUE_DEPTH, sizeof(mirrored_event_t));
    ble_central_init();
    holterlev_client_init();
    uart_command_server_init(command_dispatcher_handle_line);
}

void command_dispatcher_handle_line(char *line)
{
    cJSON *root = protocol_json_parse_line(line);
    if (root == NULL) {
        protocol_json_send_parse_error("line is not a valid JSON object");
        free(line);
        return;
    }

    cJSON *id_field = cJSON_GetObjectItemCaseSensitive(root, "id");
    if (!cJSON_IsNumber(id_field)) {
        protocol_json_send_parse_error("missing or non-numeric \"id\" field");
        cJSON_Delete(root);
        free(line);
        return;
    }
    int64_t id = (int64_t)id_field->valuedouble;

    cJSON *cmd_field = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    if (!cJSON_IsString(cmd_field)) {
        protocol_json_send_error(id, "MISSING_FIELD", "cmd");
        cJSON_Delete(root);
        free(line);
        return;
    }
    const char *cmd = cmd_field->valuestring;

    if (strcmp(cmd, "ping") == 0) cmd_ping(id, root);
    else if (strcmp(cmd, "version") == 0) cmd_version(id, root);
    else if (strcmp(cmd, "controller_status") == 0) cmd_controller_status(id, root);
    else if (strcmp(cmd, "scan") == 0) cmd_scan(id, root);
    else if (strcmp(cmd, "scan_stop") == 0) cmd_scan_stop(id, root);
    else if (strcmp(cmd, "connect") == 0) cmd_connect(id, root);
    else if (strcmp(cmd, "disconnect") == 0) cmd_disconnect(id, root);
    else if (strcmp(cmd, "discover_services") == 0) cmd_discover_services(id, root);
    else if (strcmp(cmd, "discover_characteristics") == 0) cmd_discover_characteristics(id, root);
    else if (strcmp(cmd, "read") == 0) cmd_read(id, root);
    else if (strcmp(cmd, "write") == 0) cmd_write(id, root);
    else if (strcmp(cmd, "subscribe") == 0) cmd_subscribe(id, root);
    else if (strcmp(cmd, "unsubscribe") == 0) cmd_unsubscribe(id, root);
    else if (strcmp(cmd, "wait_event") == 0) cmd_wait_event(id, root);
    else if (strcmp(cmd, "clear_bonds") == 0) cmd_clear_bonds(id, root);
    else if (strcmp(cmd, "reset_controller") == 0) cmd_reset_controller(id, root);
    else if (strcmp(cmd, "holter_connect") == 0) cmd_holter_connect(id, root);
    else if (strcmp(cmd, "holter_authenticate") == 0) cmd_holter_authenticate(id, root);
    else if (strcmp(cmd, "holter_get_status") == 0) cmd_holter_get_status(id, root);
    else if (strcmp(cmd, "holter_get_device_info") == 0) cmd_holter_get_device_info(id, root);
    else if (strcmp(cmd, "holter_get_session_status") == 0) cmd_holter_get_session_status(id, root);
    else if (strcmp(cmd, "holter_start_session") == 0) cmd_holter_start_session(id, root);
    else if (strcmp(cmd, "holter_stop_session") == 0) cmd_holter_stop_session(id, root);
    else if (strcmp(cmd, "holter_wait_state") == 0) cmd_holter_wait_state(id, root);
    else protocol_json_send_error(id, "UNKNOWN_COMMAND", cmd);

    cJSON_Delete(root);
    free(line);
}
