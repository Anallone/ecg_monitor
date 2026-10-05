"""预处理流水线：下载 -> 提取心拍 -> 去噪 -> 归一化 -> 划分 -> 缓存 numpy。

用法:
    python prepare_data.py --download        # 首次运行：下载 MIT-BIH
    python prepare_data.py                    # 仅做预处理（数据已下载）
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from config import (BEAT_WINDOW, CLASS2IDX, DS1, DS2, FS, POST_R, PRE_R,
                    PROCESSED_DIR, RECORD_IDS, map_symbol_to_class)
from data_loader import download_record, extract_beats
from preprocessing import bandpass_filter, notch_filter, zscore_normalize


def assign_split(record_id: int) -> str:
    """AAMI 推荐：DS1 训练、DS2 测试；其余（起搏记录 102/104/107/217）并入训练集。"""
    if record_id in DS1:
        return "train"
    if record_id in DS2:
        return "test"
    return "train"


def main_record_level(download: bool):
    """按记录粒度收集，DS1 内部切 85% 训练 / 15% 验证。"""
    per_record = {}  # rid -> (windows, labels, rr_feat)

    for rid in RECORD_IDS:
        if download:
            if not download_record(rid):
                print(f"[skip] record {rid} 下载失败")
                continue
        try:
            result = extract_beats(rid)
        except Exception as exc:
            # 单条记录读盘/解析异常（缺失 .dat、标注损坏等）跳过，不中断整批预处理
            print(f"[skip] record {rid} 读取失败: {exc}")
            continue
        if result is None:
            print(f"[skip] record {rid} 无有效心拍")
            continue
        windows, labels, peaks, rr_feat = result
        per_record[rid] = (windows, labels, rr_feat)
        print(f"record {rid:3d}: {len(windows)} 拍")

    # 记录级划分
    train_rids = [r for r in RECORD_IDS if assign_split(r) == "train" and r in per_record]
    test_rids = [r for r in RECORD_IDS if assign_split(r) == "test" and r in per_record]
    # DS1 内部再切 85/15 作为验证（记录粒度，保持与测试集分布一致）。
    # 说明：DS1 不含起搏记录，故 val 的 Q 类天然偏少；少数类由损失权重 + 训练驱动，
    # Q 的泛化在测试集上单独报告。
    n_val = max(1, int(len(train_rids) * 0.15))
    val_rids = train_rids[-n_val:]
    train_rids = train_rids[:-n_val]

    def pack(rids):
        w = np.concatenate([per_record[r][0] for r in rids])
        l = np.concatenate([per_record[r][1] for r in rids])
        rr = np.concatenate([per_record[r][2] for r in rids])
        return w, l, rr

    tr_w, tr_l, tr_rr = pack(train_rids)
    va_w, va_l, va_rr = pack(val_rids)
    te_w, te_l, te_rr = pack(test_rids)

    # 归一化（逐拍 z-score）
    tr_w = np.stack([zscore_normalize(x) for x in tr_w])
    va_w = np.stack([zscore_normalize(x) for x in va_w])
    te_w = np.stack([zscore_normalize(x) for x in te_w])

    # 标签 -> 下标
    tr_l = np.asarray([CLASS2IDX[c] for c in tr_l], dtype=np.int64)
    va_l = np.asarray([CLASS2IDX[c] for c in va_l], dtype=np.int64)
    te_l = np.asarray([CLASS2IDX[c] for c in te_l], dtype=np.int64)

    np.save(PROCESSED_DIR / "train_windows.npy", tr_w)
    np.save(PROCESSED_DIR / "train_labels.npy", tr_l)
    np.save(PROCESSED_DIR / "val_windows.npy", va_w)
    np.save(PROCESSED_DIR / "val_labels.npy", va_l)
    np.save(PROCESSED_DIR / "test_windows.npy", te_w)
    np.save(PROCESSED_DIR / "test_labels.npy", te_l)
    # RR 间期特征（与 windows/labels 同序）
    np.save(PROCESSED_DIR / "train_rrfeat.npy", tr_rr.astype(np.float32))
    np.save(PROCESSED_DIR / "val_rrfeat.npy", va_rr.astype(np.float32))
    np.save(PROCESSED_DIR / "test_rrfeat.npy", te_rr.astype(np.float32))

    # 缓存元信息：记录生成缓存时的关键配置，供训练侧校验缓存是否过期
    # （例如改了 BEAT_WINDOW / 划分规则后没重新跑 prepare_data.py）。
    meta = {
        "generated_by": "prepare_data.py",
        "fs": FS,
        "beat_window": BEAT_WINDOW,
        "pre_r": PRE_R,
        "post_r": POST_R,
        "num_classes": len(CLASS2IDX),
        "split": {"train": sorted(train_rids), "val": sorted(val_rids),
                  "test": sorted(test_rids)},
    }
    with open(PROCESSED_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 类别分布统计
    from collections import Counter
    print("\n=== 划分结果 ===")
    for name, lab in (("train", tr_l), ("val", va_l), ("test", te_l)):
        dist = Counter(lab.tolist())
        total = len(lab)
        print(f"{name:6s}: 总 {total:6d}  " +
              "  ".join(f"{i}({dist.get(i, 0)})" for i in range(5)))
    print(f"\n已保存到 {PROCESSED_DIR}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="下载 MIT-BIH 数据")
    args = ap.parse_args()
    main_record_level(download=args.download)
