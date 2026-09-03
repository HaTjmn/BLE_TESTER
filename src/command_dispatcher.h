#ifndef COMMAND_DISPATCHER_H
#define COMMAND_DISPATCHER_H

#ifdef __cplusplus
extern "C" {
#endif

void command_dispatcher_init(void);

/* Registered with uart_command_server as its line handler. Takes
 * ownership of `line` (frees it before returning). Never blocks for a
 * BLE operation to complete -- see ble_central.h's pending-op design. */
void command_dispatcher_handle_line(char *line);

#ifdef __cplusplus
}
#endif

#endif /* COMMAND_DISPATCHER_H */
