#ifndef ECG_FILTER_H
#define ECG_FILTER_H

/* 端侧实时数字滤波（用于屏幕显示与推理，不用于 BLE 上传）。
 * 因果 IIR 级联：4 阶 0.5Hz 高通 + 4 阶 30Hz 低通 + 1 级 52.5Hz 陷波（Q=25）。
 * 与上位机 live 滤波（preprocessing.causal_live_filter_sos）保持一致。
 * 按实测 BLE 原始信号调参：工频干扰主峰在 52.5Hz（50Hz 处无峰），
 * 故陷波中心定在 52.5Hz；30Hz 低通同时抑制 105/157.5Hz 谐波。 */

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
