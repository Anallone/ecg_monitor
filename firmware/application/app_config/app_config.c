/**
 * app_config.c — 语言与亮度的 NVS 持久化
 */
#include "app_config.h"

#include <stdlib.h>

#include "esp_log.h"
#include "i18n.h"
#include "lcd.h"
#include "nvs.h"

#define NVS_NAMESPACE "ecg_cfg"

/* 亮度固定 5 档 */
static const uint8_t k_bright_levels[5] = {20, 40, 60, 80, 100};
#define BRIGHT_LEVELS 5
static int s_bright_idx = 3;              /* 默认 80% */

static void bright_apply(void) {
    lcd_set_backlight(k_bright_levels[s_bright_idx]);
}

int app_config_bright(void) { return s_bright_idx; }

int app_config_bright_count(void) { return BRIGHT_LEVELS; }

const uint8_t* app_config_bright_table(void) { return k_bright_levels; }

void app_config_set_bright(int idx) {
    if (idx < 0) idx = 0;
    if (idx >= BRIGHT_LEVELS) idx = BRIGHT_LEVELS - 1;
    s_bright_idx = idx;
    bright_apply();
}

void app_config_load(void) {
    nvs_handle_t h;
    /* 首次上电 NVS 里还没有本命名空间（READONLY 打开返回 NOT_FOUND），
     * 此时也要照常应用默认档背光：否则保持 lcd_init 的 100%，
     * 与「默认 80%」档位不一致，设置页档位条与实际亮度对不上。 */
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        bright_apply();
        return;
    }
    uint8_t v = 0;
    if (nvs_get_u8(h, "lang", &v) == ESP_OK && i18n_lang_valid((int)v)) {
        i18n_set_lang((lang_t)v);
    }
    if (nvs_get_u8(h, "bright", &v) == ESP_OK) {
        /* 存的百分数就近映射回档位 */
        int best = 0;
        for (int i = 1; i < BRIGHT_LEVELS; i++) {
            if (abs((int)k_bright_levels[i] - (int)v) <
                abs((int)k_bright_levels[best] - (int)v)) best = i;
        }
        s_bright_idx = best;
    }
    nvs_close(h);
    bright_apply();
}

void app_config_save(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, "lang", (uint8_t)i18n_lang());
    nvs_set_u8(h, "bright", k_bright_levels[s_bright_idx]);
    nvs_commit(h);
    nvs_close(h);
}
