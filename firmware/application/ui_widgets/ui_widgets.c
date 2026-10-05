/**
 * ui_widgets.c — UI 基元实现（背景/标题/按钮/列表行/面板/跑马灯）
 */
#include "ui_widgets.h"

#include "lcd.h"

void ui_draw_bg(void) {
    lcd_clear(C_BG);
    for (int y = GRID_STEP; y < LCD_V_RES; y += GRID_STEP) {
        lcd_fill(0, y, LCD_H_RES, 1, C_GRID);
    }
    for (int x = GRID_STEP; x < LCD_H_RES; x += GRID_STEP) {
        lcd_draw_vline(x, 0, LCD_V_RES, C_GRID);
    }
}

void ui_title(const char* s) {
    lcd_draw_string_scale_center(8, s, C_ACCENT, C_BG, 2);
    int w = lcd_text_width(s, 2);
    int x = (LCD_H_RES - w) / 2;
    if (x < 0) { x = 8; w = LCD_H_RES - 16; }
    lcd_fill(x, 43, w, 1, C_ACCENT_DEEP);
}

void ui_hint(const char* s) {
    lcd_draw_string_scale_center(298, s, C_TXT_MID, C_BG, 1);
}

void ui_button(int x, int y, int w, int h, const char* label, int scale,
               btn_style_t style) {
    /* 激活态刻意**不填充**强调色，只用「卡片底 + 亮边 + 外圈光晕」：
     * 整块填充面积太大（200x76 ≈ 1.5 万像素）会刺眼且廉价，
     * 让主色只落在 1px 边线上才有「精密仪器」的观感。 */
    lcd_fill_round_rect(x, y, w, h, ROUND_R, C_PANEL);
    if (style == BTN_ACCENT) {
        /* 外圈光晕：半径同步 +2，与外框同心，四角才不会出现「双层错位」的观感 */
        lcd_draw_round_rect_outline(x - 2, y - 2, w + 4, h + 4, ROUND_R + 2, C_ACCENT_DEEP);
        lcd_draw_round_rect_outline(x, y, w, h, ROUND_R, C_ACCENT);
    } else if (style == BTN_BORDER) {
        lcd_draw_round_rect_outline(x, y, w, h, ROUND_R, C_BORDER);
    }
    int tw = lcd_text_width(label, scale);
    int tx = x + (w - tw) / 2;
    int ty = y + (h - 16 * scale) / 2;
    if (tx < x) tx = x;
    lcd_draw_string_scale(tx, ty, label,
                          style == BTN_ACCENT ? C_TXT_HI : C_TXT_MID, C_PANEL, scale);
}

void ui_row(int x, int y, int w, int h, const char* label, int clip_y0, int clip_y1) {
    int y0 = (y > clip_y0) ? y : clip_y0;
    int y1 = (y + h < clip_y1) ? (y + h) : clip_y1;
    if (y1 <= y0) return;
    lcd_fill(x, y0, w, y1 - y0, C_PANEL);
    if (y >= clip_y0 && y < clip_y1) lcd_fill(x, y, w, 1, C_BORDER);             /* 上边 */
    if (y + h - 1 >= clip_y0 && y + h - 1 < clip_y1) {                          /* 下边 */
        lcd_fill(x, y + h - 1, w, 1, C_BORDER);
    }
    lcd_draw_vline(x, y0, y1 - y0, C_BORDER);                                  /* 左边 */
    lcd_draw_vline(x + w - 1, y0, y1 - y0, C_BORDER);                          /* 右边 */
    lcd_fill(x, y0, 3, y1 - y0, C_ACCENT_DEEP);                                /* 左侧强调条 */
    lcd_draw_string_scale_clip(x + 12, y + (h - 16) / 2, label, C_TXT_HI, C_PANEL, 1,
                               clip_y0, clip_y1);
}

void ui_panel(int x, int y, int w, int h) {
    lcd_fill_round_rect(x, y, w, h, ROUND_R, C_PANEL);
    lcd_draw_round_rect_outline(x, y, w, h, ROUND_R, C_BORDER);
}

/* ─── 跑马灯边框 ───
 * 关键：按边批量绘制（一条边一次调用），不逐像素——否则每帧几十次小传输会拖垮刷新。 */

/** 把周长区间 [ta,tb) 落在同一条边上的部分批量画出（每条边最多一次调用） */
static void marquee_run(int x, int y, int w, int h, int ta, int tb, uint16_t c) {
    const int P = 2 * (w + h);
    const int t_right = w, t_bottom = w + h, t_left = 2 * w + h;
    if (ta < 0) ta = 0;
    if (tb > P) tb = P;
    if (tb <= ta) return;

    /* 上边：从左到右 */
    if (ta < t_right) {
        int b = tb < t_right ? tb : t_right;
        if (b > ta) lcd_fill(x + ta, y, b - ta, 1, c);
    }
    /* 右边：从上到下 */
    if (tb > t_right && ta < t_bottom) {
        int a = ta > t_right ? ta : t_right;
        int b = tb < t_bottom ? tb : t_bottom;
        if (b > a) lcd_draw_vline(x + w - 1, y + (a - t_right), b - a, c);
    }
    /* 下边：从右到左（区间 [a,b) 对应最左像素 x+w-b+t_bottom） */
    if (tb > t_bottom && ta < t_left) {
        int a = ta > t_bottom ? ta : t_bottom;
        int b = tb < t_left ? tb : t_left;
        if (b > a) lcd_fill(x + w - b + t_bottom, y + h - 1, b - a, 1, c);
    }
    /* 左边：从下到上（区间 [a,b) 对应最上像素 y+h-b+t_left） */
    if (tb > t_left) {
        int a = ta > t_left ? ta : t_left;
        if (tb > a) lcd_draw_vline(x, y + h - tb + t_left, tb - a, c);
    }
}

/** 画一段周长区间，自动处理跨起点的环绕 */
static void marquee_seg(int x, int y, int w, int h, int t0, int len, uint16_t c) {
    const int P = 2 * (w + h);
    if (P <= 0 || len <= 0) return;
    t0 %= P;
    if (t0 < 0) t0 += P;
    int t1 = t0 + len;
    if (t1 <= P) {
        marquee_run(x, y, w, h, t0, t1, c);
    } else {
        marquee_run(x, y, w, h, t0, P, c);
        marquee_run(x, y, w, h, 0, t1 - P, c);
    }
}

void ui_marquee_frame(int x, int y, int w, int h, int pos) {
    lcd_draw_rect(x, y, w, h, C_ACCENT_DEEP);        /* 打底轮廓 */
    const int seg = 72;                              /* 亮条总长 */
    const int head = seg / 3;                        /* 其中头段更长亮 */
    marquee_seg(x, y, w, h, pos, seg - head, C_ACCENT_DIM);          /* 尾段 */
    marquee_seg(x, y, w, h, pos + seg - head, head, C_ACCENT);       /* 头段 */
}
