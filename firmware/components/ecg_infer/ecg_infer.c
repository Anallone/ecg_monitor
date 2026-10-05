/**
 * ecg_infer.c — 轻量心电模型的手写前向推理（float32）
 *
 * 权重以 int8 存储 + per-tensor scale 反量化；bias 为折叠 BN 后的 float。
 * 模型仅数万参数，ESP32-S3 带 FPU，直接做 float 卷积即可，无需手写 int8 累加。
 *
 * 本实现不针对固定层数：逐层结构与量化参数由导出脚本写入 model_data.h 的
 * g_model_layers 描述表，这里用一个通用循环遍历，因此同一份固件代码可支持
 *   - DepthwiseSeparableCNN（基线：三层 SepConv）
 *   - ResSECNN（残差连接 + 通道注意力 + 更深主干）
 * 换模型只需重新导出 model_data.h / model_config.h，无需改动本文件。
 *
 * 单层计算顺序（与 src/export_c_model.py 的 numpy 参考前向严格一致）：
 *   深度可分离卷积 → 加 bias → ReLU →（可选）通道注意力 →（可选）残差相加 + ReLU
 *   →（可选）最大池化
 */
#include "ecg_infer.h"

#include <math.h>
#include <string.h>

#include "model_config.h"
#include "model_data.h"

/* ------------------------------------------------------------------------- */
/* 静态工作缓冲（.bss，不占任务栈）。容量由导出脚本按结构推算并写入 model_config.h，
 * 保证不会越界（比手写固定值更可靠）。 */
/* ------------------------------------------------------------------------- */
static float g_buf_a[MODEL_MAX_ACT];
static float g_buf_b[MODEL_MAX_ACT];
static float g_dw[MODEL_MAX_DW];
static float g_in[MODEL_INPUT_LEN];

static inline float relu(float x) { return x > 0.0f ? x : 0.0f; }

/* 通道注意力（Squeeze-and-Excitation）：
 * 全局平均池化 → 1x1 降维 + ReLU → 1x1 升维 + Sigmoid → 逐通道相乘。
 * 权重按行主序存放：se_q1 为 (se_h, ch)，se_q2 为 (ch, se_h)。 */
static void se_apply(const float *y, int ch, int len, const ecg_layer_t *L,
                     float *gate) {
  float g[MODEL_MAX_CH];
  float h[MODEL_MAX_HID];

  for (int c = 0; c < ch; c++) {
    float s = 0.0f;
    const float *row = y + (size_t)c * len;
    for (int t = 0; t < len; t++) {
      s += row[t];
    }
    g[c] = s / (float)len;
  }
  for (int k = 0; k < L->se_h; k++) {
    float acc = L->se_b1[k];
    for (int c = 0; c < ch; c++) {
      acc += g[c] * ((float)L->se_q1[(size_t)k * ch + c] * L->se_scale1);
    }
    h[k] = relu(acc);
  }
  for (int c = 0; c < ch; c++) {
    float acc = L->se_b2[c];
    for (int k = 0; k < L->se_h; k++) {
      acc += h[k] * ((float)L->se_q2[(size_t)c * L->se_h + k] * L->se_scale2);
    }
    gate[c] = 1.0f / (1.0f + expf(-acc));
  }
}

/* 深度可分离卷积 + bias + ReLU (+ SE) (+ 残差)：
 *   in  : (c_in, len) 行主序
 *   out : (c_out, len)，与 in 必须不是同一块缓冲（残差需要读到原始 in） */
static void sepconv(const ecg_layer_t *L, const float *in, int c_in, int len,
                    float *out) {
  const int c_out = L->c_out;
  const int k = L->k;
  const int pad = L->pad;

  /* depthwise：groups = c_in，逐通道一维卷积 */
  for (int c = 0; c < c_in; c++) {
    const float *row = in + (size_t)c * len;
    float *drow = g_dw + (size_t)c * len;
    for (int t = 0; t < len; t++) {
      float acc = 0.0f;
      for (int j = 0; j < k; j++) {
        const int src = t - pad + j;
        if (src >= 0 && src < len) {
          acc += row[src] * ((float)L->dw[c * k + j] * L->dw_scale);
        }
      }
      drow[t] = acc;
    }
  }

  /* pointwise 1x1 + 折叠后的 bias + ReLU */
  for (int co = 0; co < c_out; co++) {
    float *orow = out + (size_t)co * len;
    for (int t = 0; t < len; t++) {
      float acc = L->bias[co];
      for (int ci = 0; ci < c_in; ci++) {
        acc += g_dw[(size_t)ci * len + t] * ((float)L->pw[co * c_in + ci] * L->pw_scale);
      }
      orow[t] = relu(acc);
    }
  }

  /* 通道注意力 */
  if (L->se) {
    float gate[MODEL_MAX_CH];
    se_apply(out, c_out, len, L, gate);
    for (int c = 0; c < c_out; c++) {
      float *orow = out + (size_t)c * len;
      for (int t = 0; t < len; t++) {
        orow[t] *= gate[c];
      }
    }
  }

  /* 残差相加后再 ReLU（与 PyTorch 端一致） */
  if (L->residual) {
    for (int c = 0; c < c_out; c++) {
      float *orow = out + (size_t)c * len;
      const float *irow = in + (size_t)c * len;
      for (int t = 0; t < len; t++) {
        orow[t] = relu(orow[t] + irow[t]);
      }
    }
  }
}

/* 原地最大池化：输出写回同一块缓冲的前部。
 * 安全性：输出索引 t 读取输入 [t*s, t*s+k)，而 t*s >= t，故写入位置不会覆盖
 * 尚未读取的输入。 */
static void maxpool_inplace(float *x, int ch, int len, int k, int s,
                            int *out_len) {
  const int n = (len - k) / s + 1;
  for (int c = 0; c < ch; c++) {
    float *row = x + (size_t)c * len;
    for (int t = 0; t < n; t++) {
      float m = row[t * s];
      for (int j = 1; j < k; j++) {
        const float v = row[t * s + j];
        m = m > v ? m : v;
      }
      row[t] = m;
    }
    /* 池化后长度由 len 变为 n，调用方随即用 n 作为通道步长，所以这里必须把结果
     * 从旧步长位置搬到紧凑位置。少了这一步，通道 c>=1 的数据会被后续层按 n 寻址
     * 读到上一个通道的残留值（只有通道 0 恰好正确），导致整网输出全错。
     * 目的区间 [c*n,(c+1)*n) 只与本通道旧数据和已处理通道的陈旧副本重叠，安全。 */
    if (n != len) {
      memmove(x + (size_t)c * n, row, sizeof(float) * (size_t)n);
    }
  }
  *out_len = n;
}

int ecg_infer(const float* input, const float* rr_feat, float* logits) {
  memcpy(g_in, input, sizeof(float) * MODEL_INPUT_LEN);

  const float *cur = g_in;
  int c_in = 1;
  int len = MODEL_INPUT_LEN;
  float *buf[2] = { g_buf_a, g_buf_b };
  int which = 0;

  for (int i = 0; i < MODEL_N_SEP; i++) {
    const ecg_layer_t *L = &g_model_layers[i];
    float *out = buf[which];
    sepconv(L, cur, c_in, len, out);

    if (L->do_pool) {
      maxpool_inplace(out, L->c_out, len, L->pool_k, L->pool_s, &len);
    }
    cur = out;
    c_in = L->c_out;
    which ^= 1;            /* 下一层写入另一块缓冲，cur 始终有效 */
  }

  /* AdaptiveAvgPool1d(1)：对每个通道在时间维取均值 */
  float pooled[MODEL_MAX_POOL];
  float rr_ext[MODEL_N_RR_FEATURES];
  if (rr_feat != NULL) {
    rr_ext[0] = rr_feat[0];
    rr_ext[1] = rr_feat[1];
#if MODEL_N_RR_FEATURES >= 4
    rr_ext[2] = fminf(fmaxf(rr_feat[0] / (rr_feat[1] + 1e-6f), 0.3f), 3.0f);
    rr_ext[3] = rr_feat[1] - rr_feat[0];
#endif
  } else {
    for (int i = 0; i < MODEL_N_RR_FEATURES; i++) {
      rr_ext[i] = 1.0f;
    }
  }
  for (int c = 0; c < c_in; c++) {
    float s = 0.0f;
    const float *row = cur + (size_t)c * len;
    for (int t = 0; t < len; t++) {
      s += row[t];
    }
    pooled[c] = s / (float)len;
  }
  /* 拼接 RR 特征（无邻接 RR 时用 1.0 回退，与训练侧一致） */
  for (int i = 0; i < MODEL_N_RR_FEATURES; i++) {
    pooled[c_in + i] = rr_ext[i];
  }

  /* 全连接 */
  for (int o = 0; o < FC_OUT; o++) {
    float acc = fc_b[o];
    for (int i = 0; i < FC_IN; i++) {
      acc += pooled[i] * ((float)fc_w[o * FC_IN + i] * FC_W_SCALE);
    }
    logits[o] = acc;
  }

  /* RR 间期旁路（早搏判别）：Linear(2,16)+ReLU+Linear(16,5)，直接加到 logits。
   * 早搏（S/V）形态接近 N，RR 是唯一可靠判据，给它一条直达输出的旁路，
   * 避免被 112 维形态特征淹没。与 src/export_c_model.py 的参考前向严格一致。 */
#if MODEL_HAS_RR_HEAD
  if (rr_feat != NULL) {
    float h[RR_H];
    for (int j = 0; j < RR_H; j++) {
      float acc = rr_b1[j];
      for (int i = 0; i < MODEL_N_RR_FEATURES; i++) {
        acc += rr_ext[i] *
               ((float)rr_w1[j * MODEL_N_RR_FEATURES + i] * RR_W1_SCALE);
      }
      h[j] = relu(acc);
    }
    for (int o = 0; o < FC_OUT; o++) {
      float acc = rr_b2[o];
      for (int j = 0; j < RR_H; j++) {
        acc += h[j] * ((float)rr_w2[o * RR_H + j] * RR_W2_SCALE);
      }
      logits[o] += acc;
    }
  }
#endif
  return 0;
}
