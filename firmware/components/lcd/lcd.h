/* lcd.h — ST7789 LCD 显示 + 电容触摸模块（ESP32-S3 核心板 + 2.8寸 ST7789V 电容触摸屏）
 *
 * 显示: 240x320 RGB565，SPI2_HOST，与 SD 卡共用 MOSI/SCLK
 * 引脚: SCLK=IO39, MOSI=IO38, MISO=IO40, DC=IO42, CS=IO45, BL=IO1
 * 触摸: I2C，SDA=IO48, SCL=IO47。驱动按 I2C 探测结果自动选择：
 *       FT6x06/FT6336U(0x38) 或 CST816S(0x15)——实测本板为后者。
 */
#ifndef LCD_H
#define LCD_H

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define LCD_H_RES 240
#define LCD_V_RES 320

/* RGB565 常用颜色 */
#define LCD_COLOR_BLACK  0x0000
#define LCD_COLOR_WHITE  0xFFFF
#define LCD_COLOR_RED    0xF800
#define LCD_COLOR_GREEN  0x07E0
#define LCD_COLOR_BLUE   0x001F
#define LCD_COLOR_YELLOW 0xFFE0

/**
 * @brief 初始化 LCD 并跑自检（SERVICE_INITCALL 自动调用）
 * @return 恒为 ESP_OK；失败仅告警，不中断 initcall
 */
esp_err_t lcd_init(void);

/** @brief 画一块 RGB565 位图 */
esp_err_t lcd_draw_bitmap(int x, int y, int w, int h, const uint16_t* color565);

/** @brief 填充纯色矩形 */
esp_err_t lcd_fill(int x, int y, int w, int h, uint16_t color565);

/** @brief 清屏为纯色 */
esp_err_t lcd_clear(uint16_t color565);

/** @brief 设置背光亮度 0~100（LEDC PWM） */
esp_err_t lcd_set_backlight(uint8_t percent);

/** @brief 画一个 8x8 字符（旧接口，仅 ASCII；新代码请用 lcd_draw_string） */
esp_err_t lcd_draw_char(int x, int y, char c, uint16_t color, uint16_t bg);

/**
 * @brief 画一串字符，支持 UTF-8 中英混排
 *
 * 字形统一 16x16、每字符步进恒为 16px（ASCII 与汉字同源同尺寸渲染），
 * 故排版可按「字符数 × 16 × scale」精确计算，单行最多 15 字符（scale=1）。
 * 字形来自按需子集字库 font16.h（tools/gen_cjk_font.py 生成）；
 * 未收录的汉字会画成实心占位块，便于一眼发现字库遗漏。
 */
esp_err_t lcd_draw_string(int x, int y, const char* str, uint16_t color, uint16_t bg);

/** @brief 放大绘制单个 ASCII 字符（scale 倍最近邻，1~3） */
esp_err_t lcd_draw_char_scale(int x, int y, char c, uint16_t color, uint16_t bg, int scale);

/** @brief 放大绘制字符串（UTF-8 中英混排，scale 1~3），报警大字用 */
esp_err_t lcd_draw_string_scale(int x, int y, const char* str, uint16_t color,
                                uint16_t bg, int scale);

/** @brief 字符串像素宽度（UTF-8 感知，含中英混排），用于居中/排版 */
int lcd_text_width(const char* str, int scale);

/** @brief 在 y 行水平居中绘制放大字符串 */
esp_err_t lcd_draw_string_scale_center(int y, const char* str, uint16_t color,
                                       uint16_t bg, int scale);

/**
 * @brief 带竖向裁剪的放大字符串：只绘制落在 [clip_y0, clip_y1) 内的部分
 *
 * 供「内容可在区域内上下滚动」的界面使用（如演示样本列表）：滚出去一半的行
 * 只该露出区域内的那一半，否则半个字会被画到标题或返回按钮上。
 */
esp_err_t lcd_draw_string_scale_clip(int x, int y, const char* str, uint16_t color,
                                     uint16_t bg, int scale, int clip_y0, int clip_y1);

/** @brief 画实心圆（圆心 cx,cy，半径 r） */
esp_err_t lcd_fill_circle(int cx, int cy, int r, uint16_t color565);

/** @brief 画竖线（整列一次 blit；不要用 lcd_fill(x,y,1,h) 代替，那样会按行发 h 次事务） */
esp_err_t lcd_draw_vline(int x, int y, int h, uint16_t color565);

/** @brief 1px 描边矩形 */
esp_err_t lcd_draw_rect(int x, int y, int w, int h, uint16_t color565);

/** @brief 描边圆角矩形（按钮/卡片的发光边框） */
esp_err_t lcd_draw_round_rect_outline(int x, int y, int w, int h, int r, uint16_t color565);

/** @brief RGB565 线性混色：t=0 得 b，t=255 得 a（呼吸灯中间帧用） */
uint16_t lcd_mix(uint16_t a, uint16_t b, uint8_t t);

/** @brief 画圆角矩形（x,y 左上角，w,h 宽高，r 圆角半径） */
esp_err_t lcd_fill_round_rect(int x, int y, int w, int h, int r, uint16_t color565);

/* ─── 触摸输入（FT6336U，I2C0） ─── */

/** @brief 初始化触摸控制器 */
esp_err_t lcd_touch_init(void);

/** @brief 读取触摸状态和坐标（未按过时 pressed=false） */
esp_err_t lcd_touch_read(bool* pressed, uint16_t* x, uint16_t* y);

/** @brief 触摸按下沿检测（按下瞬间返回一次 true） */
bool lcd_touch_pressed_edge(void);

/** @brief 自检：清屏三色 + 色块 + 文字 */
esp_err_t lcd_selftest(void);

#ifdef __cplusplus
}
#endif

#endif /* LCD_H */
