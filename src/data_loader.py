"""MIT-BIH 数据下载、标注解析与 AAMI 类别归并。"""
from __future__ import annotations

import numpy as np
import wfdb

from config import DATA_DIR, RECORD_IDS, LEAD_NAME


def download_record(record_id: int) -> bool:
    """下载单条记录；返回是否成功（已存在也算成功）。"""
    try:
        wfdb.dl_database("mitdb", dl_dir=str(DATA_DIR), records=[str(record_id)])
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] record {record_id} 下载失败: {exc}")
        return False


def load_record(record_id: int):
    """读取单条记录的 signal 与 annotation。

    返回 (signal: np.ndarray (n_samples, n_leads), annotation, lead_idx)
    优先使用 MLII，找不到则用第一条导联。
    """
    record = wfdb.rdrecord(str(DATA_DIR / str(record_id)), channels=None)
    annotation = wfdb.rdann(str(DATA_DIR / str(record_id)), "atr")

    lead_idx = 0
    lead_names = record.sig_name
    if LEAD_NAME in lead_names:
        lead_idx = lead_names.index(LEAD_NAME)
    return record.p_signal, annotation, lead_idx


def extract_beats(record_id: int):
    """提取单条记录的心拍窗口、标签与 RR 间期特征。

    返回 (windows: (n, BEAT_WINDOW), labels_idx: (n,), peaks: (n,),
          rr_feat: (n, 2))
    窗口以 R 峰为中心，超出边界的丢弃；非心拍标注符号被跳过。

    rr_feat 两列分别为归一化 pre-RR / post-RR（= RR / 记录平均 RR），
    用于判别早搏（S/V）等仅凭形态难以区分的类别。
    """
    from config import (BEAT_WINDOW, N_RR_FEATURES, PRE_R, POST_R,
                        classify_beat_symbol)
    from preprocessing import bandpass_filter, notch_filter

    signal, annotation, lead_idx = load_record(record_id)
    sig = signal[:, lead_idx].astype(np.float64)
    # 整条记录滤波（0.5–45Hz 带通 + 50Hz 陷波），再切窗，避免短窗边缘效应
    sig = notch_filter(bandpass_filter(sig)).astype(np.float32)

    # 先筛出「心拍」标注（跳过节律变化/信号质量等非心拍标记）
    beat_samples, beat_labels = [], []
    for samp, symbol in zip(annotation.sample, annotation.symbol):
        cls = classify_beat_symbol(symbol)
        if cls is None:
            continue
        beat_samples.append(samp)
        beat_labels.append(cls)
    beat_samples = np.asarray(beat_samples, dtype=np.int64)
    if len(beat_samples) == 0:
        return None

    # RR 间期特征：pre = r_i - r_{i-1}，post = r_{i+1} - r_i（采样点）
    rr_sec = np.diff(beat_samples) / 360.0
    mean_rr = float(np.mean(rr_sec)) if len(rr_sec) else 1.0
    pre_rr = np.full(len(beat_samples), np.nan)
    post_rr = np.full(len(beat_samples), np.nan)
    pre_rr[1:] = rr_sec
    post_rr[:-1] = rr_sec

    windows, labels, peaks, rr_feat = [], [], [], []
    for i, (samp, cls) in enumerate(zip(beat_samples, beat_labels)):
        start, end = samp - PRE_R, samp + POST_R + 1  # 前 PRE_R + R峰本身 + 后 POST_R
        if start < 0 or end > len(sig):
            continue
        # 首尾缺少邻接 RR 时，用平均 RR 回退（避免 NaN）
        pr = pre_rr[i] if not np.isnan(pre_rr[i]) else mean_rr
        po = post_rr[i] if not np.isnan(post_rr[i]) else mean_rr
        # 裁剪到 [0.3, 3.0]：保留早搏(RR<1)/代偿间歇(RR>1)判别信息，抑制长间歇离群值
        windows.append(sig[start:end])
        labels.append(cls)
        peaks.append(samp)
        rr_feat.append([min(max(pr / mean_rr, 0.3), 3.0),
                        min(max(po / mean_rr, 0.3), 3.0)])

    if not windows:
        return None
    return (np.asarray(windows, dtype=np.float32),
            np.asarray(labels),
            np.asarray(peaks, dtype=np.int64),
            np.asarray(rr_feat, dtype=np.float32).reshape(-1, N_RR_FEATURES))
