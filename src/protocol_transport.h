#ifndef PROTOCOL_TRANSPORT_H
#define PROTOCOL_TRANSPORT_H

#ifdef __cplusplus
extern "C" {
#endif

/*
 * BLE-D4.0G: owns the ESP32-S3's native USB-Serial/JTAG peripheral
 * (fixed silicon pins, a second, physically distinct USB connector from
 * the UART0-via-CH340 port used for flashing/console/panic output) as
 * the host-automation channel (NDJSON in/out). UART0 is deliberately
 * left untouched by this module -- no driver of any kind is installed
 * on it here -- so it stays free for raw boot/panic/libbt.a diagnostic
 * capture, without any risk of the JSON protocol interleaving with it.
 *
 * Same two-task structure as the UART0 command server it replaces:
 *  - rx task: reads raw bytes, reassembles newline-delimited lines, and
 *    pushes each complete line (heap-owned, caller must free) onto a
 *    queue.
 *  - processor task: pops one line at a time and calls the registered
 *    line handler; also drains the poll_fn on every bounded wait cycle
 *    so non-line events (BLE timeout completions) still get serviced.
 */

typedef void (*protocol_transport_line_handler_t)(char *line);
typedef void (*protocol_transport_poll_fn_t)(void);

void protocol_transport_init(protocol_transport_line_handler_t handler, protocol_transport_poll_fn_t poll_fn);

/* Thread-safe: appends '\n' and writes atomically so a response and an
 * unsolicited event can never interleave into one corrupted line. */
void protocol_transport_send_line(const char *line);

#ifdef __cplusplus
}
#endif

#endif /* PROTOCOL_TRANSPORT_H */
