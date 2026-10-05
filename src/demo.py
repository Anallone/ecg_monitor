"""命令行离线演示：加载一条记录 -> 滤波 -> R峰检测 -> 逐拍分类 -> 打印统计。

用法（在项目根目录）:
    ./runtime/python/python.exe src/demo.py --record 100 --model res_se_cnn_rr4
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from config import MODEL_DIR  # noqa: E402
from data_loader import load_record  # noqa: E402
from realtime import BeatClassifier, RealtimeEngine  # noqa: E402


def demo(record_id: int, model_name: str):
    signal, _, lead_idx = load_record(record_id)
    sig = signal[:, lead_idx].astype(np.float64)

    clf = BeatClassifier(model_name, MODEL_DIR / f"{model_name}_best.pt")
    engine = RealtimeEngine(clf)
    # 引擎内部统一完成「滤波 -> R峰检测 -> 逐拍分类」，避免与训练侧不一致
    events = engine.process(sig)

    print(f"记录 {record_id}: {len(sig)} 采样点, 检测到 {len(events)} 个 R 峰")

    cnt = Counter(e["class"] for e in events)
    print("分类统计:", {k: cnt.get(k, 0) for k in ["N", "S", "V", "F", "Q"]})

    alarms = [e for e in events if e["alarm"]]
    print(f"报警事件: {len(alarms)} 次")
    for e in alarms[:10]:
        print(f"  R峰@{e['r_peak']}  {e['class']}  心率={e['hr']:.1f}  "
              f"报警={'心动过速' if e['alarm']=='high' else '心动过缓'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", type=int, default=100)
    ap.add_argument("--model", default="res_se_cnn_rr4")
    args = ap.parse_args()
    demo(args.record, args.model)
