/**
 * monitor.c — 监测页（演示模式与实时模式共用同一版式）
 *
 * 版式（自上而下）：
 *   状态行     左：当前心拍类别   右：模式（小号次要色）
 *   大数字     心率，标题色；报警时整数字变砖红（唯一的红色语义）
 *   小字行     单位 · 报警
 *   参考虚线   两条分割线（边框色）
 *   波形区     灰蓝波形 + 心拍标记（滚动）
 *   底部       仅保留 V 类计数（砖红，异常才是重点）+ 进度
 *
 * 设计令牌见 ui_widgets.h：桌面端浅底深字、端侧深底浅字，
 * 但「灰蓝主色 + 砖红报警」的语义与组件规则两端一致。
 */
#include "screen_monitor.h"

#include <stdio.h>

#include "i18n.h"
#include "lcd.h"
#include "player.h"
#include "ui_widgets.h"
#include "waveform.h"

#define DISP_POINTS     1080
#define WAVE_Y          100
#define WAVE_H          120
#define HR_NUM_Y        26      /* 大数字心率顶边（scale 3 = 48px） */
#define INFO_Y          78      /* 单位 / 报警小字 */
#define STATUS_Y        6       /* 顶部状态行 */
static int s_marquee = 0;

/* 波形渲染用的线性 scratch：直播样本在环形缓冲里、回放样本在整段缓冲里，
 * rt_sample_at() 把两者都映射成「绝对下标 -> 样本」，waveform.c 无需感知环形。
 * 静态分配（4.3KB）——主任务栈仅 3.5KB，不能放栈上。 */
static float s_scratch[DISP_POINTS];

/** 水平虚线（分割用） */
static void dashed_hline(int y, uint16_t c) {
    for (int x = 0; x < LCD_H_RES; x += 8) lcd_fill(x, y, 4, 1, c);
}

/** 演示模式报警卡片：两行（异常类型 + 锁存期极值），描边随闪烁相位翻转。 */
static void alarm_card(int kind, int hr_extreme, bool phase) {
    const int x = 8;
    const int y = WAVE_Y + WAVE_H + 2;      /* 222：波形区下方留 2px 间隙 */
    const int w = LCD_H_RES - 16;
    const int h = 50;
    uint16_t border = phase ? C_ALERT : C_ALERT_DIM;
    char buf[32];

    lcd_fill_round_rect(x, y, w, h, ROUND_R, C_ALERT_BG);
    lcd_draw_round_rect_outline(x, y, w, h, ROUND_R, border);

    const char* word = (kind == 1) ? tr(T_BIG_TACHY) : tr(T_BIG_BRADY);
    lcd_draw_string_scale(x + 12, y + 6, word, C_TXT_HI, C_ALERT_BG, 1);

    snprintf(buf, sizeof(buf), (kind == 1) ? tr(T_ALARM_PEAK) : tr(T_ALARM_LOW),
             hr_extreme);
    lcd_draw_string_scale(x + 12, y + 28, buf, C_TXT_MID, C_ALERT_BG, 1);
}

/** 确认报警按钮：砖红实心 + 深色文字，报警期间把注意力引向「确认」。 */
static void alarm_ack_button(void) {
    lcd_draw_round_rect_outline(MONITOR_ACK_X - 2, MONITOR_ACK_Y - 2,
                                MONITOR_ACK_W + 4, MONITOR_ACK_H + 4,
                                ROUND_R + 2, C_ALERT_DIM);
    lcd_fill_round_rect(MONITOR_ACK_X, MONITOR_ACK_Y, MONITOR_ACK_W, MONITOR_ACK_H,
                        ROUND_R, C_ALERT);
    int tw = lcd_text_width(tr(T_ALARM_ACK), 1);
    int tx = MONITOR_ACK_X + (MONITOR_ACK_W - tw) / 2;
    int ty = MONITOR_ACK_Y + (MONITOR_ACK_H - 16) / 2;
    lcd_draw_string_scale(tx, ty, tr(T_ALARM_ACK), C_BG, C_ALERT, 1);
}

static void monitor_draw_ex(bool live, int alarm_kind, int alarm_extreme, bool phase) {
    const rt_engine_t* e = player_engine();
    const bool has_data = (e->sig != NULL);
    const bool alarming = alarm_kind != 0;
    const int  effective_alarm = alarming ? alarm_kind : (has_data ? e->alarm : 0);
    char buf[48];

    lcd_fill(0, 0, LCD_H_RES, WAVE_Y, C_BG);

    /* ── 状态行：左「心拍类别」/ 右「模式」 ── */
    int beat_cls = (has_data && e->n_classified > 0)
                       ? (int)e->beats[e->n_classified - 1].cls : -1;
    snprintf(buf, sizeof(buf), tr(T_BEAT),
             beat_cls >= 0 ? wave_class_name(beat_cls) : tr(T_NODATA));
    lcd_draw_string_scale(8, STATUS_Y, buf,
                          beat_cls >= 0 ? wave_class_color(beat_cls) : C_TXT_LOW, C_BG, 1);

    lcd_draw_string_scale(150, STATUS_Y, live ? tr(T_REALTIME) : tr(T_DEMO),
                          C_TXT_LOW, C_BG, 1);

    /* ── 大数字心率：标题色；报警时变砖红，异常一眼可辨 ── */
    if (has_data && e->hr > 0) {
        snprintf(buf, sizeof(buf), "%.0f", (double)e->hr);
    } else {
        snprintf(buf, sizeof(buf), "--");
    }
    lcd_draw_string_scale_center(HR_NUM_Y, buf,
                                 (alarming || (has_data && e->alarm)) ? C_ALERT : C_TXT_HI,
                                 C_BG, 3);

    /* ── 小字行：单位 · 报警 ── */
    if (has_data) {
        const char* al = (effective_alarm == 1) ? tr(T_AL_TACHY)
                       : (effective_alarm == 2) ? tr(T_AL_BRADY) : tr(T_AL_NONE);
        snprintf(buf, sizeof(buf), "%s · %s", tr(T_UNIT), al);
        lcd_draw_string_scale_center(INFO_Y, buf,
                                     effective_alarm ? C_ALERT : C_TXT_LOW, C_BG, 1);
    } else {
        /* 无数据时给「等待前端信号」，不显示 0 这类误导性读数 */
        lcd_draw_string_scale_center(INFO_Y, tr(T_WAITSIG), C_TXT_LOW, C_BG, 1);
    }

    /* ── 参考虚线：把信息区与波形区分开 ── */
    dashed_hline(WAVE_Y - 6, C_BORDER);
    dashed_hline(WAVE_Y - 3, C_BORDER);

    /* ── 波形区 ── */
    if (has_data) {
        int start = rt_pos(e) - DISP_POINTS;
        if (start < 0) start = 0;
        int cnt = rt_pos(e) - start;
        for (int i = 0; i < cnt; i++) s_scratch[i] = rt_sample_at(e, start + i);
        wave_render(s_scratch, 0, cnt, WAVE_H);
        wave_overlay_beats(e, start, cnt, WAVE_H);
        wave_blit(0, WAVE_Y, LCD_H_RES, WAVE_H);
    } else {
        /* 空态：清底 + 一条基线 + 居中提示 */
        lcd_fill(0, WAVE_Y, LCD_H_RES, WAVE_H, C_BG);
        int base = WAVE_Y + WAVE_H / 2;
        lcd_fill(0, base, LCD_H_RES, 1, C_ACCENT_DEEP);
    }

    /* 跑马灯外框：两种模式都画（波形在刷新 / 正在等待采集） */
    s_marquee += 14;
    ui_marquee_frame(0, WAVE_Y, LCD_H_RES, WAVE_H, s_marquee);

    /* GUI 同款：报警时不铺整屏红底，只让波形外框在砖红/中性间闪烁。 */
    if (alarming) {
        lcd_draw_rect(0, WAVE_Y, LCD_H_RES, WAVE_H,
                      phase ? C_ALERT : C_BORDER);
    }

    /* ── 底部：报警时换成报警卡片 + 确认按钮，其余情况保持 V 计数 / 进度 ── */
    lcd_fill(0, WAVE_Y + WAVE_H, LCD_H_RES, LCD_V_RES - (WAVE_Y + WAVE_H), C_BG);
    if (alarming) {
        alarm_card(alarm_kind, alarm_extreme, phase);
        alarm_ack_button();
        return;
    }

    int sy = WAVE_Y + WAVE_H + 8;
    snprintf(buf, sizeof(buf), "V: %d", e->cls_count[2]);
    lcd_draw_string_scale(8, sy, buf, C_ALERT, C_BG, 1);

    if (live) {
        /* 实时模式没有「播放进度」概念，该位置放模式标识 */
        lcd_draw_string_scale(156, sy, tr(T_REALTIME), C_ACCENT, C_BG, 1);
    } else {
        int pct = (int)(player_progress() * 100.0f);
        if (pct < 0) pct = 0;
        if (pct > 100) pct = 100;
        snprintf(buf, sizeof(buf), "%d%%", pct);
        lcd_draw_string_scale(184, sy, buf, C_ACCENT, C_BG, 1);
    }

    ui_hint(tr(T_TAPBACK));
}

void monitor_draw(bool live) {
    monitor_draw_ex(live, 0, 0, false);
}

void monitor_draw_alarm(bool live, int kind, int hr_extreme, bool phase) {
    monitor_draw_ex(live, kind, hr_extreme, phase);
}
