#include "protocol_json.h"
#include "uart_command_server.h"
#include <string.h>
#include <stdio.h>

cJSON *protocol_json_parse_line(const char *line)
{
    cJSON *root = cJSON_Parse(line);
    if (root == NULL) {
        return NULL;
    }
    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return NULL;
    }
    return root;
}

static void send_and_delete(cJSON *root)
{
    char *text = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (text == NULL) {
        return;
    }
    uart_command_server_send_line(text);
    cJSON_free(text);
}

/* Moves every field of `extra` onto `dst`, then deletes the now-empty
 * `extra` shell. Avoids a deep copy: cJSON has no "merge" primitive, so
 * items are detached one at a time from the front. */
static void merge_and_free(cJSON *dst, cJSON *extra)
{
    if (extra == NULL) {
        return;
    }
    cJSON *child = extra->child;
    while (child != NULL) {
        cJSON *next = child->next;
        cJSON_DetachItemViaPointer(extra, child);
        cJSON_AddItemToObject(dst, child->string, child);
        child = next;
    }
    cJSON_Delete(extra);
}

void protocol_json_send_ok(int64_t id, cJSON *extra)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "response");
    cJSON_AddNumberToObject(root, "id", (double)id);
    cJSON_AddStringToObject(root, "status", "ok");
    merge_and_free(root, extra);
    send_and_delete(root);
}

void protocol_json_send_error(int64_t id, const char *error_code, const char *detail)
{
    protocol_json_send_error_ex(id, error_code, detail, NULL);
}

void protocol_json_send_error_ex(int64_t id, const char *error_code, const char *detail, cJSON *extra)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "response");
    cJSON_AddNumberToObject(root, "id", (double)id);
    cJSON_AddStringToObject(root, "status", "error");
    cJSON_AddStringToObject(root, "error", error_code != NULL ? error_code : "INTERNAL_ERROR");
    if (detail != NULL) {
        cJSON_AddStringToObject(root, "detail", detail);
    }
    merge_and_free(root, extra);
    send_and_delete(root);
}

void protocol_json_send_parse_error(const char *detail)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "response");
    cJSON_AddNullToObject(root, "id");
    cJSON_AddStringToObject(root, "status", "error");
    cJSON_AddStringToObject(root, "error", "PARSE_ERROR");
    if (detail != NULL) {
        cJSON_AddStringToObject(root, "detail", detail);
    }
    send_and_delete(root);
}

void protocol_json_send_event(const char *event_name, cJSON *extra)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "event");
    cJSON_AddStringToObject(root, "event", event_name);
    merge_and_free(root, extra);
    send_and_delete(root);
}

bool protocol_json_hex_encode(const uint8_t *data, size_t len, char *out, size_t out_cap)
{
    if (out_cap < (2 * len + 1)) {
        return false;
    }
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[2 * i]     = digits[(data[i] >> 4) & 0x0F];
        out[2 * i + 1] = digits[data[i] & 0x0F];
    }
    out[2 * len] = '\0';
    return true;
}

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int protocol_json_hex_decode(const char *hex, uint8_t *out, size_t out_cap)
{
    size_t hex_len = strlen(hex);
    if (hex_len % 2 != 0) {
        return -1;
    }
    size_t byte_len = hex_len / 2;
    if (byte_len > out_cap) {
        return -1;
    }
    for (size_t i = 0; i < byte_len; i++) {
        int hi = hex_nibble(hex[2 * i]);
        int lo = hex_nibble(hex[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            return -1;
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return (int)byte_len;
}
