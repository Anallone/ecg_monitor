/**
 * i18n.c — 双语文案的实现（语言状态 + 文案表）
 */
#include "i18n.h"

#include <stdbool.h>

static lang_t s_lang = LANG_ZH;

const char* tr(const char* const* s) { return s[s_lang]; }

lang_t i18n_lang(void) { return s_lang; }

void i18n_set_lang(lang_t lang) {
    if (lang >= 0 && lang < LANG_COUNT) s_lang = lang;
}

bool i18n_lang_valid(int v) { return v >= 0 && v < LANG_COUNT; }

/* 启动页 */
const char* const T_TITLE[2]    = {"心电监测", "ECG"};
const char* const T_SUBTITLE[2] = {"心律失常监测系统", "MONITOR"};
const char* const T_SKIP[2]     = {"点击跳过", "TAP TO SKIP"};
/* 模式选择 */
const char* const T_MODE[2]     = {"模式选择", "MODE"};
const char* const T_REALTIME[2] = {"实时模式", "LIVE"};
const char* const T_DEMO[2]     = {"演示模式", "DEMO"};
const char* const T_SETUP[2]    = {"设置", "SETUP"};
/* 设置页 */
const char* const T_SET_TITLE[2]= {"设置", "SETUP"};
const char* const T_LANG[2]     = {"语言", "LANG"};
const char* const T_LANG_V[2]   = {"中文", "ENGLISH"};
const char* const T_BRIGHT[2]   = {"亮度", "BRIGHT"};
const char* const T_BACK[2]     = {"返回", "BACK"};
/* 监测页（演示 / 实时共用版式） */
const char* const T_WAITSIG[2]  = {"等待前端信号", "WAITING SIGNAL"};
const char* const T_NODATA[2]   = {"--", "--"};
const char* const T_HR_NA[2]    = {"心率 -- 次/分", "HR -- BPM"};
const char* const T_TAPBACK[2]  = {"点击返回", "TAP TO BACK"};
/* 演示样本列表 */
const char* const T_SAMPLES[2]  = {"选择样本", "SAMPLES"};
/* 监测页状态行在 x=150 处复用本条，右侧只剩 90px（5 字符）；
 * "NO SD CARD" 宽 160px 必被裁剪成半截字符，故英文压成 5 字符（见 i18n.h 宽度预算）。 */
const char* const T_NOSD[2]     = {"无SD卡", "NO SD"};
/* 插入卡后会自动检测刷新，无需重启，故文案不含「并重启」 */
const char* const T_INSERT[2]   = {"请插入 SD 卡", "INSERT CARD"};
const char* const T_NOSMP[2]    = {"卡上无样本", "NO SAMPLES"};
const char* const T_COPY[2]     = {"请拷入样本文件", "COPY SAMPLES"};
const char* const T_BUILTIN[2]  = {"内置样本", "BUILTIN"};
const char* const T_LOADFAIL[2] = {"载入失败", "LOAD FAILED"};
/* 播放页（含格式符） */
const char* const T_HR[2]       = {"心率 %3.0f 次/分", "HR %3.0f BPM"};
const char* const T_BEAT[2]     = {"心拍 %s", "BEAT %s"};
const char* const T_AL_TACHY[2] = {"报警 心动过速", "ALARM TACHY"};
const char* const T_AL_BRADY[2] = {"报警 心动过缓", "ALARM BRADY"};
const char* const T_AL_NONE[2]  = {"报警 无", "ALARM NONE"};
/* 报警页 */
const char* const T_BIG_TACHY[2]= {"心动过速", "TACHY"};
const char* const T_BIG_BRADY[2]= {"心动过缓", "BRADY"};
const char* const T_UNIT[2]     = {"次/分", "BPM"};
const char* const T_ALARM_ACK[2]= {"确认报警", "ACK ALARM"};
const char* const T_ALARM_PEAK[2] = {"峰值 %d", "PEAK %d"};
const char* const T_ALARM_LOW[2]  = {"最低 %d", "MIN %d"};
