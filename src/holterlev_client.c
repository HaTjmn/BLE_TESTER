#include "holterlev_client.h"
#include "holterlev_protocol_ref.h"
#include "ble_central.h"
#include "protocol_json.h"

#include <string.h>
#include <stdio.h>
#include <ctype.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

typedef enum {
    SEM_NONE = 0,
    SEM_CONNECT_WAIT_CONNECT,
    SEM_CONNECT_WAIT_SVC,
    SEM_CONNECT_WAIT_CHR,
    SEM_AUTH_WAIT_WRITE,
    SEM_AUTH_POLL_READ,
    SEM_START_WAIT_WRITE,
    SEM_START_POLL_READ,
    SEM_STOP_WAIT_WRITE,
    SEM_STOP_POLL_READ,
    SEM_GET_STATUS_READ,
    SEM_GET_SESSION_STATUS_READ,
    SEM_GET_DEVICE_INFO_READ,
    SEM_WAIT_STATE_POLL,
} sem_phase_t;

static sem_phase_t s_phase = SEM_NONE;
static int64_t     s_req_id = -1;
static TickType_t  s_deadline;
static uint32_t     s_txn_counter = 1;
static uint32_t     s_expected_txn;
static uint8_t      s_target_session_state;

#define DISC_SUB_TIMEOUT_MS 8000U
#define POLL_READ_TIMEOUT_MS 3000U

static void begin(sem_phase_t phase, int64_t request_id, uint32_t timeout_ms)
{
    s_phase = phase;
    s_req_id = request_id;
    s_deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
}

void holterlev_client_init(void)
{
    s_phase = SEM_NONE;
}

bool holterlev_client_owns_request(int64_t request_id)
{
    return s_phase != SEM_NONE && s_req_id == request_id;
}

static size_t encode_message(uint8_t *out, size_t cap, uint16_t opcode, uint32_t txn_id,
                              const uint8_t *payload, uint16_t payload_len)
{
    size_t total = HLV_HEADER_LEN + payload_len;
    if (cap < total) return 0;
    out[0] = HLV_PROTOCOL_VERSION_MAJOR;
    out[1] = HLV_PROTOCOL_VERSION_MINOR;
    out[2] = (uint8_t)(opcode & 0xFF);
    out[3] = (uint8_t)((opcode >> 8) & 0xFF);
    out[4] = (uint8_t)(txn_id & 0xFF);
    out[5] = (uint8_t)((txn_id >> 8) & 0xFF);
    out[6] = (uint8_t)((txn_id >> 16) & 0xFF);
    out[7] = (uint8_t)((txn_id >> 24) & 0xFF);
    out[8] = 0;
    out[9] = 0;
    out[10] = (uint8_t)(payload_len & 0xFF);
    out[11] = (uint8_t)((payload_len >> 8) & 0xFF);
    if (payload_len > 0) memcpy(out + 12, payload, payload_len);
    return total;
}

static bool decode_status(const uint8_t *d, size_t len, hlv_status_t *out)
{
    if (len != HLV_STATUS_PAYLOAD_LEN) return false;
    out->startup_status = d[0];
    out->ads1293_ready = d[1];
    out->sd_ready = d[2];
    out->recording_capable = d[3];
    out->session_state = d[4];
    out->authorized = d[5];
    out->last_command_opcode = (uint16_t)(d[6] | (d[7] << 8));
    out->last_command_transaction_id = (uint32_t)(d[8] | (d[9] << 8) | (d[10] << 16) | (d[11] << 24));
    out->last_command_result = (uint16_t)(d[12] | (d[13] << 8));
    return true;
}

static cJSON *status_to_json(const hlv_status_t *st)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddNumberToObject(j, "startup_status", st->startup_status);
    cJSON_AddBoolToObject(j, "ads1293_ready", st->ads1293_ready != 0);
    cJSON_AddBoolToObject(j, "sd_ready", st->sd_ready != 0);
    cJSON_AddBoolToObject(j, "recording_capable", st->recording_capable != 0);
    cJSON_AddNumberToObject(j, "session_state", st->session_state);
    cJSON_AddBoolToObject(j, "authorized", st->authorized != 0);
    cJSON_AddNumberToObject(j, "last_command_opcode", st->last_command_opcode);
    cJSON_AddNumberToObject(j, "last_command_transaction_id", st->last_command_transaction_id);
    cJSON_AddNumberToObject(j, "last_command_result", st->last_command_result);
    return j;
}

static void fail(const char *code, const char *detail)
{
    int64_t id = s_req_id;
    s_phase = SEM_NONE;
    protocol_json_send_error(id, code, detail);
}

/* ---------------------------------------------------------------------
 * holter_connect
 * ------------------------------------------------------------------- */

bool holterlev_client_connect(int64_t request_id, uint32_t timeout_ms, const char *addr, const char *name)
{
    if (s_phase != SEM_NONE) return false;
    if (!ble_central_connect_start(request_id, timeout_ms, addr, name)) return false;
    begin(SEM_CONNECT_WAIT_CONNECT, request_id, timeout_ms);
    return true;
}

void holterlev_client_handle_connected(int64_t request_id)
{
    if (s_phase != SEM_CONNECT_WAIT_CONNECT) return;
    s_txn_counter = 1;
    if (!ble_central_disc_services_start(request_id, DISC_SUB_TIMEOUT_MS)) {
        fail("DISCOVERY_FAILED", "could not start service discovery after connect");
        return;
    }
    s_phase = SEM_CONNECT_WAIT_SVC;
}

void holterlev_client_handle_connect_failed(int64_t request_id, const char *error_code, const char *detail)
{
    if (s_phase != SEM_CONNECT_WAIT_CONNECT) return;
    (void)request_id;
    fail(error_code, detail);
}

void holterlev_client_handle_svc_disc_done(int64_t request_id)
{
    if (s_phase != SEM_CONNECT_WAIT_SVC) return;
    if (!ble_central_disc_characteristics_start(request_id, DISC_SUB_TIMEOUT_MS, HLV_SERVICE_UUID)) {
        fail("SERVICE_NOT_FOUND", "HolterLEV primary service UUID not present on connected peer");
        return;
    }
    s_phase = SEM_CONNECT_WAIT_CHR;
}

void holterlev_client_handle_chr_disc_done(int64_t request_id)
{
    if (s_phase != SEM_CONNECT_WAIT_CHR) return;
    s_phase = SEM_NONE;

    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "control_point_found", ble_central_find_char_val_handle(HLV_CHAR_CONTROL_POINT_UUID) != 0xFFFF);
    cJSON_AddBoolToObject(j, "status_found", ble_central_find_char_val_handle(HLV_CHAR_STATUS_UUID) != 0xFFFF);
    cJSON_AddBoolToObject(j, "data_found", ble_central_find_char_val_handle(HLV_CHAR_DATA_UUID) != 0xFFFF);
    cJSON_AddBoolToObject(j, "device_info_found", ble_central_find_char_val_handle(HLV_CHAR_DEVICE_INFO_UUID) != 0xFFFF);
    cJSON_AddBoolToObject(j, "command_response_found", ble_central_find_char_val_handle(HLV_CHAR_COMMAND_RESPONSE_UUID) != 0xFFFF);
    cJSON_AddBoolToObject(j, "status_v2_found", ble_central_find_char_val_handle(HLV_CHAR_STATUS_V2_UUID) != 0xFFFF);
    cJSON_AddStringToObject(j, "peer_addr", ble_central_peer_addr_str());
    cJSON_AddNumberToObject(j, "mtu", ble_central_negotiated_mtu());
    protocol_json_send_ok(request_id, j);
}

/* ---------------------------------------------------------------------
 * holter_authenticate / holter_start_session / holter_stop_session
 * -- write Control Point, then poll Status until it reflects our own
 * transaction_id (the ATT write ack only means the byte reached the
 * bounded command queue, not that the command task processed it yet --
 * PROTOCOL.md Section 16 explicitly warns Status timing is not
 * synchronized with command completion).
 * ------------------------------------------------------------------- */

static bool write_control_point(int64_t request_id, uint32_t timeout_ms, uint16_t opcode,
                                 const uint8_t *payload, uint16_t payload_len, sem_phase_t wait_write_phase)
{
    uint8_t buf[HLV_HEADER_LEN + 8];
    uint32_t txn = s_txn_counter++;
    size_t total = encode_message(buf, sizeof(buf), opcode, txn, payload, payload_len);
    if (total == 0) return false;
    if (!ble_central_write_start(request_id, timeout_ms, HLV_CHAR_CONTROL_POINT_UUID, buf, total, true)) {
        return false;
    }
    begin(wait_write_phase, request_id, timeout_ms);
    s_expected_txn = txn;
    return true;
}

bool holterlev_client_authenticate(int64_t request_id, uint32_t timeout_ms, const char *code)
{
    if (s_phase != SEM_NONE) return false;
    return write_control_point(request_id, timeout_ms, HLV_OPCODE_AUTH,
                                 (const uint8_t *)code, 4, SEM_AUTH_WAIT_WRITE);
}

bool holterlev_client_start_session(int64_t request_id, uint32_t timeout_ms, uint32_t duration_seconds)
{
    if (s_phase != SEM_NONE) return false;
    uint8_t payload[4] = {
        (uint8_t)(duration_seconds & 0xFF), (uint8_t)((duration_seconds >> 8) & 0xFF),
        (uint8_t)((duration_seconds >> 16) & 0xFF), (uint8_t)((duration_seconds >> 24) & 0xFF)
    };
    return write_control_point(request_id, timeout_ms, HLV_OPCODE_START, payload, 4, SEM_START_WAIT_WRITE);
}

bool holterlev_client_stop_session(int64_t request_id, uint32_t timeout_ms)
{
    if (s_phase != SEM_NONE) return false;
    return write_control_point(request_id, timeout_ms, HLV_OPCODE_STOP, NULL, 0, SEM_STOP_WAIT_WRITE);
}

static sem_phase_t poll_phase_for_write_phase(sem_phase_t write_phase)
{
    switch (write_phase) {
    case SEM_AUTH_WAIT_WRITE:  return SEM_AUTH_POLL_READ;
    case SEM_START_WAIT_WRITE: return SEM_START_POLL_READ;
    case SEM_STOP_WAIT_WRITE:  return SEM_STOP_POLL_READ;
    default: return SEM_NONE;
    }
}

void holterlev_client_handle_write_done(int64_t request_id)
{
    sem_phase_t poll_phase = poll_phase_for_write_phase(s_phase);
    if (poll_phase == SEM_NONE) return;

    if (!ble_central_read_start(request_id, POLL_READ_TIMEOUT_MS, HLV_CHAR_STATUS_UUID)) {
        fail("READ_FAILED", "could not read Status to confirm command result");
        return;
    }
    s_phase = poll_phase;
}

static const char *result_code_name(uint16_t code)
{
    switch (code) {
    case HLV_RESULT_OK: return "OK";
    case HLV_RESULT_NOT_READY: return "NOT_READY";
    case HLV_RESULT_UNAUTHORIZED: return "UNAUTHORIZED";
    case HLV_RESULT_INVALID_STATE: return "INVALID_STATE";
    case HLV_RESULT_INVALID_ARGUMENT: return "INVALID_ARGUMENT";
    case HLV_RESULT_SD_NOT_READY: return "SD_NOT_READY";
    case HLV_RESULT_INSUFFICIENT_SPACE: return "INSUFFICIENT_SPACE";
    case HLV_RESULT_FILENAME_NAMESPACE_EXHAUSTED: return "FILENAME_NAMESPACE_EXHAUSTED";
    case HLV_RESULT_STORAGE_ERROR: return "STORAGE_ERROR";
    case HLV_RESULT_BUSY: return "BUSY";
    case HLV_RESULT_NOT_SUPPORTED: return "NOT_SUPPORTED";
    case HLV_RESULT_INTERNAL_ERROR: return "INTERNAL_ERROR";
    case HLV_RESULT_ACCEPTED: return "ACCEPTED";
    case HLV_RESULT_TRANSACTION_CONFLICT: return "TRANSACTION_CONFLICT";
    default: return "UNKNOWN";
    }
}

static void retry_or_timeout_poll(int64_t request_id)
{
    if (xTaskGetTickCount() >= s_deadline) {
        fail("TIMEOUT", "Status never reflected our transaction_id within timeout_ms");
        return;
    }
    if (!ble_central_read_start(request_id, POLL_READ_TIMEOUT_MS, HLV_CHAR_STATUS_UUID)) {
        fail("READ_FAILED", "could not re-read Status while polling for command result");
    }
}

void holterlev_client_handle_read_done(int64_t request_id, const uint8_t *data, size_t len)
{
    hlv_status_t st;

    switch (s_phase) {
    case SEM_GET_STATUS_READ:
    case SEM_GET_SESSION_STATUS_READ: {
        bool session_only = (s_phase == SEM_GET_SESSION_STATUS_READ);
        s_phase = SEM_NONE;
        if (!decode_status(data, len, &st)) {
            protocol_json_send_error(request_id, "PROTOCOL_MISMATCH", "Status payload was not 14 bytes");
            return;
        }
        if (session_only) {
            cJSON *j = cJSON_CreateObject();
            cJSON_AddNumberToObject(j, "session_state", st.session_state);
            protocol_json_send_ok(request_id, j);
        } else {
            protocol_json_send_ok(request_id, status_to_json(&st));
        }
        return;
    }

    case SEM_GET_DEVICE_INFO_READ: {
        s_phase = SEM_NONE;
        if (len < 4) {
            protocol_json_send_error(request_id, "PROTOCOL_MISMATCH", "Device Info payload too short");
            return;
        }
        uint8_t fw_len = data[2];
        if ((size_t)(3 + fw_len + 1) > len) {
            protocol_json_send_error(request_id, "PROTOCOL_MISMATCH", "Device Info firmware_version_len overruns payload");
            return;
        }
        uint8_t name_len = data[3 + fw_len];
        if ((size_t)(4 + fw_len + name_len) > len) {
            protocol_json_send_error(request_id, "PROTOCOL_MISMATCH", "Device Info device_name_len overruns payload");
            return;
        }
        char fw[64] = {0}, name[64] = {0};
        memcpy(fw, data + 3, fw_len < sizeof(fw) - 1 ? fw_len : sizeof(fw) - 1);
        memcpy(name, data + 4 + fw_len, name_len < sizeof(name) - 1 ? name_len : sizeof(name) - 1);

        cJSON *j = cJSON_CreateObject();
        cJSON_AddNumberToObject(j, "protocol_major", data[0]);
        cJSON_AddNumberToObject(j, "protocol_minor", data[1]);
        cJSON_AddStringToObject(j, "firmware_version", fw);
        cJSON_AddStringToObject(j, "device_name", name);
        protocol_json_send_ok(request_id, j);
        return;
    }

    case SEM_AUTH_POLL_READ:
    case SEM_START_POLL_READ:
    case SEM_STOP_POLL_READ: {
        if (!decode_status(data, len, &st)) {
            fail("PROTOCOL_MISMATCH", "Status payload was not 14 bytes");
            return;
        }
        if (st.last_command_transaction_id != s_expected_txn) {
            retry_or_timeout_poll(request_id);
            return;
        }
        s_phase = SEM_NONE;
        cJSON *j = status_to_json(&st);
        cJSON_AddStringToObject(j, "result_name", result_code_name(st.last_command_result));
        protocol_json_send_ok(request_id, j);
        return;
    }

    case SEM_WAIT_STATE_POLL: {
        if (!decode_status(data, len, &st)) {
            fail("PROTOCOL_MISMATCH", "Status payload was not 14 bytes");
            return;
        }
        if (st.session_state != s_target_session_state) {
            retry_or_timeout_poll(request_id);
            return;
        }
        s_phase = SEM_NONE;
        protocol_json_send_ok(request_id, status_to_json(&st));
        return;
    }

    default:
        return; /* not ours */
    }
}

void holterlev_client_handle_op_error(int64_t request_id, const char *error_code, const char *detail)
{
    if (s_phase == SEM_NONE || s_req_id != request_id) return;
    fail(error_code, detail);
}

void holterlev_client_handle_op_timeout(int64_t request_id)
{
    if (s_phase == SEM_NONE || s_req_id != request_id) return;
    fail("TIMEOUT", "operation exceeded timeout_ms");
}

/* ---------------------------------------------------------------------
 * Simple single-shot reads and state-wait
 * ------------------------------------------------------------------- */

bool holterlev_client_get_status(int64_t request_id, uint32_t timeout_ms)
{
    if (s_phase != SEM_NONE) return false;
    if (!ble_central_read_start(request_id, timeout_ms, HLV_CHAR_STATUS_UUID)) return false;
    begin(SEM_GET_STATUS_READ, request_id, timeout_ms);
    return true;
}

bool holterlev_client_get_session_status(int64_t request_id, uint32_t timeout_ms)
{
    if (s_phase != SEM_NONE) return false;
    if (!ble_central_read_start(request_id, timeout_ms, HLV_CHAR_STATUS_UUID)) return false;
    begin(SEM_GET_SESSION_STATUS_READ, request_id, timeout_ms);
    return true;
}

bool holterlev_client_get_device_info(int64_t request_id, uint32_t timeout_ms)
{
    if (s_phase != SEM_NONE) return false;
    if (!ble_central_read_start(request_id, timeout_ms, HLV_CHAR_DEVICE_INFO_UUID)) return false;
    begin(SEM_GET_DEVICE_INFO_READ, request_id, timeout_ms);
    return true;
}

bool holterlev_client_wait_state(int64_t request_id, uint32_t timeout_ms, uint8_t target_session_state)
{
    if (s_phase != SEM_NONE) return false;
    if (!ble_central_read_start(request_id, POLL_READ_TIMEOUT_MS, HLV_CHAR_STATUS_UUID)) return false;
    begin(SEM_WAIT_STATE_POLL, request_id, timeout_ms);
    s_target_session_state = target_session_state;
    return true;
}
