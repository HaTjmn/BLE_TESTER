#include "uart_command_server.h"

#include <stdlib.h>
#include <string.h>

#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#define UART_PORT           UART_NUM_0
#define UART_BAUD_RATE      115200
#define UART_RX_BUF_SIZE    2048
#define UART_TX_BUF_SIZE    2048
#define MAX_LINE_LEN        4096
#define LINE_QUEUE_DEPTH    8

static SemaphoreHandle_t s_tx_mutex;
static QueueHandle_t     s_line_queue;
static uart_command_line_handler_t s_handler;

void uart_command_server_send_line(const char *line)
{
    if (line == NULL) {
        return;
    }
    size_t len = strlen(line);
    /* +1 for '\n'; written in a single locked section so a response and
     * an event from another task never interleave mid-line. */
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    uart_write_bytes(UART_PORT, line, len);
    uart_write_bytes(UART_PORT, "\n", 1);
    xSemaphoreGive(s_tx_mutex);
}

static void rx_task(void *arg)
{
    (void)arg;
    char *line_buf = malloc(MAX_LINE_LEN);
    size_t line_len = 0;
    uint8_t byte;

    while (true) {
        int n = uart_read_bytes(UART_PORT, &byte, 1, pdMS_TO_TICKS(100));
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
        if (xQueueReceive(s_line_queue, &line, portMAX_DELAY) == pdTRUE) {
            s_handler(line); /* handler owns and frees `line` */
        }
    }
}

void uart_command_server_init(uart_command_line_handler_t handler)
{
    s_handler = handler;

    uart_config_t cfg = {
        .baud_rate = UART_BAUD_RATE,
        .data_bits = UART_DATA_8_BITS,
        .parity    = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    uart_driver_install(UART_PORT, UART_RX_BUF_SIZE, UART_TX_BUF_SIZE, 0, NULL, 0);
    uart_param_config(UART_PORT, &cfg);

    s_tx_mutex = xSemaphoreCreateMutex();
    s_line_queue = xQueueCreate(LINE_QUEUE_DEPTH, sizeof(char *));

    xTaskCreate(rx_task, "uart_rx", 4096, NULL, 10, NULL);
    xTaskCreate(processor_task, "uart_cmd", 8192, NULL, 9, NULL);
}
