/**
 * screen_alarm.h — 报警页
 */
#ifndef SCREEN_ALARM_H
#define SCREEN_ALARM_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void alarm_colors(bool phase, uint16_t* bg, uint16_t* fg);
void alarm_draw_hr(uint16_t bg, uint16_t fg);
void alarm_draw(int kind, bool phase);
bool alarm_hr_changed(void);

#ifdef __cplusplus
}
#endif

#endif /* SCREEN_ALARM_H */
