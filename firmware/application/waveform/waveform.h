/**
 * waveform.h — 波形渲染（帧缓冲，一次 blit）
 *
 * 内部持有一块 LCD_H_RES x 最大高度的 RGB565 帧缓冲，先把整条波形渲染进去，
 * 再一次性 lcd_draw_bitmap 刷到屏上——避免逐列小事务拖垮刷新。
 * 开机动画与监测页共用这块缓冲（不并发调用）。
 */
#ifndef WAVEFORM_H
#define WAVEFORM_H

#include <stdint.h>

#include "realtime.h"

#ifdef __cplusplus
extern "C" {
#endif

/** AAMI 五类名称（N/S/V/F/Q）与配色 */
const char* wave_class_name(int cls);
uint16_t    wave_class_color(int cls);

/**
 * 渲染波形条（整宽 LCD_H_RES、高 h）。
 * 每列取该列覆盖采样的 min/max 画竖线；纵向范围按窗口自适应（留 15% 余量）。
 */
void wave_render(const float* sig, int from, int to, int h);

/**
 * 渲染波形条，只画前 cols 列（cols < LCD_H_RES 用于开机动画的「扫描铺满」效果）。
 * 注意：纵向范围始终按完整窗口 [from,to) 计算，这样扫描过程中纵向比例稳定，
 * 不会随着显现列数增加而跳动。
 */
void wave_render_cols(const float* sig, int from, int to, int h, int cols);

/** 在已渲染的波形上叠加已分类心拍的类别竖线 */
void wave_overlay_beats(const rt_engine_t* e, int from, int cnt, int h);

/** 把内部帧缓冲刷到屏上（x,y 为左上角，w/h 为区域尺寸） */
void wave_blit(int x, int y, int w, int h);

/** 帧缓冲最大高度（调用方需保证 h <= 此值） */
int wave_max_height(void);

#ifdef __cplusplus
}
#endif

#endif /* WAVEFORM_H */
