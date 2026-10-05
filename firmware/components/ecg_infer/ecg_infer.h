/**
 * ecg_infer.h — 轻量 ECG 模型手写前向推理（float32，权重 int8 反量化）
 *
 * 当前主线为 res_se_cnn_rr4：
 *   输入 (1, 187) + 原始 RR 2 维
 *   深度可分离卷积 + 残差 + SE 通道注意力
 *   全局平均池化 + 4 维 RR 特征 + RR 旁路
 *
 * 权重来自 export/c_model/model_data.h（int8）+ model_config.h（维度常量）。
 */
#ifndef ECG_INFER_H
#define ECG_INFER_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 对单拍 187 点 ECG 做 5 类分类
 * @param input  归一化后的 187 点 float 输入
 * @param rr_feat 原始归一化 pre-RR / post-RR（长度 >= 2；可为 NULL，此时用 1.0
 *                填充）。C 推理内部会按模型需要扩展为 pre/post/ratio/diff。
 * @param logits 输出 5 类 logits（调用方分配，长度 >= 5）
 * @return 0 成功，负值失败
 */
int ecg_infer(const float* input, const float* rr_feat, float* logits);

#ifdef __cplusplus
}
#endif

#endif /* ECG_INFER_H */
