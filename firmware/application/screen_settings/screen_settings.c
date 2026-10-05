/**
 * screen_settings.c — 设置页
 */
#include "screen_settings.h"

#include "app_config.h"
#include "i18n.h"
#include "lcd.h"
#include "ui_widgets.h"

#define SET_LANG_BTN_X 100
#define SET_LANG_BTN_Y 60
#define SET_LANG_BTN_W 124
#define SET_LANG_BTN_H 56
#define SET_MINUS_X 16
#define SET_PLUS_X  184
#define SET_BR_Y    164
#define SET_BR_BTN_W 40
#define SET_BR_BTN_H 40
#define SET_BAR_X   64
#define SET_BAR_Y   170
#define SET_BAR_W   112
#define SET_BAR_H   28
#define SET_BACK_Y  240
#define SET_BACK_H  52


void settings_draw(void) {
    ui_draw_bg();
    ui_title(tr(T_SET_TITLE));

    /* 语言面板：左侧标签 + 右侧值按钮（点击切换） */
    ui_panel(12, 54, LCD_H_RES - 24, 68);
    lcd_draw_string(24, SET_LANG_BTN_Y + 20, tr(T_LANG), C_TXT_MID, C_PANEL);
    ui_button(SET_LANG_BTN_X, SET_LANG_BTN_Y, SET_LANG_BTN_W, SET_LANG_BTN_H,
              tr(T_LANG_V), 1, BTN_ACCENT);

    /* 亮度面板：标签 + 当前值 + [-] 档位条 [+] */
    ui_panel(12, 132, LCD_H_RES - 24, 84);
    char buf[32];
    snprintf(buf, sizeof(buf), "%s  %d%%", tr(T_BRIGHT),
             app_config_bright_table()[app_config_bright()]);
    lcd_draw_string(24, 142, buf, C_TXT_MID, C_PANEL);

    ui_button(SET_MINUS_X, SET_BR_Y, SET_BR_BTN_W, SET_BR_BTN_H, "-", 2, BTN_BORDER);
    ui_button(SET_PLUS_X, SET_BR_Y, SET_BR_BTN_W, SET_BR_BTN_H, "+", 2, BTN_BORDER);
    const int n_br = app_config_bright_count();
    int seg_w = (SET_BAR_W - 4 * 4) / n_br;            /* 5 格 + 4 个 4px 间隙 */
    for (int i = 0; i < n_br; i++) {
        int sx = SET_BAR_X + i * (seg_w + 4);
        uint16_t c = (i <= app_config_bright()) ? C_ACCENT : C_BORDER;  /* 激活格用主色 */
        lcd_fill_round_rect(sx, SET_BAR_Y, seg_w, SET_BAR_H, 4, c);
    }

    ui_button(MODE_BTN_X, SET_BACK_Y, MODE_BTN_W, SET_BACK_H, tr(T_BACK), 2, BTN_BORDER);
}

settings_tap_t settings_tap(int x, int y) {
    if (ui_hit(x, y, SET_LANG_BTN_X, SET_LANG_BTN_Y, SET_LANG_BTN_W, SET_LANG_BTN_H)) {
        i18n_set_lang(i18n_lang() == LANG_ZH ? LANG_EN : LANG_ZH);
        app_config_save();
        settings_draw();                 /* 立即以新语言重绘 */
    } else if (ui_hit(x, y, SET_MINUS_X, SET_BR_Y, SET_BR_BTN_W, SET_BR_BTN_H)) {
        app_config_set_bright(app_config_bright() - 1);  /* 内部夹范围并应用背光 */
        app_config_save();
        settings_draw();
    } else if (ui_hit(x, y, SET_PLUS_X, SET_BR_Y, SET_BR_BTN_W, SET_BR_BTN_H)) {
        app_config_set_bright(app_config_bright() + 1);
        app_config_save();
        settings_draw();
    } else if (ui_hit(x, y, MODE_BTN_X, SET_BACK_Y, MODE_BTN_W, SET_BACK_H)) {
        return SETTINGS_TAP_BACK;
    }
    return SETTINGS_TAP_NONE;
}
