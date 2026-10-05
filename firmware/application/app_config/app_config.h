/**
 * app_config.h — 应用设置（NVS 持久化）
 *
 * 管理语言与屏幕亮度：进页时 load，改动后 save。语言状态实际存放在 i18n 模块，
 * 本模块负责把它与亮度一起读写 NVS。
 */
#ifndef APP_CONFIG_H
#define APP_CONFIG_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 从 NVS 读回语言与亮度，并立即应用背光（需先初始化 NVS） */
void app_config_load(void);

/** 把当前语言与亮度写入 NVS */
void app_config_save(void);

/* ─── 亮度（固定 5 档） ─── */
int  app_config_bright(void);                 /* 当前档位索引 0..count-1 */
void app_config_set_bright(int idx);          /* 夹到合法范围并立即应用背光 */
int  app_config_bright_count(void);
const uint8_t* app_config_bright_table(void); /* 各档百分比，供设置页画档位条 */

#ifdef __cplusplus
}
#endif

#endif /* APP_CONFIG_H */
