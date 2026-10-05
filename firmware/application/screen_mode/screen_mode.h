/**
 * screen_mode.h — 模式选择页
 */
#ifndef SCREEN_MODE_H
#define SCREEN_MODE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { MODE_TAP_NONE, MODE_TAP_LIVE, MODE_TAP_DEMO, MODE_TAP_SETUP } mode_tap_t;

void mode_draw(void);

/** 模式选择页命中判定（只判定，动作由调用方执行） */
mode_tap_t mode_tap(int x, int y);

#ifdef __cplusplus
}
#endif

#endif /* SCREEN_MODE_H */
