cmake_minimum_required(VERSION 3.16)

# 依赖环境变量 IDF_PATH（运行 ESP-IDF 的 export.bat / export.ps1 会自动设置）。
# 若未设置，给出清晰报错而不是硬编码本机路径，保证项目可跨机器迁移。
if(NOT DEFINED ENV{IDF_PATH})
    message(FATAL_ERROR "未设置 IDF_PATH。请先运行 ESP-IDF v5.5 的 export 脚本（如 %USERPROFILE%\\esp\\v5.5\\esp-idf\\export.bat）")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# 额外组件目录：底层公共组件 components/
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/components")

# 业务模块 application/<mod>/：各自是独立组件，由模块内的 register.cmake 自注册。
# 这里 include 一次是为了拿到 EXTRA_COMPONENT_DIRS（必须在 project() 之前）。
include("${CMAKE_CURRENT_LIST_DIR}/application/CMakeLists.txt")

project(ecg_monitor)
