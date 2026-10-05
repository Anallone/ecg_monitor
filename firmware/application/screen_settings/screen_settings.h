/**
 * screen_settings.h — 设置页
 */
#ifndef SCREEN_SETTINGS_H
#define SCREEN_SETTINGS_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { SETTINGS_TAP_NONE, SETTINGS_TAP_BACK } settings_tap_t;

void settings_draw(void);

/**
 * 设置页触摸处理：命中即执行对应动作（切换语言 / 调整亮度，并立即重绘）。
 * @return 是否点了「返回」（调用方据此回模式选择页）
 */
settings_tap_t settings_tap(int x, int y);

#ifdef __cplusplus
}
#endif

#endif /* SCREEN_SETTINGS_H */
