#ifndef HOLTERLEV_PROTOCOL_REF_H
#define HOLTERLEV_PROTOCOL_REF_H

/*
 * Mirrors docs/ble/PROTOCOL.md (v1.5) and src/holter_ble_protocol.h in the
 * HolterLEV firmware repo (c:/repos/ECG_ads1293/holterlev_1293-BLE,
 * branch fix/ble-d3-2-connection-ownership at the time this was written).
 * This is a SEPARATE repository -- these values are copied, not shared by
 * header, so a future firmware protocol change requires updating this
 * file by hand. If a command against real hardware ever produces a
 * result inconsistent with these constants, trust the firmware repo's
 * docs/ble/PROTOCOL.md, not this copy.
 */

#include <stdint.h>

#define HLV_SERVICE_UUID            "7b8e0000-6a52-4f4d-9a33-71c02e6b4d90"
#define HLV_CHAR_CONTROL_POINT_UUID "7b8e0040-6a52-4f4d-9a33-71c02e6b4d90"
#define HLV_CHAR_STATUS_UUID        "7b8e0041-6a52-4f4d-9a33-71c02e6b4d90"
#define HLV_CHAR_DATA_UUID          "7b8e0042-6a52-4f4d-9a33-71c02e6b4d90"
#define HLV_CHAR_DEVICE_INFO_UUID   "7b8e0043-6a52-4f4d-9a33-71c02e6b4d90"
#define HLV_CHAR_COMMAND_RESPONSE_UUID "7b8e0044-6a52-4f4d-9a33-71c02e6b4d90"
#define HLV_CHAR_STATUS_V2_UUID     "7b8e0045-6a52-4f4d-9a33-71c02e6b4d90"

#define HLV_ADVERTISED_NAME "NOAMED"

#define HLV_PROTOCOL_VERSION_MAJOR 1
#define HLV_PROTOCOL_VERSION_MINOR 0

/* Fixed 12-byte header: protocol_major(u8), protocol_minor(u8),
 * opcode(u16 LE), transaction_id(u32 LE), result_code(u16 LE),
 * payload_length(u16 LE) -- little-endian, byte-at-a-time, never a
 * packed struct cast (PROTOCOL.md Section 2). */
#define HLV_HEADER_LEN 12U

typedef enum
{
    HLV_OPCODE_AUTH       = 0x0001,
    HLV_OPCODE_GET_STATUS = 0x0002,
    HLV_OPCODE_START      = 0x0010,
    HLV_OPCODE_STOP       = 0x0011,
} hlv_opcode_t;

typedef enum
{
    HLV_RESULT_OK                           = 0,
    HLV_RESULT_NOT_READY                    = 1,
    HLV_RESULT_UNAUTHORIZED                 = 2,
    HLV_RESULT_INVALID_STATE                = 3,
    HLV_RESULT_INVALID_ARGUMENT             = 4,
    HLV_RESULT_SD_NOT_READY                 = 5,
    HLV_RESULT_INSUFFICIENT_SPACE           = 6,
    HLV_RESULT_FILENAME_NAMESPACE_EXHAUSTED = 7,
    HLV_RESULT_STORAGE_ERROR                = 8,
    HLV_RESULT_BUSY                         = 9,
    HLV_RESULT_NOT_SUPPORTED                = 10,
    HLV_RESULT_INTERNAL_ERROR               = 11,
    HLV_RESULT_ACCEPTED                     = 12,
    HLV_RESULT_TRANSACTION_CONFLICT         = 13,
} hlv_result_t;

/* Development-only app access code (PROTOCOL.md Section 5). Not
 * encryption, not a BLE SMP passkey -- exactly four ASCII digit bytes.
 * This is documented, non-secret, dev-bench-only behavior in the
 * firmware's own frozen protocol spec, structurally absent from any
 * release build (HOLTER_BLE_ENABLED-gated). Exposed here as a default so
 * `holter_authenticate` works with no arguments against the bench
 * firmware; callers doing negative auth testing pass their own `code`. */
#define HLV_DEV_ACCESS_CODE "2018"

/* Status v1 payload, 14 bytes (PROTOCOL.md Section 6). */
#define HLV_STATUS_PAYLOAD_LEN 14U

typedef struct
{
    uint8_t  startup_status;
    uint8_t  ads1293_ready;
    uint8_t  sd_ready;
    uint8_t  recording_capable;
    uint8_t  session_state;
    uint8_t  authorized;
    uint16_t last_command_opcode;
    uint32_t last_command_transaction_id;
    uint16_t last_command_result;
} hlv_status_t;

typedef enum
{
    HLV_SESSION_INITIALIZING = 0,
    HLV_SESSION_READY        = 1,
    HLV_SESSION_PREPARING    = 2,
    HLV_SESSION_RECORDING    = 3,
    HLV_SESSION_STOPPING     = 4,
    HLV_SESSION_ERROR        = 5,
    HLV_SESSION_NONE         = 0xFF,
} hlv_session_state_t;

#endif /* HOLTERLEV_PROTOCOL_REF_H */
