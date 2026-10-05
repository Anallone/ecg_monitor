/**
 * screen_demo.h — 演示样本列表页
 */
#ifndef SCREEN_DEMO_H
#define SCREEN_DEMO_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { DEMO_ACT_NONE, DEMO_ACT_BACK, DEMO_ACT_PLAY } demo_act_t;
void demo_draw(void);
demo_act_t demo_touch(int* pick);

#ifdef __cplusplus
}
#endif

#endif /* SCREEN_DEMO_H */
