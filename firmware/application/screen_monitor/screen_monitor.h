/**
 * screen_monitor.h — 监测页
 */
#ifndef SCREEN_MONITOR_H
#define SCREEN_MONITOR_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void monitor_draw(bool live);

/**
 * 报警（GUI 同款）：保留监测页波形，锁存报警并显示报警卡片与「确认报警」。
 * @param live       1=实时模式（状态行显示「实时模式」），0=演示模式
 * @param kind       1=心动过速，2=心动过缓
 * @param hr_extreme 锁存期极值心率（过速取峰值 / 过缓取最低）
 * @param phase      闪烁相位（红框 / 卡片描边跟随翻转）
 */
void monitor_draw_alarm(bool live, int kind, int hr_extreme, bool phase);

/* 「确认报警」按钮区域，供状态机做命中判定 */
#define MONITOR_ACK_X 8
#define MONITOR_ACK_Y 276
#define MONITOR_ACK_W 224
#define MONITOR_ACK_H 38

#ifdef __cplusplus
}
#endif

#endif /* SCREEN_MONITOR_H */
