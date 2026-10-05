/**
 * screen_alarm.c — 报警页
 */
#include "screen_alarm.h"

#include "i18n.h"
#include "lcd.h"
#include "player.h"
#include "ui_widgets.h"

#define ALARM_WORD_Y   64                    /* 异常类型大字，48px 高 */
#define ALARM_LABEL_Y  126                   /* 「心率」标签 */
#define ALARM_HR_Y     146                   /* 心率数值，48px 高 */
#define ALARM_HR_BOX_Y 142                   /* 数值局部重绘区（略大于字面，覆盖旧值） */
#define ALARM_HR_BOX_H 58
#define ALARM_UNIT_Y   206
#define ALARM_HINT_Y   272
static int  s_alarm_hr = -1;

void alarm_colors(bool phase, uint16_t* bg, uint16_t* fg) {
    /* 底色在「页面底色 / 淡砖红底」之间闪烁，大字与描边统一用唯一的红色语义 C_ALERT。
     * 小字不跟 fg：C_ALERT 在淡砖红底上只有 3.8:1，不够小字号用，故小字走 C_TXT_MID
     * （对两种底色分别 6.1 / 4.8）。 */
    *bg = phase ? C_ALERT_BG : C_BG;
    *fg = C_ALERT;
}

void alarm_draw_hr(uint16_t bg, uint16_t fg) {
    char buf[16];
    snprintf(buf, sizeof(buf), "%.0f", (double)player_engine()->hr);
    lcd_fill(0, ALARM_HR_BOX_Y, LCD_H_RES, ALARM_HR_BOX_H, bg);
    lcd_draw_string_scale_center(ALARM_HR_Y, buf, fg, bg, 3);
    s_alarm_hr = (int)(player_engine()->hr + 0.5f);
}

void alarm_draw(int kind, bool phase) {
    uint16_t bg, fg;
    alarm_colors(phase, &bg, &fg);

    lcd_clear(bg);
    lcd_draw_round_rect_outline(6, 6, LCD_H_RES - 12, LCD_V_RES - 12, 12, fg);

    const char* word = (kind == 1) ? tr(T_BIG_TACHY) : tr(T_BIG_BRADY);
    lcd_draw_string_scale_center(ALARM_WORD_Y, word, fg, bg, 3);

    lcd_draw_string_scale_center(ALARM_LABEL_Y, tr(T_HR_LABEL), C_TXT_MID, bg, 1);
    alarm_draw_hr(bg, fg);
    lcd_draw_string_scale_center(ALARM_UNIT_Y, tr(T_UNIT), C_TXT_MID, bg, 1);
    lcd_draw_string_scale_center(ALARM_HINT_Y, tr(T_TAPBACK), C_TXT_MID, bg, 1);
}
/** 心率读数是否变化（值变了才局部重绘，避免 2Hz 闪烁拖慢读数） */
bool alarm_hr_changed(void) { return (int)(player_engine()->hr + 0.5f) != s_alarm_hr; }
