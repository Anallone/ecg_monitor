/**
 * touch.c — 触摸采样与点按判定
 */
#include "touch.h"

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lcd.h"

static bool     s_touch_last = false;
static uint32_t s_last_tap_tick = 0;   /* 上次按下沿的 tick 计数（tick 域，回绕安全比较） */
static touch_state_t s_touch;

bool touch_poll(uint16_t* x, uint16_t* y) {
    bool pressed = false;
    uint16_t tx = 0, ty = 0;
    if (lcd_touch_read(&pressed, &tx, &ty) != ESP_OK) return false;

    bool edge = pressed && !s_touch_last;
    bool tap = false;
    if (edge) {
        /* tick 域比较：×portTICK_PERIOD_MS 换算 ms 会在约 497 天后 uint32 溢出、
         * 去抖失效；有符号差值比较在回绕下依然正确（250ms 远小于 2^31 tick）。 */
        uint32_t now = xTaskGetTickCount();
        s_touch.x0 = tx;
        s_touch.y0 = ty;
        if ((int32_t)(now - s_last_tap_tick) > pdMS_TO_TICKS(250)) {
            tap = true;
            s_last_tap_tick = now;
            if (x) *x = tx;
            if (y) *y = ty;
        }
    }
    s_touch.press = edge;
    s_touch.down  = pressed;
    s_touch.up    = !pressed && s_touch_last;
    /* 坐标只在按下时刷新：抬手那一帧触摸驱动不写坐标（点数=0，读回的是 0,0），
     * 直接照抄会让「抬起时的位移」= 0 - y0 这个假值，点击/返回判定全部失效。
     * 抬手时保留按下期间最后一次有效位置，位移才算得对。 */
    if (pressed) {
        s_touch.x = tx;
        s_touch.y = ty;
    }
    s_touch_last = pressed;
    return tap;
}

const touch_state_t* touch_state(void) { return &s_touch; }
