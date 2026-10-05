# 业务模块（application/<mod>/）通过各自的 register.cmake 自注册到 APPLICATION_MODULES。
# 这里再 include 一次 application/CMakeLists.txt（第一次在根 CMakeLists，为了 EXTRA_COMPONENT_DIRS），
# 把模块名收集进 main 的 PRIV_REQUIRES，保证它们都被链接进固件。
set(APPLICATION_MODULES "")
include("${CMAKE_CURRENT_LIST_DIR}/../application/CMakeLists.txt")

set(requires lcd ecg_infer sdcard realtime nvs_flash driver esp_lcd ble_gatt ecg_source)
foreach(_mod ${APPLICATION_MODULES})
    list(APPEND requires ${_mod})
endforeach()

idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES ${requires}
)
