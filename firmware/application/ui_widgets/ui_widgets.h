/**
 * ui_widgets.h — 全站共用的 UI 工具箱与配色
 *
 * 只依赖 lcd 组件。各页面用这里的基元拼版面，保证跨页风格一致。
 *
 * 配色：深底 + 单一青蓝强调（点阵屏没有 alpha/GPU 合成，毛玻璃、阴影都做不了，
 * 改用「色阶分层 + 描边发光 + 呼吸」这三样在点阵屏上真正出效果的手法）。
 * 色阶关系：C_BG < C_GRID < C_PANEL < C_TXT_LOW < C_TXT_MID < C_TXT_HI，
 * 强调色 C_ACCENT 全站只用一种，避免彩虹渐变式的廉价感。
 */
#ifndef UI_WIDGETS_H
#define UI_WIDGETS_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ─── 配色：深色莫兰迪（与 PC 端浅色端同一套设计令牌，按底色适配明度）───
 * 同一套「灰蓝主色 + 砖红报警」的语义，桌面端是浅底深字、端侧是深底浅字。
 * 对比度按 WCAG 校验（见各常量注释），文字 ≥4.5:1、图形 ≥3:1。 */
#define C_BG          0x1926   /* #1F2733 页面底色（深灰蓝，与桌面主色同族） */
#define C_GRID        0x2167   /* #262F3C 背景网格：仅比底色亮一档（装饰） */
#define C_PANEL       0x29A8   /* #2A3542 卡片 / 按钮底 */
#define C_BORDER      0x3A4A   /* #3D4856 描边 / 分割线（装饰） */
#define C_TXT_HI      0xDEFC   /* #D9DDE3 标题 / 大数字                11.0:1 */
#define C_TXT_MID     0x9D36   /* #9AA7B6 正文                          6.1:1 */
#define C_TXT_LOW     0x9515   /* #93A0AF 次要 / 占位 / 单位            5.7:1 */
/* 主强调：只用于「填充与线条」——选中态实心底、主按钮、波形线。
 * 选中态规则为「实心主色底 + 反色字（C_BG）」，两端一致。 */
#define C_ACCENT      0x8D17   /* #8FA3B8 主强调                        4.8:1 */
#define C_WAVE        0x8D17   /* #8FA3B8 波形线（与主强调同色，绿波退役） */
#define C_ACCENT_DIM  0x7C74   /* #7A8CA0 次强调 */
#define C_ACCENT_DEEP 0x5B70   /* #5E6E80 发光外圈 / 暗底 */
/* 报警：全站唯一的红色语义（边框闪烁 / 卡片变红 / 数字变红），两端一致 */
#define C_ALERT       0xC40F   /* #C4827F 报警文字                      4.9:1 */
#define C_ALERT_DIM   0xAB6D   /* #A86E6B 报警闪烁的另一相位 */
#define C_ALERT_BG    0x4987   /* #4A3238 报警页底色 */
/* AAMI 五类心拍标记色：同一组柔和色相在深底上的版本，均 ≥4:1（图形需 ≥3） */
#define C_CLASS_N     0x9D93   /* #9AB09A 正常（灰绿） */
#define C_CLASS_S     0x8D36   /* #8FA6B5 室上性（钢蓝） */
#define C_CLASS_V     0xC40F   /* #C4827F 室性（砖红，与报警同族） */
#define C_CLASS_F     0xC550   /* #C0A882 融合（奶茶） */
#define C_CLASS_Q     0xA4D8   /* #A79BC0 起搏/未分类（灰紫） */

/* ─── 通用尺寸 ─── */
#define GRID_STEP     16       /* 背景网格间距 */
#define ROUND_R       12       /* 统一圆角半径（全站一致，别混用） */
#define LINE_H        20       /* 行距（16px 字高 + 4px） */
#define ROW_H         42       /* 列表行距（36px 按钮高 + 6px 间隙） */
#define MENU_TOP      48       /* 列表页内容起始 y */

/* 内容区按钮规格（模式选择页与设置页的返回键共用） */
#define MODE_BTN_X 16
#define MODE_BTN_W 208
#define MODE_BTN_H 72

/* ─── 基元 ─── */

/**
 * 背景：深蓝黑底 + 极暗网格线（进静态页时画一次）。
 * 竖线必须走 lcd_draw_vline（整列一次 blit）——若用 lcd_fill(x,y,1,h)，
 * 一条全高竖线要发 320 次 SPI 事务，十几条就能让进页明显卡顿。
 */
void ui_draw_bg(void);

/** 顶部标题：青蓝 scale 2 居中，下方一条更暗的青蓝装饰线（发光层次） */
void ui_title(const char* s);

/** 底部提示：次文灰 scale 1，水平居中 */
void ui_hint(const char* s);

/**
 * 按钮样式。刻意区分三档，避免「到处都是框」——框本身就是一种强调，
 * 全屏都描边等于没有重点。
 *   BTN_FLAT   仅色块，无描边（次级入口）
 *   BTN_BORDER 面板底 + 暗描边（普通可点项）
 *   BTN_ACCENT 面板底 + 亮青描边 + 外圈光晕（当前主推功能）
 */
typedef enum { BTN_FLAT, BTN_BORDER, BTN_ACCENT } btn_style_t;

void ui_button(int x, int y, int w, int h, const char* label, int scale, btn_style_t style);

/**
 * 列表行：扁平色块 + 1px 描边 + 左侧青蓝指示条。
 * 刻意不用圆角卡片——密集列表用圆角既费时（每行 4 个圆角 ≈ 70 次小传输）
 * 又显臃肿，扁平行 + 强调条更接近「仪表盘」观感，也更省帧。
 *
 * 带竖向裁剪（clip_y0/clip_y1）：列表滑动时首尾行只会露出一部分，
 * 只画落在可见带内的部分，绝不会糊到标题或返回按钮上。
 */
void ui_row(int x, int y, int w, int h, const char* label, int clip_y0, int clip_y1);

/** 数值面板：用于设置页的信息块（面板底 + 暗描边） */
void ui_panel(int x, int y, int w, int h);

/**
 * 跑马灯外框：暗青整框打底（保持区域轮廓），叠一段「中亮尾 + 高亮头」的流动亮条。
 * 亮度脉冲在静止的 1px 线上动势太弱，看不出「在跑」；改为亮条沿矩形周长流动，
 * 视觉上明确表示「此区域正在实时刷新」。
 * @param pos 沿周长的位置（调用方每帧推进）
 */
void ui_marquee_frame(int x, int y, int w, int h, int pos);

/** 命中判定：点 (px,py) 是否落在矩形内 */
static inline bool ui_hit(int px, int py, int x, int y, int w, int h) {
    return px >= x && px < x + w && py >= y && py < y + h;
}

#ifdef __cplusplus
}
#endif

#endif /* UI_WIDGETS_H */
