/**
 * sdcard.c — SD 卡挂载与 ECG 样本读取
 *
 * 关键点（务必遵守）：
 *  1. **复用 LCD 已初始化的 SPI2 总线**：本组件不主动建立总线。
 *     LCD（components/lcd/lcd.c）先初始化 SPI2_HOST，SD 只是在这条总线上
 *     以 CS=41 再挂一个设备。调用顺序必须 lcd_init() -> sdcard_mount()。
 *  2. `SDSPI_HOST_DEFAULT()` 在 ESP32-S3 上默认 slot 就是 SPI2_HOST，与 LCD
 *     一致（此处显式写出以免依赖默认值）。与微雪官方 ESP32-S3-Touch-LCD-2
 *     Demo 一致：其 LCD 用 SPI2_HOST，SD 用 SDSPI_HOST_DEFAULT()。
 *  3. 共享走线较长，max_freq_khz 降到 10MHz 提高稳定性（默认 20MHz）。
 */
#include "sdcard.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"

static const char* TAG = "SDCARD";

/* 微雪 ESP32-S3-Touch-LCD-2 的 SD 卡（SDSPI）接线 */
#define SD_PIN_SCK  GPIO_NUM_39
#define SD_PIN_MOSI GPIO_NUM_38
#define SD_PIN_MISO GPIO_NUM_40
#define SD_PIN_CS   GPIO_NUM_41

/* 与 LCD 共享的总线（components/lcd/lcd.c 的 LCD_SPI_HOST） */
#define SD_SPI_HOST SPI2_HOST

/* 样本文件头 */
#define SAMPLE_MAGIC     "ECG1"
#define SAMPLE_HDR_SIZE  12   /* 4 magic + 4 n + 4 fs */

static sdmmc_card_t* s_card = NULL;
static bool s_mounted = false;

bool sdcard_mounted(void) { return s_mounted; }

esp_err_t sdcard_mount(void) {
    if (s_mounted) return ESP_OK;

    esp_vfs_fat_sdmmc_mount_config_t mount_cfg = {
        .format_if_mount_failed = false,
        .max_files = 8,
        .allocation_unit_size = 16 * 1024,
    };

    sdmmc_host_t host = SDSPI_HOST_DEFAULT();
    host.slot = SD_SPI_HOST;        /* 复用 LCD 的 SPI2_HOST（S3 上 SDSPI 默认即 SPI2_HOST） */
    host.max_freq_khz = 10000;      /* 共享走线，降到 10MHz 更稳 */

    /* 总线本应由 lcd_init() 建立；下面这次 spi_bus_initialize 只是兜底：
     * 若总线已存在，IDF 返回 ESP_ERR_INVALID_STATE，即走复用路径。
     * esp_vfs_fat_sdspi_mount 内部会走 sdspi_host_init_device()，
     * 在既有总线上以 gpio_cs 添加设备。 */
    sdspi_device_config_t slot_cfg = SDSPI_DEVICE_CONFIG_DEFAULT();
    slot_cfg.gpio_cs = SD_PIN_CS;
    slot_cfg.host_id = SD_SPI_HOST;

    /* 用与 LCD 相同的配置"尝试"初始化：若总线已被 LCD 建立，IDF 返回
     * ESP_ERR_INVALID_STATE —— 那正是我们要的复用路径，不算错误。
     * 这样无论 lcd_init() 是否先执行都能工作，且不会重复配置总线。 */
    spi_bus_config_t bus_cfg = {
        .mosi_io_num = SD_PIN_MOSI,
        .miso_io_num = SD_PIN_MISO,
        .sclk_io_num = SD_PIN_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 4000,
    };
    esp_err_t ret = spi_bus_initialize(SD_SPI_HOST, &bus_cfg, SPI_DMA_CH_AUTO);
    if (ret == ESP_ERR_INVALID_STATE) {
        ESP_LOGI(TAG, "复用 LCD 已初始化的 SPI2 总线");
    } else if (ret == ESP_OK) {
        ESP_LOGW(TAG, "SPI2 总线此前未初始化（本组件代为建立）。"
                      "建议先调用 lcd_init()，以免显示侧配置不一致。");
    } else {
        ESP_LOGE(TAG, "SPI2 总线初始化失败: %s", esp_err_to_name(ret));
        return ret;
    }

    ESP_LOGI(TAG, "mounting %s (SDSPI on SPI2, CS=IO%d, 10MHz)...",
             SDCARD_MOUNT_POINT, SD_PIN_CS);
    ret = esp_vfs_fat_sdspi_mount(SDCARD_MOUNT_POINT, &host, &slot_cfg, &mount_cfg, &s_card);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "挂载失败 (%s)：检查 SD 卡是否插入/格式化为 FAT32、CS=IO%d 接线",
                 esp_err_to_name(ret), SD_PIN_CS);
        s_card = NULL;
        return ret;
    }

    s_mounted = true;
    ESP_LOGI(TAG, "filesystem mounted at %s", SDCARD_MOUNT_POINT);
    sdmmc_card_print_info(stdout, s_card);
    return ESP_OK;
}

esp_err_t sdcard_unmount(void) {
    if (!s_mounted) return ESP_OK;
    /* 只解挂文件系统；SPI2 总线与 LCD 共享，不能 free */
    esp_err_t ret = esp_vfs_fat_sdcard_unmount(SDCARD_MOUNT_POINT, s_card);
    s_card = NULL;
    s_mounted = false;
    return ret;
}

/* 判断是否为样本文件：8.3 短名，扩展名 BIN（不区分大小写） */
static bool is_sample_name(const char* name) {
    const char* dot = strrchr(name, '.');
    if (dot == NULL) return false;
    return (strcasecmp(dot + 1, "BIN") == 0);
}

/**
 * 校验文件头，确认是本项目的 ECG 样本。
 * 卡上可能混有其他 .BIN 文件，若只按扩展名列出，选中后会加载失败——
 * 因此在扫描阶段就用 magic 过滤，菜单里只出现真正可播放的样本。
 * 校验失败时通过 reason 输出具体原因，便于排查（文件没拷进来/拷贝截断/格式不符）。
 */
static bool is_valid_sample(const char* name, const char** reason, long* out_size) {
    static char detail[64];
    char path[64];
    snprintf(path, sizeof(path), "%s/%s", SDCARD_MOUNT_POINT, name);

    FILE* f = fopen(path, "rb");
    if (f == NULL) {
        *reason = "打不开";
        return false;
    }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (out_size) *out_size = size;

    uint8_t hdr[SAMPLE_HDR_SIZE];
    size_t rd = fread(hdr, 1, sizeof(hdr), f);
    fclose(f);

    if (rd != sizeof(hdr)) {
        snprintf(detail, sizeof(detail), "文件过短(%ld 字节，需 >=%d)", size, SAMPLE_HDR_SIZE);
        *reason = detail;
        return false;
    }
    if (memcmp(hdr, SAMPLE_MAGIC, 4) != 0) {
        snprintf(detail, sizeof(detail), "magic 非 ECG1（实为 %02X%02X%02X%02X）",
                 hdr[0], hdr[1], hdr[2], hdr[3]);
        *reason = detail;
        return false;
    }
    int32_t n = 0;
    memcpy(&n, hdr + 4, 4);
    if (n <= 0 || n > 360 * 600) {
        snprintf(detail, sizeof(detail), "采样点数异常(%ld)", (long)n);
        *reason = detail;
        return false;
    }
    long want = SAMPLE_HDR_SIZE + (long)n * 2;
    if (size < want) {
        snprintf(detail, sizeof(detail), "数据不完整(%ld/%ld 字节，拷贝可能截断)",
                 size, want);
        *reason = detail;
        return false;
    }
    return true;
}

int sdcard_list(char names[][SDCARD_MAX_NAME], int max_names) {
    if (!s_mounted || names == NULL || max_names <= 0) return 0;
    DIR* dir = opendir(SDCARD_MOUNT_POINT);
    if (dir == NULL) {
        ESP_LOGW(TAG, "无法打开 %s", SDCARD_MOUNT_POINT);
        return 0;
    }
    int n = 0;
    int skipped = 0;
    struct dirent* ent;
    ESP_LOGI(TAG, "---- 扫描 %s 下的 .BIN ----", SDCARD_MOUNT_POINT);
    while ((ent = readdir(dir)) != NULL && n < max_names) {
        if (ent->d_name[0] == '.') continue;      /* 跳过 . / .. / 隐藏文件 */
        if (!is_sample_name(ent->d_name)) continue;
        const char* reason = NULL;
        long size = 0;
        if (!is_valid_sample(ent->d_name, &reason, &size)) {
            ESP_LOGW(TAG, "  跳过 %-12s (%ld 字节)：%s", ent->d_name, size, reason);
            skipped++;
            continue;
        }
        strncpy(names[n], ent->d_name, SDCARD_MAX_NAME - 1);
        names[n][SDCARD_MAX_NAME - 1] = '\0';
        ESP_LOGI(TAG, "  样本 %d: %-12s (%ld 字节)", n + 1, names[n], size);
        n++;
    }
    closedir(dir);
    ESP_LOGI(TAG, "发现 %d 个有效样本（跳过 %d 个非本项目 .BIN）", n, skipped);
    if (n == 0) {
        ESP_LOGW(TAG, "把 tools/export_samples.py 生成的 S100.BIN/S200.BIN/... 拷到卡根目录");
    }
    return n;
}

esp_err_t sdcard_load(const char* name, float** out, int* out_n, int* out_fs) {
    if (!s_mounted) return ESP_ERR_INVALID_STATE;
    if (name == NULL || out == NULL || out_n == NULL) return ESP_ERR_INVALID_ARG;
    *out = NULL;
    *out_n = 0;

    char path[64];
    snprintf(path, sizeof(path), "%s/%s", SDCARD_MOUNT_POINT, name);

    FILE* f = fopen(path, "rb");
    if (f == NULL) {
        ESP_LOGE(TAG, "打不开 %s", path);
        return ESP_ERR_NOT_FOUND;
    }

    uint8_t hdr[SAMPLE_HDR_SIZE];
    if (fread(hdr, 1, sizeof(hdr), f) != sizeof(hdr) ||
        memcmp(hdr, SAMPLE_MAGIC, 4) != 0) {
        ESP_LOGE(TAG, "%s 头部非法（magic 应为 ECG1）", name);
        fclose(f);
        return ESP_ERR_INVALID_SIZE;
    }
    int32_t n = 0, fs = 0;
    memcpy(&n, hdr + 4, 4);
    memcpy(&fs, hdr + 8, 4);
    if (n <= 0 || n > 360 * 600) {   /* 上限 10 分钟，防异常文件 */
        ESP_LOGE(TAG, "%s 采样点数异常: %ld", name, (long)n);
        fclose(f);
        return ESP_ERR_INVALID_SIZE;
    }

    float* buf = (float*)malloc((size_t)n * sizeof(float));
    if (buf == NULL) {
        ESP_LOGE(TAG, "内存不足：需要 %ld 字节", (long)(n * sizeof(float)));
        fclose(f);
        return ESP_ERR_NO_MEM;
    }

    /* 分块读取 int16 -> float，避免再分配一块 int16 缓冲 */
    enum { CHUNK = 512 };
    int16_t tmp[CHUNK];
    int got = 0;
    while (got < n) {
        int want = (n - got) < CHUNK ? (n - got) : CHUNK;
        size_t rd = fread(tmp, sizeof(int16_t), (size_t)want, f);
        if (rd == 0) break;
        for (size_t i = 0; i < rd; i++) {
            buf[got + i] = (float)tmp[i] / SDCARD_SAMPLE_SCALE;
        }
        got += (int)rd;
    }
    fclose(f);

    if (got != n) {
        ESP_LOGE(TAG, "%s 数据不完整：期望 %ld 点，实得 %d", name, (long)n, got);
        free(buf);
        return ESP_ERR_INVALID_SIZE;
    }

    *out = buf;
    *out_n = n;
    if (out_fs) *out_fs = (int)fs;
    ESP_LOGI(TAG, "已载入 %s：%d 点 (%d Hz, %.1f 秒)", name, n, (int)fs, (float)n / (float)fs);
    return ESP_OK;
}

esp_err_t sdcard_open_sample(const char* name, FILE** out_file, int* out_n, int* out_fs) {
    if (!s_mounted || name == NULL || out_file == NULL || out_n == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *out_file = NULL;
    *out_n = 0;

    char path[64];
    snprintf(path, sizeof(path), "%s/%s", SDCARD_MOUNT_POINT, name);
    FILE* f = fopen(path, "rb");
    if (f == NULL) return ESP_ERR_NOT_FOUND;

    uint8_t hdr[SAMPLE_HDR_SIZE];
    if (fread(hdr, 1, sizeof(hdr), f) != sizeof(hdr) ||
        memcmp(hdr, SAMPLE_MAGIC, 4) != 0) {
        fclose(f);
        return ESP_ERR_INVALID_SIZE;
    }
    int32_t n = 0, fs = 0;
    memcpy(&n, hdr + 4, 4);
    memcpy(&fs, hdr + 8, 4);
    if (n <= 0 || n > 360 * 600) {
        fclose(f);
        return ESP_ERR_INVALID_SIZE;
    }
    *out_file = f;
    *out_n = n;
    if (out_fs) *out_fs = (int)fs;
    return ESP_OK;
}

int sdcard_read_chunk(FILE* f, float* dst, int max_n) {
    if (f == NULL || dst == NULL || max_n <= 0) return 0;
    enum { CHUNK = 256 };
    int16_t tmp[CHUNK];
    int got = 0;
    while (got < max_n) {
        int want = (max_n - got) < CHUNK ? (max_n - got) : CHUNK;
        size_t rd = fread(tmp, sizeof(int16_t), (size_t)want, f);
        if (rd == 0) break;
        for (size_t i = 0; i < rd; i++) {
            dst[got + i] = (float)tmp[i] / SDCARD_SAMPLE_SCALE;
        }
        got += (int)rd;
    }
    return got;
}

void sdcard_close_file(FILE* f) {
    if (f) fclose(f);
}

void sdcard_free(float* buf) {
    if (buf) free(buf);
}
