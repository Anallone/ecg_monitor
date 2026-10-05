/* 自动生成：勿手动编辑。由 src/export_c_model.py 生成。 */
#ifndef MODEL_CONFIG_H
#define MODEL_CONFIG_H

#define MODEL_INPUT_LEN 187
#define MODEL_NUM_CLASSES 5
#define MODEL_N_RR_FEATURES 4
#define MODEL_HAS_RR_HEAD 1

/* RR 间期旁路维度与量化 scale（MODEL_HAS_RR_HEAD=1 时有效） */
#define RR_H 16
#define RR_W1_SCALE 0.00631957167f
#define RR_W2_SCALE 0.00979119398f

/* 工作缓冲容量（由导出脚本按结构推算，保证不越界） */
#define MODEL_MAX_ACT 5236
#define MODEL_MAX_DW  2604
#define MODEL_MAX_CH  112
#define MODEL_MAX_HID 14
#define MODEL_MAX_POOL 116

#define FC_IN 116
#define FC_OUT 5
#define FC_W_SCALE 0.00332070875f
#endif
