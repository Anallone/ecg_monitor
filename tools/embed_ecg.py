"""从 MIT-BIH 记录截取一段 ECG，生成 C 数组嵌入固件（firmware/application/player/ecg_sample.h）。

用法:
    ./runtime/python/python.exe tools/embed_ecg.py --record 100 --start 100 --len 1800
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

from data_loader import load_record  # noqa: E402
from preprocessing import bandpass_filter, notch_filter  # noqa: E402


def main(record_id: int, start: int, length: int):
    signal, _, lead_idx = load_record(record_id)
    sig = signal[:, lead_idx].astype(np.float64)
    sig = notch_filter(bandpass_filter(sig)).astype(np.float32)
    seg = sig[start:start + length]
    seg = (seg - seg.mean()) / (seg.std() + 1e-6)

    out = (Path(__file__).resolve().parent.parent / "firmware" /
           "application" / "player" / "ecg_sample.h")
    lines = ["/* 自动生成：由 tools/embed_ecg.py 生成。 */",
             "#ifndef ECG_SAMPLE_H", "#define ECG_SAMPLE_H",
             f"#define ECG_SAMPLE_LEN {len(seg)}",
             f"static const float g_ecg_sample[{len(seg)}] = {{"]
    body = ", ".join(f"{float(x):.6g}f" for x in seg)
    lines.append("    " + body)
    lines.append("};")
    lines.append("#endif")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成 {out}（{len(seg)} 采样点，记录 {record_id}）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", type=int, default=100)
    ap.add_argument("--start", type=int, default=100)
    ap.add_argument("--len", type=int, default=1800)
    args = ap.parse_args()
    main(args.record, args.start, args.len)
