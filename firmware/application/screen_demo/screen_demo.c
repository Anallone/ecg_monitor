/**
 * screen_demo.c — 演示样本列表页
 */
#include "screen_demo.h"

#include "i18n.h"
#include "lcd.h"
#include "player.h"
#include "sdcard.h"
#include "touch.h"
#include "ui_widgets.h"

#define DEMO_ROWS     5                 /* 一屏可见行数（决定列表带高度） */
#define DEMO_ROW_X    8
#define DEMO_ROW_W    214               /* 右侧 226..232 留给滚动条 */
#define DEMO_ROW_H    36
#define DEMO_BAND_Y0  MENU_TOP          /* 列表可见区：带外的部分一律裁掉 */
#define DEMO_BAND_Y1  (MENU_TOP + DEMO_ROWS * ROW_H)
#define DEMO_BAR_X    226
#define DEMO_BAR_W    6
#define DEMO_BAR_Y    MENU_TOP
#define DEMO_BAR_H    (DEMO_ROWS * ROW_H - 6)
#define DEMO_DRAG_SLOP    TOUCH_TAP_SLOP_PX
#define DEMO_BTN_MOVE_TOL 20            /* 返回/内置按钮的点击位移容忍（按钮比行高大，容错放宽） */
#define DEMO_BACK_X   12
#define DEMO_BACK_Y   262
#define DEMO_BACK_W   (LCD_H_RES - 24)
#define DEMO_BACK_H   38
#define DEMO_BUILTIN_X 12
#define DEMO_BUILTIN_Y (MENU_TOP + LINE_H * 2 + 16)
#define DEMO_BUILTIN_W (LCD_H_RES - 24)
#define DEMO_BUILTIN_H 40
static int  s_demo_px     = 0;     /* 列表已向上滚动的像素数（0 = 首行贴住带顶） */
static int  s_demo_px0    = 0;     /* 本次按下瞬间的 s_demo_px（拖动按位移 1:1 跟随） */
static bool s_demo_drag   = false; /* 本次触摸已判定为滑动（滑动过就不触发选中） */
static bool s_demo_bar    = false; /* 本次触摸起点落在滚动条上（拉动 = 直接定位） */
static bool s_demo_inlist = false; /* 本次触摸起点在列表带内（只有它才产生滚动） */
static bool s_demo_armed = false;

static int demo_max_px(void) {
    int total = player_count() * ROW_H;
    int band  = DEMO_BAND_Y1 - DEMO_BAND_Y0;
    return (total > band) ? (total - band) : 0;
}

static int demo_clamp_px(int px) {
    int mx = demo_max_px();
    if (px < 0) px = 0;
    if (px > mx) px = mx;
    return px;
}

static int demo_thumb_h(void) {
    int th = (player_count() > 0) ? DEMO_BAR_H * DEMO_ROWS / player_count() : DEMO_BAR_H;
    if (th < 28) th = 28;
    if (th > DEMO_BAR_H) th = DEMO_BAR_H;
    return th;
}

static void draw_demo_list(void) {
    const int band_h = DEMO_BAND_Y1 - DEMO_BAND_Y0;
    lcd_fill(0, DEMO_BAND_Y0, LCD_H_RES, band_h, C_BG);
    /* 背景网格是「面板底纹」，不随列表滚动，故在带内按原样补画 */
    for (int y = (DEMO_BAND_Y0 / GRID_STEP + 1) * GRID_STEP; y < DEMO_BAND_Y1; y += GRID_STEP) {
        lcd_fill(0, y, LCD_H_RES, 1, C_GRID);
    }
    for (int x = GRID_STEP; x < LCD_H_RES; x += GRID_STEP) {
        lcd_draw_vline(x, DEMO_BAND_Y0, band_h, C_GRID);
    }

    const int off   = s_demo_px % ROW_H;      /* 首行被滚出带顶的像素 */
    const int first = s_demo_px / ROW_H;
    char line[SDCARD_MAX_NAME + 16];
    for (int i = first; i < player_count(); i++) {
        int y = DEMO_BAND_Y0 + (i - first) * ROW_H - off;
        if (y >= DEMO_BAND_Y1) break;
        snprintf(line, sizeof(line), "%d %s", i + 1, player_name(i));
        ui_row(DEMO_ROW_X, y, DEMO_ROW_W, DEMO_ROW_H, line, DEMO_BAND_Y0, DEMO_BAND_Y1);
    }

    /* 滚动条：样本多于一屏才出现（既是「下面还有」的提示，也可直接拖） */
    const int max_px = demo_max_px();
    if (max_px > 0) {
        int th = demo_thumb_h();
        int ty = DEMO_BAR_Y + (DEMO_BAR_H - th) * s_demo_px / max_px;
        lcd_fill_round_rect(DEMO_BAR_X, DEMO_BAR_Y, DEMO_BAR_W, DEMO_BAR_H, DEMO_BAR_W / 2,
                            C_PANEL);
        lcd_fill_round_rect(DEMO_BAR_X, ty, DEMO_BAR_W, th, DEMO_BAR_W / 2, C_ACCENT);
    }
}

void demo_draw(void) {
    ui_draw_bg();
    ui_title(tr(T_SAMPLES));

    if (player_count() > 0) {
        s_demo_px = demo_clamp_px(s_demo_px);   /* 样本数变化后夹回合法范围 */
        draw_demo_list();
    } else {
        s_demo_px = 0;
        /* 无样本可播：区分「卡没插」与「卡上没样本」，避免误导排查方向 */
        bool mounted = sdcard_mounted();
        lcd_draw_string(16, MENU_TOP, mounted ? tr(T_NOSMP) : tr(T_NOSD),
                        C_ALERT, C_BG);
        lcd_draw_string(16, MENU_TOP + LINE_H, mounted ? tr(T_COPY) : tr(T_INSERT),
                        C_TXT_MID, C_BG);
        ui_button(DEMO_BUILTIN_X, DEMO_BUILTIN_Y, DEMO_BUILTIN_W, DEMO_BUILTIN_H,
                  tr(T_BUILTIN), 1, BTN_ACCENT);
    }

    /* 明确的返回按钮：无论有无样本都在同一位置，触摸即可回模式选择页 */
    ui_button(DEMO_BACK_X, DEMO_BACK_Y, DEMO_BACK_W, DEMO_BACK_H, tr(T_BACK), 2, BTN_BORDER);
}

static bool demo_pt_in_list(uint16_t x, uint16_t y) {
    return y >= DEMO_BAND_Y0 && y < DEMO_BAND_Y1 &&
           x >= DEMO_ROW_X && x < DEMO_ROW_X + DEMO_ROW_W;
}

demo_act_t demo_touch(int* pick) {
    *pick = -1;
    const int max_px = demo_max_px();

    if (touch_state()->press) {
        s_demo_px0    = s_demo_px;
        s_demo_drag   = false;
        s_demo_armed  = true;      /* 按下发生在本页，这次手势才归本页处理 */
        s_demo_inlist = demo_pt_in_list(touch_state()->x0, touch_state()->y0);
        /* 起点落在滚动条上：拖动 = 直接定位（点哪滚哪） */
        s_demo_bar    = (max_px > 0) && (touch_state()->x0 >= DEMO_BAR_X - 4) &&
                        (touch_state()->y0 >= DEMO_BAND_Y0) && (touch_state()->y0 < DEMO_BAND_Y1);
    }

    /* 拖动中：只有列表带内/滚动条才滚动；按在按钮或标题上时列表纹丝不动 */
    if (touch_state()->down && max_px > 0 && (s_demo_inlist || s_demo_bar)) {
        int px = s_demo_px;
        if (s_demo_bar) {
            int th    = demo_thumb_h();
            int track = DEMO_BAR_H - th;                 /* 滑块可移动范围 */
            int rel   = (int)touch_state()->y - DEMO_BAR_Y - th / 2;
            if (rel <= 0) {
                px = 0;
            } else if (track <= 0 || rel >= track) {
                px = max_px;
            } else {
                px = (int)(((long)rel * max_px + track / 2) / track);
            }
        } else {
            int dy = (int)touch_state()->y - (int)touch_state()->y0;
            if (!s_demo_drag && (dy > DEMO_DRAG_SLOP || dy < -DEMO_DRAG_SLOP)) s_demo_drag = true;
            if (s_demo_drag) px = s_demo_px0 - dy;   /* 向下拖 = 看更靠前的样本 */
        }
        px = demo_clamp_px(px);
        if (px != s_demo_px) {
            s_demo_px   = px;
            s_demo_drag = true;
            draw_demo_list();
        }
    }

    if (touch_state()->up) {
        int      dy     = (int)touch_state()->y - (int)touch_state()->y0;
        bool     armed  = s_demo_armed;
        bool     moved  = s_demo_drag;
        bool     swiped = touch_state()->gesture == TOUCH_GESTURE_SWIPE;
        bool     tapped = touch_state()->gesture == TOUCH_GESTURE_TAP;
        uint16_t px     = touch_state()->x0, py = touch_state()->y0;   /* 命中判定用按下点 */
        s_demo_armed  = false;
        s_demo_drag   = false;
        s_demo_inlist = false;
        s_demo_bar    = false;
        if (!armed) return DEMO_ACT_NONE;   /* 上一页按下的手势不归本页 */

        /* 返回键：不在滚动区内，位移容忍放宽一些，按得稍偏也算点中 */
        if (!swiped && abs(dy) <= DEMO_BTN_MOVE_TOL &&
            ui_hit(px, py, DEMO_BACK_X, DEMO_BACK_Y, DEMO_BACK_W, DEMO_BACK_H)) {
            return DEMO_ACT_BACK;
        }
        if (player_count() == 0) {
            if (!swiped && abs(dy) <= DEMO_BTN_MOVE_TOL &&
                ui_hit(px, py, DEMO_BUILTIN_X, DEMO_BUILTIN_Y, DEMO_BUILTIN_W, DEMO_BUILTIN_H)) {
                *pick = PLAYER_BUILTIN;
                return DEMO_ACT_PLAY;
            }
        } else if (tapped && !moved && abs(dy) <= DEMO_DRAG_SLOP) {
            /* 没滑动 = 点击选中。行位置与 draw_demo_list 用同一套布局（含首行滚出的像素） */
            const int off   = s_demo_px % ROW_H;
            const int first = s_demo_px / ROW_H;
            for (int i = first; i < player_count(); i++) {
                int ry = DEMO_BAND_Y0 + (i - first) * ROW_H - off;
                if (ry >= DEMO_BAND_Y1) break;
                int ry0 = (ry > DEMO_BAND_Y0) ? ry : DEMO_BAND_Y0;
                int ry1 = (ry + DEMO_ROW_H < DEMO_BAND_Y1) ? (ry + DEMO_ROW_H) : DEMO_BAND_Y1;
                if (ry1 <= ry0) continue;
                if (py >= ry0 && py < ry1 && ui_hit(px, py, DEMO_ROW_X, ry0, DEMO_ROW_W, ry1 - ry0)) {
                    *pick = i;
                    return DEMO_ACT_PLAY;
                }
            }
        }
    }
    return DEMO_ACT_NONE;
}
