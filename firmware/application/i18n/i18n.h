/**
 * i18n.h — 双语文案（中/英）
 *
 * 全站文案集中在此：每个条目是一张 [2] 的表，用 tr() 按当前语言取。
 * 语言由 NVS 持久化（见 app_config 模块），断电保留。
 *
 * 注意：字库为按需子集，由 tools/gen_cjk_font.py 扫描本模块生成；改文案后必须重跑该脚本。
 *
 * 宽度预算（ST7789 240x320，16px 点阵）：
 *   scale3 每字符 48px（单行最多 5 字符），scale2 每字符 32px（最多 7），
 *   scale1 每字符 16px（最多 15）。英文文案必须按此压词，否则会越界
 *   （lcd_draw_bitmap 已加裁剪兜底，但截断的字也不好看）。
 */
#ifndef I18N_H
#define I18N_H

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum { LANG_ZH = 0, LANG_EN = 1, LANG_COUNT = 2 } lang_t;

/** 按当前语言取文案（s 是 [2] 的文案表） */
const char* tr(const char* const* s);

lang_t i18n_lang(void);
void   i18n_set_lang(lang_t lang);
/** 语言是否合法（用于 NVS 读回时的范围校验） */
bool   i18n_lang_valid(int v);

/* 启动页 */
extern const char* const T_TITLE[2];
extern const char* const T_SUBTITLE[2];
extern const char* const T_SKIP[2];
/* 模式选择 */
extern const char* const T_MODE[2];
extern const char* const T_REALTIME[2];
extern const char* const T_DEMO[2];
extern const char* const T_SETUP[2];
/* 设置页 */
extern const char* const T_SET_TITLE[2];
extern const char* const T_LANG[2];
/* 语言值显示「当前选中的语言」本身（用 s_lang 索引）：
 * ZH 显示「中文」、EN 显示「ENGLISH」。
 * 曾误写成 {"中文","CHINESE"}——英文模式下会显示 CHINESE，与当前界面语言自相矛盾。 */
extern const char* const T_LANG_V[2];
extern const char* const T_BRIGHT[2];
extern const char* const T_BACK[2];
/* 监测页（演示 / 实时共用版式） */
extern const char* const T_WAITSIG[2];
extern const char* const T_NODATA[2];
/* 无数据时的心率行单独一条：直接套 T_HR 会显示成 "心率 0 次/分"，是误导性的读数 */
extern const char* const T_HR_NA[2];
extern const char* const T_TAPBACK[2];
/* 演示样本列表 */
extern const char* const T_SAMPLES[2];
extern const char* const T_NOSD[2];
extern const char* const T_INSERT[2];
extern const char* const T_NOSMP[2];
extern const char* const T_COPY[2];
extern const char* const T_BUILTIN[2];
extern const char* const T_LOADFAIL[2];
/* 播放页（含格式符） */
extern const char* const T_HR[2];
extern const char* const T_BEAT[2];
extern const char* const T_AL_TACHY[2];
extern const char* const T_AL_BRADY[2];
extern const char* const T_AL_NONE[2];
/* 报警页 */
extern const char* const T_BIG_TACHY[2];
extern const char* const T_BIG_BRADY[2];
extern const char* const T_UNIT[2];
/* 报警页的「心率」标签。用独立的短标签而不是 T_HR——后者含格式符与单位，
 * 那行文案是给监测页单行显示用的，拆不开。 */
extern const char* const T_HR_LABEL[2];

#ifdef __cplusplus
}
#endif

#endif /* I18N_H */
