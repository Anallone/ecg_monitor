# ecg_source — ECG 采集源抽象（内存源 / 重采样 / MAX30003 SPI 驱动）
#
# ecg_source.c     不依赖 ESP-IDF，可主机编译测试重采样
# ecg_max30003.c   依赖 ESP-IDF（driver / log / freertos）
idf_component_register(
    SRCS         "ecg_source.c" "ecg_max30003.c" "ecg_filter.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES driver log
)
