"""可加载的数据集清单。

对应端侧「演示模式」的样本列表——那边扫 SD 卡上的 S*.BIN，这边扫：
  - data/      下的 MIT-BIH 原始记录（48 条，连续信号，适合流式分析）
  - sdcard/    下导出给固件的 .BIN 样本（与固件同一格式，便于两端对照）

GUI 通过 available_datasets() 拿到 [(显示名, 加载函数)] 列表渲染成下拉框。
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Callable

import numpy as np

from config import DATA_DIR, ROOT

SDCARD_DIR = ROOT / "sdcard"
SAMPLE_SCALE = 2000.0      # 与固件 SDCARD_SAMPLE_SCALE 一致
SAMPLE_MAGIC = b"ECG1"


def list_mitbih_records() -> list[int]:
    """data/ 下实际存在的 MIT-BIH 记录号（按 .hea 文件判断，避免列出未下载的）。"""
    if not DATA_DIR.exists():
        return []
    ids: list[int] = []
    for hea in DATA_DIR.glob("*.hea"):
        try:
            ids.append(int(hea.stem))
        except ValueError:
            continue                # 跳过非记录名的 .hea
    return sorted(ids)


def list_bin_samples() -> list[str]:
    """sdcard/ 下导出给固件的样本文件名。"""
    if not SDCARD_DIR.exists():
        return []
    return sorted(p.name for p in SDCARD_DIR.glob("*.BIN"))


def load_mitbih(record_id: int) -> np.ndarray:
    """读一条 MIT-BIH 记录的优先导联，返回原始 float 信号。

    注意返回的是**未滤波**的原始信号：RealtimeEngine.process() 内部会做
    带通 + 陷波，与端侧「样本已预滤波」的约定不同，这里保持与训练/评估一致。
    """
    from data_loader import load_record

    signal, _, lead_idx = load_record(record_id)
    return signal[:, lead_idx].astype(np.float32)


def load_bin(name: str) -> np.ndarray:
    """读 sdcard/ 下导出给固件的 .BIN（ECG1 + n + fs + int16），返回 float 信号。

    这些样本在导出时**已滤波并做过整段 z-score**（见 tools/export_samples.py），
    再送进 process() 会被二次滤波——幅度略有变化但不影响分类演示。
    """
    path = SDCARD_DIR / name
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != SAMPLE_MAGIC:
            raise ValueError(f"{name} 不是有效的 ECG 样本（magic={magic!r}）")
        n, _fs = struct.unpack("<ii", f.read(8))
        raw = np.frombuffer(f.read(n * 2), dtype="<i2")
    if len(raw) != n:
        raise ValueError(f"{name} 数据不完整：期望 {n} 点，实得 {len(raw)}")
    return raw.astype(np.float32) / SAMPLE_SCALE


def available_datasets() -> list[tuple[str, Callable[[], np.ndarray]]]:
    """返回 [(显示名, 加载函数)]，供 GUI 下拉框使用。

    加载函数延迟到用户点「加载」时才执行——MIT-BIH 记录读盘较慢，
    不该在窗口初始化时把 48 条全读一遍。
    """
    out: list[tuple[str, Callable[[], np.ndarray]]] = []
    for rid in list_mitbih_records():
        out.append((f"MIT-BIH 记录 {rid}", lambda r=rid: load_mitbih(r)))
    for name in list_bin_samples():
        out.append((f"SD 样本 {name}", lambda n=name: load_bin(n)))
    return out
