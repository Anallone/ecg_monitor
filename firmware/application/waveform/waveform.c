/**
 * waveform.c — 波形渲染实现（帧缓冲）
 */
#include "waveform.h"

#include "lcd.h"
#include "ui_widgets.h"

/* 帧缓冲高度上限：监测页用 156，开机动画用 120，取大者。 */
#define WAVE_BUF_H 156

static uint16_t s_wave[LCD_H_RES * WAVE_BUF_H];

/* AAMI 五类（N/S/V/F/Q） */
static const char* const k_class_names[5] = {"N", "S", "V", "F", "Q"};
static const uint16_t k_class_colors[5] = {
    C_CLASS_N, C_CLASS_S, C_CLASS_V, C_CLASS_F, C_CLASS_Q,
};

const char* wave_class_name(int cls) {
    return (cls >= 0 && cls < 5) ? k_class_names[cls] : "-";
}

uint16_t wave_class_color(int cls) {
    return (cls >= 0 && cls < 5) ? k_class_colors[cls] : C_TXT_MID;
}

int wave_max_height(void) { return WAVE_BUF_H; }

void wave_render_cols(const float* sig, int from, int to, int h, int cols) {
    const int w = LCD_H_RES;
    if (h > WAVE_BUF_H) h = WAVE_BUF_H;
    if (h < 1) return;
    for (int i = 0; i < w * h; i++) s_wave[i] = C_BG;

    if (from < 0) from = 0;
    if (cols > w) cols = w;
    if (cols < 0) cols = 0;
    int cnt = to - from;
    if (cnt < 2) return;

    float lo = 1e9f, hi = -1e9f;
    for (int i = from; i < to; i++) {
        float v = sig[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
    }
    float rng = hi - lo;
    if (rng < 1e-6f) rng = 1.0f;
    lo -= rng * 0.15f;
    hi += rng * 0.15f;
    rng = hi - lo;

    for (int x = 0; x < cols; x++) {
        int s0 = from + (int)((long)cnt * x / w);
        int s1 = from + (int)((long)cnt * (x + 1) / w);
        if (s1 <= s0) s1 = s0 + 1;
        if (s1 > to) s1 = to;

        float cmin = 1e9f, cmax = -1e9f;
        for (int i = s0; i < s1; i++) {
            float v = sig[i];
            if (v < cmin) cmin = v;
            if (v > cmax) cmax = v;
        }
        if (cmin > cmax) continue;

        int y0 = h - 1 - (int)((cmax - lo) / rng * (h - 1));
        int y1 = h - 1 - (int)((cmin - lo) / rng * (h - 1));
        if (y0 < 0) y0 = 0;
        if (y0 > h - 1) y0 = h - 1;
        if (y1 < 0) y1 = 0;
        if (y1 > h - 1) y1 = h - 1;
        if (y0 > y1) { int t = y0; y0 = y1; y1 = t; }
        for (int yy = y0; yy <= y1; yy++) s_wave[yy * w + x] = C_WAVE;
    }
}

void wave_render(const float* sig, int from, int to, int h) {
    wave_render_cols(sig, from, to, h, LCD_H_RES);
}

void wave_overlay_beats(const rt_engine_t* e, int from, int cnt, int h) {
    if (cnt < 1) return;
    if (h > WAVE_BUF_H) h = WAVE_BUF_H;
    const int w = LCD_H_RES;
    for (int i = 0; i < e->n_beats; i++) {
        int r = (int)e->beats[i].r;
        if (r < from || r >= from + cnt) continue;
        if (e->beats[i].cls < 0) continue;
        int x = (int)((long)(r - from) * w / cnt);
        if (x < 0 || x >= w) continue;
        uint16_t c = wave_class_color(e->beats[i].cls);   /* 带 0..4 边界检查，防越界 */
        for (int yy = 0; yy < h; yy++) s_wave[yy * w + x] = c;
    }
}

void wave_blit(int x, int y, int w, int h) {
    if (h > WAVE_BUF_H) h = WAVE_BUF_H;
    lcd_draw_bitmap(x, y, w, h, s_wave);
}
