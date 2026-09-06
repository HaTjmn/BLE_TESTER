idf_component_register(
    SRCS
        "app_main.c"
        "protocol_json.c"
        "uart_command_server.c"
        "ble_central.c"
        "command_dispatcher.c"
        "holterlev_client.c"
        "file_transfer_hash.c"

    INCLUDE_DIRS
        "."

    REQUIRES
        bt
        json
        nvs_flash
        mbedtls
)
