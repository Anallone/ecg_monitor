# ble_stream -- 演示会话推流（player 样本 -> BLE Notify）
idf_component_register(
    SRCS         "ble_stream.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES ble_gatt player
)
