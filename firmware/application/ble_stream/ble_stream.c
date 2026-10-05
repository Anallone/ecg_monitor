/**
 * ble_stream.c — 演示会话推流实现
 *
 * 独立任务按 1× 实时速率（360 样本/s）推流：每包样本数与节拍按协商后的 ATT MTU
 * 决定（MTU 越大每包装得越多、包率越低），每包组一帧 BLE_TYPE_WAVE；每秒附一帧
 * 状态心跳。样本推完或断开即发「会话结束」并退出任务。
 */
#include "ble_stream.h"

#include <math.h>
#include <string.h>

#include "ble_gatt.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char* TAG = "BLESTREAM";

#define STREAM_FS 360           /* 演示样本采样率，推流按 1× 实时速率走 */
#define STREAM_MAX_PKT 60       /* 每包样本上限：60 样本 = 120B 载荷，帧 125B（需 MTU>=128） */
#define SESSION_NAME_MAX 48
/* 会话名实际送出的字节数上限：默认 ATT MTU 23 下单次 Notify 的载荷上限是 20 字节，
 * 帧头尾占 5，故 name 最多 12 字节（含 mode 那 1 字节）。上位机会协商更大 MTU，
 * 但这里按最保守值截断，保证默认 MTU 下会话开始帧也发得出去。 */
#define SESSION_NAME_SEND_MAX 12

static TaskHandle_t s_task = NULL;
static volatile bool s_active = false;   /* 推流任务存活标志（end() 轮询用，避免直接轮询句柄的竞态） */
static volatile bool s_run = false;
static volatile bool s_disconnected = false;

static const float* s_sig = NULL;
static int          s_n = 0;
static int          s_pos = 0;
static uint8_t      s_mode = BLE_MODE_DEMO;
static char         s_name[SESSION_NAME_MAX] = {0};
static bool         s_feed_active = false;
static int          s_feed_fs = STREAM_FS;
static int          s_feed_total = 0;
static int          s_feed_sent = 0;
static uint32_t     s_feed_last_hb = 0;

static void on_disconnect(void) { s_disconnected = true; }

void ble_stream_init(void) {
    ble_gatt_set_on_disconnect(on_disconnect);
}

static void stream_task(void* arg) {
    (void)arg;
    uint32_t sent = 0;
    uint32_t last_hb = 0;

    /* 按协商后的 ATT MTU 决定每包样本数与节拍：MTU 越大每包装得越多、包率越低。
     * 默认 MTU 23 只能 7 样本/包 ≈52 包/s，Windows 端 30ms 连接间隔一包一事件
     * 扛不住会稳定丢 1/3；ble_gatt.c 在连接时主动 exchange MTU 后，这里一包能装
     * 几十样本、包率降到个位数，丢包归零。
     * 帧开销：ATT 通知头 3B + 帧头尾 5B，剩余每 2B 一个 int16 样本。 */
    uint16_t mtu = ble_gatt_mtu();
    int pkt = ((int)mtu - 3 - 5) / 2;
    if (pkt < 1) pkt = 1;
    if (pkt > STREAM_MAX_PKT) pkt = STREAM_MAX_PKT;
    int tick_ms = pkt * 1000 / STREAM_FS;      /* 保持 360 样本/s 的 1× 实时速率 */
    if (tick_ms < 1) tick_ms = 1;
    ESP_LOGI(TAG, "mtu=%u pkt=%d tick=%dms", (unsigned)mtu, pkt, tick_ms);

    int16_t  pcm[STREAM_MAX_PKT];

    while (s_run && s_pos < s_n) {
        if (!ble_gatt_connected()) break;

        int cnt = s_n - s_pos;
        if (cnt > pkt) cnt = pkt;
        for (int i = 0; i < cnt; i++) {
            float v = s_sig[s_pos + i] * BLE_SAMPLE_SCALE;
            if (v > 32767.0f) v = 32767.0f;
            if (v < -32768.0f) v = -32768.0f;
            pcm[i] = (int16_t)lrintf(v);
        }
        ble_gatt_notify(BLE_TYPE_WAVE, (const uint8_t*)pcm, (uint8_t)(cnt * 2));
        s_pos += cnt;
        sent += (uint32_t)cnt;

        /* 状态心跳：每秒一次（电量本板无电量计，占位 0xFF）。
         * tick 域比较：xTaskGetTickCount()×portTICK_PERIOD_MS 换算毫秒会在
         * 连续运行约 497 天（100Hz）后 uint32 溢出、比较失效；有符号差值
         * 比较在回绕下依然正确（1000ms 间隔远小于 2^31 tick）。 */
        uint32_t now = xTaskGetTickCount();
        if ((int32_t)(now - last_hb) >= pdMS_TO_TICKS(1000)) {
            last_hb = now;
            uint8_t st[10];
            st[0] = s_mode;
            st[1] = 0xFF;
            st[2] = (uint8_t)(sent & 0xFF);
            st[3] = (uint8_t)((sent >> 8) & 0xFF);
            st[4] = (uint8_t)((sent >> 16) & 0xFF);
            st[5] = (uint8_t)((sent >> 24) & 0xFF);
            memset(&st[6], 0, 4);   /* overflow 计数（预留，实时环形缓冲用） */
            ble_gatt_notify(BLE_TYPE_STATUS, st, sizeof(st));
        }

        vTaskDelay(pdMS_TO_TICKS(tick_ms));
    }

    if (ble_gatt_connected()) {
        ble_gatt_notify(BLE_TYPE_SESSION_END, NULL, 0);
    }
    ESP_LOGI(TAG, "session end: sent %u/%d", (unsigned)sent, s_n);

    s_task = NULL;
    s_run = false;
    s_active = false;
    vTaskDelete(NULL);
}

void ble_stream_begin(uint8_t mode, const char* name, const float* sig, int n) {
    ble_stream_end();   /* 同一时刻只允许一个会话 */

    if (sig == NULL || n <= 0) return;
    if (!ble_gatt_connected() || !ble_gatt_subscribed()) {
        ESP_LOGI(TAG, "no BLE subscriber, skip streaming");
        return;
    }

    s_sig = sig;
    s_n = n;
    s_pos = 0;
    s_mode = mode;
    strncpy(s_name, name ? name : "", sizeof(s_name) - 1);
    s_name[sizeof(s_name) - 1] = '\0';

    uint8_t payload[1 + SESSION_NAME_MAX];
    payload[0] = mode;
    size_t nl = strlen(s_name);
    if (nl > SESSION_NAME_SEND_MAX) nl = SESSION_NAME_SEND_MAX;   /* 见该宏注释 */
    memcpy(&payload[1], s_name, nl);
    ble_gatt_notify(BLE_TYPE_SESSION_START, payload, (uint8_t)(1 + nl));
    ESP_LOGI(TAG, "session start: mode=%u name=%s n=%d", (unsigned)mode, s_name, n);

    s_run = true;
    s_active = true;
    if (xTaskCreate(stream_task, "ble_stream", 3072, NULL, 4, &s_task) != pdPASS) {
        ESP_LOGE(TAG, "task create failed");
        s_run = false;
        s_active = false;
    }
}

static void send_session_start(uint8_t mode, const char* name) {
    uint8_t payload[1 + SESSION_NAME_MAX];
    payload[0] = mode;
    size_t nl = strlen(name);
    if (nl > SESSION_NAME_SEND_MAX) nl = SESSION_NAME_SEND_MAX;
    memcpy(&payload[1], name, nl);
    ble_gatt_notify(BLE_TYPE_SESSION_START, payload, (uint8_t)(1 + nl));
}

void ble_stream_begin_feed(uint8_t mode, const char* name, int fs, int total) {
    ble_stream_end();
    if (!ble_gatt_connected() || !ble_gatt_subscribed()) return;

    s_feed_active = true;
    s_feed_fs = (fs > 0) ? fs : STREAM_FS;
    s_feed_total = total;
    s_feed_sent = 0;
    s_feed_last_hb = 0;
    s_mode = mode;
    strncpy(s_name, name ? name : "", sizeof(s_name) - 1);
    s_name[sizeof(s_name) - 1] = '\0';
    send_session_start(mode, s_name);
    ESP_LOGI(TAG, "feed session start: mode=%u name=%s n=%d", (unsigned)mode, s_name, total);
}

void ble_stream_feed(const float* samples, int n) {
    if (!s_feed_active || samples == NULL || n <= 0) return;
    if (!ble_gatt_connected() || !ble_gatt_subscribed()) {
        s_feed_active = false;
        return;
    }

    uint16_t mtu = ble_gatt_mtu();
    int pkt = ((int)mtu - 3 - 5) / 2;
    if (pkt < 1) pkt = 1;
    if (pkt > STREAM_MAX_PKT) pkt = STREAM_MAX_PKT;
    int16_t pcm[STREAM_MAX_PKT];
    int off = 0;
    while (off < n) {
        int cnt = n - off;
        if (cnt > pkt) cnt = pkt;
        for (int i = 0; i < cnt; i++) {
            float v = samples[off + i] * BLE_SAMPLE_SCALE;
            if (v > 32767.0f) v = 32767.0f;
            if (v < -32768.0f) v = -32768.0f;
            pcm[i] = (int16_t)lrintf(v);
        }
        ble_gatt_notify(BLE_TYPE_WAVE, (const uint8_t*)pcm, (uint8_t)(cnt * 2));
        off += cnt;
        s_feed_sent += cnt;
    }

    uint32_t now = xTaskGetTickCount();   /* tick 域比较，同 stream_task 心跳（溢出回绕安全） */
    if ((int32_t)(now - s_feed_last_hb) >= pdMS_TO_TICKS(1000)) {
        s_feed_last_hb = now;
        uint8_t st[10];
        st[0] = s_mode;
        st[1] = 0xFF;
        st[2] = (uint8_t)(s_feed_sent & 0xFF);
        st[3] = (uint8_t)((s_feed_sent >> 8) & 0xFF);
        st[4] = (uint8_t)((s_feed_sent >> 16) & 0xFF);
        st[5] = (uint8_t)((s_feed_sent >> 24) & 0xFF);
        memset(&st[6], 0, 4);
        ble_gatt_notify(BLE_TYPE_STATUS, st, sizeof(st));
    }

    if (s_feed_total > 0 && s_feed_sent >= s_feed_total) {
        if (ble_gatt_connected()) {
            ble_gatt_notify(BLE_TYPE_SESSION_END, NULL, 0);
        }
        ESP_LOGI(TAG, "feed session end: sent %d/%d", s_feed_sent, s_feed_total);
        s_feed_active = false;
    }
}

bool ble_stream_feed_active(void) {
    return s_feed_active;
}

void ble_stream_end(void) {
    s_run = false;
    if (s_feed_active) {
        if (ble_gatt_connected()) {
            ble_gatt_notify(BLE_TYPE_SESSION_END, NULL, 0);
        }
        ESP_LOGI(TAG, "feed session end by stop: sent %d/%d", s_feed_sent, s_feed_total);
        s_feed_active = false;
    }
    if (!s_active) return;

    /* 等任务自行退出（每拍最多 ~STREAM_MAX_PKT/STREAM_FS 秒，这里留足 ~300ms） */
    for (int i = 0; i < 60 && s_active; i++) {
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    if (s_active) {                 /* 兜底：任务卡住则强制删除。
                                     * 任务退出序列先置 s_task=NULL 再置 s_active=false，
                                     * 若恰在两步之间超时，s_task 已是 NULL——vTaskDelete(NULL)
                                     * 删除的是调用者自身（主循环任务），必须先存局部变量判空。 */
        TaskHandle_t t = s_task;
        if (t != NULL) vTaskDelete(t);
        s_active = false;
    }
    s_task = NULL;
}

bool ble_stream_disconnect_pending(void) {
    if (!s_disconnected) return false;
    s_disconnected = false;
    return true;
}
