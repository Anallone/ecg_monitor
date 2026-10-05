/**
 * realtime.c — ECG 流式处理引擎实现
 *
 * 设计要点：
 *  1. 样本已完整在 RAM，取 187 点窗口直接按索引切片，无需环形缓冲。
 *  2. 但「检测」必须因果：积分信号用尾随移动平均（只看过去 54 点），
 *     阈值用过去 1800 点滑动窗。绝不用未来采样，保证是真流式而非事后回放。
 *  3. 分类延迟一拍：post-RR 需要下一个 R 峰。第 i 拍在第 i+1 拍检测到之后
 *     才分类（post-RR 用真值）；样本最后一拍用 mean_rr 回退。
 *     心率与报警不延迟——R 峰一确认就更新，保证报警及时。
 */
#include "realtime.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "ecg_infer.h"
#include "esp_log.h"

static const char* TAG = "RT";

/* 因果移动平均窗：0.15s @360Hz */
#define RT_MA_WIN 54

/* ------------------------------------------------------------------------- */
/* 工具                                                                      */
/* ------------------------------------------------------------------------- */

/* 逐拍 z-score（与 src/preprocessing.zscore_normalize 一致：std<1e-6 时只减均值） */
static void zscore(const float* in, int n, float* out) {
    float mean = 0.0f;
    for (int i = 0; i < n; i++) mean += in[i];
    mean /= (float)n;
    float var = 0.0f;
    for (int i = 0; i < n; i++) {
        float d = in[i] - mean;
        var += d * d;
    }
    float std = sqrtf(var / (float)n);
    if (std < 1e-6f) {
        for (int i = 0; i < n; i++) out[i] = in[i] - mean;
    } else {
        for (int i = 0; i < n; i++) out[i] = (in[i] - mean) / std;
    }
}

static float clampf(float v, float lo, float hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

/**
 * 预计算因果积分信号：diff[i]=sig[i]-sig[i-1]（diff[0]=0），
 * integ[i] = 尾随 MA_WIN 个 diff² 的均值。只依赖过去数据。
 */
static void build_integrated(const float* sig, int n, float* integ) {
    float acc = 0.0f;
    float prev = sig[0];
    for (int i = 0; i < n; i++) {
        float d = sig[i] - prev;
        prev = sig[i];
        float sq = d * d;
        acc += sq;
        if (i >= RT_MA_WIN) {
            /* 滑出窗口的那一项是 diff[i-RT_MA_WIN] */
            int k = i - RT_MA_WIN;
            float dout = sig[k] - (k > 0 ? sig[k - 1] : sig[0]);
            acc -= dout * dout;
            integ[i] = acc / (float)RT_MA_WIN;
        } else {
            integ[i] = acc / (float)(i + 1);
        }
    }
}

/* ------------------------------------------------------------------------- */
/* 取数：回放=整段直接下标；直播=有界环形（i & (cap-1)）                        */
/* ------------------------------------------------------------------------- */
static inline float sig_at(const rt_engine_t* e, int i) {
    return e->live ? e->sig_ring[i & (e->sig_cap - 1)] : e->sig[i];
}

static inline float integ_at(const rt_engine_t* e, int i) {
    return e->live ? e->integ_ring[i & (e->integ_cap - 1)] : e->integ[i];
}

float rt_sample_at(const rt_engine_t* e, int i) {
    if (e == NULL || i < 0 || i >= e->n) return 0.0f;
    return sig_at(e, i);
}

/* ------------------------------------------------------------------------- */
/* 滑动阈值窗：过去 RT_THR_WIN 点的 mean + 0.5*std（环形增量维护）             */
/* ------------------------------------------------------------------------- */
typedef struct {
    float* buf;
    int    idx;
    int    cnt;
    double sum;
    double sumsq;
} thr_win_t;

static void thr_win_reset(thr_win_t* w, float* storage) {
    w->buf = storage;
    w->idx = 0;
    w->cnt = 0;
    w->sum = 0.0;
    w->sumsq = 0.0;
}

static void thr_win_push(thr_win_t* w, float v) {
    if (w->cnt == RT_THR_WIN) {
        float old = w->buf[w->idx];
        w->sum -= old;
        w->sumsq -= (double)old * old;
    } else {
        w->cnt++;
    }
    w->buf[w->idx] = v;
    w->sum += v;
    w->sumsq += (double)v * v;
    w->idx = (w->idx + 1) % RT_THR_WIN;
}

static float thr_win_value(const thr_win_t* w) {
    if (w->cnt < 2) return 0.0f;
    double mean = w->sum / w->cnt;
    double var = w->sumsq / w->cnt - mean * mean;
    if (var < 0.0) var = 0.0;
    return (float)(mean + 0.5 * sqrt(var));
}

/* ------------------------------------------------------------------------- */
/* 生命周期                                                                   */
/* ------------------------------------------------------------------------- */
int rt_init(rt_engine_t* e, const float* sig, int n, int fs, int speed) {
    if (e == NULL || sig == NULL || n <= RT_WIN_LEN) return -1;
    memset(e, 0, sizeof(*e));
    e->sig = sig;
    e->n = n;
    e->fs = (fs > 0) ? fs : RT_FS;
    e->speed = (speed > 0) ? speed : 1;
    e->last_r = -1;
    e->mean_rr = 1.0f;      /* 与训练侧「无邻接 RR 回退 1.0」一致 */

    e->integ = (float*)malloc((size_t)n * sizeof(float));
    e->thr_win = (float*)malloc((size_t)RT_THR_WIN * sizeof(float));
    if (e->integ == NULL || e->thr_win == NULL) {
        rt_deinit(e);
        return -1;
    }
    build_integrated(sig, n, e->integ);
    ESP_LOGI(TAG, "engine ready: n=%d fs=%d speed=%dx", n, e->fs, e->speed);
    return 0;
}

void rt_deinit(rt_engine_t* e) {
    if (e == NULL) return;
    if (e->integ) { free(e->integ); e->integ = NULL; }
    if (e->thr_win) { free(e->thr_win); e->thr_win = NULL; }
    if (e->sig_ring) { free(e->sig_ring); e->sig_ring = NULL; }
    if (e->integ_ring) { free(e->integ_ring); e->integ_ring = NULL; }
    if (e->live) e->sig = NULL;      /* live 时 sig 指向 sig_ring，已释放 */
}

void rt_reset(rt_engine_t* e) {
    if (e == NULL) return;
    /* 直播模式（无限流）不支持回绕重播：环里只有最近一小段，回绕等于重放旧数据。
     * 游标保持不动，避免把历史当成新样本再检测一遍。 */
    if (e->live) return;
    e->pos = 0;
    e->last_r = -1;
    e->thr = 0.0f;
    e->rr_sum = 0.0f;
    e->rr_cnt = 0;
    e->mean_rr = 1.0f;
    e->hr = 0.0f;
    e->alarm = 0;
    e->alarm_cand = 0;
    e->alarm_cnt = 0;
    e->rr_win_idx = 0;
    e->rr_win_cnt = 0;
    memset(e->rr_win, 0, sizeof(e->rr_win));
    e->n_beats = 0;
    e->n_classified = 0;
    memset(e->cls_count, 0, sizeof(e->cls_count));
    if (e->thr_win) memset(e->thr_win, 0, (size_t)RT_THR_WIN * sizeof(float));
    e->thr_idx = 0;
    e->thr_cnt = 0;
    e->thr_sum = 0.0;
    e->thr_sumsq = 0.0;
}

bool rt_finished(const rt_engine_t* e) {
    return e->live ? false : (e->pos >= e->n);   /* 直播是无限流，永不「结束」 */
}
int  rt_pos(const rt_engine_t* e) { return e->pos; }
float rt_progress(const rt_engine_t* e) {
    return e->n > 0 ? (float)e->pos / (float)e->n : 0.0f;
}

/* ------------------------------------------------------------------------- */
/* R 峰确认：更新心率/报警，入队                                                      */
/* ------------------------------------------------------------------------- */
/* 心率平滑窗口：只保留最近 RT_HR_MED_N 个有效 RR，取中位数抑制单个早搏/漏检的跳变 */
static void rr_win_push(rt_engine_t* e, float rr) {
    e->rr_win[e->rr_win_idx] = rr;
    e->rr_win_idx = (e->rr_win_idx + 1) % RT_HR_MED_N;
    if (e->rr_win_cnt < RT_HR_MED_N) e->rr_win_cnt++;
}

static float rr_win_median(const rt_engine_t* e) {
    int n = e->rr_win_cnt;
    if (n <= 0) return 0.0f;

    float v[RT_HR_MED_N];
    memcpy(v, e->rr_win, (size_t)n * sizeof(float));
    /* 插入排序：n 固定为 RT_HR_MED_N（5），开销可忽略 */
    for (int i = 1; i < n; i++) {
        float key = v[i];
        int j = i - 1;
        while (j >= 0 && v[j] > key) {
            v[j + 1] = v[j];
            j--;
        }
        v[j + 1] = key;
    }
    int mid = n / 2;
    return (n % 2) ? v[mid] : 0.5f * (v[mid - 1] + v[mid]);
}

static void on_r_detected(rt_engine_t* e, int r) {
    if (e->n_beats >= RT_MAX_BEATS) return;

    /* 心率 / 报警：立即更新，不延迟 */
    if (e->last_r >= 0) {
        float rr = (float)(r - e->last_r) / (float)e->fs;
        float med;
        if (rr > 0.2f && rr < 3.0f) {          /* 20~300 bpm */
            e->rr_sum += rr;
            e->rr_cnt++;
            e->mean_rr = e->rr_sum / (float)e->rr_cnt;
            rr_win_push(e, rr);
            med = rr_win_median(e);
            e->hr = (med > 1e-6f) ? 60.0f / med : 0.0f;
            /* 报警确认：连续 RT_HR_ALARM_CONFIRM 拍越界才触发，抑制噪声误报 */
            int cur = (e->hr > RT_HR_HIGH) ? 1 : ((e->hr < RT_HR_LOW) ? 2 : 0);
            if (cur != 0 && cur == e->alarm_cand) {
                e->alarm_cnt++;
            } else {
                e->alarm_cand = cur;
                e->alarm_cnt = (cur != 0) ? 1 : 0;
            }
            e->alarm = (e->alarm_cnt >= RT_HR_ALARM_CONFIRM) ? e->alarm_cand : 0;
        }
    }

    rt_beat_t* b = &e->beats[e->n_beats++];
    b->r = r;
    b->cls = -1;
    b->alarm = (int8_t)e->alarm;
    e->last_r = r;
}

/* ------------------------------------------------------------------------- */
/* 分类（延迟一拍，以取得真实 post-RR）                                        */
/* ------------------------------------------------------------------------- */
static bool classify_beat(rt_engine_t* e, int idx) {
    const rt_beat_t* b = &e->beats[idx];
    int r = (int)b->r;

    int start = r - RT_PRE_R;
    int end = r + RT_POST_R + 1;
    if (start < 0 || end > e->n) return true;   /* 边界不足，标记为已处理但不出结果 */
    /* 直播：窗口左端若已被环形缓冲覆盖（仅在极端延迟下才可能），同样不出结果 */
    if (e->live && start < e->n - e->sig_cap) return true;

    float win[RT_WIN_LEN];
    float norm[RT_WIN_LEN];
    for (int k = 0; k < RT_WIN_LEN; k++) win[k] = sig_at(e, start + k);
    zscore(win, RT_WIN_LEN, norm);

    /* RR 特征：延迟分类保证下一拍已知；首/末拍回退 mean_rr */
    float pre_sec = (idx > 0) ? (float)(r - e->beats[idx - 1].r) / (float)e->fs
                              : e->mean_rr;
    float post_sec = (idx + 1 < e->n_beats) ? (float)(e->beats[idx + 1].r - r) / (float)e->fs
                                            : e->mean_rr;
    float mrr = (e->mean_rr > 1e-3f) ? e->mean_rr : 1.0f;
    float rr_feat[2];
    rr_feat[0] = clampf(pre_sec / mrr, 0.3f, 3.0f);
    rr_feat[1] = clampf(post_sec / mrr, 0.3f, 3.0f);

    float logits[5];
    if (ecg_infer(norm, rr_feat, logits) != 0) return true;

    int best = 0;
    for (int i = 1; i < 5; i++) {
        if (logits[i] > logits[best]) best = i;
    }
    e->beats[idx].cls = (int8_t)best;
    e->cls_count[best]++;
    return true;
}

/**
 * 推进待分类游标。仅当「下一拍已存在」（post-RR 可得）或 tail=true 时才分类。
 * @return 本次分类完成的数量
 */
static int flush_classify(rt_engine_t* e, bool tail) {
    int done = 0;
    while (e->n_classified < e->n_beats) {
        int idx = e->n_classified;
        bool next_known = (idx + 1 < e->n_beats);
        if (!next_known && !tail) break;
        classify_beat(e, idx);
        e->n_classified++;
        done++;
    }
    return done;
}

/* ------------------------------------------------------------------------- */
/* 推进                                                                      */
/* ------------------------------------------------------------------------- */
int rt_tick(rt_engine_t* e, int tick_ms) {
    if (e == NULL || e->sig == NULL || e->thr_win == NULL) return 0;
    if (!e->live && e->integ == NULL) return 0;
    if (tick_ms <= 0) tick_ms = 20;

    int step = (e->speed * e->fs * tick_ms) / 1000;
    if (step < 1) step = 1;

    int from = e->pos;
    /* 直播防御：若主循环长时间未 tick（> 环容量 ≈5.7s），pos 会落后到环已覆盖的区域，
     * 此时按旧下标读数会读到被覆盖的新值。跳过这段陈旧区（丢的是检测、不是数据），
     * 正常 20ms 一轮根本走不到这里。 */
    if (e->live) {
        int floor_i = e->n - (e->sig_cap - RT_WIN_LEN - RT_REFINE_FWD);
        if (floor_i < 0) floor_i = 0;
        if (from < floor_i) from = floor_i;
    }
    int to = from + step;
    if (to > e->n) to = e->n;
    /* 直播：峰位精修要看 cand+RT_REFINE_FWD 个采样。这些点还没喂进来时处理，
     * 精修窗口会被截断、择出不同的峰位（与回放路径结果对不上）。故只推进到
     * 「仍留足前瞻」的位置；尾段样本会在后续数据到达后再处理，不会丢。 */
    if (e->live) {
        int limit = e->n - RT_REFINE_FWD;
        if (limit < 0) limit = 0;
        if (to > limit) to = limit;
    }
    if (from >= to) return 0;

    thr_win_t w;
    if (e->live) {
        /* 直播：阈值窗跨 tick 增量维护。环形缓冲只有最近 cap 点，回看 1800 点重建
         * 会读到已被覆盖的旧值；增量推进天然只看最近 1800 个 integ。 */
        w.buf = e->thr_win;
        w.idx = e->thr_idx;
        w.cnt = e->thr_cnt;
        w.sum = e->thr_sum;
        w.sumsq = e->thr_sumsq;
    } else {
        /* 回放：整段常驻，按过去的 RT_THR_WIN 个积分值重建（O(1800)/tick 可忽略） */
        thr_win_reset(&w, e->thr_win);
        int wstart = from - RT_THR_WIN;
        if (wstart < 0) wstart = 0;
        for (int i = wstart; i < from; i++) thr_win_push(&w, integ_at(e, i));
    }

    /* 逐采样推进：喂阈值窗 + 因果峰值检测 */
    for (int i = from; i < to; i++) {
        thr_win_push(&w, integ_at(e, i));
        if (i < RT_WARMUP) continue;

        e->thr = thr_win_value(&w);
        if (e->thr < 1e-9f) e->thr = 1e-9f;

        /* 确认 i-1 是否为峰：需要 i 作为右邻（1 点延迟，因果） */
        if (i < 2) continue;
        float pm = integ_at(e, i - 1);
        if (!(pm > e->thr && pm >= integ_at(e, i) && pm >= integ_at(e, i - 2))) continue;

        int cand = i - 1;
        if (e->last_r >= 0 && (cand - e->last_r) < RT_MIN_DIST) continue;

        /* 精修：非对称邻域取原信号最大点（补偿尾随 MA 的触发延迟，见 realtime.h） */
        int lo = cand - RT_REFINE_BACK;
        int hi = cand + RT_REFINE_FWD;
        if (lo < 0) lo = 0;
        if (hi >= e->n) hi = e->n - 1;
        int best = lo;
        for (int j = lo + 1; j <= hi; j++) {
            if (sig_at(e, j) > sig_at(e, best)) best = j;
        }
        if (e->last_r >= 0 && (best - e->last_r) < RT_MIN_DIST) continue;

        on_r_detected(e, best);
    }

    if (e->live) {
        e->thr_idx = w.idx;
        e->thr_cnt = w.cnt;
        e->thr_sum = w.sum;
        e->thr_sumsq = w.sumsq;
    }

    e->pos = to;

    /* 分类：回放播到末尾时冲刷最后一拍（post-RR 用 mean_rr 回退）。
     * 直播是无限流，追平缓冲属常态，不能当「已到末尾」——下一拍会补上真实 post-RR。 */
    return flush_classify(e, !e->live && rt_finished(e));
}

/**
 * 冲刷待分类末拍（tail=true，post-RR 用 mean_rr 回退）。
 *
 * 直播引擎不能在 rt_tick 里判断「已到末尾」（无限流），但 SD 流式回放终会播完，
 * 此时由播放层显式调用本函数，让最后一拍也进入统计（否则 cls_count 少 1）。
 * 幂等：已分类的拍不会被重复处理。
 */
int rt_flush(rt_engine_t* e) {
    if (e == NULL) return 0;
    return flush_classify(e, true);
}

/* ------------------------------------------------------------------------- */
/* 直播：初始化与喂数                                                          */
/* ------------------------------------------------------------------------- */
int rt_init_live(rt_engine_t* e, int fs, int speed, int cap) {
    if (e == NULL) return -1;
    if (cap < RT_WIN_LEN + 4) return -1;
    if ((cap & (cap - 1)) != 0) return -1;   /* 必须 2 的幂（下标用掩码映射） */

    memset(e, 0, sizeof(*e));
    e->fs = (fs > 0) ? fs : RT_FS;
    e->speed = (speed > 0) ? speed : 1;
    e->last_r = -1;
    e->mean_rr = 1.0f;      /* 与训练侧「无邻接 RR 回退 1.0」一致 */
    e->live = true;

    e->sig_ring = (float*)malloc((size_t)cap * sizeof(float));
    e->integ_ring = (float*)malloc((size_t)cap * sizeof(float));
    e->thr_win = (float*)malloc((size_t)RT_THR_WIN * sizeof(float));
    if (e->sig_ring == NULL || e->integ_ring == NULL || e->thr_win == NULL) {
        rt_deinit(e);
        return -1;
    }
    e->sig_cap = cap;
    e->integ_cap = cap;
    e->sig = e->sig_ring;   /* 显示层用 sig != NULL 判「有无数据」 */
    e->n = 0;
    e->integ_acc = 0.0f;
    ESP_LOGI(TAG, "live engine ready: fs=%d cap=%d", e->fs, cap);
    return 0;
}

void rt_feed(rt_engine_t* e, const float* samples, int cnt) {
    if (e == NULL || !e->live || samples == NULL || cnt <= 0) return;

    for (int k = 0; k < cnt; k++) {
        int i = e->n;
        float cur = samples[k];
        e->sig_ring[i & (e->sig_cap - 1)] = cur;

        /* 因果积分：尾随 RT_MA_WIN 个 diff² 的均值，逐样本滚动（同 build_integrated） */
        float prev = (i > 0) ? sig_at(e, i - 1) : cur;
        float d = cur - prev;
        e->integ_acc += d * d;
        if (i >= RT_MA_WIN) {
            int j = i - RT_MA_WIN;                       /* 滑出窗口的那一项 */
            float jprev = (j > 0) ? sig_at(e, j - 1) : sig_at(e, j);
            float dout = sig_at(e, j) - jprev;
            e->integ_acc -= dout * dout;
            e->integ_ring[i & (e->integ_cap - 1)] = e->integ_acc / (float)RT_MA_WIN;
        } else {
            e->integ_ring[i & (e->integ_cap - 1)] = e->integ_acc / (float)(i + 1);
        }
        e->n = i + 1;
    }
}
