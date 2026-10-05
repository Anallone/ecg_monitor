# sdcard — SD 卡挂载与 ECG 样本读取（SDSPI，复用 LCD 的 SPI2 总线）
# fatfs 组件在 IDF v5.5 内部已 REQUIRES sdmmc/esp_driver_sdmmc/esp_driver_sdspi，
# 并导出 esp_vfs_fat.h（esp_vfs_fat_sdspi_mount 即声明于其中），故此处依赖 fatfs 即可。
idf_component_register(
    SRCS         "sdcard.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES fatfs driver esp_driver_spi
)
