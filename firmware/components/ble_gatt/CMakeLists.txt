# ble_gatt -- BLE GATT 服务：演示样本流 + 状态心跳（NimBLE 外设）
idf_component_register(
    SRCS         "ble_gatt.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES bt nvs_flash
)
