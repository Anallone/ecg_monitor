"""信号预处理：去噪滤波、Pan-Tompkins R 峰检测、心拍分割与归一化。

`r_peaks` 检测独立于 wfdb 标注，用于「实时引擎」模拟真实流式场景
（标注仅用于训练标签）。
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks

from config import BEAT_WINDOW, FS, POST_R, PRE_R


# ---------------------------------------------------------------------------
# 滤波
# ---------------------------------------------------------------------------
def bandpass_filter(sig: np.ndarray, fs: int = FS,
                    low: float = 0.5, high: float = 45.0) -> np.ndarray:
    """0.5–45 Hz 带通滤波。"""
    nyq = fs / 2.0
    b, a = butter(4, [low / nyq, high / nyq], btype="band")
    return filtfilt(b, a, sig)


def notch_filter(sig: np.ndarray, fs: int = FS, f0: float = 50.0, q: float = 30.0) -> np.ndarray:
    """50 Hz 工频陷波。"""
    b, a = butter(2, [f0 - 1, f0 + 1], btype="bandstop", fs=fs)
    return filtfilt(b, a, sig)


def preprocess_signal(sig: np.ndarray) -> np.ndarray:
    """完整去噪流程。"""
    sig = notch_filter(bandpass_filter(sig))
    return sig.astype(np.float32)


def causal_live_filter_sos(fs: int = FS) -> np.ndarray:
    """端侧实时采集的因果滤波 SOS（须与 firmware/components/ecg_source/ecg_filter.c 保持一致）。

    4 阶 Butterworth 0.5Hz 高通 + 4 阶 Butterworth 30Hz 低通 + 1 级 50Hz 陷波（Q=25）。
    返回 shape (5, 6) 的 SOS，可直接用于 scipy.signal.sosfilt；每节 a0 已归一化为 1。
    """
    def bq(kind: str, fc: float, q: float) -> list[float]:
        w0 = 2.0 * np.pi * fc / fs
        cw = np.cos(w0)
        sw = np.sin(w0)
        alpha = sw / (2.0 * q)
        a0 = 1.0 + alpha
        if kind == "hp":
            b0, b1, b2 = (1.0 + cw) / 2.0, -(1.0 + cw), (1.0 + cw) / 2.0
        elif kind == "lp":
            b0, b1, b2 = (1.0 - cw) / 2.0, 1.0 - cw, (1.0 - cw) / 2.0
        else:  # notch
            b0, b1, b2 = 1.0, -2.0 * cw, 1.0
        a1 = -2.0 * cw
        a2 = 1.0 - alpha
        return [b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]

    return np.array([
        bq("hp", 0.5, 0.54119610),
        bq("hp", 0.5, 1.30656296),
        bq("lp", 30.0, 0.54119610),
        bq("lp", 30.0, 1.30656296),
        bq("notch", 50.0, 25.0),
    ])


# ---------------------------------------------------------------------------
# Pan-Tompkins R 峰检测（简化但稳健的实现）
# ---------------------------------------------------------------------------
def pan_tompkins(sig: np.ndarray, fs: int = FS,
                 min_hr: float = 30.0, max_hr: float = 220.0) -> np.ndarray:
    """简化 Pan-Tompkins：差分 + 平方 + 移动平均 + 自适应阈值。"""
    # 差分（近似导数）
    diff = np.diff(sig)
    diff = np.concatenate([diff[:1], diff])
    squared = diff ** 2
    # 移动平均积分窗口（约 150ms）
    win = max(1, int(0.15 * fs))
    kernel = np.ones(win) / win
    integrated = np.convolve(squared, kernel, mode="same")

    # 自适应双阈值
    signal_mean = np.mean(integrated)
    signal_std = np.std(integrated)
    threshold = signal_mean + 0.5 * signal_std

    # 峰值间距约束：最小心率 30bpm -> 最大 RR 2s；最大心率 220bpm -> 最小 RR ~0.27s
    min_dist = max(1, int(60.0 / max_hr * fs))
    peaks, _ = find_peaks(integrated, height=threshold, distance=min_dist)

    # 二次精修：在候选峰值邻域内取原信号真实最大值
    refined = []
    search = int(0.05 * fs)
    for p in peaks:
        lo, hi = max(0, p - search), min(len(sig), p + search + 1)
        refined.append(lo + int(np.argmax(sig[lo:hi])))
    return np.asarray(refined, dtype=np.int64)


# ---------------------------------------------------------------------------
# 心拍分割 / 归一化
# ---------------------------------------------------------------------------
def segment_beat(sig: np.ndarray, r_peak: int,
                 pre: int = PRE_R, post: int = POST_R) -> np.ndarray | None:
    """以 R 峰为中心截取窗口（pre + 1 + post），越界返回 None。"""
    start, end = r_peak - pre, r_peak + post + 1
    if start < 0 or end > len(sig):
        return None
    return sig[start:end]


def zscore_normalize(win: np.ndarray) -> np.ndarray:
    """单拍 Z-score 归一化，避免除零。"""
    std = np.std(win)
    if std < 1e-6:
        return win - np.mean(win)
    return (win - np.mean(win)) / std
