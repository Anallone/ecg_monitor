/**
 * screen_mode.c — 模式选择页
 */
#include "screen_mode.h"

#include "i18n.h"
#include "ui_widgets.h"

#define BTN_LIVE_Y 58
#define BTN_DEMO_Y 143
#define BTN_SET_Y  228


void mode_draw(void) {
    ui_draw_bg();
    ui_title(tr(T_MODE));
    /* 按需求：三个入口统一用青蓝亮边 + 外圈光晕，视觉规格完全一致。
     * （注：三者等强会让「哪个是当前可用功能」不再一眼可辨，
     *   若之后想重新拉开主次，把实时/设置改回 BTN_BORDER 即可。） */
    ui_button(MODE_BTN_X, BTN_LIVE_Y, MODE_BTN_W, MODE_BTN_H, tr(T_REALTIME), 2, BTN_ACCENT);
    ui_button(MODE_BTN_X, BTN_DEMO_Y, MODE_BTN_W, MODE_BTN_H, tr(T_DEMO), 2, BTN_ACCENT);
    ui_button(MODE_BTN_X, BTN_SET_Y, MODE_BTN_W, MODE_BTN_H, tr(T_SETUP), 2, BTN_ACCENT);
}

mode_tap_t mode_tap(int x, int y) {
    if (ui_hit(x, y, MODE_BTN_X, BTN_LIVE_Y, MODE_BTN_W, MODE_BTN_H)) return MODE_TAP_LIVE;
    if (ui_hit(x, y, MODE_BTN_X, BTN_DEMO_Y, MODE_BTN_W, MODE_BTN_H)) return MODE_TAP_DEMO;
    if (ui_hit(x, y, MODE_BTN_X, BTN_SET_Y,  MODE_BTN_W, MODE_BTN_H)) return MODE_TAP_SETUP;
    return MODE_TAP_NONE;
}
