/**
 * main.c — ECG 心律失常监测系统端侧固件（微雪 ESP32-S3 2.8寸触控屏）
 *
 * 交互流程：
 *   上电动画(ST_BOOT) -> 模式选择(ST_MODE) -> 实时模式(ST_REALTIME)
 *                                          -> 演示模式样本列表(ST_DEMO_MENU)
 *                                             -> 播放(ST_PLAY，报警与 GUI 同款内联锁存)
 *                                          -> 设置(ST_SETTINGS)：语言 / 亮度
 *
 * 架构：业务逻辑全部在 application/ 下按模块拆分，每个模块是独立 ESP-IDF 组件、
 * 由各自的 register.cmake 自注册（机制见 firmware/application/CMakeLists.txt）：
 *   i18n          双语文案            app_config  NVS 设置（语言/亮度）
 *   touch         触摸采样与点按       ui_widgets  UI 基元与配色
 *   waveform      波形帧缓冲渲染       player      样本目录 + 回放引擎
 *   screen_boot / screen_mode / screen_settings / screen_demo /
 *   screen_monitor                   五个页面（绘制 + 私有状态 + 触摸处理）
 *
 * 本文件只保留：app_state_t 六状态枚举 + app_main 的初始化顺序与状态机。
 *
 * 说明：
 *  - 页面私有布局常量随各自模块走；全站共用的在 ui_widgets.h。
 *  - 实时模式接 MAX30003 采集前端（ecg_source + live 环形引擎）；未接模块时显示等待页。
 *  - 字库为按需子集，由 tools/gen_cjk_font.py 扫描 application/ 生成；改文案后必须重跑。
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "ecg_infer.h"
#include "ecg_filter.h"
#include "ecg_max30003.h"
#include "ecg_source.h"
#include "lcd.h"
#include "realtime.h"
#include "sdcard.h"

/* 业务模块（application/<mod>/，见 firmware/application/CMakeLists.txt） */
#include "app_config.h"
#include "ble_gatt.h"
#include "ble_stream.h"
#include "i18n.h"
#include "player.h"
#include "touch.h"
#include "ui_widgets.h"
#include "waveform.h"

/* 页面模块 */
#include "screen_boot.h"
#include "screen_demo.h"
#include "screen_mode.h"
#include "screen_monitor.h"
#include "screen_settings.h"

static const char* TAG = "ECG";

/* ── 实时采集（MAX30003）状态 ──
 * 只在首次进入实时页时尝试启动：驱动在探测失败时不会释放已初始化的 SPI 总线，
 * 重复启动会失败，故用 s_live_tried 记住「已尝试过」。 */
static ecg_source_t       s_live_src;
static ecg_max30003_src_t s_live_m3;
static ecg_filter_t       s_live_filter;
static bool               s_live_tried = false;
static bool               s_live_on = false;

/* ========================================================================= */
/* 配色与布局常量                                                            */
/* ========================================================================= */

#define LINE_H   20                     /* 行距（16px 字高 + 4px） */
#define ROW_H    42                     /* 列表行距（36px 按钮高 + 6px 间隙） */
#define MENU_TOP 48

/* 演示样本列表：大按钮 + 明确的返回按钮（原先靠「点屏幕底部」返回，不可发现）。
 * 按钮高度从 26px 提到 36px（触摸目标更好点），宽度用满 214px。
 * 纵向预算：MENU_TOP(48) + 5*ROW_H(42) = 258，返回键 262..300，屏高 320。
 * 列表按**像素**滚动（手指 1:1 跟动、松手立即停），首尾行只露出一部分时按带裁掉；
 * 样本多于一屏时右侧出现滚动条，也可直接拖滚动条定位。 */
/* 手势判定（取向：可预测优先——松手即停，不做惯性/吸附这类「松手后还在动」的效果） */
/* 无 SD 样本时的内置样本按钮（空态版式与命中判定共用同一组常量） */

/* 模式选择页按钮：**三个入口完全等宽等高**（此前设置按钮偏矮，规格不统一）。
 * 光晕向外扩 2px，故底部留白按 +2px 校验，避免被屏幕裁掉。
 * 纵向预算：58 起，3*72 + 2*13(间隙) = 242 -> 到 300，屏高 320 留白 18px。 */

/* 设置页 */

/* 播放参数 */
#define PLAY_SPEED_MULT 4
#define PLAYER_TICK_MS         20

/* 启动动画 */

/* 动画时间轴（帧号）。点阵屏没有 alpha，淡入靠 lcd_mix 把前景色从底色插到目标色。 */

/* ========================================================================= */
/* 状态                                                                      */
/* ========================================================================= */
typedef enum {
    ST_BOOT, ST_MODE, ST_REALTIME, ST_SETTINGS, ST_DEMO_MENU, ST_PLAY
} app_state_t;

/* 演示样本列表的滑动状态 */
/* 本页「按下沿」已看到：只有按下也发生在本页，抬起时才允许选中。
 * 列表页是本工程唯一在**抬起**时才动作的页面（为了区分滑动与点击），
 * 若不做这个门闩，「在上一页按下 → 切到本页 → 松手」会用同一个手势
 * 在本页再点一次（例如点「演示模式」后松手就顺手把某个样本播了）。 */

/* 报警页上次绘制的心率整数值：值变了就局部重绘，保证读数实时 */

/* 启动动画（只需帧号，各阶段进度由其推算） */

/* 跑马灯沿波形区周长的位置，由播放页重绘循环推进 */

/* ========================================================================= */
/* 各页面绘制                                                                */
/* ========================================================================= */
/* 启动页各元素的淡入/展开进度：把帧号线性映射到 0..255 */






/**
 * 监测页：**演示模式与实时模式共用同一版式**，唯一区别是数据来源——
 * 演示来自 SD 样本回放，实时来自采集前端。两者渲染合并，避免两套 UI 各自演化。
 *
 * 数据来源由 rt 引擎的 sig 指针体现：
 *   sig != NULL  有数据（演示模式 start_play 已初始化引擎；实时模式采集已启动）
 *   sig == NULL  无数据（实时模式未接采集前端；或引擎已停止）
 * 无数据时波形区画一条基线 + 居中提示，模拟「无信号输入的监护仪」，
 * 版式与有数据时完全一致，接入采集后无需改 UI。
 */


/* ─── 演示样本列表：滚动范围 / 滚动条 / 重绘 ─── */

/** 可滚动的最大像素数：样本不够一屏时为 0（列表不滚） */


/** 把滚动位置夹回合法范围（样本数变化、拖动越界都走这里） */


/** 滚动条滑块高度：按可见比例算，最矮 28px（太矮就不好拖） */


/**
 * 只重绘列表带（拖动时每帧调用）：清带 -> 补背景网格 -> 画可见行 -> 滚动条。
 * 不整页重绘是滚动流畅的关键：标题、返回键、按钮都不必跟着一起刷。
 */





/** 点 (x,y) 是否落在列表滚动区（列表带内、行宽范围内） */


/**
 * 演示列表触摸。规则刻意保持"可预测"：
 *   - 只有按在**列表带内**（或滚动条上）才滚动：按返回键时手指抖几像素不会把列表带跑，
 *     返回键不会被"抢走"；
 *   - 手指到哪列表到哪（1:1 像素跟随），**松手立即停**——不做惯性滑行、也不自动吸附，
 *     否则松手后画面还在动，既停不下来也停不到想要的位置；
 *   - 抬起时再决定动作：按钮用宽容忍（位移 <= 20px 就算点中），列表行要求基本没滑动
 *     （位移 <= 10px）才算点击选中，滑动过就只滚动、不选中。
 *
 * 另外两道防误选：按下必须也发生在本页（见 screen_demo 模块的门闩，切页松手不会顺手选中）；
 * 命中判定一律用"按下点"，松手瞬间的抖动不参与。
 * @param pick 输出选中的样本索引（PLAYER_BUILTIN 为内置样本）
 */



/* 报警页：整屏闪烁 + 大字（phase 由调用方翻转）。
 * 安全语义优先：红色不动。加一圈描边把它「框」出来，闪烁时压强更强。
 * 版式自上而下：异常类型大字 -> 「心率」标签 -> 心率数值 -> 单位 -> 确认提示。 */

/** 报警页配色：phase=true 为红底白字，false 为深底红字 */


/**
 * 只重绘心率数值区：闪烁相位没变但心率更新时走这里。
 * 4 倍速回放时心率约每 0.2s 变一次，等 250ms 的闪烁相位再刷会明显跳数；
 * 而为了一个数字整屏重画又太贵（clear 一次是 320 次 blit）。
 *
 * 数值用 "%.0f" 而不是 "%3.0f"：后者给两位/一位数补前导空格，
 * 居中绘制时数字整体偏右半格——这就是「心跳次数没居中」的原因。
 */




/* ========================================================================= */
/* 主流程                                                                    */
/* ========================================================================= */


void app_main(void) {
    ESP_LOGI(TAG, "ECG monitor booting...");

    /* 1. LCD 先初始化：它建立 SPI2 总线，SD 复用该总线 */
    if (lcd_init() != ESP_OK) {
        ESP_LOGE(TAG, "LCD init failed");
    }
    lcd_clear(C_BG);

    /* 2. NVS：读设置并立即应用背光 */
    esp_err_t nv = nvs_flash_init();
    if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nv = nvs_flash_init();
    }
    if (nv == ESP_OK) {
        app_config_load();          /* 读回语言/亮度，内部立即应用背光 */
    } else {
        ESP_LOGW(TAG, "NVS init failed (%s), use defaults", esp_err_to_name(nv));
    }
    ESP_LOGI(TAG, "lang=%d bright=%d%%", (int)i18n_lang(),
             app_config_bright_table()[app_config_bright()]);

    /* 2.5 BLE：GATT 服务 + 广播（上位机据此接收演示样本流）。失败不致命，设备仍可本地演示 */
    {
        esp_err_t be = ble_gatt_init();
        if (be == ESP_OK) {
            ble_stream_init();
        } else {
            ESP_LOGW(TAG, "BLE init failed (%s), continue without BLE", esp_err_to_name(be));
        }
    }

    /* 3. SD 挂载 + 样本扫描（在动画前完成，失败不致命；样本目录归 player 管理） */
    player_scan();

    /* 4. 状态机 */
    app_state_t st = ST_BOOT;
    bool alarm_phase = false;
    int  alarm_kind = 0;
    int  alarm_hr_extreme = 0;   /* 报警锁存期极值（过速峰值 / 过缓最低，GUI 同款） */
    uint32_t alarm_next_flip = 0;

    boot_enter();

    for (;;) {
        uint16_t tx = 0, ty = 0;
        bool tapped = touch_poll(&tx, &ty);

        /* BLE 断开：若正在推流回放，停止并回到模式选择页（断连不空转推流） */
        bool ble_lost = ble_stream_disconnect_pending();
        if (ble_lost && st == ST_PLAY) {
            ESP_LOGI(TAG, "BLE disconnected, stop playback");
            ble_stream_end();
            player_stop();
            st = ST_MODE;
            mode_draw();
            vTaskDelay(pdMS_TO_TICKS(PLAYER_TICK_MS));
            continue;
        }

        switch (st) {
        case ST_BOOT:
            /* 动画播完或触摸跳过 -> 模式选择 */
            if (tapped || boot_done()) {
                st = ST_MODE;
                mode_draw();
                break;
            }
            boot_frame();
            boot_advance();
            vTaskDelay(pdMS_TO_TICKS(BOOT_TICK_MS));
            break;

        case ST_MODE:
            if (tapped) {
                switch (mode_tap(tx, ty)) {
                case MODE_TAP_LIVE:                 /* 实时模式：与演示共用版式，数据源为采集前端 */
                    if (!s_live_tried) {
                        s_live_tried = true;
                        /* 原生 512 SPS → 重采样到 RT_FS(360)；scale 把 18-bit 满量程归一到 ±1。
                         * 采集不到（未接模块）时保持空监测页，屏上显示「等待前端信号」。 */
                        ecg_src_max30003_init(&s_live_src, &s_live_m3, 512, RT_FS,
                                              1.0f / 131072.0f);
                        if (ecg_max30003_start(&s_live_m3) == ESP_OK) {
                            if (player_start_live(RT_FS)) {
                                s_live_on = true;
                                ecg_filter_init(&s_live_filter, RT_FS);
                                ESP_LOGI(TAG, "实时采集已启动");
                            } else {
                                /* 引擎起不来必须回收前端：s_live_on 恒为 false 会让
                                 * 退出路径跳过 ecg_max30003_stop，采集任务与 SPI3
                                 * 会永久泄漏（且 s_live_tried 已置位，无法再重试） */
                                ecg_max30003_stop();
                                ESP_LOGW(TAG, "实时引擎初始化失败，仅显示等待页");
                            }
                        } else {
                            ESP_LOGW(TAG, "实时前端不可用（检查 MAX30003 接线），仅显示等待页");
                        }
                    }
                    alarm_kind = 0;
                    alarm_hr_extreme = 0;
                    alarm_phase = false;
                    st = ST_REALTIME;
                    monitor_draw(true);
                    break;
                case MODE_TAP_DEMO:
                    alarm_kind = 0;
                    alarm_hr_extreme = 0;
                    alarm_phase = false;
                    st = ST_DEMO_MENU;
                    demo_draw();
                    break;
                case MODE_TAP_SETUP:
                    st = ST_SETTINGS;
                    settings_draw();
                    break;
                default: break;
                }
            }
            vTaskDelay(pdMS_TO_TICKS(PLAYER_TICK_MS));
            break;

        case ST_REALTIME:
            if (tapped) {
                if (alarm_kind != 0 &&
                    ui_hit(tx, ty, MONITOR_ACK_X, MONITOR_ACK_Y,
                           MONITOR_ACK_W, MONITOR_ACK_H)) {
                    /* GUI 同款：确认报警只解除锁存，实时监测继续。 */
                    ESP_LOGI(TAG, "live alarm ack (%s), continue monitoring",
                             alarm_kind == 1 ? "TACHY" : "BRADY");
                    alarm_kind = 0;
                    alarm_hr_extreme = 0;
                    alarm_phase = false;
                    monitor_draw(true);
                    break;
                }
                if (s_live_on) {                 /* 停采集并释放 SPI 总线，再回模式页 */
                    ble_stream_end();
                    ecg_max30003_stop();
                    player_stop();
                    s_live_on = false;
                }
                alarm_kind = 0;
                alarm_hr_extreme = 0;
                alarm_phase = false;
                st = ST_MODE;
                mode_draw();
                break;
            }
            if (s_live_on) {
                /* 实时模式同样把 360Hz 样本推给上位机；上位机可能后连接，故每轮按需 begin。 */
                if (!ble_stream_feed_active()) {
                    ble_stream_begin_feed(BLE_MODE_LIVE, "LIVE", RT_FS, 0);
                }
                /* 拉取已重采样到 RT_FS 的实时样本 -> 喂直播引擎 -> 推进一拍检测/分类。
                 * 20ms 一轮、每轮约 7 点，与 RT_FS 实时速率匹配。 */
                float raw[128];
                int got = s_live_src.read(&s_live_src, raw, 128);
                if (got > 0) {
                    float filtered[128];
                    ecg_filter_run(&s_live_filter, raw, got, filtered);
                    player_feed(filtered, got);   /* 屏显 + 端侧推理用滤波后数据 */
                    ble_stream_feed(raw, got);    /* BLE 上传保持原始数据 */
                }
                player_tick();

                /* GUI 同款锁存：心率越界即锁存，并记录锁存期极值。 */
                if (alarm_kind == 0 && player_engine()->alarm != 0) {
                    alarm_kind = player_engine()->alarm;
                    alarm_hr_extreme = (int)(player_engine()->hr + 0.5f);
                    alarm_phase = true;
                    alarm_next_flip = xTaskGetTickCount();
                    ESP_LOGW(TAG, "LIVE ALARM: %s %.0f bpm",
                             player_engine()->alarm == 1 ? "TACHY" : "BRADY",
                             (double)player_engine()->hr);
                }

                if (alarm_kind != 0) {
                    int hr = (int)(player_engine()->hr + 0.5f);
                    if (alarm_kind == 1) {
                        if (hr > alarm_hr_extreme) alarm_hr_extreme = hr;
                    } else {
                        if (alarm_hr_extreme <= 0) alarm_hr_extreme = hr;
                        else if (hr < alarm_hr_extreme) alarm_hr_extreme = hr;
                    }

                    uint32_t now = xTaskGetTickCount();
                    if ((int32_t)(now - alarm_next_flip) >= 0) {
                        alarm_phase = !alarm_phase;
                        alarm_next_flip = now + pdMS_TO_TICKS(250);   /* ~2Hz，与 GUI 一致 */
                    }
                    monitor_draw_alarm(true, alarm_kind, alarm_hr_extreme, alarm_phase);
                } else {
                    monitor_draw(true);
                }
            } else {
                /* 未接采集前端：保持空监测页（等待前端信号） */
                monitor_draw(true);
            }
            vTaskDelay(pdMS_TO_TICKS(PLAYER_TICK_MS));
            break;

        case ST_SETTINGS:
            if (tapped && settings_tap(tx, ty) == SETTINGS_TAP_BACK) {
                st = ST_MODE;
                mode_draw();
            }
            vTaskDelay(pdMS_TO_TICKS(PLAYER_TICK_MS));
            break;

        case ST_DEMO_MENU: {
            /* 滚动/选中的判定都放在抬起时（见 demo_menu_touch），
             * 这样「按住拖动」不会顺手把样本播放起来 */
            int pick = -1;
            demo_act_t act = demo_touch(&pick);
            if (act == DEMO_ACT_BACK) {
                st = ST_MODE;
                mode_draw();
                break;
            }
            if (act == DEMO_ACT_PLAY) {
                if (player_start(pick)) {
                    int bn = 0;
                    const float* bsig = player_active_sig(&bn);
                    if (bsig != NULL) {
                        ble_stream_begin(BLE_MODE_DEMO, player_active_name(), bsig, bn);
                    } else {
                        ble_stream_begin_feed(BLE_MODE_DEMO, player_active_name(),
                                              RT_FS, player_active_len());
                    }
                    st = ST_PLAY;
                } else {
                    lcd_draw_string(16, 302, tr(T_LOADFAIL), C_ALERT, C_BG);
                }
            }
            vTaskDelay(pdMS_TO_TICKS(PLAYER_TICK_MS));
            break;
        }

        case ST_PLAY: {
            if (tapped) {
                if (alarm_kind != 0 &&
                    ui_hit(tx, ty, MONITOR_ACK_X, MONITOR_ACK_Y,
                           MONITOR_ACK_W, MONITOR_ACK_H)) {
                    /* GUI 同款：确认报警只解除锁存，播放继续（波形/读数不中断）。 */
                    ESP_LOGI(TAG, "alarm ack (%s), continue playback",
                             alarm_kind == 1 ? "TACHY" : "BRADY");
                    alarm_kind = 0;
                    alarm_hr_extreme = 0;
                    alarm_phase = false;
                    monitor_draw(false);
                } else {
                    ble_stream_end();
                    player_stop();
                    alarm_kind = 0;
                    alarm_hr_extreme = 0;
                    alarm_phase = false;
                    st = ST_DEMO_MENU;
                    demo_draw();
                }
                break;
            }

            player_tick();

            /* GUI 同款锁存：心率越界即锁存，并记录锁存期极值（过速峰值/过缓最低）。 */
            if (alarm_kind == 0 && player_engine()->alarm != 0) {
                alarm_kind = player_engine()->alarm;
                alarm_hr_extreme = (int)(player_engine()->hr + 0.5f);
                alarm_phase = true;
                alarm_next_flip = xTaskGetTickCount();
                ESP_LOGW(TAG, "ALARM: %s %.0f bpm",
                         player_engine()->alarm == 1 ? "TACHY" : "BRADY",
                         (double)player_engine()->hr);
            }

            if (alarm_kind != 0) {
                int hr = (int)(player_engine()->hr + 0.5f);
                if (alarm_kind == 1) {
                    if (hr > alarm_hr_extreme) alarm_hr_extreme = hr;
                } else {
                    if (alarm_hr_extreme <= 0) alarm_hr_extreme = hr;
                    else if (hr < alarm_hr_extreme) alarm_hr_extreme = hr;
                }

                uint32_t now = xTaskGetTickCount();
                if ((int32_t)(now - alarm_next_flip) >= 0) {
                    alarm_phase = !alarm_phase;
                    alarm_next_flip = now + pdMS_TO_TICKS(250);   /* ~2Hz，与 GUI 一致 */
                }
                monitor_draw_alarm(false, alarm_kind, alarm_hr_extreme, alarm_phase);
            } else {
                monitor_draw(false);
            }

            if (player_finished()) {
                player_flush();   /* 直播引擎不自动冲刷末拍，先冲刷再取统计 */
                if (alarm_kind != 0) {
                    /* 锁存期间样本先播完：续播，保证心率/波形继续推进（与 GUI 播放继续一致）。 */
                    player_reset_keep_hr();
                } else {
                    ESP_LOGI(TAG, "done %s: N%d S%d V%d F%d Q%d", player_current_name(),
                             player_engine()->cls_count[0], player_engine()->cls_count[1],
                             player_engine()->cls_count[2], player_engine()->cls_count[3],
                             player_engine()->cls_count[4]);
                    ble_stream_end();
                    player_stop();
                    st = ST_DEMO_MENU;
                    demo_draw();
                }
            }
            vTaskDelay(pdMS_TO_TICKS(PLAYER_TICK_MS));
            break;
        }

        }
    }
}
