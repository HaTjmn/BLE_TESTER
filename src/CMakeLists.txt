idf_component_register(
    SRCS
        "app_main.c"
        "protocol_json.c"
        "protocol_transport.c"
        "ble_central.c"
        "command_dispatcher.c"
        "holterlev_client.c"
        "file_transfer_hash.c"
        "file_integrity_diag.c"

    INCLUDE_DIRS
        "."

    REQUIRES
        bt
        json
        nvs_flash
        mbedtls
        esp_driver_usb_serial_jtag
)
