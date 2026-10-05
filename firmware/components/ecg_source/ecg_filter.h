#ifndef ECG_FILTER_H
#define ECG_FILTER_H

/* 端侧实时数字滤波（用于屏幕显示与推理，不用于 BLE 上传）。
 * 因果 IIR 级联：2 级 0.5Hz 高通 + 2 级 30Hz 低通 + 1 级 50Hz 陷波。
 * 依据真实 BLE 原始数据优化：把低通从 45Hz 降到 30Hz 后，52Hz 附近
 * 的采集干扰被显著抑制，R-R 抖动明显下降。 */

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float b0, b1, b2, a1, a2;
    float x1, x2, y1, y2;
} ecg_biquad_t;

typedef struct {
    ecg_biquad_t bq[5];
} ecg_filter_t;

/** @brief 初始化滤波器并清零状态。 */
void ecg_filter_init(ecg_filter_t* f, int fs);

/** @brief 对一段样本做因果滤波（in 与 out 可为同一缓冲）。 */
void ecg_filter_run(ecg_filter_t* f, const float* in, int n, float* out);

#ifdef __cplusplus
}
#endif

#endif /* ECG_FILTER_H */
