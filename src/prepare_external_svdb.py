"""把 SVDB 记录重采样后并入训练/验证，测试集保持 MIT-BIH DS2。"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np
import wfdb
from scipy.signal import resample_poly

from config import BEAT_WINDOW, CLASS2IDX, FS, N_RR_FEATURES, PRE_R, POST_R, \
    PROCESSED_DIR, classify_beat_symbol
from preprocessing import bandpass_filter, notch_filter, zscore_normalize


def extract_svdb_record(base_dir: Path, rid: str):
    record = wfdb.rdrecord(str(base_dir / rid), channels=None)
    ann = wfdb.rdann(str(base_dir / rid), "atr")
    src_fs = int(record.fs)
    lead_idx = 0
    if "MLII" in record.sig_name:
        lead_idx = record.sig_name.index("MLII")
    sig = record.p_signal[:, lead_idx].astype(np.float64)
    if src_fs != FS:
        sig = resample_poly(sig, FS, src_fs).astype(np.float32)
    else:
        sig = sig.astype(np.float32)
    sig = notch_filter(bandpass_filter(sig, fs=FS)).astype(np.float32)

    beat_samples, beat_labels = [], []
    for samp, symbol in zip(ann.sample, ann.symbol):
        cls = classify_beat_symbol(symbol)
        if cls is None:
            continue
        beat_samples.append(int(round(samp * FS / src_fs)))
        beat_labels.append(cls)
    beat_samples = np.asarray(beat_samples, dtype=np.int64)
    if len(beat_samples) == 0:
        return None

    rr_sec = np.diff(beat_samples) / FS
    mean_rr = float(np.mean(rr_sec)) if len(rr_sec) else 1.0
    pre_rr = np.full(len(beat_samples), np.nan)
    post_rr = np.full(len(beat_samples), np.nan)
    pre_rr[1:] = rr_sec
    post_rr[:-1] = rr_sec

    windows, labels, peaks, rr_feat = [], [], [], []
    for i, (samp, cls) in enumerate(zip(beat_samples, beat_labels)):
        start, end = samp - PRE_R, samp + POST_R + 1
        if start < 0 or end > len(sig):
            continue
        pr = pre_rr[i] if not np.isnan(pre_rr[i]) else mean_rr
        po = post_rr[i] if not np.isnan(post_rr[i]) else mean_rr
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


def main(src_dir: str = str(PROCESSED_DIR),
         svdb_dir: str = "data_ext/svdb/mit-bih-supraventricular-arrhythmia-database-1.0.0",
         dst_dir: str = "processed_svdb",
         val_frac: float = 0.15) -> None:
    src = Path(src_dir)
    svdb = Path(svdb_dir)
    dst = Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)

    base_train = (np.load(src / "train_windows.npy"),
                  np.load(src / "train_labels.npy"),
                  np.load(src / "train_rrfeat.npy"))
    base_val = (np.load(src / "val_windows.npy"),
                np.load(src / "val_labels.npy"),
                np.load(src / "val_rrfeat.npy"))

    rids = wfdb.get_record_list("svdb")
    per_record = {}
    for rid in rids:
        # 本地缺失 .dat/.hea 或格式异常时跳过该记录，而不是中断整批预处理
        try:
            result = extract_svdb_record(svdb, rid)
        except Exception as exc:
            print(f"[skip] {rid}: {exc}")
            continue
        if result is None:
            print(f"[skip] {rid}")
            continue
        per_record[rid] = result
        print(f"{rid}: {len(result[0])} 拍")

    n_val = max(1, int(len(per_record) * val_frac))
    all_rids = sorted(per_record)
    val_rids = all_rids[-n_val:]
    train_rids = all_rids[:-n_val]

    def pack(rids):
        if not rids:
            return (np.empty((0, BEAT_WINDOW), dtype=np.float32),
                    np.empty((0,), dtype=np.int64),
                    np.empty((0, N_RR_FEATURES), dtype=np.float32))
        w = np.concatenate([per_record[r][0] for r in rids])
        l = np.concatenate([per_record[r][1] for r in rids])
        rr = np.concatenate([per_record[r][3] for r in rids])
        if rr.ndim == 1:
            rr = rr.reshape(-1, N_RR_FEATURES)
        return w, l, rr

    ext_tr_w, ext_tr_l, ext_tr_rr = pack(train_rids)
    ext_va_w, ext_va_l, ext_va_rr = pack(val_rids)

    def zscore_stack(w):
        # 空集时 np.stack([]) 会抛「need at least one array to stack」，显式返回空数组
        if len(w) == 0:
            return np.empty((0, BEAT_WINDOW), dtype=np.float32)
        return np.stack([zscore_normalize(x) for x in w])

    ext_tr_w = zscore_stack(ext_tr_w)
    ext_va_w = zscore_stack(ext_va_w)
    ext_tr_l = np.asarray([CLASS2IDX[c] for c in ext_tr_l], dtype=np.int64)
    ext_va_l = np.asarray([CLASS2IDX[c] for c in ext_va_l], dtype=np.int64)

    def join(a, b):
        b_rr = b[2]
        if b_rr.ndim == 1:
            b_rr = b_rr.reshape(-1, N_RR_FEATURES)
        return (np.concatenate([a[0], b[0]]),
                np.concatenate([a[1], b[1]]),
                np.concatenate([a[2], b_rr]))

    tr = join(base_train, (ext_tr_w, ext_tr_l, ext_tr_rr))
    va = join(base_val, (ext_va_w, ext_va_l, ext_va_rr))
    te = (np.load(src / "test_windows.npy"),
          np.load(src / "test_labels.npy"),
          np.load(src / "test_rrfeat.npy"))

    for name, arr in (("train", tr), ("val", va), ("test", te)):
        np.save(dst / f"{name}_windows.npy", arr[0].astype(np.float32))
        np.save(dst / f"{name}_labels.npy", arr[1].astype(np.int64))
        np.save(dst / f"{name}_rrfeat.npy", arr[2].astype(np.float32))

    for name, arr in (("train", tr), ("val", va), ("test", te)):
        dist = Counter(arr[1].tolist())
        print(name, "总", len(arr[1]),
              " ".join(f"{i}({dist.get(i,0)})" for i in range(5)))
    print(f"已保存到 {dst}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(PROCESSED_DIR))
    ap.add_argument("--svdb-dir",
                    default="data_ext/svdb/mit-bih-supraventricular-arrhythmia-database-1.0.0")
    ap.add_argument("--dst", default="processed_svdb")
    ap.add_argument("--val-frac", type=float, default=0.15)
    args = ap.parse_args()
    main(args.src, args.svdb_dir, args.dst, args.val_frac)
