"""心拍数据集：加载预处理缓存，按记录划分，支持加权采样/过采样。"""
from __future__ import annotations

import numpy as np
import torch
from torch.utils.data import Dataset

from config import CLASS2IDX, CLASSES


class BeatDataset(Dataset):
    """封装 (windows, labels[, rr_feat]) 的 PyTorch Dataset。

    windows: (n, seq_len)
    labels:  (n,) int 类别下标
    rr_feat: (n, 2) 归一化 pre-RR / post-RR（可选，None 时用 0 填充）
    """

    def __init__(self, windows: np.ndarray, labels: np.ndarray,
                 rr_feat: np.ndarray | None = None):
        self.windows = torch.as_tensor(windows, dtype=torch.float32).unsqueeze(1)
        self.labels = torch.as_tensor(labels, dtype=torch.long)
        if rr_feat is None:
            rr_feat = np.zeros((len(windows), 2), dtype=np.float32)
        self.rr_feat = torch.as_tensor(rr_feat, dtype=torch.float32)

    def __len__(self):
        return len(self.windows)

    def __getitem__(self, idx):
        return self.windows[idx], self.rr_feat[idx], self.labels[idx]


def _augment_beat(x: torch.Tensor, rng: np.random.Generator) -> torch.Tensor:
    """单拍数据增强：幅度缩放 + 时间抖动 ±5 采样 + 高斯噪声（仅训练集）。"""
    x = x.clone()

    # 1. 幅度缩放 0.8~1.2（模拟电极阻抗变化）
    scale = float(rng.uniform(0.8, 1.2))
    x = x * scale

    # 2. 时间抖动：随机平移 ±5 采样，边缘填 0
    shift = int(rng.integers(-5, 6))
    if shift != 0:
        x = torch.roll(x, shift, dims=-1)
        if shift > 0:
            x[..., :shift] = 0
        else:
            x[..., shift:] = 0

    # 3. 高斯噪声
    noise_std = float(rng.uniform(0.0, 0.1))
    if noise_std > 0:
        x = x + torch.randn_like(x) * noise_std
    return x


class AugmentedBeatDataset(BeatDataset):
    """带在线数据增强的心拍数据集（对抗跨记录过拟合）。

    增强：随机幅度缩放（模拟电极阻抗变化）、随机时间抖动（±5 采样）、
    随机高斯噪声。仅作用于训练集。
    """

    def __init__(self, windows: np.ndarray, labels: np.ndarray,
                 rr_feat: np.ndarray | None = None, seed: int = 0):
        super().__init__(windows, labels, rr_feat)
        self.rng = np.random.default_rng(seed)

    def __getitem__(self, idx):
        x, rr, y = self.windows[idx], self.rr_feat[idx], self.labels[idx]  # x: (1, L)
        return _augment_beat(x, self.rng), rr, y


class AugmentedBalancedBeatDataset(BeatDataset):
    """增强 + 均匀过采样：每类采样到相同目标数 target，且逐拍做数据增强。

    应对 train/test 类别分布偏移（起搏记录全在训练集 → Q 训练 7112 但测试仅 142；
    S 训练仅 793 但测试 2050）：少数类重复采样到 target、多数类随机下采样到
    target，再叠加幅度/抖动/噪声增强，避免过采样导致的死记硬背。
    """

    def __init__(self, windows: np.ndarray, labels: np.ndarray,
                 rr_feat: np.ndarray | None = None, seed: int = 0,
                 target: int = 12000):
        super().__init__(windows, labels, rr_feat)
        self.rng = np.random.default_rng(seed)
        labels_np = np.asarray(labels)
        self.class_indices = [np.where(labels_np == c)[0] for c in range(len(CLASSES))]
        # 空类无法过采样：旧代码回退到样本 0，会以「该槽位类别」之名喂入他类样本、
        # 悄悄污染类别分布。这里在构造时快速失败，而不是静默产出错误样本。
        missing = [CLASSES[c] for c, ix in enumerate(self.class_indices) if len(ix) == 0]
        if missing:
            raise ValueError(f"均匀过采样要求每类都有样本，缺失类别: {missing}")
        self.target = target

    def __len__(self):
        return self.target * len(CLASSES)

    def __getitem__(self, idx):
        class_idx = idx % len(CLASSES)
        within = idx // len(CLASSES)
        indices = self.class_indices[class_idx]
        real_idx = indices[within % len(indices)]
        x, rr, y = self.windows[real_idx], self.rr_feat[real_idx], self.labels[real_idx]
        return _augment_beat(x, self.rng), rr, y


class AugmentedOversampleDataset(BeatDataset):
    """增强 + 目标化过采样：少数类垫高到 floor，多数类保持原频率。

    只补「真正欠采样」的少数类（本数据 S：train 793 vs test 2050），不像
    AugmentedBalancedBeatDataset 那样把多数类也压到同数——那样会丢多数类多样性、
    还额外过采样本就过多的 Q。多数类（N）保持原数量，少数类上采样到 floor。
    """

    def __init__(self, windows: np.ndarray, labels: np.ndarray,
                 rr_feat: np.ndarray | None = None, seed: int = 0,
                 floor: int = 8000):
        super().__init__(windows, labels, rr_feat)
        self.rng = np.random.default_rng(seed)
        labels_np = np.asarray(labels)
        self.class_indices = [np.where(labels_np == c)[0] for c in range(len(CLASSES))]
        counts = [len(ix) for ix in self.class_indices]
        self.per_class_n = [max(c, floor) if c > 0 else 0 for c in counts]
        self.offsets = np.concatenate([[0], np.cumsum(self.per_class_n)]).astype(np.int64)

    def __len__(self):
        return int(sum(self.per_class_n))

    def __getitem__(self, idx):
        class_idx = int(np.searchsorted(self.offsets, idx, side="right") - 1)
        within = idx - self.offsets[class_idx]
        indices = self.class_indices[class_idx]
        if len(indices) == 0:
            real_idx = 0
        else:
            real_idx = indices[within % len(indices)]
        x, rr, y = self.windows[real_idx], self.rr_feat[real_idx], self.labels[real_idx]
        return _augment_beat(x, self.rng), rr, y


class BalancedBeatDataset(BeatDataset):
    """带类别平衡采样（过采样少数类）的数据集封装。"""

    def __init__(self, windows: np.ndarray, labels: np.ndarray,
                 rr_feat: np.ndarray | None = None):
        super().__init__(windows, labels, rr_feat)
        labels_np = np.asarray(labels)
        self.class_indices = [np.where(labels_np == c)[0] for c in range(len(CLASSES))]
        # 空类同 AugmentedBalancedBeatDataset：快速失败而非静默喂入他类样本。
        missing = [CLASSES[c] for c, ix in enumerate(self.class_indices) if len(ix) == 0]
        if missing:
            raise ValueError(f"类别平衡采样要求每类都有样本，缺失类别: {missing}")
        self.max_class_count = max(len(ix) for ix in self.class_indices)

    def __len__(self):
        return self.max_class_count * len(CLASSES)

    def __getitem__(self, idx):
        class_idx = idx % len(CLASSES)
        within = idx // len(CLASSES)
        real_idx = self.class_indices[class_idx][within % len(self.class_indices[class_idx])]
        return self.windows[real_idx], self.rr_feat[real_idx], self.labels[real_idx]


def labels_to_idx(labels: np.ndarray) -> np.ndarray:
    """字符串标签 -> 类别下标。"""
    return np.asarray([CLASS2IDX[x] for x in labels], dtype=np.int64)
