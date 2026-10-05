/**
 * screen_boot.h — 启动页
 */
#ifndef SCREEN_BOOT_H
#define SCREEN_BOOT_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 启动动画帧间隔（主循环延时用） */
#define BOOT_TICK_MS 20

void boot_enter(void);
void boot_frame(void);
bool boot_done(void);
void boot_advance(void);

#ifdef __cplusplus
}
#endif

#endif /* SCREEN_BOOT_H */
