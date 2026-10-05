# player -- sample catalog + playback engine (owns rt_engine_t)
idf_component_register(
    SRCS         "player.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES realtime sdcard ble_stream
)
