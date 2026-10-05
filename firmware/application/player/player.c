/**
 * player.c — 样本目录 + 回放引擎
 */
#include "player.h"

#include <stdio.h>
#include <string.h>

#include "ecg_sample.h"
#include "ble_stream.h"
#include "esp_log.h"
#include "sdcard.h"

static const char* TAG = "PLAYER";

/* ─── 样本目录（SD 卡扫描结果） ─── */
static char s_names[SDCARD_MAX_FILES][SDCARD_MAX_NAME];
static int  s_count = 0;

/* ─── 当前回放的引擎与样本 ─── */
static rt_engine_t s_engine;
static float*      s_sig = NULL;
static int         s_sig_n = 0;
static char        s_cur_name[SDCARD_MAX_NAME] = {0};
static FILE*       s_file = NULL;
static int         s_file_n = 0;
static int         s_file_fed = 0;
static int         s_file_fs = 0;   /* 文件头采样率：喂入速率按它换算，而非固定 360 */

int player_scan(void) {
    if (sdcard_mount() != ESP_OK) {
        ESP_LOGW(TAG, "SD unavailable, will fall back to builtin");
        s_count = 0;
        return 0;
    }
    s_count = sdcard_list(s_names, SDCARD_MAX_FILES);
    ESP_LOGI(TAG, "SD ready, %d samples", s_count);
    return s_count;
}

int player_count(void) { return s_count; }

const char* player_name(int idx) {
    return (idx >= 0 && idx < s_count) ? s_names[idx] : "";
}

const char* player_current_name(void) { return s_cur_name; }

const rt_engine_t* player_engine(void) { return &s_engine; }

const float* player_builtin(int* out_len) {
    if (out_len) *out_len = ECG_SAMPLE_LEN;
    return g_ecg_sample;
}

const float* player_active_sig(int* out_len) {
    if (s_engine.sig == NULL || s_sig_n <= 0) {
        if (out_len) *out_len = 0;
        return NULL;
    }
    if (out_len) *out_len = s_sig_n;
    return s_engine.sig;
}

int player_active_len(void) {
    if (s_file) return s_file_n;
    return s_sig_n;
}

float player_progress(void) {
    if (s_file) {
        return (s_file_n > 0) ? (float)s_file_fed / (float)s_file_n : 0.0f;
    }
    return rt_progress(&s_engine);
}

const char* player_active_name(void) { return s_cur_name; }

void player_stop(void) {
    rt_deinit(&s_engine);
    if (s_file) {
        sdcard_close_file(s_file);
        s_file = NULL;
    }
    if (s_sig) {
        sdcard_free(s_sig);
        s_sig = NULL;
    }
    /* 必须清掉引擎里的 sig：SD 样本缓冲已释放，留着就是悬垂指针。
     * 监测页正是用 sig != NULL 判断「有无数据」的。 */
    s_engine.sig = NULL;
    s_engine.n = 0;
    s_sig_n = 0;
    s_file_n = 0;
    s_file_fed = 0;
    s_file_fs = 0;
    s_cur_name[0] = '\0';
}

bool player_start(int idx) {
    player_stop();

    if (idx == PLAYER_BUILTIN) {
        /* 内置样本：数据在 flash（g_ecg_sample），不 malloc，故 s_sig 保持 NULL */
        s_sig = NULL;
        s_sig_n = ECG_SAMPLE_LEN;
        strncpy(s_cur_name, "BUILTIN", sizeof(s_cur_name) - 1);
        if (rt_init(&s_engine, g_ecg_sample, s_sig_n, RT_FS, PLAYER_SPEED) != 0) {
            ESP_LOGE(TAG, "engine init failed");
            return false;
        }
        ESP_LOGI(TAG, "play builtin: %d samples", s_sig_n);
        return true;
    }

    if (idx < 0 || idx >= s_count) return false;

    int n = 0, fs = RT_FS;
    if (sdcard_open_sample(s_names[idx], &s_file, &n, &fs) != ESP_OK) {
        ESP_LOGE(TAG, "open %s failed", s_names[idx]);
        return false;
    }
    s_file_n = n;
    s_file_fed = 0;
    s_file_fs = fs;
    s_sig = NULL;
    s_sig_n = 0;
    strncpy(s_cur_name, s_names[idx], sizeof(s_cur_name) - 1);

    if (rt_init_live(&s_engine, fs, PLAYER_SPEED, RT_LIVE_CAP) != 0) {
        ESP_LOGE(TAG, "engine init failed");
        player_stop();
        return false;
    }
    ESP_LOGI(TAG, "stream %s: %d samples", s_cur_name, n);
    return true;
}

void player_tick(void) {
    if (s_file) {
        /* 按文件头采样率换算喂入量：引擎用文件 fs 推进，喂入若按固定 360 会与
         * 之失配（非 360Hz 样本波形被拉伸/压缩）。s_file_fs 在 open 时写入。 */
        int fs = (s_file_fs > 0) ? s_file_fs : RT_FS;
        int want = (PLAYER_SPEED * fs * PLAYER_TICK_MS) / 1000;
        if (want < 1) want = 1;
        float chunk[64];
        while (want > 0) {
            int maxn = (want > (int)(sizeof(chunk) / sizeof(chunk[0])))
                           ? (int)(sizeof(chunk) / sizeof(chunk[0])) : want;
            int got = sdcard_read_chunk(s_file, chunk, maxn);
        if (got <= 0) break;
            player_feed(chunk, got);
            ble_stream_feed(chunk, got);
            s_file_fed += got;
            want -= got;
        }
    }
    rt_tick(&s_engine, PLAYER_TICK_MS);
}

bool player_start_live(int fs) {
    player_stop();
    if (rt_init_live(&s_engine, fs, 1, RT_LIVE_CAP) != 0) {
        ESP_LOGE(TAG, "live engine init failed");
        return false;
    }
    /* 直播没有外部样本缓冲（数据在引擎内部环里），故 s_sig 保持 NULL、s_sig_n=0，
     * 这也让 player_active_sig() 对 BLE 返回 NULL —— 直播暂不走 BLE 上传。 */
    s_sig = NULL;
    s_sig_n = 0;
    strncpy(s_cur_name, "LIVE", sizeof(s_cur_name) - 1);
    ESP_LOGI(TAG, "live mode: fs=%d cap=%d", fs, RT_LIVE_CAP);
    return true;
}

void player_feed(const float* samples, int n) {
    rt_feed(&s_engine, samples, n);
}

bool player_finished(void) {
    if (s_file) {
        /* 流式回放：文件喂完且引擎追到接近末尾即可结束；预留峰位精修前瞻 */
        return s_file_fed >= s_file_n &&
               s_engine.pos >= s_file_n - RT_REFINE_FWD;
    }
    return rt_finished(&s_engine);
}

void player_flush(void) {
    /* SD 流式回放走 live 引擎，rt_tick 不会自动冲刷末拍；播完后由调用方显式冲刷，
     * 让最后一拍进入 cls_count（内置样本走 rt_init，rt_tick 已自动冲刷）。幂等。 */
    if (s_file) rt_flush(&s_engine);
}

void player_reset_keep_hr(void) {
    /* 报警锁存期间样本可能先播完（4 倍速下一段样本只要几秒）。
     * 播完就从开头继续回放：否则游标停在末尾，报警页的心率会永远定格在
     * 最后一个读数上，看起来像「死值」。只保留 hr（下一拍到来前继续显示
     * 上一次心率），其余检测状态全部复位。 */
    float keep_hr = s_engine.hr;
    rt_reset(&s_engine);
    s_engine.hr = keep_hr;
    /* 注意：SD 流式回放走 live 引擎，rt_reset 对 live 是空操作（环里只有最近
     * 一段，回绕等于重放旧数据）。文件游标播完后不会自己回绕，这里由 player
     * 层重开文件续播，兑现上面注释的「从开头继续回放」，否则报警页的心率会
     * 定格在末尾读数上。重开失败则保持定格（不影响触摸确认退出报警）。 */
    if (s_file && s_file_fed >= s_file_n) {
        sdcard_close_file(s_file);
        s_file = NULL;
        int n = 0;
        if (sdcard_open_sample(s_cur_name, &s_file, &n, NULL) != ESP_OK) {
            ESP_LOGW(TAG, "rewind %s failed", s_cur_name);
        } else {
            s_file_n = n;
            s_file_fed = 0;
            ESP_LOGI(TAG, "rewind %s: %d samples", s_cur_name, n);
        }
    }
}
