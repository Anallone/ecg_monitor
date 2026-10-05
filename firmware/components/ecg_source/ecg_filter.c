#include "ecg_filter.h"

#include <math.h>
#include <stddef.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846f
#endif

static void biquad_init_hp(ecg_biquad_t* bq, float fc, float fs, float q) {
    float w0 = 2.0f * (float)M_PI * fc / fs;
    float cw = cosf(w0);
    float sw = sinf(w0);
    float alpha = sw / (2.0f * q);
    float a0 = 1.0f + alpha;
    bq->b0 = (1.0f + cw) / 2.0f;
    bq->b1 = -(1.0f + cw);
    bq->b2 = (1.0f + cw) / 2.0f;
    bq->a1 = -2.0f * cw;
    bq->a2 = 1.0f - alpha;
    bq->b0 /= a0; bq->b1 /= a0; bq->b2 /= a0;
    bq->a1 /= a0; bq->a2 /= a0;
    bq->x1 = bq->x2 = bq->y1 = bq->y2 = 0.0f;
}

static void biquad_init_lp(ecg_biquad_t* bq, float fc, float fs, float q) {
    float w0 = 2.0f * (float)M_PI * fc / fs;
    float cw = cosf(w0);
    float sw = sinf(w0);
    float alpha = sw / (2.0f * q);
    float a0 = 1.0f + alpha;
    bq->b0 = (1.0f - cw) / 2.0f;
    bq->b1 = 1.0f - cw;
    bq->b2 = (1.0f - cw) / 2.0f;
    bq->a1 = -2.0f * cw;
    bq->a2 = 1.0f - alpha;
    bq->b0 /= a0; bq->b1 /= a0; bq->b2 /= a0;
    bq->a1 /= a0; bq->a2 /= a0;
    bq->x1 = bq->x2 = bq->y1 = bq->y2 = 0.0f;
}

static void biquad_init_notch(ecg_biquad_t* bq, float f0, float fs, float q) {
    float w0 = 2.0f * (float)M_PI * f0 / fs;
    float cw = cosf(w0);
    float sw = sinf(w0);
    float alpha = sw / (2.0f * q);
    float a0 = 1.0f + alpha;
    bq->b0 = 1.0f;
    bq->b1 = -2.0f * cw;
    bq->b2 = 1.0f;
    bq->a1 = -2.0f * cw;
    bq->a2 = 1.0f - alpha;
    bq->b0 /= a0; bq->b1 /= a0; bq->b2 /= a0;
    bq->a1 /= a0; bq->a2 /= a0;
    bq->x1 = bq->x2 = bq->y1 = bq->y2 = 0.0f;
}

void ecg_filter_init(ecg_filter_t* f, int fs) {
    if (f == NULL) return;
    if (fs <= 0) fs = 360;
    /* 4 阶 Butterworth 需两个 2 阶节按 0.5412 / 1.3066 配对，
     * 与上位机 scipy butter(4, ...) 的极点一致；两节都用 0.707 是 Linkwitz-Riley。 */
    biquad_init_hp(&f->bq[0], 0.5f, (float)fs, 0.54119610f);
    biquad_init_hp(&f->bq[1], 0.5f, (float)fs, 1.30656296f);
    biquad_init_lp(&f->bq[2], 30.0f, (float)fs, 0.54119610f);
    biquad_init_lp(&f->bq[3], 30.0f, (float)fs, 1.30656296f);
    /* 50Hz 工频陷波：Q=25 对应 -3dB 带宽约 2Hz（49~51Hz）。
     * 此前实测的 52.5Hz 主峰是 FCLK 频率错误把 50Hz 搬移所致；FCLK 修正后
     * 工频回到 50Hz，陷波中心也应回到 50Hz。 */
    biquad_init_notch(&f->bq[4], 50.0f, (float)fs, 25.0f);
}

static float biquad_step(ecg_biquad_t* bq, float x) {
    float y = bq->b0 * x + bq->b1 * bq->x1 + bq->b2 * bq->x2
              - bq->a1 * bq->y1 - bq->a2 * bq->y2;
    bq->x2 = bq->x1;
    bq->x1 = x;
    bq->y2 = bq->y1;
    bq->y1 = y;
    return y;
}

void ecg_filter_run(ecg_filter_t* f, const float* in, int n, float* out) {
    if (f == NULL || in == NULL || out == NULL || n <= 0) return;
    for (int i = 0; i < n; i++) {
        float v = in[i];
        for (int j = 0; j < 5; j++) {
            v = biquad_step(&f->bq[j], v);
        }
        out[i] = v;
    }
}
