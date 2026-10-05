"""轻量 1D CNN 模型族。

- DepthwiseSeparableCNN：主线基线（深度可分离卷积，参数量极小）
- StandardCNN：对比基线（标准卷积）
- BinaryCNN：二值化网络（BinaryNet，用于横向对比）

所有模型输入为 (batch, 1, 187)，输出 5 类 logits。
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


def conv1d_out_len(length: int, kernel: int, stride: int = 1, pad: int = 0) -> int:
    return (length + 2 * pad - kernel) // stride + 1


class _SepConvBlock(nn.Module):
    """深度可分离卷积 + BN + ReLU。"""

    def __init__(self, cin: int, cout: int, kernel: int, stride: int = 1):
        super().__init__()
        self.depthwise = nn.Conv1d(cin, cin, kernel, stride=stride,
                                   padding=kernel // 2, groups=cin, bias=False)
        self.pointwise = nn.Conv1d(cin, cout, 1, bias=False)
        self.bn = nn.BatchNorm1d(cout)

    def forward(self, x):
        x = self.depthwise(x)
        x = self.pointwise(x)
        x = self.bn(x)
        return F.relu(x)


class _StdConvBlock(nn.Module):
    """标准卷积 + BN + ReLU。"""

    def __init__(self, cin: int, cout: int, kernel: int, stride: int = 1):
        super().__init__()
        self.conv = nn.Conv1d(cin, cout, kernel, stride=stride,
                              padding=kernel // 2, bias=False)
        self.bn = nn.BatchNorm1d(cout)

    def forward(self, x):
        return F.relu(self.bn(self.conv(x)))


class DepthwiseSeparableCNN(nn.Module):
    """主线轻量模型：约 2–4 层深度可分离卷积 + 全局池化 + 分类头。

    可选拼接 RR 间期特征（归一化 pre-RR / post-RR），在全局池化后与形态特征
    拼接后送入全连接层，用于判别早搏（S/V）等仅凭波形难以区分的类别。
    """

    def __init__(self, num_classes: int = 5, in_len: int = 187,
                 width: int = 24, dropout: float = 0.2, rr_features: bool = True):
        super().__init__()
        self.rr_features = rr_features
        self.blocks = nn.Sequential(
            _SepConvBlock(1, width, 9, stride=1),
            nn.MaxPool1d(2),
            nn.Dropout(dropout),
            _SepConvBlock(width, width * 2, 7, stride=1),
            nn.MaxPool1d(2),
            nn.Dropout(dropout),
            _SepConvBlock(width * 2, width * 4, 5, stride=1),
            nn.MaxPool1d(2),
            nn.Dropout(dropout),
        )
        fc_in = width * 4 + (2 if rr_features else 0)
        self.head = nn.Sequential(
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Dropout(dropout),
            nn.Linear(fc_in, num_classes),
        )

    def forward(self, x, rr=None):
        x = self.blocks(x)
        # 全局池化 + flatten + dropout（不含 Linear）
        x = self.head[:-1](x)
        if self.rr_features:
            if rr is None:
                # 与固件 ecg_infer.c 的 NULL 回退一致：缺 RR 时全部置 1.0
                # （= 平均 RR 归一化值，训练侧缺邻接 RR 同样回退 1.0）
                rr = torch.ones(x.shape[0], 2, dtype=x.dtype, device=x.device)
            x = torch.cat([x, rr], dim=1)
        return self.head[-1](x)


class StandardCNN(nn.Module):
    """标准 1D CNN 对比基线（参数更多）。"""

    def __init__(self, num_classes: int = 5, in_len: int = 187,
                 width: int = 16):
        super().__init__()
        self.blocks = nn.Sequential(
            _StdConvBlock(1, width, 9),
            nn.MaxPool1d(2),
            _StdConvBlock(width, width * 2, 7),
            nn.MaxPool1d(2),
            _StdConvBlock(width * 2, width * 4, 5),
            nn.MaxPool1d(2),
        )
        self.head = nn.Sequential(
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Linear(width * 4, num_classes),
        )

    def forward(self, x):
        return self.head(self.blocks(x))


class BinaryConv1d(nn.Module):
    """二值化卷积：权重与激活均二值化（STE 直通估计）。"""

    def __init__(self, cin: int, cout: int, kernel: int, stride: int = 1,
                 padding: int = 0):
        super().__init__()
        self.conv = nn.Conv1d(cin, cout, kernel, stride=stride,
                              padding=padding, bias=False)
        self.bn = nn.BatchNorm1d(cout)

    @staticmethod
    def _binarize(x: torch.Tensor) -> torch.Tensor:
        return torch.where(x >= 0, 1.0, -1.0)

    def forward(self, x):
        # 二值化权重（推断时用符号函数，训练时通过 STE 直通）
        if self.training:
            w = self._binarize(self.conv.weight)
            # STE：前向用二值，反向直接回传原始权重梯度
            w = self.conv.weight + (w - self.conv.weight).detach()
        else:
            w = self._binarize(self.conv.weight)
        x = self._binarize(x)
        x = F.conv1d(x, w, bias=None, stride=self.conv.stride,
                     padding=self.conv.padding)
        return self.bn(x)


class BinaryCNN(nn.Module):
    """二值化网络对比基线。"""

    def __init__(self, num_classes: int = 5, in_len: int = 187,
                 width: int = 32):
        super().__init__()
        self.blocks = nn.Sequential(
            BinaryConv1d(1, width, 9, padding=4),
            nn.MaxPool1d(2),
            BinaryConv1d(width, width * 2, 7, padding=3),
            nn.MaxPool1d(2),
            BinaryConv1d(width * 2, width * 4, 5, padding=2),
            nn.MaxPool1d(2),
        )
        self.head = nn.Sequential(
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Linear(width * 4, num_classes),
        )

    def forward(self, x):
        return self.head(self.blocks(x))


class _SE1d(nn.Module):
    """通道注意力（Squeeze-and-Excitation），只增加极少参数。"""

    def __init__(self, channels: int, reduction: int = 8):
        super().__init__()
        hidden = max(8, channels // reduction)
        self.fc1 = nn.Conv1d(channels, hidden, 1)
        self.fc2 = nn.Conv1d(hidden, channels, 1)

    def forward(self, x):
        s = F.adaptive_avg_pool1d(x, 1)
        s = torch.sigmoid(self.fc2(F.relu(self.fc1(s))))
        return x * s


class _ResSepBlock(nn.Module):
    """深度可分离卷积 + BN + ReLU + 通道注意力，可选残差连接。"""

    def __init__(self, cin: int, cout: int, kernel: int, stride: int = 1,
                 residual: bool = False, se: bool = True):
        super().__init__()
        self.depthwise = nn.Conv1d(cin, cin, kernel, stride=stride,
                                   padding=kernel // 2, groups=cin, bias=False)
        self.pointwise = nn.Conv1d(cin, cout, 1, bias=False)
        self.bn = nn.BatchNorm1d(cout)
        self.se = _SE1d(cout) if se else nn.Identity()
        self.residual = bool(residual and cin == cout and stride == 1)

    def forward(self, x):
        y = self.se(F.relu(self.bn(self.pointwise(self.depthwise(x)))))
        return F.relu(y + x) if self.residual else y


class ResSECNN(nn.Module):
    """残差 + 通道注意力的深度可分离 CNN（<50 KB 约束内尽可能先进）。

    相对基线 DepthwiseSeparableCNN 的增强：
      - 残差连接：缓解深层梯度衰减，允许堆叠更多卷积块；
      - 通道注意力（SE）：以极少参数实现特征重标定；
      - 更大的首层卷积核（k=11）与更宽的通道（28/56/112），扩大感受野与容量。
    实测参数量 37,270，int8 部署体积约 40.2 KB，仍低于 50 KB 约束。
    """

    rr_features = True

    def __init__(self, num_classes: int = 5, in_len: int = 187,
                 width: int = 28, dropout: float = 0.2, n_rr: int = 2):
        super().__init__()
        self.n_rr = n_rr
        w = width
        self.stem = _ResSepBlock(1, w, 11, se=False)
        self.blocks = nn.Sequential(
            nn.MaxPool1d(2), nn.Dropout(dropout),
            _ResSepBlock(w, w, 7, residual=True),
            _ResSepBlock(w, w, 7, residual=True),
            nn.MaxPool1d(2), nn.Dropout(dropout),
            _ResSepBlock(w, w * 2, 5),
            _ResSepBlock(w * 2, w * 2, 5, residual=True),
            nn.MaxPool1d(2), nn.Dropout(dropout),
            _ResSepBlock(w * 2, w * 4, 3),
            _ResSepBlock(w * 4, w * 4, 3, residual=True),
        )
        self.head = nn.Sequential(
            nn.AdaptiveAvgPool1d(1),
            nn.Flatten(),
            nn.Dropout(dropout),
            nn.Linear(w * 4 + n_rr, num_classes),
        )
        # RR 间期旁路：早搏（S/V）形态接近 N，唯一可靠判据是 RR 间期（早搏 RR 短、
        # 代偿间歇长）。给 RR 一条直达 logits 的旁路，避免被 112 维形态特征淹没——
        # 否则 S 类（形态≈N）会被整体压成 N。旁路输出与主 head 相加，与
        # export_c_model.py / ecg_infer.c 的 rr_head 实现严格对应。
        self.rr_head = nn.Sequential(
            nn.Linear(n_rr, 16),
            nn.ReLU(),
            nn.Linear(16, num_classes),
        )
        # 先验漂移校正（logit adjustment）：训练集与部署集的类别先验不同——起搏
        # 记录全部并入训练集，使训练集 Q 占 13.4%、F 占 2.6%，而验证集 Q 为 0、
        # F 仅 0.2%。模型因此朝训练先验过度预测 F/Q（F 的精确率仅 0.0012）。
        # 这里加一个逐类常数偏置 b_c = -log(pi_train_c / pi_val_c)，把 logits 校回
        # 部署分布。由 train.py 按 train/val 标签统计写入（不触碰测试集），随
        # state_dict 一起保存，导出时折进 fc_b，保证 Python 与固件行为一致。
        self.register_buffer("logit_bias", torch.zeros(num_classes))

    def forward(self, x, rr=None):
        x = self.blocks(self.stem(x))
        x = self.head[:-1](x)
        if rr is None:
            # 无 RR 输入：与固件 ecg_infer.c 的 NULL 回退严格一致——rr_ext 全部置
            # 1.0（= 平均 RR 归一化值，训练侧缺邻接 RR 同样回退 1.0），且跳过
            # rr_head（固件在 rr_feat==NULL 时不走旁路）。
            rr = torch.ones(x.shape[0], self.n_rr, dtype=x.dtype, device=x.device)
            return self.head[-1](torch.cat([x, rr], dim=1)) + self.logit_bias
        if self.n_rr == 4 and rr.shape[1] == 2:
            rr = _extend_rr(rr)
        return (self.head[-1](torch.cat([x, rr], dim=1))
                + self.rr_head(rr) + self.logit_bias)


def _extend_rr(rr: torch.Tensor) -> torch.Tensor:
    """把 2 维 RR 特征扩展为 4 维：pre、post、pre/post、post-pre。"""
    pre = rr[:, 0:1]
    post = rr[:, 1:2]
    ratio = (pre / (post + 1e-6)).clamp(0.3, 3.0)
    diff = post - pre
    return torch.cat([rr, ratio, diff], dim=1)


class ResSECNNRR4(ResSECNN):
    """RR 特征扩展版：在 ResSECNN 基础上额外输入 pre/post 与 post-pre。"""

    def __init__(self, num_classes: int = 5, in_len: int = 187,
                 width: int = 28, dropout: float = 0.2):
        super().__init__(num_classes=num_classes, in_len=in_len,
                         width=width, dropout=dropout, n_rr=4)


MODEL_REGISTRY = {
    "ds_cnn": DepthwiseSeparableCNN,
    "std_cnn": StandardCNN,
    "binary_cnn": BinaryCNN,
    "res_se_cnn": ResSECNN,
    "res_se_cnn_rr4": ResSECNNRR4,
}


def build_model(name: str, num_classes: int = 5) -> nn.Module:
    if name not in MODEL_REGISTRY:
        raise ValueError(f"未知模型: {name}，可选 {list(MODEL_REGISTRY)}")
    return MODEL_REGISTRY[name](num_classes=num_classes)


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
