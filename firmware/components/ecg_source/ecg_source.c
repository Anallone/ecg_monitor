/**
 * ecg_source.c — 采集源抽象实现（内存源 / 重采样 / MAX30003 环形缓冲）
 *
 * 本文件**刻意不依赖 ESP-IDF**（只用 libc），因此：
 *   - 可以在主机上用 gcc 单独编译，对拍 ecg_resampler_run() 的正确性；
 *   - 只有 ecg_max30003.c（SPI 部分）依赖 ESP-IDF。
 *
 * ⚠️ 未编译、未上硬件验证。归一化/重采样参数需按实测调整。
 */
#include "ecg_source.h"

#include <string.h>

/* ------------------------------------------------------------------------- */
/* 内存源                                                                     */
/* ------------------------------------------------------------------------- */

static int mem_read(ecg_source_t* self, float* out, int max) {
    ecg_mem_src_t* c = (ecg_mem_src_t*)self->ctx;
    if (max <= 0 || c->pos >= c->n) return 0;

    int avail = c->n - c->pos;
    int k = (max < avail) ? max : avail;
    memcpy(out, &c->sig[c->pos], sizeof(float) * (size_t)k);
    c->pos += k;
    return k;
}

static bool mem_finished(ecg_source_t* self) {
    ecg_mem_src_t* c = (ecg_mem_src_t*)self->ctx;
    return c->pos >= c->n;
}

static void mem_deinit(ecg_source_t* self) {
    (void)self;   /* sig 由调用方持有，这里不释放 */
}

void ecg_src_mem_init(ecg_source_t* src, ecg_mem_src_t* ctx,
                      const char* name, const float* sig, int n, int fs) {
    ctx->sig = sig;
    ctx->n = (n > 0) ? n : 0;
    ctx->pos = 0;

    src->name = name;
    src->fs = fs;
    src->ctx = ctx;
    src->read = mem_read;
    src->finished = mem_finished;
    src->deinit = mem_deinit;
}

/* ------------------------------------------------------------------------- */
/* 线性插值重采样                                                              */
/* ------------------------------------------------------------------------- */
/*
 * 坐标系：把每个输入块 + 上一块尾样本看成虚拟数组
 *     V[0] = prev,  V[k] = in[k-1]   (k = 1..n_in)
 * 输出点落在虚拟坐标 p, p+ratio, p+2*ratio, ...，ratio = fs_in / fs_out。
 * 插值需要 V[floor(p)] 与 V[floor(p)+1]；当 floor(p)+1 > n_in 时只有
 * frac == 0 才能只靠 V[floor(p)] 输出，否则把 p 留给下一块。
 * （这个 frac==0 的放宽是必要的：否则恒等率会丢掉每块最后一个样本。）
 *
 * 已验证（Python 逐行复刻，2026-09-11）：
 *   - 512→360：5120 点输入恰得 3600 点
 *   - **分块独立性**：chunk = 1/7/128/256/511/512/1024 的输出与一次性处理
 *     完全一致（最大偏差 ~1e-10，仅双精度舍入）
 *   - 恒等 360→360：严格逐样本等于输入
 *   - 降采样 512→128：5120 点恰得 1280 点
 *   - 升采样尾部会少若干点（128→512 少 3 点）——这是流式插值的固有性质：
 *     那些点需要下一块的样本，实时流中会在下一块补上，非 bug。
 *
 * 首块：prev = in[0]，phase 从 1.0 起步（虚拟坐标 1.0 恰为 in[0]），
 * 因此不会重复也不会丢首样本。
 *
 * 块尾：prev 更新为 in[n_in-1]，phase 折算为 p - n_in（恒 >= 0），
 * 所以块边界连续——这是流式场景的关键。
 */

void ecg_resampler_init(ecg_resampler_t* r, int fs_in, int fs_out) {
    r->fs_in = (fs_in > 0) ? fs_in : 1;
    r->fs_out = (fs_out > 0) ? fs_out : 1;
    r->phase = 0.0;
    r->prev = 0.0f;
    r->has_prev = false;
}

int ecg_resampler_run(ecg_resampler_t* r, const float* in, int n_in,
                      float* out, int out_cap) {
    if (r == NULL || in == NULL || out == NULL) return 0;
    if (n_in <= 0 || out_cap <= 0) return 0;

    const double ratio = (double)r->fs_in / (double)r->fs_out;
    if (!(ratio > 0.0)) return 0;

    if (!r->has_prev) {
        r->prev = in[0];
        r->phase = 1.0;
        r->has_prev = true;
    }

    const double p_limit = (double)n_in;   /* 虚拟坐标上界（含） */
    double p = r->phase;
    int n_out = 0;

    while (p <= p_limit && n_out < out_cap) {
        int i0 = (int)p;                 /* p >= 0，见上面的不变量；i0 <= n_in */
        int i1 = i0 + 1;
        double frac = p - (double)i0;

        float v0 = (i0 == 0) ? r->prev : in[i0 - 1];   /* i0 <= n_in → i0-1 <= n_in-1，安全 */
        float v1;
        if (i1 <= n_in) {
            v1 = in[i1 - 1];
        } else {
            /* i1 越界：仅当 frac == 0 时可只靠 v0 输出（对应 V[n_in]=in[n_in-1]）；
             * 否则需要下一块的样本才能插值，把 p 留给下一块。 */
            if (frac > 0.0) break;
            v1 = v0;
        }

        out[n_out++] = (float)((double)v0 + ((double)v1 - (double)v0) * frac);
        p += ratio;
    }

    /* 收尾：携带状态给下一块。out_cap 过小时 p 可能 < n_in，做保护性钳位，
     * 但正常调用（out_cap 按接口注释给足）不会走到这里。 */
    r->prev = in[n_in - 1];
    {
        double carry = p - (double)n_in;
        r->phase = (carry > 0.0) ? carry : 0.0;
    }
    return n_out;
}

/* ------------------------------------------------------------------------- */
/* MAX30003 采集源                                                            */
/* ------------------------------------------------------------------------- */

static int m3_read(ecg_source_t* self, float* out, int max) {
    ecg_max30003_src_t* c = (ecg_max30003_src_t*)self->ctx;
    if (max <= 0) return 0;

    int written = 0;
    for (;;) {
        /* 1. 先吐 stage 里的余量 */
        if (c->stage_pos < c->stage_len) {
            int left = c->stage_len - c->stage_pos;
            int room = max - written;
            int k = (left < room) ? left : room;
            if (k > 0) {
                memcpy(out + written, c->stage + c->stage_pos,
                       sizeof(float) * (size_t)k);
                c->stage_pos += k;
                written += k;
            }
            if (written >= max) break;
        }

        /* 2. stage 空了：从环形缓冲拉一块原生样本并重采样 */
        int tail = c->tail;
        int head = c->head;
        int avail = head - tail;
        if (avail < 0) avail += ECG_MAX30003_RING;
        if (avail == 0) break;                    /* 暂无新数据 */

        int n = (avail < ECG_MAX30003_CHUNK) ? avail : ECG_MAX30003_CHUNK;
        for (int i = 0; i < n; i++) {
            c->tmp[i] = c->ring[tail];
            if (++tail >= ECG_MAX30003_RING) tail = 0;
        }
        c->tail = tail;                           /* 提交读指针 */

        c->stage_len = ecg_resampler_run(&c->rs, c->tmp, n,
                                         c->stage, ECG_MAX30003_STAGE);
        c->stage_pos = 0;
        /* stage_len==0 时继续拉下一块；环形缓冲取空即退出，不会死循环 */
    }
    return written;
}

static bool m3_finished(ecg_source_t* self) {
    (void)self;
    return false;   /* 流式源永不结束 */
}

static void m3_deinit(ecg_source_t* self) {
    (void)self;
}

void ecg_src_max30003_init(ecg_source_t* src, ecg_max30003_src_t* ctx,
                           int fs_native, int fs_out, float scale) {
    ctx->head = 0;
    ctx->tail = 0;
    ctx->overflow = 0;
    ctx->fs_native = (fs_native > 0) ? fs_native : 512;
    ctx->scale = scale;
    ctx->stage_len = 0;
    ctx->stage_pos = 0;
    ecg_resampler_init(&ctx->rs, ctx->fs_native, fs_out);

    src->name = "MAX30003";
    src->fs = fs_out;
    src->ctx = ctx;
    src->read = m3_read;
    src->finished = m3_finished;
    src->deinit = m3_deinit;
}

void ecg_src_max30003_feed(ecg_max30003_src_t* c, int32_t raw) {
    if (c == NULL) return;

    int head = c->head;
    int next = head + 1;
    if (next >= ECG_MAX30003_RING) next = 0;

    if (next == c->tail) {
        /* 环形缓冲满：丢弃本样本并计数（消费端太慢，需加大 RING 或提高 read 频率） */
        c->overflow++;
        return;
    }

    c->ring[head] = (float)raw * c->scale;
    c->head = next;
}
