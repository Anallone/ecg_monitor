"""从 MIT-BIH 记录导出多个 ECG 样本为固件可读的二进制文件（供 SD 卡播放）。

用途：第一步端侧功能——把多份样本预先导出到 SD 卡，固件扫描目录、触摸选择、
流式播放处理并显示。信号在 Python 端完成滤波与归一化，固件端只做检测+分类。

二进制格式（小端）：
    偏移  类型      内容
    0     char[4]   magic = "ECG1"
    4     int32     n（采样点数）
    8     int32     fs（=360）
    12    int16[n]  信号，已滤波 + 整段 z-score，定标 scale（固件读入后 /scale 还原 float）

文件名：S<record>.BIN（FAT 短名 8.3；固件侧 CONFIG_FATFS_LFN_NONE=y，必须短名大写）

用法（项目根目录）：
    ./runtime/python/python.exe tools/export_samples.py
    ./runtime/python/python.exe tools/export_samples.py --records 100,200 --len 7200
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

from data_loader import load_record  # noqa: E402
from preprocessing import bandpass_filter, notch_filter  # noqa: E402

MAGIC = b"ECG1"
FS = 360


def build_sample(record_id: int, start: int, length: int, scale: int) -> np.ndarray:
    """读记录 -> 滤波 -> 截取 -> 整段 z-score -> 定标为 int16。

    与 tools/embed_ecg.py 的处理保持一致（整条记录滤波后截取、整段归一化），
    使固件端拿到的是「已去噪、零均值单位方差」的信号，可直接做 R 峰检测。
    """
    signal, _, lead_idx = load_record(record_id)
    sig = signal[:, lead_idx].astype(np.float64)
    sig = notch_filter(bandpass_filter(sig))
    seg = sig[start:start + length]
    if len(seg) == 0:
        raise ValueError(f"记录 {record_id} 在 start={start} 处无数据")
    seg = (seg - seg.mean()) / (seg.std() + 1e-6)
    # 定标到 int16；clip 防止极端值溢出（z-score 后 |x|>16 极罕见）
    q = np.clip(seg * scale, -32768, 32767).astype(np.int16)
    return q


def write_sample(path: Path, data: np.ndarray, fs: int = FS) -> None:
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<ii", len(data), fs))
        f.write(data.astype("<i2").tobytes())


def main(records: list[int], start: int, length: int, scale: int, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for rid in records:
        try:
            data = build_sample(rid, start, length, scale)
        except Exception as exc:  # noqa: BLE001
            print(f"[跳过] record {rid}: {exc}")
            continue
        name = f"S{rid}.BIN"
        path = out_dir / name
        write_sample(path, data)
        size = path.stat().st_size
        print(f"已导出 {path}  记录={rid}  {len(data)} 点 ({len(data)/FS:.1f} s)  "
              f"{size} 字节  scale={scale}")
        ok += 1
    print(f"\n完成：{ok}/{len(records)} 个样本 -> {out_dir}")
    if ok:
        print("把该目录下的 .BIN 文件拷贝到 microSD 卡根目录（FAT32）即可。")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="导出 ECG 样本供固件 SD 卡播放")
    ap.add_argument("--records", default="100,200,210,232",
                    help="逗号分隔的 MIT-BIH 记录号（默认 100,200,210,232）")
    ap.add_argument("--start", type=int, default=0, help="起始采样点（默认 0）")
    ap.add_argument("--len", type=int, default=10800, help="长度（默认 10800 = 30 秒 @360Hz）")
    ap.add_argument("--scale", type=int, default=2000, help="int16 定标系数（默认 2000）")
    ap.add_argument("--out", default="sdcard", help="输出目录（默认 sdcard/）")
    args = ap.parse_args()

    recs = [int(x) for x in args.records.replace(" ", "").split(",") if x]
    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out
    sys.exit(main(recs, args.start, args.len, args.scale, out))
