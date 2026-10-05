/**
 * touch.h — 触摸输入
 *
 * 全站唯一的 lcd_touch_read 调用点：每 tick 调一次 touch_poll()，
 * 它返回「本 tick 是否为点按」（带 250ms 去抖），并把完整手势快照存下来，
 * 供需要区分「点击/拖动」的页面（演示列表页）用 touch_state() 取。
 */
#ifndef TOUCH_H
#define TOUCH_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * 触摸手势快照：touch_poll() 每 tick 刷新一次。
 * 除「点按」上报外，还带上按下起点坐标，供列表页做上下滑动——
 * 这样手势页与其他页共用同一份 I2C 读数，不必各自再调一次 lcd_touch_read。
 */
typedef struct {
    bool     press;    /* 本 tick 按下沿 */
    bool     down;     /* 当前按住 */
    bool     up;       /* 本 tick 抬起 */
    uint16_t x, y;     /* 最后一次有效触点坐标（抬手后保持按下期间的最后值） */
    uint16_t x0, y0;   /* 本次按下的起点坐标 */
} touch_state_t;

/**
 * 采样一次触摸。
 * @param x,y 若非 NULL，点按时写入触点坐标
 * @return 本 tick 是否发生「点按」（按下沿 + 250ms 去抖）
 */
bool touch_poll(uint16_t* x, uint16_t* y);

/** 最近一次采样得到的手势快照（只读） */
const touch_state_t* touch_state(void);

#ifdef __cplusplus
}
#endif

#endif /* TOUCH_H */
