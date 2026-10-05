/**
 * screen_boot.c — 启动页
 */
#include "screen_boot.h"

#include "lcd.h"
#include "i18n.h"
#include "player.h"
#include "ui_widgets.h"
#include "waveform.h"

#define BOOT_FRAMES    100            /* 100 * 20ms = 2 秒 */
#define BOOT_WAVE_Y    140
#define BOOT_WAVE_H    120
#define BOOT_WIN       600            /* 波形显示窗口（采样点） */
#define BF_TITLE0 0                   /* 标题淡入 */
#define BF_TITLE1 16
#define BF_LINE0  10                  /* 强调线自中心向两侧展开 */
#define BF_LINE1  30
#define BF_SUB0   20                  /* 副标题淡入 */
#define BF_SUB1   36
#define BF_SWEEP  60                  /* 波形扫描铺满的帧号（之后转为滚动运行） */
#define BF_HINT0  70                  /* 底部提示淡入 */
#define BF_HINT1  92
#define BF_TEXT_END 36                /* 此后文字不再重绘（避免每帧擦写拖帧率） */
static int s_boot_frame = 0;

static uint8_t boot_ramp(int f, int f0, int f1) {
    if (f <= f0) return 0;
    if (f >= f1) return 255;
    return (uint8_t)((long)(f - f0) * 255 / (f1 - f0));
}

void boot_enter(void) {
    s_boot_frame = 0;
    ui_draw_bg();          /* 深底 + 网格（静态底，只在进入时画一次） */
    boot_frame();          /* 立刻画首帧，避免出现空白 */
}

void boot_frame(void);   /* 前置声明：boot_enter 会立即画首帧 */



/**
 * 启动动画单帧。时间轴见 BF_* 常量：
 *   标题淡入 -> 强调线自中心展开 -> 副标题淡入 -> 波形逐列扫描铺满 -> 转滚动 -> 提示淡入
 */
void boot_frame(void) {
    const int f = s_boot_frame;

    /* 文字区：只在淡入阶段逐帧重绘（重绘需先擦除整块，约 76 次 blit，
     * 全程重绘会明显拖帧率；淡入结束后画面已定型，无需再动）。 */
    if (f < BF_TEXT_END) {
        lcd_fill(0, 26, LCD_H_RES, 78, C_BG);

        uint16_t tc = lcd_mix(C_TXT_HI, C_BG, boot_ramp(f, BF_TITLE0, BF_TITLE1));
        lcd_draw_string_scale_center(30, tr(T_TITLE), tc, C_BG, 3);

        /* 强调线自中心向两侧展开，比直接出现更有「启动」感 */
        int tw = lcd_text_width(tr(T_TITLE), 3);
        int full = (tw < LCD_H_RES - 40) ? tw : (LCD_H_RES - 40);
        int cur = full * boot_ramp(f, BF_LINE0, BF_LINE1) / 255;
        lcd_fill((LCD_H_RES - cur) / 2, 86, cur, 2, C_ACCENT);

        uint16_t sc = lcd_mix(C_TXT_MID, C_BG, boot_ramp(f, BF_SUB0, BF_SUB1));
        lcd_draw_string_scale_center(96, tr(T_SUBTITLE), sc, C_BG, 1);
    }

    int boot_len = 0;
    const float* boot_sig = player_builtin(&boot_len);
    int from, to, cols;
    if (f < BF_SWEEP) {
        /* 扫描铺满：窗口固定，逐列显现，模拟监护仪开机描记 */
        from = 0;
        to = (BOOT_WIN < boot_len) ? BOOT_WIN : boot_len;
        cols = LCD_H_RES * (f + 1) / BF_SWEEP;
    } else {
        /* 铺满后转为滚动运行 */
        int span = BOOT_FRAMES - BF_SWEEP;
        int adv = (f - BF_SWEEP) * ((boot_len - BOOT_WIN) / (span > 0 ? span : 1));
        to = adv + BOOT_WIN;
        if (to > boot_len) to = boot_len;
        from = to - BOOT_WIN;
        cols = LCD_H_RES;
    }
    wave_render_cols(boot_sig, from, to, BOOT_WAVE_H, cols);
    wave_blit(0, BOOT_WAVE_Y, LCD_H_RES, BOOT_WAVE_H);
    /* 扫描光标：明亮竖线标示描记位置（仅扫描阶段） */
    if (f < BF_SWEEP && cols > 0 && cols < LCD_H_RES) {
        lcd_draw_vline(cols, BOOT_WAVE_Y, BOOT_WAVE_H, C_ACCENT);
    }

    /* 底部提示淡入（末尾才出现，避免一开始就抢注意力） */
    if (f >= BF_HINT0) {
        lcd_fill(0, 294, LCD_H_RES, 20, C_BG);
        uint16_t hc = lcd_mix(C_TXT_MID, C_BG, boot_ramp(f, BF_HINT0, BF_HINT1));
        lcd_draw_string_scale_center(298, tr(T_SKIP), hc, C_BG, 1);
    }
    /* 启动页不再叠额外光效：波形扫描 + 逐级淡入已足够，多加反而显乱。 */
}
/** 动画是否播完 */
bool boot_done(void) { return s_boot_frame >= BOOT_FRAMES; }
/** 推进一帧 */
void boot_advance(void) { s_boot_frame++; }
