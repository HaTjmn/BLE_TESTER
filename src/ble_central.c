#include "ble_central.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "store/config/ble_store_config.h"
#include "host/ble_store.h"

#include "freertos/FreeRTOS.h"
#include "freertos/timers.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_system.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"

#include "holterlev_protocol_ref.h"
#include "file_transfer_hash.h"
#include "file_integrity_diag.h"

/* ---------------------------------------------------------------------
 * State
 * ------------------------------------------------------------------- */

static ble_central_state_t s_state = BLE_CENTRAL_STATE_IDLE;
static uint8_t  s_own_addr_type;
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
/* BLE-D4.0F: distinct from s_pending's per-OPERATION generation -- this
 * counts successful BLE CONNECTIONS (bumped once per BLE_GAP_EVENT_CONNECT
 * success), so a diagnostic can tell "which connection attempt" a given
 * received byte arrived over, across a reconnect. Protected by the same
 * lock as s_conn_handle, since they always change together. */
static uint32_t s_connection_generation = 0;
static uint16_t s_mtu = 23;
static char     s_peer_addr_str[18] = {0};
static char     s_peer_name[32] = {0};

static ble_central_service_t        s_services[BLE_CENTRAL_MAX_SERVICES];
static int                          s_service_count = 0;
static ble_central_characteristic_t s_chrs[BLE_CENTRAL_MAX_CHARACTERISTICS];
static int                          s_chr_count = 0;

/* One pending async operation. `generation` is bumped every time a new
 * one starts; any in-flight NimBLE callback captures its own generation
 * at start time and is discarded if it no longer matches -- this is how
 * a late completion after a timeout is safely ignored without needing
 * to truly cancel the underlying NimBLE procedure. */
typedef struct {
    bool             active;
    uint32_t         generation;
    ble_central_op_t op;
    int64_t          request_id;
    TickType_t       deadline_tick;
    /* op-specific scratch */
    char             name_filter[32];
    char             addr_filter[18];
    char             service_uuid_filter[BLE_CENTRAL_UUID_STR_LEN];
    int              scan_results_count;
    char             connect_name[32];
    uint16_t         target_val_handle;
    uint8_t          write_buf[BLE_CENTRAL_MAX_READ_LEN];
    size_t           write_len;
} pending_op_t;

static pending_op_t s_pending = {0};
static TimerHandle_t s_timeout_timer;

/* BLE-D4.0E: the ONE lock protecting every read-modify-write of s_pending,
 * s_state, s_conn_handle, and the trace ring buffer -- these are touched
 * from three different tasks (UART processor_task via begin_pending(),
 * the NimBLE host task via GAP/GATT callbacks, and the FreeRTOS Timer
 * Service Task via timeout_timer_cb()) with no prior synchronization
 * (BLE-D4.0D Step 2 audit). A spinlock (portMUX), not a mutex/semaphore:
 * every section under it is a few field reads/writes, never a blocking
 * call, so there is nothing to invert priority against and no reason to
 * pay for a semaphore. Never hold this across JSON/UART/NimBLE-API/
 * allocation work -- see the timer callback and *_claim* helpers below. */
static portMUX_TYPE s_central_state_lock = portMUX_INITIALIZER_UNLOCKED;

/* BLE-D4.0E: who is allowed to terminate a pending operation, and why. */
typedef enum {
    CLAIM_REASON_COMPLETED = 0,
    CLAIM_REASON_TIMEOUT,
    CLAIM_REASON_DISCONNECT_CANCELLED,
    CLAIM_REASON_ABORT,
} claim_reason_t;

/* Stable snapshot handed to whichever context wins the atomic claim --
 * everything the claimer needs to finish the job OUTSIDE the lock. */
typedef struct {
    ble_central_op_t op;
    int64_t          request_id;
    uint32_t         generation;
    claim_reason_t   reason;
} pending_claim_t;

/* BLE-D4.0E Step 2/3: the Timer Service Task must never build JSON or
 * touch UART. It posts a small, fixed-size event here instead; the
 * protocol processor task (protocol_transport.c, 8192-byte stack) drains
 * it and does the actual protocol_json_send_error() work. Depth 8 is generous --
 * only one operation is ever pending at a time by this codebase's own
 * design, so at most one timeout event is ever truly in flight; the
 * margin is for a burst of forced-timeout diagnostic testing. */
#define TIMEOUT_EVENT_QUEUE_DEPTH 8
static QueueHandle_t s_timeout_event_queue;

/* BLE-D4.0E Step 3 counters -- exposed via diag_health for hardware
 * validation, never logged from the timer callback itself. */
static uint32_t s_timeout_events_queued = 0;
static uint32_t s_timeout_events_queue_full = 0;
static uint32_t s_timeout_events_processed = 0;
static uint32_t s_late_completions_ignored = 0;       /* a normal GATT/GAP completion arrived after the op had already been claimed (timed out/cancelled) */
static uint32_t s_duplicate_completions_prevented = 0; /* the timer tried to claim an op a normal completion had already claimed first */

/* BLE-D4.0E Test 2 race-test counters -- declared here (not next to the
 * rest of the race-test machinery further below) so timeout_timer_cb()
 * can reference them without a forward declaration. */
static uint32_t s_race_completion_won = 0;
static uint32_t s_race_timeout_won = 0;
static uint32_t s_race_neither_won = 0; /* should never happen -- tracked as a correctness canary */

/* BLE-D4.0G Step 3: the NimBLE host task's BLE_GAP_EVENT_NOTIFY_RX case
 * must never build JSON, hash a payload, or touch a transport -- all of
 * that used to run synchronously in the callback (confirmed by direct
 * code audit in BLE-D4.0G Step 1). It now only classifies the handle,
 * copies the payload into a fixed-size queue item, and returns. A
 * dedicated worker task drains the queue and does the actual
 * hex/cJSON/transport/hash/reassembly work on its own large stack.
 * Depth 32 is a deliberate bound: a stall in the worker (e.g. a slow
 * transport write) must show up as a queue-full/rejected count, never as
 * unbounded memory growth or a silently truncated file transfer. */
#define NOTIFY_QUEUE_DEPTH 32

typedef struct {
    uint16_t attr_handle;
    bool     has_uuid;
    char     uuid[BLE_CENTRAL_UUID_STR_LEN];
    uint8_t  data[BLE_CENTRAL_MAX_READ_LEN];
    uint16_t len;
    bool     indication;
    uint32_t connection_generation;
} notify_queue_item_t;

static QueueHandle_t s_notify_queue;
static uint32_t s_notify_queue_full = 0;         /* xQueueSend failed -- notification rejected, not silently dropped-and-later-padded */
static uint32_t s_notify_queue_highwater = 0;    /* max observed queue depth since boot */
static uint32_t s_notify_worker_processed = 0;   /* worker task pulled and handled this many items */
static uint32_t s_notify_records_forwarded = 0;  /* of those, this many actually reached command_dispatcher_on_notification (excludes hash-only-mode items) */
static uint32_t s_notify_cb_max_duration_us = 0; /* worst-case time spent inside the NOTIFY_RX case itself */

/* Atomically claims the currently-active pending operation for
 * termination -- succeeds for at most one caller per generation. Does
 * NOT check the deadline; use try_claim_pending_timeout() for that.
 * Safe to call from any task; never blocks. */
static bool try_claim_pending_completion(uint32_t generation, claim_reason_t reason, pending_claim_t *out)
{
    bool ok = false;
    portENTER_CRITICAL(&s_central_state_lock);
    if (s_pending.active && s_pending.generation == generation) {
        out->op = s_pending.op;
        out->request_id = s_pending.request_id;
        out->generation = s_pending.generation;
        out->reason = reason;
        s_pending.active = false;
        ok = true;
    }
    portEXIT_CRITICAL(&s_central_state_lock);
    return ok;
}

/* Timer-only: atomically checks the deadline AND claims in the SAME
 * critical section -- no separate check-then-claim window for a GATT
 * completion to slip through. Deadline comparison is wrap-safe: a signed
 * subtraction of two TickType_t values is correct across a rollover as
 * long as the true elapsed time is under half the TickType_t range,
 * which holds for any timeout this codebase ever configures (seconds,
 * against a 32-bit tick count). */
static bool try_claim_pending_timeout(pending_claim_t *out)
{
    bool ok = false;
    portENTER_CRITICAL(&s_central_state_lock);
    if (s_pending.active) {
        TickType_t now = xTaskGetTickCount();
        int32_t remaining = (int32_t)(s_pending.deadline_tick - now);
        if (remaining <= 0) {
            out->op = s_pending.op;
            out->request_id = s_pending.request_id;
            out->generation = s_pending.generation;
            out->reason = CLAIM_REASON_TIMEOUT;
            s_pending.active = false;
            ok = true;
        }
    }
    portEXIT_CRITICAL(&s_central_state_lock);
    return ok;
}

/* Disconnect-cancellation only: claims whatever is currently active
 * regardless of generation (a disconnect cancels ANY in-flight op, not
 * one specific one the caller already knows the generation of). */
static bool try_claim_pending_any(claim_reason_t reason, pending_claim_t *out)
{
    bool ok = false;
    portENTER_CRITICAL(&s_central_state_lock);
    if (s_pending.active) {
        out->op = s_pending.op;
        out->request_id = s_pending.request_id;
        out->generation = s_pending.generation;
        out->reason = reason;
        s_pending.active = false;
        ok = true;
    }
    portEXIT_CRITICAL(&s_central_state_lock);
    return ok;
}

/* Atomic combined read of active+generation+op -- used by callbacks that
 * only need to decide whether to keep accumulating (e.g. one more scan
 * result or discovered service), not to claim/terminate anything yet. */
static bool op_is_current_with_op(uint32_t generation, ble_central_op_t op)
{
    bool match;
    portENTER_CRITICAL(&s_central_state_lock);
    match = s_pending.active && s_pending.generation == generation && s_pending.op == op;
    portEXIT_CRITICAL(&s_central_state_lock);
    return match;
}

/* BLE-D4.0B diagnostics: last-rejection snapshot, real disconnect reason,
 * and a bounded state-transition trace. All purely additive/read-only from
 * the outside -- see ble_central.h for field meanings. */
static ble_central_reject_info_t     s_last_reject = {0};
static ble_central_disconnect_info_t s_last_disconnect = {0};
static bool     s_local_disconnect_requested = false;
static int64_t  s_connect_established_ms = 0;

static ble_central_trace_entry_t s_trace[BLE_CENTRAL_TRACE_DEPTH];
static uint32_t s_trace_total = 0; /* monotonically increasing count ever recorded */

/* BLE-D4.0C Step 1: boot generation survives a software reset, task/int
 * watchdog reset, or panic reboot (RTC fast memory is untouched by those --
 * only a true power-on/brownout reset clears it, detected via the magic
 * value going missing). This is what lets a client tell "the chip
 * rebooted" apart from "the BLE link merely dropped" -- s_conn_handle and
 * s_state alone cannot make that distinction. */
#define BOOT_GENERATION_MAGIC 0x424C4531U /* "BLE1" */
RTC_NOINIT_ATTR static uint32_t s_boot_generation_magic;
RTC_NOINIT_ATTR static uint32_t s_boot_generation_raw;

static uint32_t s_boot_generation;
static int64_t  s_boot_uptime_base_ms;
static esp_reset_reason_t s_reset_reason;
static uint32_t s_min_free_internal_heap_ever;
static uint32_t s_notify_count = 0;
static uint32_t s_notify_bytes = 0;
static TaskHandle_t s_host_task_handle = NULL;

static const char *reset_reason_name(esp_reset_reason_t r)
{
    /* Standard esp_reset_reason_t values (esp_system.h) -- verified
     * against this framework's own header, not guessed. Anything this
     * switch doesn't recognize still reaches the caller's numeric
     * reset_reason_code field untouched. */
    switch (r) {
        case ESP_RST_UNKNOWN:   return "UNKNOWN";
        case ESP_RST_POWERON:   return "POWERON";
        case ESP_RST_EXT:       return "EXTERNAL_PIN";
        case ESP_RST_SW:        return "SOFTWARE";
        case ESP_RST_PANIC:     return "PANIC";
        case ESP_RST_INT_WDT:   return "INTERRUPT_WDT";
        case ESP_RST_TASK_WDT:  return "TASK_WDT";
        case ESP_RST_WDT:       return "OTHER_WDT";
        case ESP_RST_DEEPSLEEP: return "DEEPSLEEP_WAKE";
        case ESP_RST_BROWNOUT:  return "BROWNOUT";
        case ESP_RST_SDIO:      return "SDIO";
        /* BLE-D4.0G Step 5: observed on hardware -- opening a fresh raw
         * host-side handle against the native USB-Serial/JTAG port (this
         * board's second, dedicated protocol channel) resets the chip via
         * its own USB peripheral, distinct from UART0/CH340's
         * USB_UART_CHIP_RESET. Confirmed against esp_system.h, not
         * guessed; harmless (host-connection-triggered, not a crash). */
        case ESP_RST_USB:       return "USB_PERIPHERAL_RESET";
        case ESP_RST_JTAG:      return "JTAG";
        default:                return NULL;
    }
}

static int64_t now_ms(void);

/* Called once, as early as possible in ble_central_init(). */
static void record_boot(void)
{
    s_reset_reason = esp_reset_reason();
    if (s_boot_generation_magic != BOOT_GENERATION_MAGIC) {
        /* First boot since a true power-on/brownout (or first flash ever) --
         * RTC fast memory contents are undefined until this magic is set. */
        s_boot_generation_raw = 0;
        s_boot_generation_magic = BOOT_GENERATION_MAGIC;
    }
    s_boot_generation_raw++;
    s_boot_generation = s_boot_generation_raw;
    s_boot_uptime_base_ms = now_ms();
    s_min_free_internal_heap_ever = (uint32_t)esp_get_free_heap_size();
}

static int64_t now_ms(void)
{
    return (int64_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
}

/* The ONLY place s_state is ever assigned. Records a bounded trace entry
 * (Step 5) alongside every transition, using current s_pending/s_conn_handle
 * as the trace's own snapshot of "what was going on at the time". */
static void set_state(ble_central_state_t new_state, const char *event, int32_t result_code)
{
    ble_central_trace_entry_t *e = &s_trace[s_trace_total % BLE_CENTRAL_TRACE_DEPTH];
    e->timestamp_ms = now_ms();
    e->prev_state = (uint8_t)s_state;
    e->new_state = (uint8_t)new_state;
    strlcpy(e->event, event, sizeof(e->event));
    e->conn_handle = s_conn_handle;
    /* BLE-D4.0C Step 5: gate on `active`, matching pending_request_id below --
     * s_pending.op is leftover from the last operation even after
     * clear_pending(), and reporting it unconditionally made a completed
     * operation look like it was still pending. */
    e->pending_op = s_pending.active ? (uint8_t)s_pending.op : (uint8_t)BLE_CENTRAL_OP_NONE;
    e->pending_request_id = s_pending.active ? s_pending.request_id : -1;
    e->result_code = result_code;
    s_trace_total++;
    s_state = new_state;
}

static void set_reject(ble_central_reject_reason_t reason, const char *char_uuid)
{
    s_last_reject.reason = reason;
    s_last_reject.state = s_state;
    s_last_reject.conn_handle = s_conn_handle;
    s_last_reject.pending_active = s_pending.active;
    s_last_reject.pending_op = s_pending.active ? s_pending.op : BLE_CENTRAL_OP_NONE;
    s_last_reject.pending_request_id = s_pending.active ? s_pending.request_id : -1;
    if (char_uuid != NULL) {
        strlcpy(s_last_reject.characteristic_uuid, char_uuid, sizeof(s_last_reject.characteristic_uuid));
        s_last_reject.characteristic_cached = (ble_central_find_char_val_handle(char_uuid) != 0xFFFF);
    } else {
        s_last_reject.characteristic_uuid[0] = '\0';
        s_last_reject.characteristic_cached = false;
    }
}

/* Best-effort symbolic names for the standard Bluetooth Core Spec HCI
 * Error Codes most relevant to an unexpected disconnect. Anything not
 * listed here is preserved numerically as "UNKNOWN_0xNN" -- never
 * silently discarded (BLE-D4.0B Step 4).
 *
 * NimBLE wraps an HCI-status-derived host error through
 * BLE_HS_HCI_ERR(x) = BLE_HS_ERR_HCI_BASE(0x200) + x (host/ble_hs.h), and
 * event->disconnect.reason arrives already wrapped this way for the
 * common case (e.g. a locally-initiated ble_gap_terminate() reliably
 * produces 0x216 = 0x200 + 0x16 "local host terminated", confirmed live
 * on real hardware). Unwrap that base before the symbolic lookup, while
 * `reason_code` itself keeps the untouched raw value. */
static const char *hci_reason_name(int reason)
{
    if (reason >= 0x200 && reason <= 0x2FF) {
        reason -= 0x200;
    }
    switch (reason) {
        case 0x08: return "CONNECTION_TIMEOUT";           /* supervision timeout */
        case 0x13: return "REMOTE_USER_TERMINATED";
        case 0x14: return "REMOTE_LOW_RESOURCES";
        case 0x15: return "REMOTE_POWER_OFF";
        case 0x16: return "LOCAL_HOST_TERMINATED";
        case 0x1F: return "UNSPECIFIED_ERROR";
        case 0x22: return "LMP_RESPONSE_TIMEOUT";
        case 0x28: return "INSTANT_PASSED";
        case 0x2F: return "INSUFFICIENT_SECURITY";
        case 0x3B: return "UNACCEPTABLE_CONN_PARAMS";
        case 0x3D: return "CONN_FAILED_TO_ESTABLISH";
        case 0x3E: return "CONN_FAILED_TO_ESTABLISH_SYNC_TIMEOUT";
        default:   return NULL;
    }
}

static void record_disconnect(uint16_t conn_handle, int reason)
{
    s_last_disconnect.valid = true;
    s_last_disconnect.reason_code = reason;
    const char *name = hci_reason_name(reason);
    if (name != NULL) {
        strlcpy(s_last_disconnect.reason_name, name, sizeof(s_last_disconnect.reason_name));
    } else {
        snprintf(s_last_disconnect.reason_name, sizeof(s_last_disconnect.reason_name), "UNKNOWN_0x%02X", reason);
    }
    s_last_disconnect.locally_initiated = s_local_disconnect_requested;
    s_last_disconnect.conn_handle = conn_handle;
    s_last_disconnect.previous_state = (uint8_t)s_state;
    s_last_disconnect.had_pending_op = s_pending.active;
    s_last_disconnect.pending_op_at_disconnect = s_pending.active ? (uint8_t)s_pending.op : (uint8_t)BLE_CENTRAL_OP_NONE;
    s_last_disconnect.pending_request_id_at_disconnect = s_pending.active ? s_pending.request_id : -1;
    s_last_disconnect.timestamp_ms = now_ms();
    s_last_disconnect.connection_duration_ms = s_connect_established_ms > 0
        ? (uint32_t)(s_last_disconnect.timestamp_ms - s_connect_established_ms)
        : 0;
    s_local_disconnect_requested = false;
    s_connect_established_ms = 0;
}

/* ---------------------------------------------------------------------
 * UUID / address helpers
 * ------------------------------------------------------------------- */

static int hex_val(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static bool uuid_str_to_ble_uuid(const char *str, ble_uuid_any_t *out)
{
    size_t len = strlen(str);
    if (len == 4) {
        int b3 = hex_val(str[0]), b2 = hex_val(str[1]), b1 = hex_val(str[2]), b0 = hex_val(str[3]);
        if (b3 < 0 || b2 < 0 || b1 < 0 || b0 < 0) return false;
        out->u.type = BLE_UUID_TYPE_16;
        out->u16.value = (uint16_t)((b3 << 12) | (b2 << 8) | (b1 << 4) | b0);
        return true;
    }
    if (len == 36 && str[8] == '-' && str[13] == '-' && str[18] == '-' && str[23] == '-') {
        uint8_t bytes[16];
        int bi = 0;
        for (size_t i = 0; i < len; i++) {
            if (str[i] == '-') continue;
            int hi = hex_val(str[i]);
            i++;
            int lo = hex_val(str[i]);
            if (hi < 0 || lo < 0 || bi >= 16) return false;
            bytes[bi++] = (uint8_t)((hi << 4) | lo);
        }
        if (bi != 16) return false;
        out->u.type = BLE_UUID_TYPE_128;
        for (int i = 0; i < 16; i++) {
            out->u128.value[i] = bytes[15 - i];
        }
        return true;
    }
    return false;
}

static void ble_uuid_to_uuid_str(const ble_uuid_any_t *u, char *out, size_t cap)
{
    if (u->u.type == BLE_UUID_TYPE_16) {
        snprintf(out, cap, "%04x", u->u16.value);
    } else if (u->u.type == BLE_UUID_TYPE_128) {
        const uint8_t *v = u->u128.value;
        snprintf(out, cap, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                 v[15], v[14], v[13], v[12], v[11], v[10], v[9], v[8],
                 v[7], v[6], v[5], v[4], v[3], v[2], v[1], v[0]);
    } else {
        snprintf(out, cap, "unsupported-uuid-type");
    }
}

static void addr_to_str(const ble_addr_t *addr, char *out, size_t cap)
{
    snprintf(out, cap, "%02x:%02x:%02x:%02x:%02x:%02x",
             addr->val[5], addr->val[4], addr->val[3], addr->val[2], addr->val[1], addr->val[0]);
}

static bool str_to_addr(const char *str, ble_addr_t *out)
{
    unsigned int b[6];
    if (sscanf(str, "%02x:%02x:%02x:%02x:%02x:%02x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6) {
        return false;
    }
    out->val[5] = (uint8_t)b[0];
    out->val[4] = (uint8_t)b[1];
    out->val[3] = (uint8_t)b[2];
    out->val[2] = (uint8_t)b[3];
    out->val[1] = (uint8_t)b[4];
    out->val[0] = (uint8_t)b[5];
    out->type = BLE_ADDR_PUBLIC;
    return true;
}

/* ---------------------------------------------------------------------
 * Pending-op bookkeeping
 * ------------------------------------------------------------------- */

static uint32_t begin_pending(ble_central_op_t op, int64_t request_id, uint32_t timeout_ms)
{
    uint32_t gen;
    portENTER_CRITICAL(&s_central_state_lock);
    s_pending.active = true;
    s_pending.generation++;
    s_pending.op = op;
    s_pending.request_id = request_id;
    /* A fresh deadline for a fresh generation -- a new request can never
     * inherit a previous one's deadline_tick because this is the only
     * place deadline_tick is ever written, always alongside a generation
     * bump, under the same critical section. */
    s_pending.deadline_tick = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    gen = s_pending.generation;
    portEXIT_CRITICAL(&s_central_state_lock);
    return gen;
}

static void clear_pending(void)
{
    portENTER_CRITICAL(&s_central_state_lock);
    s_pending.active = false;
    portEXIT_CRITICAL(&s_central_state_lock);
}

static bool op_is_current(uint32_t generation)
{
    bool current;
    portENTER_CRITICAL(&s_central_state_lock);
    current = s_pending.active && s_pending.generation == generation;
    portEXIT_CRITICAL(&s_central_state_lock);
    return current;
}

/* BLE-D4.0E: fixed-size, POD -- safe to copy through a FreeRTOS queue.
 * Carries `reason` so the SAME lightweight post-to-queue path serves
 * both the real timer and the diagnostic race-test timer: EVERY FreeRTOS
 * software timer callback runs on the one shared Timer Service Task, so
 * a second timer calling command_dispatcher_* directly would reintroduce
 * exactly the bug this queue exists to eliminate -- confirmed the hard
 * way (see the BLE-D4.0E report's race-test panic, root-caused to this
 * exact mistake in the first version of race_completion_timer_cb()). */
typedef struct {
    ble_central_op_t op;
    int64_t          request_id;
    uint32_t         generation;
    claim_reason_t   reason;
} timeout_event_t;

/* Runs on the FreeRTOS Timer Service Task. Deliberately minimal: one
 * atomic claim-or-nothing check, at most two cheap NimBLE cancel calls
 * (already non-blocking posts to the NimBLE host task, same as before
 * this change), one state-trace update (also lock-protected, no
 * allocation), and one non-blocking queue post. No cJSON, no UART, no
 * mutex wait, no allocation -- that work now happens in
 * drain_timeout_events() on the UART processor task instead. */
static void timeout_timer_cb(TimerHandle_t t)
{
    (void)t;
    pending_claim_t claim;
    if (!try_claim_pending_timeout(&claim)) return;

    if (claim.op == BLE_CENTRAL_OP_SCAN) {
        ble_gap_disc_cancel();
        set_state(BLE_CENTRAL_STATE_IDLE, "scan_timeout", 0);
    } else if (claim.op == BLE_CENTRAL_OP_CONNECT) {
        ble_gap_conn_cancel();
        set_state(BLE_CENTRAL_STATE_IDLE, "connect_timeout", 0);
    } else if (claim.op == BLE_CENTRAL_OP_DIAG_RACE_TEST) {
        s_race_timeout_won++;
    }
    /* GATT ops (discovery/read/write/subscribe): nothing to actively
     * cancel in NimBLE; we just stop waiting. A completion callback that
     * arrives after this point will fail its own claim attempt (the
     * generation is already spent) and count itself as a late
     * completion -- see try_claim_pending_completion()'s call sites. */

    timeout_event_t ev = { .op = claim.op, .request_id = claim.request_id,
                            .generation = claim.generation, .reason = claim.reason };
    if (xQueueSend(s_timeout_event_queue, &ev, 0) == pdTRUE) {
        s_timeout_events_queued++;
    } else {
        /* Queue full is not expected (depth 8, at most one op pending at
         * a time by design) but must never silently drop a terminal
         * response nor block this task waiting for room. */
        s_timeout_events_queue_full++;
    }
}

/* ---------------------------------------------------------------------
 * GATT callbacks
 * ------------------------------------------------------------------- */

static int disc_svc_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                        const struct ble_gatt_svc *service, void *arg)
{
    (void)conn_handle;
    uint32_t gen = (uint32_t)(uintptr_t)arg;
    if (!op_is_current(gen)) return 0;

    if (error->status == 0 && service != NULL) {
        if (s_service_count < BLE_CENTRAL_MAX_SERVICES) {
            ble_central_service_t *s = &s_services[s_service_count++];
            ble_uuid_to_uuid_str(&service->uuid, s->uuid, sizeof(s->uuid));
            s->start_handle = service->start_handle;
            s->end_handle = service->end_handle;
        }
        return 0;
    }

    pending_claim_t claim;
    if (!try_claim_pending_completion(gen, CLAIM_REASON_COMPLETED, &claim)) {
        s_late_completions_ignored++;
        return 0;
    }
    if (error->status == BLE_HS_EDONE) {
        set_state(BLE_CENTRAL_STATE_READY, "svc_disc_done", 0);
        command_dispatcher_on_svc_disc_done(claim.request_id, s_services, s_service_count);
    } else {
        set_state(BLE_CENTRAL_STATE_CONNECTED, "svc_disc_failed", (int32_t)error->status);
        command_dispatcher_on_op_error(claim.request_id, "DISCOVERY_FAILED", "service discovery error");
    }
    return 0;
}

static int disc_chr_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                        const struct ble_gatt_chr *chr, void *arg)
{
    (void)conn_handle;
    uint32_t gen = (uint32_t)(uintptr_t)arg;
    if (!op_is_current(gen)) return 0;

    if (error->status == 0 && chr != NULL) {
        if (s_chr_count < BLE_CENTRAL_MAX_CHARACTERISTICS) {
            ble_central_characteristic_t *c = &s_chrs[s_chr_count++];
            ble_uuid_to_uuid_str(&chr->uuid, c->uuid, sizeof(c->uuid));
            c->def_handle = chr->def_handle;
            c->val_handle = chr->val_handle;
            c->properties = chr->properties;
        }
        return 0;
    }

    pending_claim_t claim;
    if (!try_claim_pending_completion(gen, CLAIM_REASON_COMPLETED, &claim)) {
        s_late_completions_ignored++;
        return 0;
    }
    if (error->status == BLE_HS_EDONE) {
        set_state(BLE_CENTRAL_STATE_READY, "chr_disc_done", 0);
        command_dispatcher_on_chr_disc_done(claim.request_id, s_chrs, s_chr_count);
    } else {
        set_state(BLE_CENTRAL_STATE_READY, "chr_disc_failed", (int32_t)error->status);
        command_dispatcher_on_op_error(claim.request_id, "DISCOVERY_FAILED", "characteristic discovery error");
    }
    return 0;
}

static int read_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                    struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    uint32_t gen = (uint32_t)(uintptr_t)arg;
    pending_claim_t claim;
    if (!try_claim_pending_completion(gen, CLAIM_REASON_COMPLETED, &claim)) {
        s_late_completions_ignored++;
        return 0;
    }
    set_state(BLE_CENTRAL_STATE_READY, "read_done", (int32_t)error->status);

    if (error->status != 0) {
        command_dispatcher_on_op_error(claim.request_id, "READ_FAILED", "GATT read error");
        return 0;
    }
    uint16_t len = OS_MBUF_PKTLEN(attr->om);
    if (len > BLE_CENTRAL_MAX_READ_LEN) len = BLE_CENTRAL_MAX_READ_LEN;
    uint8_t buf[BLE_CENTRAL_MAX_READ_LEN];
    os_mbuf_copydata(attr->om, 0, len, buf);
    command_dispatcher_on_read_done(claim.request_id, buf, len);
    return 0;
}

static int write_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                     struct ble_gatt_attr *attr, void *arg)
{
    (void)conn_handle;
    (void)attr;
    uint32_t gen = (uint32_t)(uintptr_t)arg;
    pending_claim_t claim;
    if (!try_claim_pending_completion(gen, CLAIM_REASON_COMPLETED, &claim)) {
        s_late_completions_ignored++;
        return 0;
    }
    set_state(BLE_CENTRAL_STATE_READY, "write_done", (int32_t)error->status);

    if (error->status != 0) {
        command_dispatcher_on_op_error(claim.request_id, "WRITE_FAILED", "GATT write error");
        return 0;
    }
    if (claim.op == BLE_CENTRAL_OP_SUBSCRIBE) {
        command_dispatcher_on_subscribe_done(claim.request_id, true);
    } else if (claim.op == BLE_CENTRAL_OP_UNSUBSCRIBE) {
        command_dispatcher_on_subscribe_done(claim.request_id, false);
    } else {
        command_dispatcher_on_write_done(claim.request_id);
    }
    return 0;
}

static int mtu_cb(uint16_t conn_handle, const struct ble_gatt_error *error,
                   uint16_t mtu, void *arg)
{
    (void)conn_handle; (void)arg;
    if (error->status == 0) {
        s_mtu = mtu;
    }
    return 0;
}

/* ---------------------------------------------------------------------
 * GAP event callback
 * ------------------------------------------------------------------- */

static int gap_event_cb(struct ble_gap_event *event, void *arg);

static void report_scan_match(const struct ble_gap_disc_desc *disc)
{
    struct ble_hs_adv_fields fields;
    char addr_str[18];
    char name_buf[32] = {0};

    ble_hs_adv_parse_fields(&fields, disc->data, disc->length_data);
    addr_to_str(&disc->addr, addr_str, sizeof(addr_str));
    if (fields.name != NULL && fields.name_len > 0) {
        size_t n = fields.name_len < sizeof(name_buf) - 1 ? fields.name_len : sizeof(name_buf) - 1;
        memcpy(name_buf, fields.name, n);
        name_buf[n] = '\0';
    }

    if (s_pending.name_filter[0] != '\0' && strcmp(name_buf, s_pending.name_filter) != 0) {
        return;
    }
    if (s_pending.addr_filter[0] != '\0' && strcasecmp(addr_str, s_pending.addr_filter) != 0) {
        return;
    }
    if (s_pending.service_uuid_filter[0] != '\0') {
        bool matched = false;
        for (int i = 0; i < fields.num_uuids128 && !matched; i++) {
            char u[BLE_CENTRAL_UUID_STR_LEN];
            ble_uuid_any_t any = { .u128 = fields.uuids128[i] };
            ble_uuid_to_uuid_str(&any, u, sizeof(u));
            if (strcasecmp(u, s_pending.service_uuid_filter) == 0) matched = true;
        }
        if (!matched) return;
    }

    s_pending.scan_results_count++;
    command_dispatcher_on_device_found(addr_str, name_buf, disc->rssi);

    /* connect --name resolves its address via an internal scan phase */
    if (s_pending.op == BLE_CENTRAL_OP_CONNECT && s_pending.connect_name[0] != '\0' &&
        strcmp(name_buf, s_pending.connect_name) == 0) {
        uint32_t gen = s_pending.generation;
        ble_gap_disc_cancel();
        struct ble_gap_conn_params conn_params;
        memset(&conn_params, 0, sizeof(conn_params));
        conn_params.scan_itvl = 0x0010;
        conn_params.scan_window = 0x0010;
        conn_params.itvl_min = 24;
        conn_params.itvl_max = 40;
        conn_params.latency = 0;
        conn_params.supervision_timeout = 256;
        set_state(BLE_CENTRAL_STATE_CONNECTING, "connect_by_name_resolved", 0);
        ble_gap_connect(s_own_addr_type, &disc->addr, 10000, &conn_params,
                         gap_event_cb, (void *)(uintptr_t)gen);
    }
}

static int gap_event_cb(struct ble_gap_event *event, void *arg)
{
    uint32_t gen = (uint32_t)(uintptr_t)arg;

    switch (event->type) {
    case BLE_GAP_EVENT_DISC:
        if (op_is_current_with_op(gen, BLE_CENTRAL_OP_SCAN) ||
            op_is_current_with_op(gen, BLE_CENTRAL_OP_CONNECT)) {
            report_scan_match(&event->disc);
        }
        return 0;

    case BLE_GAP_EVENT_DISC_COMPLETE: {
        if (!op_is_current_with_op(gen, BLE_CENTRAL_OP_SCAN)) return 0;
        pending_claim_t claim;
        if (!try_claim_pending_completion(gen, CLAIM_REASON_COMPLETED, &claim)) {
            s_late_completions_ignored++;
            return 0;
        }
        int count = s_pending.scan_results_count; /* read-only scratch field, single-writer (this task) by construction */
        set_state(BLE_CENTRAL_STATE_IDLE, "scan_complete", 0);
        command_dispatcher_on_scan_done(claim.request_id, count);
        return 0;
    }

    case BLE_GAP_EVENT_CONNECT: {
        if (!op_is_current_with_op(gen, BLE_CENTRAL_OP_CONNECT)) {
            if (event->connect.status == 0) {
                ble_gap_terminate(event->connect.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
            }
            return 0;
        }
        char name_copy[32];
        portENTER_CRITICAL(&s_central_state_lock);
        strlcpy(name_copy, s_pending.connect_name, sizeof(name_copy));
        portEXIT_CRITICAL(&s_central_state_lock);

        pending_claim_t claim;
        if (!try_claim_pending_completion(gen, CLAIM_REASON_COMPLETED, &claim)) {
            s_late_completions_ignored++;
            if (event->connect.status == 0) {
                ble_gap_terminate(event->connect.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
            }
            return 0;
        }

        if (event->connect.status == 0) {
            portENTER_CRITICAL(&s_central_state_lock);
            s_conn_handle = event->connect.conn_handle;
            s_connection_generation++;
            portEXIT_CRITICAL(&s_central_state_lock);
            s_connect_established_ms = now_ms();
            set_state(BLE_CENTRAL_STATE_CONNECTED, "gap_connected", 0);
            struct ble_gap_conn_desc desc;
            ble_gap_conn_find(s_conn_handle, &desc);
            addr_to_str(&desc.peer_id_addr, s_peer_addr_str, sizeof(s_peer_addr_str));
            strlcpy(s_peer_name, name_copy, sizeof(s_peer_name));
            ble_gattc_exchange_mtu(s_conn_handle, mtu_cb, NULL);
            command_dispatcher_on_connected(claim.request_id, s_peer_addr_str, s_peer_name);
        } else {
            set_state(BLE_CENTRAL_STATE_IDLE, "gap_connect_failed", (int32_t)event->connect.status);
            command_dispatcher_on_connect_failed(claim.request_id, "CONNECT_FAILED", "GAP connect failed");
        }
        return 0;
    }

    case BLE_GAP_EVENT_DISCONNECT: {
        uint16_t disconnected_handle = event->disconnect.conn.conn_handle;
        portENTER_CRITICAL(&s_central_state_lock);
        bool is_current_conn = (disconnected_handle == s_conn_handle);
        if (is_current_conn) {
            s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        }
        portEXIT_CRITICAL(&s_central_state_lock);

        if (is_current_conn) {
            /* Capture the real reason and full context BEFORE clearing any
             * of it -- record_disconnect() and set_state() both read
             * s_pending/s_state/s_conn_handle as they currently stand
             * (s_conn_handle already cleared above, matching the ordering
             * record_disconnect() expects for connection_duration_ms). */
            record_disconnect(disconnected_handle, event->disconnect.reason);
            set_state(BLE_CENTRAL_STATE_IDLE, "gap_disconnect", event->disconnect.reason);
            s_service_count = 0;
            s_chr_count = 0;
            s_mtu = 23;
            pending_claim_t claim;
            if (try_claim_pending_any(CLAIM_REASON_DISCONNECT_CANCELLED, &claim)) {
                command_dispatcher_on_op_error(claim.request_id, "DISCONNECTED", "peer disconnected mid-operation");
            }
            /* BLE-D4.0B Step 4: report the real captured reason, never the
             * old hardcoded "peer_disconnect" string regardless of cause. */
            command_dispatcher_on_disconnected(s_last_disconnect.reason_name);
        }
        return 0;
    }

    case BLE_GAP_EVENT_NOTIFY_RX: {
        int64_t cb_start_us = esp_timer_get_time();

        notify_queue_item_t item;
        item.has_uuid = false;
        for (int i = 0; i < s_chr_count; i++) {
            if (s_chrs[i].val_handle == event->notify_rx.attr_handle) {
                strlcpy(item.uuid, s_chrs[i].uuid, sizeof(item.uuid));
                item.has_uuid = true;
                break;
            }
        }
        uint16_t len = OS_MBUF_PKTLEN(event->notify_rx.om);
        if (len > BLE_CENTRAL_MAX_READ_LEN) len = BLE_CENTRAL_MAX_READ_LEN;
        item.attr_handle = event->notify_rx.attr_handle;
        item.len = len;
        os_mbuf_copydata(event->notify_rx.om, 0, len, item.data);
        item.indication = event->notify_rx.indication;
        item.connection_generation = ble_central_connection_generation();

        /* BLE-D4.0C Step 4: cheap counters only, no logging, updated here
         * and read back later from a safe task context (cmd_diag_health). */
        s_notify_count++;
        s_notify_bytes += len;
        uint32_t free_now = (uint32_t)esp_get_free_heap_size();
        if (free_now < s_min_free_internal_heap_ever) s_min_free_internal_heap_ever = free_now;

        /* BLE-D4.0G Step 3: everything that used to run right here --
         * hex/cJSON encoding, the transport write, hash ingestion,
         * absolute-offset reassembly -- now runs on notify_worker_task
         * instead. This callback's only remaining jobs are classifying
         * the handle, copying the payload, and handing it off. */
        if (xQueueSend(s_notify_queue, &item, 0) != pdTRUE) {
            s_notify_queue_full++;
        } else {
            UBaseType_t depth = uxQueueMessagesWaiting(s_notify_queue);
            if (depth > s_notify_queue_highwater) s_notify_queue_highwater = depth;
        }

        int64_t duration_us = esp_timer_get_time() - cb_start_us;
        if (duration_us > 0 && (uint32_t)duration_us > s_notify_cb_max_duration_us) {
            s_notify_cb_max_duration_us = (uint32_t)duration_us;
        }
        return 0;
    }

    case BLE_GAP_EVENT_MTU:
        s_mtu = event->mtu.value;
        return 0;

    default:
        return 0;
    }
}

/* ---------------------------------------------------------------------
 * NimBLE host lifecycle
 * ------------------------------------------------------------------- */

static void on_reset(int reason)
{
    set_state(BLE_CENTRAL_STATE_ERROR, "host_reset", reason);
}

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &s_own_addr_type);
    set_state(BLE_CENTRAL_STATE_IDLE, "host_sync", 0);
}

static void host_task(void *param)
{
    (void)param;
    s_host_task_handle = xTaskGetCurrentTaskHandle();
    nimble_port_run();
    nimble_port_freertos_deinit();
}

/* BLE-D4.0G Step 3: drains s_notify_queue on its own 8192-byte stack --
 * everything the old inline BLE_GAP_EVENT_NOTIFY_RX handler used to do
 * (hash ingestion, absolute-offset reassembly, hex/cJSON encoding, the
 * transport write) now happens here instead of on the NimBLE host task. */
static void notify_worker_task(void *arg)
{
    (void)arg;
    notify_queue_item_t item;
    while (true) {
        if (xQueueReceive(s_notify_queue, &item, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        s_notify_worker_processed++;
        const char *uuid_str = item.has_uuid ? item.uuid : NULL;

        bool is_file_transfer_data = (uuid_str != NULL) &&
            (strcasecmp(uuid_str, HLV_CHAR_FILE_TRANSFER_DATA_UUID) == 0);

        if (is_file_transfer_data && file_integrity_diag_is_active()) {
            file_integrity_diag_ingest(item.data, item.len, item.connection_generation);
        }

        if (is_file_transfer_data && file_transfer_hash_is_enabled()) {
            file_transfer_hash_ingest(item.data, item.len);
        } else {
            command_dispatcher_on_notification(item.attr_handle, uuid_str, item.data, item.len,
                                                 item.indication);
            s_notify_records_forwarded++;
        }
    }
}

void ble_central_init(void)
{
    record_boot();
    file_transfer_hash_init();
    file_integrity_diag_init();
    memset(&s_pending, 0, sizeof(s_pending));
    nimble_port_init();

    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    /* This NimBLE port's config-store module (unlike upstream Mynewt
     * NimBLE) has no separate ble_store_config_init() cache-load step --
     * read/write/delete go straight to NVS on each call. */
    ble_hs_cfg.store_read_cb = ble_store_config_read;
    ble_hs_cfg.store_write_cb = ble_store_config_write;
    ble_hs_cfg.store_delete_cb = ble_store_config_delete;

    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_svc_gap_device_name_set("holter-test-ctrl");

    s_timeout_event_queue = xQueueCreate(TIMEOUT_EVENT_QUEUE_DEPTH, sizeof(timeout_event_t));

    s_timeout_timer = xTimerCreate("op_timeout", pdMS_TO_TICKS(200), pdTRUE, NULL, timeout_timer_cb);
    xTimerStart(s_timeout_timer, 0);

    s_notify_queue = xQueueCreate(NOTIFY_QUEUE_DEPTH, sizeof(notify_queue_item_t));
    xTaskCreate(notify_worker_task, "notify_worker", 8192, NULL, 9, NULL);

    nimble_port_freertos_init(host_task);
}

/* ---------------------------------------------------------------------
 * Public getters
 * ------------------------------------------------------------------- */

ble_central_state_t ble_central_get_state(void) { return s_state; }
bool ble_central_is_connected(void) { return s_conn_handle != BLE_HS_CONN_HANDLE_NONE; }
const char *ble_central_peer_addr_str(void) { return s_peer_addr_str; }
const char *ble_central_peer_name(void) { return s_peer_name; }
uint16_t ble_central_negotiated_mtu(void) { return s_mtu; }

uint32_t ble_central_connection_generation(void)
{
    uint32_t gen;
    portENTER_CRITICAL(&s_central_state_lock);
    gen = s_connection_generation;
    portEXIT_CRITICAL(&s_central_state_lock);
    return gen;
}

uint16_t ble_central_find_char_val_handle(const char *char_uuid)
{
    for (int i = 0; i < s_chr_count; i++) {
        if (strcasecmp(s_chrs[i].uuid, char_uuid) == 0) {
            return s_chrs[i].val_handle;
        }
    }
    return 0xFFFF;
}

/* ---------------------------------------------------------------------
 * Public operations
 * ------------------------------------------------------------------- */

bool ble_central_scan_start(int64_t request_id, uint32_t timeout_ms,
                             const char *name_filter, const char *addr_filter,
                             const char *service_uuid_filter)
{
    if (s_pending.active) { set_reject(BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING, NULL); return false; }
    if (s_state != BLE_CENTRAL_STATE_IDLE) { set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL); return false; }

    struct ble_gap_disc_params params;
    memset(&params, 0, sizeof(params));
    params.passive = 0;
    params.filter_duplicates = 1;
    params.itvl = 0x0010;
    params.window = 0x0010;

    uint32_t gen = begin_pending(BLE_CENTRAL_OP_SCAN, request_id, timeout_ms);
    s_pending.name_filter[0] = '\0';
    s_pending.addr_filter[0] = '\0';
    s_pending.service_uuid_filter[0] = '\0';
    s_pending.scan_results_count = 0;
    if (name_filter) strlcpy(s_pending.name_filter, name_filter, sizeof(s_pending.name_filter));
    if (addr_filter) strlcpy(s_pending.addr_filter, addr_filter, sizeof(s_pending.addr_filter));
    if (service_uuid_filter) strlcpy(s_pending.service_uuid_filter, service_uuid_filter, sizeof(s_pending.service_uuid_filter));

    int rc = ble_gap_disc(s_own_addr_type, timeout_ms, &params, gap_event_cb, (void *)(uintptr_t)gen);
    if (rc != 0) {
        clear_pending();
        set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL);
        return false;
    }
    set_state(BLE_CENTRAL_STATE_SCANNING, "scan_start", 0);
    return true;
}

bool ble_central_scan_stop(void)
{
    if (s_state != BLE_CENTRAL_STATE_SCANNING) return false;
    ble_gap_disc_cancel();
    return true;
}

bool ble_central_connect_start(int64_t request_id, uint32_t timeout_ms,
                                const char *addr, const char *name)
{
    if (s_pending.active) { set_reject(BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING, NULL); return false; }
    if (s_state != BLE_CENTRAL_STATE_IDLE) { set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL); return false; }
    if ((addr == NULL || addr[0] == '\0') && (name == NULL || name[0] == '\0')) {
        set_reject(BLE_CENTRAL_REJECT_INVALID_ARGUMENT, NULL);
        return false;
    }

    uint32_t gen = begin_pending(BLE_CENTRAL_OP_CONNECT, request_id, timeout_ms);
    s_pending.connect_name[0] = '\0';

    if (addr != NULL && addr[0] != '\0') {
        ble_addr_t peer;
        if (!str_to_addr(addr, &peer)) {
            clear_pending();
            set_reject(BLE_CENTRAL_REJECT_INVALID_ARGUMENT, NULL);
            return false;
        }
        struct ble_gap_conn_params conn_params;
        memset(&conn_params, 0, sizeof(conn_params));
        conn_params.scan_itvl = 0x0010;
        conn_params.scan_window = 0x0010;
        conn_params.itvl_min = 24;
        conn_params.itvl_max = 40;
        conn_params.supervision_timeout = 256;
        int rc = ble_gap_connect(s_own_addr_type, &peer, timeout_ms, &conn_params,
                                  gap_event_cb, (void *)(uintptr_t)gen);
        if (rc != 0) {
            clear_pending();
            set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL);
            return false;
        }
        set_state(BLE_CENTRAL_STATE_CONNECTING, "connect_start_addr", 0);
        return true;
    }

    /* Resolve by advertised name: scan first, connect on match (see
     * report_scan_match()). */
    strlcpy(s_pending.connect_name, name, sizeof(s_pending.connect_name));
    s_pending.name_filter[0] = '\0';
    s_pending.addr_filter[0] = '\0';
    s_pending.service_uuid_filter[0] = '\0';
    s_pending.scan_results_count = 0;

    struct ble_gap_disc_params params;
    memset(&params, 0, sizeof(params));
    params.passive = 0;
    params.filter_duplicates = 1;
    params.itvl = 0x0010;
    params.window = 0x0010;

    int rc = ble_gap_disc(s_own_addr_type, timeout_ms, &params, gap_event_cb, (void *)(uintptr_t)gen);
    if (rc != 0) {
        clear_pending();
        set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL);
        return false;
    }
    set_state(BLE_CENTRAL_STATE_CONNECTING, "connect_start_name_scan", 0);
    return true;
}

bool ble_central_disconnect_start(int64_t request_id)
{
    if (s_state == BLE_CENTRAL_STATE_CONNECTING) {
        /* No real GAP connection ever existed yet, so no BLE_GAP_EVENT_DISCONNECT
         * will follow -- do NOT set s_local_disconnect_requested here, or it
         * would incorrectly leak forward and mislabel the NEXT real disconnect. */
        ble_gap_conn_cancel();
        clear_pending();
        set_state(BLE_CENTRAL_STATE_IDLE, "connect_cancelled", 0);
        command_dispatcher_on_disconnected("connect_cancelled");
        return true;
    }
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        set_reject(BLE_CENTRAL_REJECT_LINK_NOT_CONNECTED, NULL);
        return false;
    }
    s_local_disconnect_requested = true;
    set_state(BLE_CENTRAL_STATE_DISCONNECTING, "disconnect_requested", 0);
    ble_gap_terminate(s_conn_handle, BLE_ERR_REM_USER_CONN_TERM);
    (void)request_id; /* completion arrives via BLE_GAP_EVENT_DISCONNECT */
    return true;
}

bool ble_central_disc_services_start(int64_t request_id, uint32_t timeout_ms)
{
    if (s_pending.active) { set_reject(BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING, NULL); return false; }
    if (s_state != BLE_CENTRAL_STATE_CONNECTED && s_state != BLE_CENTRAL_STATE_READY) {
        set_reject(s_conn_handle == BLE_HS_CONN_HANDLE_NONE
                        ? BLE_CENTRAL_REJECT_LINK_NOT_CONNECTED : BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL);
        return false;
    }

    s_service_count = 0;
    uint32_t gen = begin_pending(BLE_CENTRAL_OP_DISC_SERVICES, request_id, timeout_ms);
    set_state(BLE_CENTRAL_STATE_DISCOVERING, "svc_disc_start", 0);
    int rc = ble_gattc_disc_all_svcs(s_conn_handle, disc_svc_cb, (void *)(uintptr_t)gen);
    if (rc != 0) {
        clear_pending();
        set_state(BLE_CENTRAL_STATE_CONNECTED, "svc_disc_start_failed", rc);
        set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL);
        return false;
    }
    return true;
}

bool ble_central_disc_characteristics_start(int64_t request_id, uint32_t timeout_ms,
                                             const char *service_uuid)
{
    if (s_pending.active) { set_reject(BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING, NULL); return false; }
    if (s_state != BLE_CENTRAL_STATE_CONNECTED && s_state != BLE_CENTRAL_STATE_READY) {
        set_reject(s_conn_handle == BLE_HS_CONN_HANDLE_NONE
                        ? BLE_CENTRAL_REJECT_LINK_NOT_CONNECTED : BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL);
        return false;
    }

    uint16_t start = 0x0001, end = 0xFFFF;
    if (service_uuid != NULL && service_uuid[0] != '\0') {
        bool found = false;
        for (int i = 0; i < s_service_count; i++) {
            if (strcasecmp(s_services[i].uuid, service_uuid) == 0) {
                start = s_services[i].start_handle;
                end = s_services[i].end_handle;
                found = true;
                break;
            }
        }
        if (!found) { set_reject(BLE_CENTRAL_REJECT_SERVICE_NOT_DISCOVERED, service_uuid); return false; }
    }

    s_chr_count = 0;
    uint32_t gen = begin_pending(BLE_CENTRAL_OP_DISC_CHARACTERISTICS, request_id, timeout_ms);
    set_state(BLE_CENTRAL_STATE_DISCOVERING, "chr_disc_start", 0);
    int rc = ble_gattc_disc_all_chrs(s_conn_handle, start, end, disc_chr_cb, (void *)(uintptr_t)gen);
    if (rc != 0) {
        clear_pending();
        set_state(BLE_CENTRAL_STATE_READY, "chr_disc_start_failed", rc);
        set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, NULL);
        return false;
    }
    return true;
}

bool ble_central_read_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid)
{
    if (s_pending.active) { set_reject(BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING, char_uuid); return false; }
    if (s_state != BLE_CENTRAL_STATE_READY) {
        set_reject(s_conn_handle == BLE_HS_CONN_HANDLE_NONE
                        ? BLE_CENTRAL_REJECT_LINK_NOT_CONNECTED : BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, char_uuid);
        return false;
    }
    uint16_t handle = ble_central_find_char_val_handle(char_uuid);
    if (handle == 0xFFFF) { set_reject(BLE_CENTRAL_REJECT_CHARACTERISTIC_NOT_DISCOVERED, char_uuid); return false; }

    uint32_t gen = begin_pending(BLE_CENTRAL_OP_READ, request_id, timeout_ms);
    int rc = ble_gattc_read(s_conn_handle, handle, read_cb, (void *)(uintptr_t)gen);
    if (rc != 0) {
        clear_pending();
        set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, char_uuid);
        return false;
    }
    return true;
}

bool ble_central_write_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid,
                              const uint8_t *data, size_t len, bool with_response)
{
    if (s_pending.active) { set_reject(BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING, char_uuid); return false; }
    if (s_state != BLE_CENTRAL_STATE_READY) {
        set_reject(s_conn_handle == BLE_HS_CONN_HANDLE_NONE
                        ? BLE_CENTRAL_REJECT_LINK_NOT_CONNECTED : BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, char_uuid);
        return false;
    }
    if (len > BLE_CENTRAL_MAX_READ_LEN) { set_reject(BLE_CENTRAL_REJECT_PAYLOAD_TOO_LARGE, char_uuid); return false; }
    uint16_t handle = ble_central_find_char_val_handle(char_uuid);
    if (handle == 0xFFFF) { set_reject(BLE_CENTRAL_REJECT_CHARACTERISTIC_NOT_DISCOVERED, char_uuid); return false; }

    uint32_t gen = begin_pending(BLE_CENTRAL_OP_WRITE, request_id, timeout_ms);
    memcpy(s_pending.write_buf, data, len);
    s_pending.write_len = len;

    int rc;
    if (with_response) {
        rc = ble_gattc_write_flat(s_conn_handle, handle, s_pending.write_buf, len,
                                    write_cb, (void *)(uintptr_t)gen);
    } else {
        rc = ble_gattc_write_no_rsp_flat(s_conn_handle, handle, s_pending.write_buf, len);
        if (rc == 0) {
            clear_pending();
            command_dispatcher_on_write_done(request_id);
            return true;
        }
    }
    if (rc != 0) {
        clear_pending();
        set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, char_uuid);
        return false;
    }
    return true;
}

static bool subscribe_common(int64_t request_id, uint32_t timeout_ms, const char *char_uuid,
                              const uint8_t *cccd_value, ble_central_op_t op)
{
    if (s_pending.active) { set_reject(BLE_CENTRAL_REJECT_GATT_OPERATION_PENDING, char_uuid); return false; }
    if (s_state != BLE_CENTRAL_STATE_READY) {
        set_reject(s_conn_handle == BLE_HS_CONN_HANDLE_NONE
                        ? BLE_CENTRAL_REJECT_LINK_NOT_CONNECTED : BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, char_uuid);
        return false;
    }
    uint16_t val_handle = ble_central_find_char_val_handle(char_uuid);
    if (val_handle == 0xFFFF) { set_reject(BLE_CENTRAL_REJECT_CHARACTERISTIC_NOT_DISCOVERED, char_uuid); return false; }
    /* CCCD is the descriptor immediately following the value attribute
     * -- true for every NimBLE-GATT-server characteristic that declares
     * NOTIFY/INDICATE with no other descriptor ahead of it, which is how
     * HolterLEV's own NimBLE-based GATT table is built (PROTOCOL.md
     * Section 1/8). Known simplification: a peer with additional
     * descriptors declared before the CCCD would need real descriptor
     * discovery instead -- see docs/PROTOCOL_REFERENCE.md limitations. */
    uint16_t cccd_handle = val_handle + 1;

    uint32_t gen = begin_pending(op, request_id, timeout_ms);
    memcpy(s_pending.write_buf, cccd_value, 2);
    s_pending.write_len = 2;
    int rc = ble_gattc_write_flat(s_conn_handle, cccd_handle, s_pending.write_buf, 2,
                                    write_cb, (void *)(uintptr_t)gen);
    if (rc != 0) {
        clear_pending();
        set_reject(BLE_CENTRAL_REJECT_CENTRAL_NOT_READY, char_uuid);
        return false;
    }
    return true;
}

bool ble_central_subscribe_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid,
                                  bool indicate)
{
    uint8_t val[2] = { indicate ? (uint8_t)0x02 : (uint8_t)0x01, 0x00 };
    return subscribe_common(request_id, timeout_ms, char_uuid, val, BLE_CENTRAL_OP_SUBSCRIBE);
}

bool ble_central_unsubscribe_start(int64_t request_id, uint32_t timeout_ms, const char *char_uuid)
{
    uint8_t val[2] = { 0x00, 0x00 };
    return subscribe_common(request_id, timeout_ms, char_uuid, val, BLE_CENTRAL_OP_UNSUBSCRIBE);
}

void ble_central_clear_bonds(void)
{
    ble_store_clear();
}

void ble_central_reset(void)
{
    clear_pending();
    if (s_conn_handle != BLE_HS_CONN_HANDLE_NONE) {
        s_local_disconnect_requested = true;
        ble_gap_terminate(s_conn_handle, BLE_ERR_REM_USER_CONN_TERM);
    }
    if (s_state == BLE_CENTRAL_STATE_SCANNING) {
        ble_gap_disc_cancel();
    }
    s_service_count = 0;
    s_chr_count = 0;
    set_state(BLE_CENTRAL_STATE_IDLE, "reset", 0);
}

/* ---------------------------------------------------------------------
 * BLE-D4.0B diagnostics accessors
 * ------------------------------------------------------------------- */

const ble_central_reject_info_t *ble_central_last_reject_info(void) { return &s_last_reject; }
const ble_central_disconnect_info_t *ble_central_last_disconnect_info(void) { return &s_last_disconnect; }
uint32_t ble_central_trace_total_count(void) { return s_trace_total; }

bool ble_central_trace_get(uint32_t logical_index, ble_central_trace_entry_t *out)
{
    if (logical_index >= s_trace_total) return false;
    if (logical_index >= BLE_CENTRAL_TRACE_DEPTH) return false;
    uint32_t slot = (s_trace_total - 1 - logical_index) % BLE_CENTRAL_TRACE_DEPTH;
    *out = s_trace[slot];
    return true;
}

/* ---------------------------------------------------------------------
 * BLE-D4.0C boot/health diagnostics accessors (Steps 1 and 4)
 * ------------------------------------------------------------------- */

void ble_central_get_boot_info(ble_central_boot_info_t *out)
{
    out->reset_reason_code = (int32_t)s_reset_reason;
    const char *name = reset_reason_name(s_reset_reason);
    if (name != NULL) {
        strlcpy(out->reset_reason_name, name, sizeof(out->reset_reason_name));
    } else {
        snprintf(out->reset_reason_name, sizeof(out->reset_reason_name), "UNKNOWN_%d", (int)s_reset_reason);
    }
    out->boot_generation = s_boot_generation;
    out->uptime_ms = (uint32_t)(now_ms() - s_boot_uptime_base_ms);
}

void ble_central_get_health(ble_central_health_t *out)
{
    out->free_internal_heap = (uint32_t)esp_get_free_heap_size();
    if (out->free_internal_heap < s_min_free_internal_heap_ever) {
        s_min_free_internal_heap_ever = out->free_internal_heap; /* catch a dip since the last notify, too */
    }
    out->minimum_free_internal_heap = s_min_free_internal_heap_ever;
    out->largest_free_internal_block = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    out->notifications_received = s_notify_count;
    out->notification_bytes_received = s_notify_bytes;
    out->nimble_host_task_stack_highwater_bytes = s_host_task_handle != NULL
        ? (uint32_t)uxTaskGetStackHighWaterMark(s_host_task_handle)
        : 0;
    /* BLE-D4.0D Step 3: the actual task implicated in the captured panic
     * backtrace (prvTimerTask) -- xTimerGetTimerDaemonTaskHandle() is the
     * documented, ESP-IDF-available way to get its handle without needing
     * to guess or wrap the daemon task ourselves. */
    TaskHandle_t timer_task = xTimerGetTimerDaemonTaskHandle();
    out->timer_task_stack_highwater_bytes = timer_task != NULL
        ? (uint32_t)uxTaskGetStackHighWaterMark(timer_task)
        : 0;
    portENTER_CRITICAL(&s_central_state_lock);
    out->pending_operation_active = s_pending.active;
    portEXIT_CRITICAL(&s_central_state_lock);
    out->state_transition_total = s_trace_total;
    out->timeout_events_queued = s_timeout_events_queued;
    out->timeout_events_queue_full = s_timeout_events_queue_full;
    out->timeout_events_processed = s_timeout_events_processed;
    out->late_completions_ignored = s_late_completions_ignored;
    out->duplicate_completions_prevented = s_duplicate_completions_prevented;

    out->notify_queue_depth = s_notify_queue != NULL ? (uint32_t)uxQueueMessagesWaiting(s_notify_queue) : 0;
    out->notify_queue_highwater = s_notify_queue_highwater;
    out->notify_queue_full_events = s_notify_queue_full;
    out->notify_worker_processed = s_notify_worker_processed;
    out->notify_records_forwarded = s_notify_records_forwarded;
    out->notify_callback_max_duration_us = s_notify_cb_max_duration_us;
}

/* BLE-D4.0E Step (drain): runs on the protocol processor task (large stack),
 * called from protocol_transport's poll hook every ~20ms. Pops every
 * currently-queued event -- from either timer -- and turns it into the
 * matching response on this task's own stack, never the Timer Service
 * Task's. A real timeout produces the same TIMEOUT response
 * command_dispatcher_on_op_timeout() always did; a race-test win (the
 * ONLY other way an event reaches this queue) produces the
 * RACE_TEST_COMPLETED response instead. */
void ble_central_drain_timeout_events(void)
{
    timeout_event_t ev;
    while (xQueueReceive(s_timeout_event_queue, &ev, 0) == pdTRUE) {
        s_timeout_events_processed++;
        if (ev.reason == CLAIM_REASON_TIMEOUT) {
            command_dispatcher_on_op_timeout(ev.request_id);
        } else {
            command_dispatcher_on_op_error(ev.request_id, "RACE_TEST_COMPLETED",
                ev.reason == CLAIM_REASON_DISCONNECT_CANCELLED
                    ? "synthetic disconnect cancellation won the race"
                    : "synthetic completion won the race");
        }
    }
}

/* BLE-D4.0E: diagnostic-only synthetic pending operation for
 * diag_force_timeout -- exercises the REAL timer -> claim -> queue ->
 * drain -> UART pipeline without ever touching NimBLE. Refuses to clobber
 * a genuinely in-flight operation (this codebase's own one-pending-op
 * invariant, checked atomically so a real begin_pending() racing with
 * this can't interleave): returns false and does nothing in that case,
 * rather than silently stomping the real operation's generation. */
bool ble_central_diag_force_timeout(int64_t request_id)
{
    bool started = false;
    portENTER_CRITICAL(&s_central_state_lock);
    if (!s_pending.active) {
        s_pending.active = true;
        s_pending.generation++;
        s_pending.op = BLE_CENTRAL_OP_NONE;
        s_pending.request_id = request_id;
        /* A 1-tick deadline (not 0) guarantees try_claim_pending_timeout()'s
         * `remaining <= 0` check is already true the moment the timer next
         * runs, without depending on pdMS_TO_TICKS(0) happening to floor
         * to something timing-sensitive. */
        s_pending.deadline_tick = xTaskGetTickCount() + pdMS_TO_TICKS(1);
        started = true;
    }
    portEXIT_CRITICAL(&s_central_state_lock);
    return started;
}

/* BLE-D4.0E: deterministic timeout/completion race test. A synthetic
 * pending op is given a fixed deadline; a one-shot timer independently
 * fires a synthetic "completion" (or, in disconnect mode, a disconnect
 * cancellation) at deadline+offset_ms -- negative offsets land before the
 * real timeout, positive ones after. Both the completion-timer callback
 * and the real periodic timeout_timer_cb() race for the SAME atomic
 * claim; exactly one can ever win, by construction, regardless of which
 * fires first. This is what Test 2 measures. */
#define RACE_TEST_DEADLINE_MS 300U

typedef struct {
    int64_t  request_id;
    uint32_t generation;
    bool     disconnect_mode;
} race_test_ctx_t;

static TimerHandle_t   s_race_completion_timer;
static race_test_ctx_t s_race_ctx;

/* Runs on the FreeRTOS Timer Service Task, same as timeout_timer_cb() --
 * EVERY software timer's callback shares that one task, so this must be
 * exactly as lightweight: claim-or-nothing, then a non-blocking queue
 * post, nothing else. The first version of this function called
 * command_dispatcher_on_op_error() directly and reintroduced the exact
 * JSON/UART-on-timer-task defect the rest of this change eliminates --
 * confirmed on hardware (a real "Stack canary watchpoint triggered (Tmr
 * Svc)" panic, BLE-D4.0E report). Never repeat that mistake here. */
static void race_completion_timer_cb(TimerHandle_t t)
{
    (void)t;
    pending_claim_t claim;
    bool ok = s_race_ctx.disconnect_mode
        ? try_claim_pending_any(CLAIM_REASON_DISCONNECT_CANCELLED, &claim)
        : try_claim_pending_completion(s_race_ctx.generation, CLAIM_REASON_COMPLETED, &claim);
    if (!ok) {
        /* The real timer already claimed (or is about to claim) this
         * generation first -- this is the "duplicate prevented" side of
         * the race from the completion's own point of view. */
        s_duplicate_completions_prevented++;
        return;
    }
    s_race_completion_won++;
    timeout_event_t ev = { .op = claim.op, .request_id = claim.request_id,
                            .generation = claim.generation, .reason = claim.reason };
    if (xQueueSend(s_timeout_event_queue, &ev, 0) == pdTRUE) {
        s_timeout_events_queued++;
    } else {
        s_timeout_events_queue_full++;
    }
}

bool ble_central_diag_race_test(int64_t request_id, int32_t offset_ms, bool disconnect_mode)
{
    uint32_t gen;
    bool started = false;
    portENTER_CRITICAL(&s_central_state_lock);
    if (!s_pending.active) {
        s_pending.active = true;
        s_pending.generation++;
        s_pending.op = BLE_CENTRAL_OP_DIAG_RACE_TEST;
        s_pending.request_id = request_id;
        s_pending.deadline_tick = xTaskGetTickCount() + pdMS_TO_TICKS(RACE_TEST_DEADLINE_MS);
        gen = s_pending.generation;
        started = true;
    }
    portEXIT_CRITICAL(&s_central_state_lock);
    if (!started) return false;

    s_race_ctx.request_id = request_id;
    s_race_ctx.generation = gen;
    s_race_ctx.disconnect_mode = disconnect_mode;

    int32_t fire_ms = (int32_t)RACE_TEST_DEADLINE_MS + offset_ms;
    if (fire_ms < 1) fire_ms = 1;
    if (s_race_completion_timer == NULL) {
        s_race_completion_timer = xTimerCreate("race_test", pdMS_TO_TICKS((uint32_t)fire_ms),
                                                pdFALSE, NULL, race_completion_timer_cb);
    } else {
        xTimerChangePeriod(s_race_completion_timer, pdMS_TO_TICKS((uint32_t)fire_ms), 0);
    }
    xTimerStart(s_race_completion_timer, 0);
    return true;
}

void ble_central_diag_race_test_counters(uint32_t *completion_won, uint32_t *timeout_won, uint32_t *neither_won)
{
    *completion_won = s_race_completion_won;
    *timeout_won = s_race_timeout_won;
    *neither_won = s_race_neither_won;
}
