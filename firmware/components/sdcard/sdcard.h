/**
 * sdcard.h — SD 卡挂载与 ECG 样本读取（微雪 ESP32-S3-Touch-LCD-2）
 *
 * 硬件：SD 卡走 SDSPI，SCK=IO39 / MOSI=IO38 / MISO=IO40 / CS=IO41。
 *       与 LCD 共用 39/38/40 三线，靠 CS 区分（LCD CS=IO45）。
 *       两者同在 SPI2_HOST（与微雪官方 ESP32-S3-Touch-LCD-2 Demo 一致：
 *       LCD 用 SPI2_HOST，SDSPI_HOST_DEFAULT() 在 ESP32-S3 上默认也是 SPI2_HOST）。
 *       SD 复用 LCD 已初始化的总线——因此必须 sdcard_mount() 之前先 lcd_init()。
 *
 * 样本文件格式（小端，由 tools/export_samples.py 生成）：
 *   偏移 0   char[4]  magic = "ECG1"
 *   偏移 4   int32    n   采样点数
 *   偏移 8   int32    fs  采样率（360）
 *   偏移 12  int16[n] 信号，已滤波 + z-score，定标 SDCARD_SAMPLE_SCALE
 */
#ifndef SDCARD_H
#define SDCARD_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SDCARD_MOUNT_POINT "/sdcard"
#define SDCARD_MAX_NAME    32   /* 8.3 短名足够；留余量便于显示 */
#define SDCARD_MAX_FILES   16

/* 样本 int16 定标系数：float_value = int16_value / SDCARD_SAMPLE_SCALE */
#define SDCARD_SAMPLE_SCALE 2000.0f

/** @brief 挂载 SD 卡（复用 LCD 已初始化的 SPI2 总线）。失败返回非 ESP_OK。 */
esp_err_t sdcard_mount(void);

/** @brief 卸载（仅解挂文件系统，不释放共享的 SPI 总线）。 */
esp_err_t sdcard_unmount(void);

/** @brief 是否已挂载 */
bool sdcard_mounted(void);

/**
 * @brief 扫描 /sdcard 下的样本文件（.BIN，8.3 短名）
 * @param names     输出文件名数组（不含路径），调用方分配 [max_names][SDCARD_MAX_NAME]
 * @param max_names 最多返回多少个
 * @return 实际找到的数量（>=0）；未挂载返回 0
 */
int sdcard_list(char names[][SDCARD_MAX_NAME], int max_names);

/**
 * @brief 读取样本文件到 float 数组（已按 SDCARD_SAMPLE_SCALE 还原）
 * @param name    文件名（不含路径，如 "S100.BIN"）
 * @param out     输出缓冲指针（malloc 分配，调用方用 sdcard_free 释放）
 * @param out_n   输出采样点数
 * @param out_fs  输出采样率（可为 NULL）
 * @return ESP_OK / ESP_ERR_NOT_FOUND / ESP_ERR_INVALID_SIZE / ESP_ERR_NO_MEM
 */
esp_err_t sdcard_load(const char* name, float** out, int* out_n, int* out_fs);

/** @brief 打开样本并读取头部，文件指针停留在数据区起始处。 */
esp_err_t sdcard_open_sample(const char* name, FILE** out_file, int* out_n, int* out_fs);

/** @brief 从已打开样本流式读取至多 max_n 个 float 采样点。返回实际读取点数。 */
int sdcard_read_chunk(FILE* f, float* dst, int max_n);

/** @brief 关闭流式样本文件。 */
void sdcard_close_file(FILE* f);

/** @brief 释放 sdcard_load 分配的缓冲 */
void sdcard_free(float* buf);

#ifdef __cplusplus
}
#endif

#endif /* SDCARD_H */
