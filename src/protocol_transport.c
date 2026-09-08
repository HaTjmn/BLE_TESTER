#include "protocol_transport.h"

#include <stdlib.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

/* Worst case one line: a File Transfer Data notification, 512 hex-encoded
 * payload bytes (1024 chars) plus JSON envelope overhead -- comfortably
 * under MAX_LINE_LEN. Sized well above the peripheral's own 256-byte
 * default so a single line rarely spans more than one TX FIFO drain
 * cycle at USB Full Speed. */
#define USB_TX_BUF_SIZE      4096
#define USB_RX_BUF_SIZE      4096
#define MAX_LINE_LEN         4096
#define LINE_QUEUE_DEPTH     8
#define POLL_INTERVAL_MS     20

static SemaphoreHandle_t s_tx_mutex;
static QueueHandle_t     s_line_queue;
static protocol_transport_line_handler_t s_handler;
static protocol_transport_poll_fn_t s_poll_fn;

void protocol_transport_send_line(const char *line)
{
    if (line == NULL) {
        return;
    }
    size_t len = strlen(line);
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    usb_serial_jtag_write_bytes(line, len, portMAX_DELAY);
    usb_serial_jtag_write_bytes("\n", 1, portMAX_DELAY);
    xSemaphoreGive(s_tx_mutex);
}

static void rx_task(void *arg)
{
    (void)arg;
    char *line_buf = malloc(MAX_LINE_LEN);
    size_t line_len = 0;
    uint8_t byte;

    while (true) {
        int n = usb_serial_jtag_read_bytes(&byte, 1, pdMS_TO_TICKS(100));
        if (n <= 0) {
            continue;
        }
        if (byte == '\r') {
            continue; /* tolerate CRLF from a host-side terminal */
        }
        if (byte == '\n') {
            if (line_len == 0) {
                continue; /* ignore blank lines */
            }
            char *owned = malloc(line_len + 1);
            memcpy(owned, line_buf, line_len);
            owned[line_len] = '\0';
            line_len = 0;
            if (xQueueSend(s_line_queue, &owned, 0) != pdTRUE) {
                /* Processor task is somehow behind by LINE_QUEUE_DEPTH
                 * whole commands -- drop rather than block the rx path. */
                free(owned);
            }
            continue;
        }
        if (line_len >= (MAX_LINE_LEN - 1)) {
            /* Oversized line: drop what we have so far and resync on the
             * next '\n'. A malformed/unterminated stream must never grow
             * memory unboundedly or crash the controller. */
            line_len = 0;
            continue;
        }
        line_buf[line_len++] = (char)byte;
    }
}

static void processor_task(void *arg)
{
    (void)arg;
    char *line;
    while (true) {
        if (xQueueReceive(s_line_queue, &line, pdMS_TO_TICKS(POLL_INTERVAL_MS)) == pdTRUE) {
            s_handler(line); /* handler owns and frees `line` */
        }
        if (s_poll_fn != NULL) {
            s_poll_fn();
        }
    }
}

void protocol_transport_init(protocol_transport_line_handler_t handler, protocol_transport_poll_fn_t poll_fn)
{
    s_handler = handler;
    s_poll_fn = poll_fn;

    usb_serial_jtag_driver_config_t cfg = {
        .tx_buffer_size = USB_TX_BUF_SIZE,
        .rx_buffer_size = USB_RX_BUF_SIZE,
    };
    usb_serial_jtag_driver_install(&cfg);

    s_tx_mutex = xSemaphoreCreateMutex();
    s_line_queue = xQueueCreate(LINE_QUEUE_DEPTH, sizeof(char *));

    xTaskCreate(rx_task, "usb_rx", 4096, NULL, 10, NULL);
    xTaskCreate(processor_task, "usb_cmd", 8192, NULL, 9, NULL);
}
