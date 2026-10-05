/* lcd.c — ST7789 LCD 显示实现（ESP32-S3 核心板 + 2.8寸 ST7789V 电容触摸屏）
 *
 * 240x320 RGB565，SPI2_HOST 与 SD 卡共用 MOSI/SCLK
 * 引脚: SCLK=IO39, MOSI=IO38, MISO=IO40, DC=IO42, CS=IO45, BL=IO1
 */
#include "lcd.h"

#include <math.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_st7789.h"
#include "esp_lcd_touch_cst816s.h"
#include "esp_lcd_touch_ft5x06.h"
#include "font16.h"   /* 16x16 点阵字库：ASCII 全集 + 按需汉字（tools/gen_cjk_font.py 生成） */
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char* TAG = "LCD";

#define LCD_PIN_SCLK GPIO_NUM_39
#define LCD_PIN_MOSI GPIO_NUM_38
#define LCD_PIN_MISO GPIO_NUM_40 /* 与 SD 卡共用 */
#define LCD_PIN_DC   GPIO_NUM_42
#define LCD_PIN_CS   GPIO_NUM_45
#define LCD_PIN_RST  GPIO_NUM_5 /* ST7789 硬件复位（P028X101 屏 RST 必须受控复位，悬空不开机） */
#define LCD_PIN_BL   GPIO_NUM_1 /* 背光控制（P028X101: PWR→MOS→LEDK，LEDA 直连 VCC） */

/* 触摸 FT6336U 走 I2C0（2.8 寸屏 FocalTech 触摸，实测地址 0x38） */
#define TOUCH_I2C_NUM I2C_NUM_0
#define TOUCH_PIN_SDA GPIO_NUM_48
#define TOUCH_PIN_SCL GPIO_NUM_47

/* 背光 LEDC PWM */
#define LCD_BL_LEDC_MODE     LEDC_LOW_SPEED_MODE
#define LCD_BL_LEDC_TIMER    LEDC_TIMER_0
#define LCD_BL_LEDC_CHANNEL  LEDC_CHANNEL_0
#define LCD_BL_LEDC_DUTY_RES LEDC_TIMER_10_BIT
#define LCD_BL_LEDC_FREQ_HZ  10000
#define LCD_BL_LEDC_MAX_DUTY ((1 << 10) - 1) /* 1023 */

#define LCD_SPI_HOST SPI2_HOST
#define LCD_PCLK_HZ  (80 * 1000 * 1000)

static esp_lcd_panel_handle_t s_panel = NULL;

/* 颜色传输完成信号量：esp_lcd_panel_draw_bitmap 是异步队列传输（直接引用用户缓冲区，
 * 不拷贝），用信号量在每次绘制后同步等待完成，避免共享缓冲区被下一次绘制覆盖。 */
static SemaphoreHandle_t s_color_trans_done = NULL;

static bool lcd_on_color_trans_done(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_io_event_data_t* edata, void* user_ctx) {
  (void)panel_io;
  (void)edata;
  (void)user_ctx;
  if (s_color_trans_done) {
    /* 回调在 SPI 中断上下文触发，必须用 FromISR 版本 */
    BaseType_t higher_prio_task_woken = pdFALSE;
    xSemaphoreGiveFromISR(s_color_trans_done, &higher_prio_task_woken);
    portYIELD_FROM_ISR(higher_prio_task_woken);
  }
  return false;
}

/* ─── 8x8 点阵字体（标准 font8x8 子集：空格/数字/冒号/A-Z/x） ─── */
typedef struct {
  char    c;
  uint8_t bmp[8];
} glyph_t;

static const glyph_t font8x8[] = {
    {' ', {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}},
    {'0', {0x3C, 0x66, 0x6E, 0x76, 0x66, 0x66, 0x3C, 0x00}},
    {'1', {0x18, 0x38, 0x18, 0x18, 0x18, 0x18, 0x7E, 0x00}},
    {'2', {0x3C, 0x66, 0x06, 0x0C, 0x18, 0x30, 0x7E, 0x00}},
    {'3', {0x3C, 0x66, 0x06, 0x1C, 0x06, 0x66, 0x3C, 0x00}},
    {'4', {0x0C, 0x1C, 0x3C, 0x6C, 0x7E, 0x0C, 0x0C, 0x00}},
    {'5', {0x7E, 0x60, 0x7C, 0x06, 0x06, 0x66, 0x3C, 0x00}},
    {'6', {0x3C, 0x60, 0x7C, 0x66, 0x66, 0x66, 0x3C, 0x00}},
    {'7', {0x7E, 0x06, 0x0C, 0x18, 0x30, 0x30, 0x30, 0x00}},
    {'8', {0x3C, 0x66, 0x66, 0x3C, 0x66, 0x66, 0x3C, 0x00}},
    {'9', {0x3C, 0x66, 0x66, 0x3E, 0x06, 0x66, 0x3C, 0x00}},
    {':', {0x00, 0x00, 0x18, 0x18, 0x00, 0x18, 0x18, 0x00}},
    {'<', {0x08, 0x18, 0x30, 0x60, 0x30, 0x18, 0x08, 0x00}},
    {'>', {0x10, 0x18, 0x0C, 0x06, 0x0C, 0x18, 0x10, 0x00}},
    {'A', {0x18, 0x3C, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x00}},
    {'B', {0x7C, 0x66, 0x66, 0x7C, 0x66, 0x66, 0x7C, 0x00}},
    {'C', {0x3C, 0x66, 0x60, 0x60, 0x60, 0x66, 0x3C, 0x00}},
    {'D', {0x78, 0x6C, 0x66, 0x66, 0x66, 0x6C, 0x78, 0x00}},
    {'E', {0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x7E, 0x00}},
    {'F', {0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x60, 0x00}},
    {'G', {0x3C, 0x66, 0x60, 0x6E, 0x66, 0x66, 0x3C, 0x00}},
    {'H', {0x66, 0x66, 0x66, 0x7E, 0x66, 0x66, 0x66, 0x00}},
    {'I', {0x7E, 0x18, 0x18, 0x18, 0x18, 0x18, 0x7E, 0x00}},
    {'J', {0x1E, 0x0C, 0x0C, 0x0C, 0x0C, 0x6C, 0x38, 0x00}},
    {'K', {0x66, 0x6C, 0x78, 0x70, 0x78, 0x6C, 0x66, 0x00}},
    {'L', {0x60, 0x60, 0x60, 0x60, 0x60, 0x60, 0x7E, 0x00}},
    {'M', {0x63, 0x77, 0x7F, 0x6B, 0x63, 0x63, 0x63, 0x00}},
    {'N', {0x66, 0x76, 0x7E, 0x7E, 0x6E, 0x66, 0x66, 0x00}},
    {'O', {0x3C, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00}},
    {'P', {0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60, 0x60, 0x00}},
    {'Q', {0x3C, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x0E, 0x00}},
    {'R', {0x7C, 0x66, 0x66, 0x7C, 0x78, 0x6C, 0x66, 0x00}},
    {'S', {0x3C, 0x66, 0x60, 0x3C, 0x06, 0x66, 0x3C, 0x00}},
    {'T', {0x7E, 0x18, 0x18, 0x18, 0x18, 0x18, 0x18, 0x00}},
    {'U', {0x66, 0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x00}},
    {'V', {0x66, 0x66, 0x66, 0x66, 0x66, 0x3C, 0x18, 0x00}},
    {'W', {0x63, 0x63, 0x63, 0x6B, 0x7F, 0x77, 0x63, 0x00}},
    {'X', {0x66, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x66, 0x00}},
    {'Y', {0x66, 0x66, 0x66, 0x3C, 0x18, 0x18, 0x18, 0x00}},
    {'Z', {0x7E, 0x06, 0x0C, 0x18, 0x30, 0x60, 0x7E, 0x00}},
    {'x', {0x00, 0x00, 0x66, 0x3C, 0x18, 0x3C, 0x66, 0x00}},
};
static const size_t font8x8_len = sizeof(font8x8) / sizeof(font8x8[0]);

static const uint8_t* lcd_get_glyph(char c) {
  for (size_t i = 0; i < font8x8_len; i++) {
    if (font8x8[i].c == c) return font8x8[i].bmp;
  }
  return font8x8[0].bmp; /* 找不到用空格 */
}

esp_err_t lcd_draw_char(int x, int y, char c, uint16_t color, uint16_t bg) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  const uint8_t* bmp = lcd_get_glyph(c);
  /* 整字 8x8 位图一次传输（static 缓冲，避免逐行小传输被共享 SPI 总线干扰丢行） */
  static uint16_t glyph[8 * 8];
  for (int row = 0; row < 8; row++) {
    uint8_t bits = bmp[row];
    for (int col = 0; col < 8; col++) {
      glyph[row * 8 + col] = (bits & (0x80 >> col)) ? color : bg;
    }
  }
  return lcd_draw_bitmap(x, y, 8, 8, glyph);
}

/* ─── UTF-8 与中文字库 ───
 * 行高统一按汉字 16px：汉字画在 y..y+15，ASCII 8x8 垂直居中画在 y+4..y+11，
 * 使中英混排视觉对齐。 */

/* 解码一个 UTF-8 字符；返回消耗的字节数（1~4），*cp 填 Unicode 码点 */
static int utf8_decode(const char* s, uint32_t* cp) {
  const uint8_t* p = (const uint8_t*)s;
  if (p[0] < 0x80) { *cp = p[0]; return 1; }
  if ((p[0] & 0xE0) == 0xC0 && (p[1] & 0xC0) == 0x80) {
    *cp = ((uint32_t)(p[0] & 0x1F) << 6) | (p[1] & 0x3F);
    return 2;
  }
  if ((p[0] & 0xF0) == 0xE0 && (p[1] & 0xC0) == 0x80 && (p[2] & 0xC0) == 0x80) {
    *cp = ((uint32_t)(p[0] & 0x0F) << 12) | ((uint32_t)(p[1] & 0x3F) << 6) |
          (p[2] & 0x3F);
    return 3;
  }
  if ((p[0] & 0xF8) == 0xF0) { *cp = '?'; return 4; }
  *cp = '?';
  return 1;
}

/* 查字库（按码点二分）。找到返回点阵指针，否则 NULL */
static const uint8_t* font16_lookup(uint32_t cp) {
  int lo = 0, hi = FONT16_COUNT - 1;
  while (lo <= hi) {
    int mid = (lo + hi) / 2;
    uint16_t c = g_font16[mid].code;
    if (c == cp) return g_font16[mid].bmp;
    if (c < cp) lo = mid + 1; else hi = mid - 1;
  }
  return NULL;
}

/**
 * 画一个 16x16 字形（ASCII 与汉字同源同尺寸，可放大）。
 * 未收录时画实心占位块——便于一眼发现字库遗漏（如新增了未生成的汉字）。
 */
/* 放大字形用的临时缓冲（64x64 上限）。普通绘制与带裁剪的绘制共用一份，
 * 免得 8KB 缓冲在固件里出现两处。非重入：本工程只有主循环在画屏。 */
static uint16_t s_glyph_buf[16 * 4 * 16 * 4];

/**
 * 把 16x16 字形按 scale 渲染进 buf，返回 0 成功；-1 表示字库缺字（由调用方画占位块）。
 * 拆出来是为了让「带裁剪的绘制」复用同一份点阵生成逻辑。
 */
static int glyph16_render(uint32_t cp, uint16_t color, uint16_t bg, int scale, uint16_t* buf,
                          int* out_w, int* out_h) {
  const uint8_t* bmp = font16_lookup(cp);
  if (bmp == NULL) return -1;
  const int w = FONT16_W * scale;
  for (int row = 0; row < FONT16_H; row++) {
    uint8_t hi = bmp[row * 2];
    uint8_t lo = bmp[row * 2 + 1];
    for (int col = 0; col < FONT16_W; col++) {
      uint8_t bit = (col < 8) ? (hi & (0x80 >> col)) : (lo & (0x80 >> (col - 8)));
      uint16_t px = bit ? color : bg;
      for (int dy = 0; dy < scale; dy++) {
        int base = (row * scale + dy) * w + col * scale;
        for (int dx = 0; dx < scale; dx++) {
          buf[base + dx] = px;
        }
      }
    }
  }
  *out_w = w;
  *out_h = FONT16_H * scale;
  return 0;
}

static esp_err_t draw_glyph16(int x, int y, uint32_t cp, uint16_t color, uint16_t bg,
                              int scale) {
  if (scale < 1) scale = 1;
  if (scale > 4) scale = 4;
  int w = 0, h = 0;
  if (glyph16_render(cp, color, bg, scale, s_glyph_buf, &w, &h) != 0) {
    return lcd_fill(x, y, FONT16_W * scale, FONT16_H * scale, color);   /* 缺字占位 */
  }
  return lcd_draw_bitmap(x, y, w, h, s_glyph_buf);
}

/**
 * 同 draw_glyph16，但只画落在 [clip_y0, clip_y1) 竖带内的扫描线。
 * 滚动列表的首尾行只会露出一部分，不裁剪就会把半个字画到标题或返回按钮上。
 */
static esp_err_t draw_glyph16_clip(int x, int y, uint32_t cp, uint16_t color, uint16_t bg,
                                   int scale, int clip_y0, int clip_y1) {
  if (scale < 1) scale = 1;
  if (scale > 4) scale = 4;
  int w = FONT16_W * scale, h = FONT16_H * scale;
  int y0 = (y > clip_y0) ? y : clip_y0;
  int y1 = (y + h < clip_y1) ? (y + h) : clip_y1;
  if (y1 <= y0) return ESP_OK;                       /* 整字都在带外 */
  if (glyph16_render(cp, color, bg, scale, s_glyph_buf, &w, &h) != 0) {
    return lcd_fill(x, y0, w, y1 - y0, color);       /* 缺字占位（同样裁剪） */
  }
  /* 只 blit 相交的那几条扫描线：指针跳过带外的前 (y0-y) 行 */
  return lcd_draw_bitmap(x, y0, w, y1 - y0, s_glyph_buf + (size_t)(y0 - y) * w);
}

/* 字符串的像素宽度：每字符步进恒为 FONT16_W * scale（中英等高同宽） */
int lcd_text_width(const char* str, int scale) {
  if (str == NULL) return 0;
  if (scale < 1) scale = 1;
  int n = 0;
  while (*str) {
    uint32_t cp;
    str += utf8_decode(str, &cp);
    n++;
  }
  return n * FONT16_W * scale;
}

esp_err_t lcd_draw_string(int x, int y, const char* str, uint16_t color, uint16_t bg) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (str == NULL) return ESP_ERR_INVALID_ARG;
  while (*str) {
    uint32_t cp;
    int len = utf8_decode(str, &cp);
    draw_glyph16(x, y, cp, color, bg, 1);
    x += FONT16_W;
    str += len;
  }
  return ESP_OK;
}

/* 放大绘制单个 ASCII 字符（8x8 最近邻放大） */
esp_err_t lcd_draw_char_scale(int x, int y, char c, uint16_t color, uint16_t bg, int scale) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (scale < 1) scale = 1;
  if (scale > 3) scale = 3;                 /* 大字上限 3x，保证缓冲不溢出 */
  const uint8_t* bmp = lcd_get_glyph(c);
  const int w = 8 * scale, h = 8 * scale;
  static uint16_t buf[8 * 3 * 8 * 3];       /* 24x24 上限 */
  for (int row = 0; row < 8; row++) {
    uint8_t bits = bmp[row];
    for (int col = 0; col < 8; col++) {
      uint16_t px = (bits & (0x80 >> col)) ? color : bg;
      for (int dy = 0; dy < scale; dy++) {
        int base = (row * scale + dy) * w + col * scale;
        for (int dx = 0; dx < scale; dx++) {
          buf[base + dx] = px;
        }
      }
    }
  }
  return lcd_draw_bitmap(x, y, w, h, buf);
}

esp_err_t lcd_draw_string_scale(int x, int y, const char* str, uint16_t color,
                                uint16_t bg, int scale) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (str == NULL) return ESP_ERR_INVALID_ARG;
  if (scale < 1) scale = 1;
  /* 越界告警：超宽会被 lcd_draw_bitmap 裁剪（不会花屏），但字会被切掉，
   * 说明该文案需要压词或降 scale。尽早从串口发现，别等看到屏幕才知道。 */
  int total = lcd_text_width(str, scale);
  if (x + total > LCD_H_RES) {
    ESP_LOGW(TAG, "text overflow: x=%d w=%d > %d, clipped: \"%s\" (scale %d)",
             x, total, LCD_H_RES, str, scale);
  }
  while (*str) {
    uint32_t cp;
    int len = utf8_decode(str, &cp);
    draw_glyph16(x, y, cp, color, bg, scale);
    x += FONT16_W * scale;
    str += len;
  }
  return ESP_OK;
}

/* 水平居中绘制（报警大字用） */
esp_err_t lcd_draw_string_scale_center(int y, const char* str, uint16_t color,
                                       uint16_t bg, int scale) {
  int w = lcd_text_width(str, scale);
  int x = (LCD_H_RES - w) / 2;
  if (x < 0) x = 0;
  return lcd_draw_string_scale(x, y, str, color, bg, scale);
}

/* 带竖向裁剪的放大字符串：只绘制落在 [clip_y0, clip_y1) 内的部分。
 * 滚动列表用——被滚出去一半的行只该露出带内的那一半。 */
esp_err_t lcd_draw_string_scale_clip(int x, int y, const char* str, uint16_t color,
                                     uint16_t bg, int scale, int clip_y0, int clip_y1) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (str == NULL) return ESP_ERR_INVALID_ARG;
  if (scale < 1) scale = 1;
  while (*str) {
    uint32_t cp;
    int len = utf8_decode(str, &cp);
    draw_glyph16_clip(x, y, cp, color, bg, scale, clip_y0, clip_y1);
    x += FONT16_W * scale;
    str += len;
  }
  return ESP_OK;
}

/* ─── 公开 API ─── */

/**
 * 画位图。**必须先把区域裁剪到屏幕内**：
 * ST7789 的列/行地址范围一旦超过面板尺寸（240x320），像素会**回绕到下一行**，
 * 表现为画面错位、文字重叠花屏。这类越界以前没有被拦截——中文文案宽度合适
 * 所以没暴露，换成英文后（如 "ECG MONITOR" 在 scale3 下宽 528px）立刻触发。
 * 这里做通用裁剪，任何调用方越界都只会被截断，不会污染整屏。
 */
esp_err_t lcd_draw_bitmap(int x, int y, int w, int h, const uint16_t* color565) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (color565 == NULL || w <= 0 || h <= 0) return ESP_ERR_INVALID_ARG;

  const int stride = w;                 /* 裁剪前要先记住行跨距 */
  if (x < 0) { color565 += -x; w += x; x = 0; }
  if (y < 0) { color565 += (size_t)(-y) * stride; h += y; y = 0; }
  if (x + w > LCD_H_RES) w = LCD_H_RES - x;
  if (y + h > LCD_V_RES) h = LCD_V_RES - y;
  if (w <= 0 || h <= 0) return ESP_OK;  /* 完全在屏外：静默丢弃 */

  esp_err_t ret = esp_lcd_panel_draw_bitmap(s_panel, x, y, x + w, y + h, color565);
  /* 等待异步队列传输完成，保证调用方缓冲区在传输期间有效 */
  if (ret == ESP_OK && s_color_trans_done) {
    xSemaphoreTake(s_color_trans_done, portMAX_DELAY);
  }
  return ret;
}

/* 一次性发送的行数。原实现每行一次 blit（一次 SPI 事务），清一整屏 240x320
 * 要发 320 次事务；整片同色时按 N 行一组发送，事务数直接除以 N。
 * 缓冲区是纯色，任意 w<=LCD_H_RES 都能用（驱动按 w*h 连续读，等价于同一颜色）。 */
#define LCD_FILL_CHUNK_ROWS 16

esp_err_t lcd_fill(int x, int y, int w, int h, uint16_t color565) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (w <= 0 || h <= 0) return ESP_ERR_INVALID_ARG;

  static uint16_t chunk[LCD_H_RES * LCD_FILL_CHUNK_ROWS];
  static uint16_t chunk_color = 0;
  static bool     chunk_valid = false;
  if (!chunk_valid || chunk_color != color565) {
    for (size_t i = 0; i < sizeof(chunk) / sizeof(chunk[0]); i++) {
      chunk[i] = color565;
    }
    chunk_color = color565;
    chunk_valid = true;
  }
  for (int row = 0; row < h; row += LCD_FILL_CHUNK_ROWS) {
    int n = h - row;
    if (n > LCD_FILL_CHUNK_ROWS) n = LCD_FILL_CHUNK_ROWS;
    esp_err_t ret = lcd_draw_bitmap(x, y + row, w, n, chunk);
    if (ret != ESP_OK) return ret;
  }
  return ESP_OK;
}

/**
 * 画一条竖线。**必须用整列一次 blit**，不能靠 lcd_fill(x,y,1,h)：
 * 后者内部按行循环，1×320 的线要发 320 次 SPI 事务（约 30ms），
 * 十几条网格线就会让进页卡顿。这里一列一个缓冲、一次传完。
 */
esp_err_t lcd_draw_vline(int x, int y, int h, uint16_t color565) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (h <= 0) return ESP_ERR_INVALID_ARG;
  if (x < 0 || x >= LCD_H_RES) return ESP_OK;

  static uint16_t col[LCD_V_RES];
  if (h > LCD_V_RES) h = LCD_V_RES;
  for (int i = 0; i < h; i++) col[i] = color565;
  return lcd_draw_bitmap(x, y, 1, h, col);
}

/** 1px 描边矩形（四条边各一次 blit，共 4 次） */
esp_err_t lcd_draw_rect(int x, int y, int w, int h, uint16_t color565) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (w <= 0 || h <= 0) return ESP_ERR_INVALID_ARG;
  lcd_fill(x, y, w, 1, color565);                 /* 上 */
  lcd_fill(x, y + h - 1, w, 1, color565);         /* 下 */
  lcd_draw_vline(x, y, h, color565);              /* 左 */
  lcd_draw_vline(x + w - 1, y, h, color565);      /* 右 */
  return ESP_OK;
}

/**
 * 描边圆角矩形。用于按钮/卡片的「发光边框」——比实心描边更轻，能勾出轮廓。
 * 直边用高效原语；四个圆角逐像素（仅 4*(r+1) 个点，r=8 时 36 次，可忽略）。
 */
esp_err_t lcd_draw_round_rect_outline(int x, int y, int w, int h, int r, uint16_t color565) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (w <= 0 || h <= 0) return ESP_ERR_INVALID_ARG;
  if (r < 0) r = 0;
  if (r > w / 2) r = w / 2;
  if (r > h / 2) r = h / 2;

  /* 四条直边（避开圆角区间） */
  lcd_fill(x + r, y, w - 2 * r, 1, color565);              /* 上 */
  lcd_fill(x + r, y + h - 1, w - 2 * r, 1, color565);      /* 下 */
  lcd_draw_vline(x, y + r, h - 2 * r, color565);           /* 左 */
  lcd_draw_vline(x + w - 1, y + r, h - 2 * r, color565);   /* 右 */

  /* 四个圆角。
   * 圆弧是「以 (x+r, y+r) 为圆心、半径 r」的圆在第一象限的部分，
   * 故对行偏移 dy（0..r）：水平偏移 dx = r - sqrt(r² - (r-dy)²)。
   * 端点校验：dy=0 -> dx=r（接上边起点 x+r）；dy=r -> dx=0（接左边起点 y+r）。
   *
   * 两个必须这么写的理由（都踩过）：
   *  1) 曾误写成 sqrt(r² - dy²)，圆弧会从「外角 (x,y)」连到「内角 (x+r,y+r)」，
   *     方向相反 -> 四角交叉错位。
   *  2) 画弧必须逐行画**水平小段**（从上一行的 dx 到本行的 dx），不能只点单像素。
   *     r=12 时相邻两行 dx 落差可达 5px，单像素画出来是断的阶梯，
   *     观感就是「圆角没接上两侧直线」。
   */
  int prev = r;                                   /* dy=0 的 dx，正好等于 r */
  for (int dy = 0; dy <= r; dy++) {
    int k = r - dy;                               /* 距圆心的行距 */
    int dx = r - (int)(sqrtf((float)(r * r - k * k)) + 0.5f);
    int lo = (dx < prev) ? dx : prev;
    int hi = (dx > prev) ? dx : prev;
    int runw = hi - lo + 1;
    int yt = y + dy, yb = y + h - 1 - dy;
    lcd_fill(x + lo, yt, runw, 1, color565);              /* 左上 */
    lcd_fill(x + w - 1 - hi, yt, runw, 1, color565);      /* 右上 */
    lcd_fill(x + lo, yb, runw, 1, color565);              /* 左下 */
    lcd_fill(x + w - 1 - hi, yb, runw, 1, color565);      /* 右下 */
    prev = dx;
  }
  return ESP_OK;
}

/**
 * RGB565 线性混色：t=0 返回 b，t=255 返回 a。
 * 用于呼吸灯的中间帧（在暗青与亮青之间插值），省去为每个亮度存一张色表。
 */
uint16_t lcd_mix(uint16_t a, uint16_t b, uint8_t t) {
  int ar = (a >> 11) & 0x1F, ag = (a >> 5) & 0x3F, ab = a & 0x1F;
  int br = (b >> 11) & 0x1F, bg = (b >> 5) & 0x3F, bb = b & 0x1F;
  int r = br + ((ar - br) * t) / 255;
  int g = bg + ((ag - bg) * t) / 255;
  int bl = bb + ((ab - bb) * t) / 255;
  return (uint16_t)((r << 11) | (g << 5) | bl);
}

esp_err_t lcd_clear(uint16_t color565) {
  return lcd_fill(0, 0, LCD_H_RES, LCD_V_RES, color565);
}

esp_err_t lcd_fill_circle(int cx, int cy, int r, uint16_t color565) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (r < 0) return ESP_ERR_INVALID_ARG;

  for (int dy = -r; dy <= r; dy++) {
    int half = (int)(sqrtf((float)(r * r - dy * dy)) + 0.5f);
    esp_err_t ret = lcd_fill(cx - half, cy + dy, 2 * half + 1, 1, color565);
    if (ret != ESP_OK) return ret;
  }
  return ESP_OK;
}

esp_err_t lcd_fill_round_rect(int x, int y, int w, int h, int r, uint16_t color565) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;
  if (w <= 0 || h <= 0) return ESP_ERR_INVALID_ARG;
  if (r < 0) r = 0;
  if (r > w / 2) r = w / 2;
  if (r > h / 2) r = h / 2;

  /* 中间十字矩形 */
  lcd_fill(x + r, y, w - 2 * r, h, color565);
  lcd_fill(x, y + r, w, h - 2 * r, color565);
  /* 四个圆角 */
  if (r > 0) {
    lcd_fill_circle(x + r, y + r, r, color565);
    lcd_fill_circle(x + w - r - 1, y + r, r, color565);
    lcd_fill_circle(x + r, y + h - r - 1, r, color565);
    lcd_fill_circle(x + w - r - 1, y + h - r - 1, r, color565);
  }
  return ESP_OK;
}

static void backlight_init(void) {
  ledc_timer_config_t timer = {
      .speed_mode = LCD_BL_LEDC_MODE,
      .timer_num = LCD_BL_LEDC_TIMER,
      .duty_resolution = LCD_BL_LEDC_DUTY_RES,
      .freq_hz = LCD_BL_LEDC_FREQ_HZ,
      .clk_cfg = LEDC_AUTO_CLK,
  };
  ledc_timer_config(&timer);

  ledc_channel_config_t ch = {
      .speed_mode = LCD_BL_LEDC_MODE,
      .channel = LCD_BL_LEDC_CHANNEL,
      .timer_sel = LCD_BL_LEDC_TIMER,
      .intr_type = LEDC_INTR_DISABLE,
      .gpio_num = LCD_PIN_BL,
      .duty = 0,
      .hpoint = 0,
  };
  ledc_channel_config(&ch);
}

esp_err_t lcd_set_backlight(uint8_t percent) {
  if (percent > 100) percent = 100;
  uint32_t duty = (uint32_t)percent * LCD_BL_LEDC_MAX_DUTY / 100;
  ledc_set_duty(LCD_BL_LEDC_MODE, LCD_BL_LEDC_CHANNEL, duty);
  ledc_update_duty(LCD_BL_LEDC_MODE, LCD_BL_LEDC_CHANNEL);
  return ESP_OK;
}

/* ─── 触摸输入（FocalTech FT6336U，I2C 0x38，esp_lcd_touch ft5x06 组件） ─── */

static i2c_master_bus_handle_t s_i2c_bus = NULL;
static esp_lcd_touch_handle_t s_touch = NULL;
static bool s_touch_bus_probed = false;

/* ─── 触摸芯片 I2C 探测（识别屏上触摸 IC 型号） ─── */
static bool s_probe_ft = false;  /* I2C 0x38（FT6x06 系）有应答 */
static bool s_probe_cst = false; /* I2C 0x15（CST816S）有应答 */

static const char* lcd_touch_addr_name(uint8_t addr) {
  switch (addr) {
    case 0x15: return "CST816S / CST 系列";
    case 0x38: return "FT6236 / FT6x06 系列";
    case 0x5D: return "GT911 (ADDR 拉高)";
    case 0x14: return "GT911 (ADDR 拉低)";
    default:   return "未知，需对照触摸 IC 手册";
  }
}

/* 全地址扫描 I2C 总线，打印所有应答地址（含常见触摸芯片名）。无应答是排查接线/供电的关键。 */
static void lcd_touch_probe(void) {
  ESP_LOGI(TAG, "---- I2C touch probe: SDA=IO%d SCL=IO%d ----", TOUCH_PIN_SDA, TOUCH_PIN_SCL);
  s_probe_ft = false;
  s_probe_cst = false;
  int hit = 0;
  for (uint16_t addr = 0x08; addr <= 0x77; addr++) {
    if (i2c_master_probe(s_i2c_bus, addr, 20) != ESP_OK) {
      continue;
    }
    ESP_LOGI(TAG, "  0x%02X <-- ACK : %s", addr, lcd_touch_addr_name((uint8_t)addr));
    if (addr == 0x38) s_probe_ft = true;
    if (addr == 0x15) s_probe_cst = true;
    hit++;
  }
  if (hit == 0) {
    ESP_LOGW(TAG, "I2C 探测无任何应答：检查触摸 VCC=3V3、SDA=IO%d/SCL=IO%d 是否接对、触摸 RST/INT 是否悬空、模块是否上电",
             TOUCH_PIN_SDA, TOUCH_PIN_SCL);
  } else {
    ESP_LOGI(TAG, "探测到 %d 个 I2C 应答地址，据此接入对应触摸驱动", hit);
  }
}

esp_err_t lcd_touch_init(void) {
  /* 1. I2C 总线初始化（新 i2c_master 驱动，与 esp_lcd_touch 组件一致） */
  i2c_master_bus_config_t bus_config = {
      .i2c_port = TOUCH_I2C_NUM,
      .sda_io_num = TOUCH_PIN_SDA,
      .scl_io_num = TOUCH_PIN_SCL,
      .clk_source = I2C_CLK_SRC_DEFAULT,
      .glitch_ignore_cnt = 7,
      .flags.enable_internal_pullup = true,
  };
  esp_err_t ret = i2c_new_master_bus(&bus_config, &s_i2c_bus);
  if (ret != ESP_OK) return ret;

  /* I2C 总线就绪后立即扫描各触摸芯片地址（只在首次成功建总线时执行一次） */
  if (!s_touch_bus_probed) {
    s_touch_bus_probed = true;
    lcd_touch_probe();
  }

  /* 2. 按 I2C 探测结果选择触摸驱动：FT6x06(0x38) 优先，否则 CST816S(0x15)。
   *    两者都未命中时直接跳过，不再盲目 init 刷一堆 I2C 错误日志。 */
  /* x_max/y_max 应为面板宽/高：本屏竖屏 240x320（swap_xy=false）。
   * 注意：CST816S 驱动直接返回控制器原始坐标、不使用这两个值；此处主要对
   * FT6x06 路径生效（该路径当前无硬件可测）。 */
  esp_lcd_touch_config_t tp_cfg = {
      .x_max = LCD_H_RES,
      .y_max = LCD_V_RES,
      .rst_gpio_num = -1,
      .int_gpio_num = -1,
      .flags = {
          .swap_xy = 0,
          .mirror_x = 0,
          .mirror_y = 0,
      },
  };
  esp_lcd_panel_io_handle_t io_handle = NULL;

  if (s_probe_ft) {
    esp_lcd_panel_io_i2c_config_t io_config = ESP_LCD_TOUCH_IO_I2C_FT5x06_CONFIG();
    ret = esp_lcd_new_panel_io_i2c(s_i2c_bus, &io_config, &io_handle);
    if (ret != ESP_OK) return ret;
    ret = esp_lcd_touch_new_i2c_ft5x06(io_handle, &tp_cfg, &s_touch);
    if (ret != ESP_OK) return ret;
    ESP_LOGI(TAG, "touch ready: FT6x06/FT6336U (I2C 0x38)");
    return ESP_OK;
  }

  /* 本板实测触摸 IC 为 CST816S（0x15）。CST816S 深度休眠时不响应 I2C probe，
   * 0x15 会在上电一段时间后从总线扫描里“消失”；因此 probe 未命中也不能据此放弃，
   * 仍按 CST816S 初始化。读 ID 已通过 Kconfig 关闭，init 不会因芯片休眠失败；
   * 后续 lcd_touch_read 轮询会在首次触摸时把芯片唤醒。 */
  {
    esp_lcd_panel_io_i2c_config_t io_config = ESP_LCD_TOUCH_IO_I2C_CST816S_CONFIG();
    ret = esp_lcd_new_panel_io_i2c(s_i2c_bus, &io_config, &io_handle);
    if (ret != ESP_OK) return ret;
    ret = esp_lcd_touch_new_i2c_cst816s(io_handle, &tp_cfg, &s_touch);
    if (ret != ESP_OK) return ret;
    ESP_LOGI(TAG, "touch ready: CST816S (I2C 0x15)%s",
             s_probe_cst ? "" : " [probe miss, poll-wake fallback]");
    return ESP_OK;
  }
}

/* FT6336U 无触摸一段时间后会进入低功耗，过快/过频轮询易触发偶发 I2C NACK。
 * 这里做两层防护：轮询限速 + 读失败退避，并把失败静默降级为"无触摸"，
 * 避免驱动组件内部反复打印 "I2C read error"。 */
#define TOUCH_POLL_MIN_INTERVAL_MS 30  /* 轮询间隔下限（≤ ~33Hz） */
#define TOUCH_ERROR_BACKOFF_MS     150 /* 读失败后的退避时长，期间不再碰 I2C */

static TickType_t s_touch_last_poll = 0;
static TickType_t s_touch_backoff_until = 0;

/* 上次真实读到的状态。限速/退避时沿用，而不是硬编码返回"无触摸"——
 * 否则调用方按 20ms 轮询、本函数 30ms 限速，会周期性返回 false，
 * 把"一直按住"误判成连续"松开→按下"，按下沿检测被反复触发。 */
static bool     s_last_pressed = false;
static uint16_t s_last_x = 0;
static uint16_t s_last_y = 0;

esp_err_t lcd_touch_read(bool* pressed, uint16_t* x, uint16_t* y) {
  if (s_touch == NULL) return ESP_ERR_INVALID_STATE;

  TickType_t now = xTaskGetTickCount();

  /* 未到下次采样时机（限速中或退避中）：沿用上次状态，不发起 I2C 读。
   * 有符号差值比较：tick 回绕（100Hz 下约 497 天）下裸比较会把「退避中」
   * 误判成「退避已到期」或反向锁死触摸，差值比较则回绕安全。 */
  if ((int32_t)(now - s_touch_backoff_until) < 0 ||
      (int32_t)(now - s_touch_last_poll) < pdMS_TO_TICKS(TOUCH_POLL_MIN_INTERVAL_MS)) {
    if (pressed) *pressed = s_last_pressed;
    if (x) *x = s_last_x;
    if (y) *y = s_last_y;
    return ESP_OK;
  }
  s_touch_last_poll = now;

  uint16_t tx[1] = {0};
  uint16_t ty[1] = {0};
  uint8_t  cnt = 0;

  esp_err_t ret = esp_lcd_touch_read_data(s_touch);
  if (ret != ESP_OK) {
    /* 读失败：进入退避（休眠唤醒瞬间偶发失败属正常，不中断 UI），沿用上次状态 */
    s_touch_backoff_until = now + pdMS_TO_TICKS(TOUCH_ERROR_BACKOFF_MS);
    if (pressed) *pressed = s_last_pressed;
    if (x) *x = s_last_x;
    if (y) *y = s_last_y;
    return ESP_OK;
  }

  bool p = esp_lcd_touch_get_coordinates(s_touch, tx, ty, NULL, &cnt, 1);
  s_last_pressed = p && (cnt > 0);
  /* 坐标只在**按下**时更新：抬手那一帧点数=0，驱动不会写 x/y（这里是零初始化的
   * 局部数组，值恒为 0,0），若照抄回去，调用方会拿到 (0,0) 这个假坐标——
   * 「抬手时的位移」会被算成 -y0，点击/长按判定全部失效。保留最后一次有效位置即可。 */
  if (s_last_pressed) {
    s_last_x = tx[0];
    s_last_y = ty[0];
  }

  if (pressed) *pressed = s_last_pressed;
  if (x) *x = s_last_x;
  if (y) *y = s_last_y;
  return ESP_OK;
}

bool lcd_touch_pressed_edge(void) {
  static bool s_last = false;
  bool        pressed = false;
  uint16_t    x = 0, y = 0;
  if (lcd_touch_read(&pressed, &x, &y) != ESP_OK) {
    return false;
  }
  bool edge = pressed && !s_last;
  s_last = pressed;
  return edge;
}

esp_err_t lcd_selftest(void) {
  if (s_panel == NULL) return ESP_ERR_INVALID_STATE;

  /* 依次全屏红/绿/蓝各 1.5s，便于肉眼核对三色 */
  ESP_LOGI(TAG, "selftest: 全屏红 R(0xF800)");
  lcd_clear(LCD_COLOR_RED);
  vTaskDelay(pdMS_TO_TICKS(1500));
  ESP_LOGI(TAG, "selftest: 全屏绿 G(0x07E0)");
  lcd_clear(LCD_COLOR_GREEN);
  vTaskDelay(pdMS_TO_TICKS(1500));
  ESP_LOGI(TAG, "selftest: 全屏蓝 B(0x001F)");
  lcd_clear(LCD_COLOR_BLUE);
  vTaskDelay(pdMS_TO_TICKS(1500));

  /* 停在一屏文字，确认最终显示正常 */
  lcd_clear(LCD_COLOR_BLACK);
  lcd_draw_string(24, 80, "LCD OK", LCD_COLOR_WHITE, LCD_COLOR_BLACK);
  lcd_draw_string(24, 120, "240x320", LCD_COLOR_GREEN, LCD_COLOR_BLACK);
  lcd_draw_string(24, 160, "ST7789", LCD_COLOR_YELLOW, LCD_COLOR_BLACK);

  ESP_LOGI(TAG, "selftest PASS");
  return ESP_OK;
}

esp_err_t lcd_init(void) {
  if (s_panel != NULL) {
    ESP_LOGW(TAG, "LCD already initialized");
    return ESP_OK;
  }

  /* 0. 创建颜色传输完成信号量（lcd_draw_bitmap 用于同步等待异步队列传输） */
  if (s_color_trans_done == NULL) {
    s_color_trans_done = xSemaphoreCreateBinary();
  }

  /* 1. 背光 PWM 初始化并点亮 */
  backlight_init();
  lcd_set_backlight(100);

  /* 2. SPI 总线（与 SD 卡共用 MOSI/SCLK，可能已初始化） */
  spi_bus_config_t bus_cfg = {
      .mosi_io_num = LCD_PIN_MOSI,
      .miso_io_num = LCD_PIN_MISO,
      .sclk_io_num = LCD_PIN_SCLK,
      .quadwp_io_num = -1,
      .quadhd_io_num = -1,
      .max_transfer_sz = 4000,
  };
  esp_err_t ret = spi_bus_initialize(LCD_SPI_HOST, &bus_cfg, SPI_DMA_CH_AUTO);
  if (ret == ESP_ERR_INVALID_STATE) {
    ESP_LOGI(TAG, "SPI bus already initialized (shared with SD), reuse");
  } else if (ret != ESP_OK) {
    ESP_LOGE(TAG, "SPI bus init failed: %s", esp_err_to_name(ret));
    return ESP_OK; /* initcall 不允许非零返回 */
  }

  /* 3. 面板 IO（SPI） */
  esp_lcd_panel_io_handle_t io_handle = NULL;
  esp_lcd_panel_io_spi_config_t io_config = {
      .dc_gpio_num = LCD_PIN_DC,
      .cs_gpio_num = LCD_PIN_CS,
      .pclk_hz = LCD_PCLK_HZ,
      .lcd_cmd_bits = 8,
      .lcd_param_bits = 8,
      .spi_mode = 0,
      .trans_queue_depth = 10,
      .on_color_trans_done = lcd_on_color_trans_done,
  };
  ret = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_SPI_HOST, &io_config, &io_handle);
  if (ret != ESP_OK) {
    ESP_LOGE(TAG, "panel IO spi failed: %s", esp_err_to_name(ret));
    return ESP_OK;
  }

  /* 4. ST7789 面板驱动（reset_gpio_num 非 -1 → 走硬件复位时序，屏 RST 必须接 GPIO5）
   * rgb_ele_order: LCD_RGB_ELEMENT_ORDER_RGB（MADCTL BGR=0），与微雪官方 Demo
   * ESP32-S3-Touch-LCD-2 一致。
   * data_endian: LITTLE——ST7789 RAMCTRL 默认 big endian，会把每个颜色字两字节
   * 颠倒（红 0xF800→0x00F8 变蓝）。官方 Demo 是靠 LVGL 的 CONFIG_LV_COLOR_16_SWAP=y
   * 做同样的事；本项目不用 LVGL，故在面板层设为小端，效果等价。 */
  esp_lcd_panel_dev_config_t panel_config = {
      .reset_gpio_num = LCD_PIN_RST,
      .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
      .data_endian = LCD_RGB_DATA_ENDIAN_LITTLE,
      .bits_per_pixel = 16,
  };
  ret = esp_lcd_new_panel_st7789(io_handle, &panel_config, &s_panel);
  if (ret != ESP_OK) {
    ESP_LOGE(TAG, "ST7789 panel failed: %s", esp_err_to_name(ret));
    s_panel = NULL;
    return ESP_OK;
  }

  /* 5. 初始化面板
   * invert_color(true)：本板 IPS 屏需要 INVON，否则颜色整体反转（黑↔白、红↔青）。
   * 依据微雪官方 Demo ESP32-S3-Touch-LCD-2（06_lvgl_example）的初始化序列——
   * 它显式调用 esp_lcd_panel_invert_color(panel, true)；而 IDF 的
   * panel_st7789_init() 只发 SLPOUT/MADCTL/COLMOD/RAMCTRL，从不发 INVON，
   * 反色位会保持面板上电默认值，因此必须显式发。 */
  esp_lcd_panel_reset(s_panel);
  esp_lcd_panel_init(s_panel);
  esp_lcd_panel_mirror(s_panel, false, false);
  esp_lcd_panel_swap_xy(s_panel, false);
  esp_lcd_panel_disp_on_off(s_panel, true);
  esp_lcd_panel_invert_color(s_panel, true);

  ESP_LOGI(TAG, "LCD init OK (%dx%d)", LCD_H_RES, LCD_V_RES);

  /* 6. 触摸初始化（失败仅告警，不影响显示） */
  ret = lcd_touch_init();
  if (ret != ESP_OK) {
    ESP_LOGW(TAG, "touch init failed: %s", esp_err_to_name(ret));
  }

  /* 7. 自检（默认关闭，避免干扰 ECG 演示；可在 lcd_init 前显式调用 lcd_selftest） */
#if LCD_AUTO_SELFTEST
  lcd_selftest();
#endif

  return ESP_OK;
}
