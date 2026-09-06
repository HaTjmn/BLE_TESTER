#ifndef UART_COMMAND_SERVER_H
#define UART_COMMAND_SERVER_H

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Owns UART0 (the same wire used for flashing/monitor -- 115200 8N1) as
 * the host-automation channel. Two tasks:
 *  - rx task: reads raw bytes, reassembles newline-delimited lines, and
 *    pushes each complete line (heap-owned, caller must free) onto a
 *    queue. Never blocks on command execution, so it keeps accepting
 *    bytes (e.g. a scan_stop) while a previous command is still being
 *    processed.
 *  - processor task: pops one line at a time and calls the registered
 *    line handler. The handler itself must not block for BLE completion
 *    (see command_dispatcher.c) -- only for the deliberately-blocking
 *    wait_event command, which is documented as a wait, not a fault.
 *
 * Log output is fully disabled on this UART (sdkconfig.defaults) so no
 * ESP_LOG line can ever corrupt a JSON response line.
 */

typedef void (*uart_command_line_handler_t)(char *line);

/* BLE-D4.0E: called from the processor task on every bounded wait cycle,
 * whether or not a line arrived -- this is how a queued event that isn't
 * a UART line (the Timer Service Task's own timeout events) gets drained
 * on a task with a large enough stack for JSON/UART work, without this
 * module needing to know anything about BLE or the timeout mechanism.
 * May be NULL if the caller has nothing to poll. */
typedef void (*uart_command_poll_fn_t)(void);

void uart_command_server_init(uart_command_line_handler_t handler, uart_command_poll_fn_t poll_fn);

/* Thread-safe: appends '\n' and writes atomically so a response and an
 * unsolicited event (e.g. a BLE notification, emitted from the NimBLE
 * host task) can never interleave into one corrupted line. */
void uart_command_server_send_line(const char *line);

#ifdef __cplusplus
}
#endif

#endif /* UART_COMMAND_SERVER_H */
