"""全局配置：路径、信号参数、AAMI 类别映射、模型超参数。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # PyInstaller onedir 打包后：data/ models/ sdcard/ 等资源由 assemble_package.py
    # 拷贝到 exe 同级目录，运行时从这里解析（__file__ 会指向 _internal，不能用）。
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"              # 原始 MIT-BIH 记录（wfdb 下载）
PROCESSED_DIR = ROOT / "processed"    # 预处理后的 numpy 缓存
MODEL_DIR = ROOT / "models"           # 训练产物（权重 / onnx / tflite）
EXPORT_DIR = ROOT / "export"
REPORT_DIR = ROOT / "docs" / "report"

for _d in (DATA_DIR, PROCESSED_DIR, MODEL_DIR, EXPORT_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 信号参数（MIT-BIH 心律失常数据库）
# ---------------------------------------------------------------------------
FS = 360                    # 采样率 Hz
RECORD_IDS = [              # 48 条记录；测试集沿用 AAMI 推荐划分
    100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
    111, 112, 113, 114, 115, 116, 117, 118, 119, 121,
    122, 123, 124, 200, 201, 202, 203, 205, 207, 208,
    209, 210, 212, 213, 214, 215, 217, 219, 220, 221,
    222, 223, 228, 230, 231, 232, 233, 234,
]
# DS1 / DS2（AAMI 标准按记录划分，避免同记录数据泄漏）
DS1 = [101, 106, 108, 109, 112, 114, 115, 116, 118, 119, 122, 124, 201,
       203, 205, 207, 208, 209, 215, 220, 223, 230]
DS2 = [100, 103, 105, 111, 113, 117, 121, 123, 200, 202, 210, 212, 213,
       214, 219, 221, 222, 228, 231, 232, 233, 234]
# 其余 4 条（102,104,107,217）为起搏记录，AAMI 中归入训练/测试需谨慎处理；
# 本工程将其并入训练集以保留 Q 类（起搏）样本，报告中说明。

BEAT_WINDOW = 187           # 心拍窗口采样点数（R 峰前 64 / 后 122）
PRE_R = 64
POST_R = 122
LEAD_NAME = "MLII"          # 首选导联

# RR 间期特征（归一化 pre-RR / post-RR，供 FC 头与时域形态拼接）
N_RR_FEATURES = 2

# ---------------------------------------------------------------------------
# AAMI 类别
# ---------------------------------------------------------------------------
# 任务要求 5 类：N / S / V / F / Q
CLASSES = ["N", "S", "V", "F", "Q"]
CLASS2IDX = {c: i for i, c in enumerate(CLASSES)}
IDX2CLASS = {i: c for i, c in enumerate(CLASSES)}

# MIT-BIH 逐拍标注符号 -> AAMI 类别
_AAMI_N = {"N", "L", "R", "e", "j"}            # 正常 / 左束支 / 右束支 / 房性逸搏 / 交界性逸搏
_AAMI_S = {"A", "a", "J", "S"}                  # 房性早搏 / 交界性早搏 / 室上性异位
_AAMI_V = {"V", "E"}                            # 室性早搏 / 室性逸搏
_AAMI_F = {"F"}                                 # 融合
_AAMI_Q = {"/", "f", "Q"}                       # 起搏 / 起搏融合 / 无法分类

# 非心拍标注符号（节律变化 / 信号质量 / 注释等），提取心拍时直接跳过。
# 否则这些标记会被误标成 Q 类，污染 Q 的训练标签。
# 注意：classify_beat_symbol 先把符号 .upper() 再比对，所以字母符号这里必须写
# 大写——原先写小写 "x" 时它永远匹配不上，会落到 map_symbol_to_class 的兜底
# 分支被标成 Q（全库 193 个 'x'，其中测试集 135 个，占测试 Q 的 95%）。
NON_BEAT_SYMBOLS = {"+", "~", "!", "\"", "X", "|", "[", "]", "(", ")"}


def map_symbol_to_class(symbol: str) -> str:
    """把 MIT-BIH 单个 beat 标注符号映射到 AAMI 五类之一。"""
    s = symbol.strip().upper()
    if s in _AAMI_N:
        return "N"
    if s in _AAMI_S:
        return "S"
    if s in _AAMI_V:
        return "V"
    if s in _AAMI_F:
        return "F"
    if s in _AAMI_Q:
        return "Q"
    return "Q"  # 无法分类归入 Q


def classify_beat_symbol(symbol: str) -> str | None:
    """把标注符号映射到 AAMI 五类；非心拍标记返回 None（用于过滤）。"""
    s = symbol.strip().upper()
    if s in NON_BEAT_SYMBOLS:
        return None
    return map_symbol_to_class(s)

# ---------------------------------------------------------------------------
# 模型超参数
# ---------------------------------------------------------------------------
NUM_CLASSES = len(CLASSES)
MODEL_INPUT_LEN = BEAT_WINDOW
SEED = 42
BATCH_SIZE = 256
EPOCHS = 60
LR = 1e-3
