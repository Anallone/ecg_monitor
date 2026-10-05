"""训练入口：加载预处理数据 -> 训练 -> 评估 -> 保存权重与指标。"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.metrics import f1_score
from torch.utils.data import DataLoader

from config import (BATCH_SIZE, CLASSES, EPOCHS, LR, MODEL_DIR,
                    MODEL_INPUT_LEN, POST_R, PRE_R, PROCESSED_DIR, SEED)
from dataset import (AugmentedBalancedBeatDataset, AugmentedBeatDataset,
                    AugmentedOversampleDataset, BalancedBeatDataset,
                    BeatDataset)
from models import build_model, count_parameters

torch.manual_seed(SEED)
np.random.seed(SEED)


def load_split(split: str, base_dir=PROCESSED_DIR):
    """读取预处理的 {split} 数据。返回 (windows, labels, rr_feat)。"""
    base_dir = Path(base_dir)
    win = np.load(base_dir / f"{split}_windows.npy")
    lab = np.load(base_dir / f"{split}_labels.npy")
    rr = None
    rr_path = base_dir / f"{split}_rrfeat.npy"
    if rr_path.exists():
        rr = np.load(rr_path)
    return win, lab, rr


def check_cache_config(base_dir) -> None:
    """校验预处理缓存与当前配置是否一致（prepare_data.py 会写 meta.json 记录关键参数）。

    改了 BEAT_WINDOW / PRE_R / POST_R / 类别数却没重新生成缓存时，缓存与配置会
    静默错位（例如窗口长度变了仍用旧窗口训练）；这里只告警，不阻断。
    """
    meta_path = Path(base_dir) / "meta.json"
    if not meta_path.exists():
        return  # 旧版本缓存没有 meta.json，无法校验
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for key, expect in (("beat_window", MODEL_INPUT_LEN),
                        ("pre_r", PRE_R),
                        ("post_r", POST_R),
                        ("num_classes", len(CLASSES))):
        if meta.get(key) is not None and meta.get(key) != expect:
            print(f"[warn] 预处理缓存与当前配置不一致：缓存 {key}={meta.get(key)}，"
                  f"当前配置 {key}={expect}，建议重新运行 prepare_data.py")


def _has_rr(model) -> bool:
    return bool(getattr(model, "rr_features", False))


def class_balanced_weights(class_counts, beta: float = 0.9999) -> np.ndarray:
    """有效样本数类别权重（Cui et al. 2019，Class-Balanced Loss）。

    w_c = (1 - beta) / (1 - beta^{n_c})，随类别样本数平滑变化、上限有界。
    相比 sqrt(max/count)+cap，它对 Q 类（300:1 不平衡）补偿更充分且不会
    因截断让 S/F/Q 都被压到同一个 3.0 上限。
    """
    counts = np.asarray(class_counts, dtype=np.float64)
    counts = np.where(counts <= 0, 1.0, counts)
    eff_num = (1.0 - beta ** counts) / (1.0 - beta)
    w = 1.0 / eff_num
    w = w / w.sum() * len(counts)          # 归一化到均值为 1，稳定 loss 尺度
    return w.astype(np.float32)


def focal_loss(logits: torch.Tensor, targets: torch.Tensor,
               alpha: torch.Tensor | None = None,
               gamma: float = 2.0) -> torch.Tensor:
    """Focal Loss（Lin et al. 2017）。

    多数类样本易分，梯度被 (1-p)^γ 压低；少数类样本难分，仍保留接近全额的
    梯度，从而把优化重点从「N 对 N 的万无一失」转到「S/F/Q 别再被吞掉」。
    alpha 用类别权重进一步平衡。
    """
    ce = F.cross_entropy(logits, targets, reduction="none")
    pt = torch.exp(-ce)                    # 预测正确类别的 softmax 概率
    w = (1.0 - pt) ** gamma
    if alpha is not None:
        w = w * alpha[targets]
    return w.mean()


def prior_drift_bias(counts_train, counts_val, num_classes: int) -> np.ndarray:
    """先验漂移校正（logit adjustment）：b_c = -log(pi_train_c / pi_val_c)。

    训练集与部署集类别先验不一致时（本工程把起搏记录并入训练集，训练集 Q 占
    13.4% 而验证集 0；F 训练 2.6% 而验证 0.2%），模型会朝训练先验过度预测这些
    类。在 logits 上加一个逐类常数即等价于把预测分布校回部署先验。
    验证集某类样本为 0 时无法估计其先验，该类不校正（偏置保持 0）。
    """
    pt = np.asarray(counts_train, dtype=np.float64)
    pv = np.asarray(counts_val, dtype=np.float64)
    # 防短数组：验证集某类样本为 0 时 np.bincount 可能比 num_classes 短（如本数据
    # val 无 Q 类），不补齐会在循环里越界。
    if len(pt) < num_classes:
        pt = np.pad(pt, (0, num_classes - len(pt)))
    if len(pv) < num_classes:
        pv = np.pad(pv, (0, num_classes - len(pv)))
    pt = pt / pt.sum()
    pv = pv / pv.sum()
    b = np.zeros(num_classes, dtype=np.float32)
    for c in range(num_classes):
        if pt[c] > 0 and pv[c] > 0:
            b[c] = -np.log(pt[c] / pv[c])
    return b


def effective_class_counts(train_ds, y_tr_idx, num_classes: int) -> np.ndarray:
    """训练集「有效」类别频数：过采样/下采样后实际参与训练的分布。"""
    per = getattr(train_ds, "per_class_n", None)        # AugmentedOversampleDataset
    if per is not None:
        return np.asarray(per, dtype=np.float64)
    tgt = getattr(train_ds, "target", None)             # AugmentedBalancedBeatDataset
    if tgt is not None:
        return np.full(num_classes, float(tgt), dtype=np.float64)
    mx = getattr(train_ds, "max_class_count", None)     # BalancedBeatDataset
    if mx is not None:
        return np.full(num_classes, float(mx), dtype=np.float64)
    return np.bincount(y_tr_idx, minlength=num_classes).astype(np.float64)


def evaluate(model, loader, device, bias=None):
    model.eval()
    preds, trues = [], []
    with torch.no_grad():
        for x, rr, y in loader:
            x = x.to(device)
            if _has_rr(model):
                rr = rr.to(device)
                out = model(x, rr)
            else:
                out = model(x)
            if bias is not None:
                out = out + bias
            preds.append(out.argmax(dim=1).cpu())
            trues.append(y.cpu())
    preds = torch.cat(preds).numpy()
    trues = torch.cat(trues).numpy()
    return preds, trues


def collect_logits(model, loader, device):
    """收集验证集 logits 与标签，用于训练后 logit 偏置校准。"""
    model.eval()
    logits_list, trues_list = [], []
    with torch.no_grad():
        for x, rr, y in loader:
            x = x.to(device)
            if _has_rr(model):
                rr = rr.to(device)
                out = model(x, rr)
            else:
                out = model(x)
            logits_list.append(out.cpu())
            trues_list.append(y.cpu())
    return torch.cat(logits_list).numpy(), torch.cat(trues_list).numpy()


def _calibration_score(preds, trues, metric: str) -> float:
    if metric == "acc":
        return float((preds == trues).mean())
    if metric == "balanced":
        acc = float((preds == trues).mean())
        f1 = float(f1_score(trues, preds, average="macro", zero_division=0))
        return 0.6 * acc + 0.4 * f1
    return float(f1_score(trues, preds, average="macro", zero_division=0))


def _selection_score(acc: float, f1: float, metric: str) -> float:
    """训练早停/选模使用的标量分数。"""
    if metric == "acc":
        return acc
    if metric == "balanced":
        return 0.6 * acc + 0.4 * f1
    return f1


def calibrate_logit_bias(logits: np.ndarray, trues: np.ndarray,
                         base_bias: np.ndarray, metric: str = "acc",
                         grid_lo: float = -4.0, grid_hi: float = 4.0,
                         step: float = 0.05, rounds: int = 2) -> np.ndarray:
    """在验证集上对逐类 logit 偏置做坐标上升，提升 acc 或 macro F1。

    只使用 train/val 划分（绝不接触 test），等价于部署前做阈值/先验校准。
    返回 base_bias + delta，训练脚本会把它写入 checkpoint 的 logit_bias，
    导出 C 模型时再折进 fc_b，保证 Python 与固件一致。
    """
    base = np.asarray(base_bias, dtype=np.float32)
    deltas = np.zeros_like(base)
    n_classes = logits.shape[1]

    for _ in range(rounds):
        for c in range(n_classes):
            best_delta = float(deltas[c])
            best_score = -np.inf
            for d in np.arange(grid_lo, grid_hi + 1e-9, step):
                cur = deltas.copy()
                cur[c] = float(d)
                preds = np.argmax(logits + base + cur, axis=1)
                score = _calibration_score(preds, trues, metric)
                if score > best_score:
                    best_score = score
                    best_delta = float(d)
            deltas[c] = best_delta
    return base + deltas


def train(model_name: str, sampler: str = "augov", epochs: int = EPOCHS,
          patience: int = 15, weight_mode: str = "cb", metric: str = "f1",
          weight_cap: float = 3.0, loss: str = "focal", gamma: float = 2.0,
          target: int = 12000, floor: int = 8000,
          class_weight: str | None = None, smooth: float = 0.0,
          tag: str | None = None, calibrate: bool = False,
          init_weights: str | None = None, lr: float = LR,
          processed_dir: str = str(PROCESSED_DIR)):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"使用设备: {device}")

    processed_path = Path(processed_dir)
    check_cache_config(processed_path)
    X_tr, y_tr, rr_tr = load_split("train", processed_path)
    X_va, y_va, rr_va = load_split("val", processed_path)
    # prepare_data.py 已保存 int64 类别下标（0..4），直接使用
    y_tr_idx = y_tr.astype(np.int64)
    y_va_idx = y_va.astype(np.int64)

    # sampler:
    #   "augov"  -> 少数类垫高到 floor + 数据增强（默认；只补真正欠采样的类）
    #   "augbal" -> 均匀过采样到 target/类 + 数据增强（激进，会丢多数类多样性）
    #   "bal"    -> 均匀过采样到多数类数量（无增强，旧对比基线）
    #   "aug"    -> 不过采样，仅数据增强（原始分布）
    if sampler == "augov":
        train_ds = AugmentedOversampleDataset(X_tr, y_tr_idx, rr_tr, floor=floor)
    elif sampler == "augbal":
        train_ds = AugmentedBalancedBeatDataset(X_tr, y_tr_idx, rr_tr, target=target)
    elif sampler == "bal":
        train_ds = BalancedBeatDataset(X_tr, y_tr_idx, rr_tr)
    else:
        train_ds = AugmentedBeatDataset(X_tr, y_tr_idx, rr_tr)
    val_ds = BeatDataset(X_va, y_va_idx, rr_va)
    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True,
                              num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=BATCH_SIZE, shuffle=False,
                            num_workers=0)

    model = build_model(model_name).to(device)
    if init_weights:
        print(f"加载初始权重: {init_weights}")
        pretrained = torch.load(init_weights, map_location=device, weights_only=True)
        model_dict = model.state_dict()
        transfer = {k: v for k, v in pretrained.items()
                    if k in model_dict and v.shape == model_dict[k].shape}
        skipped = set(pretrained) - set(transfer)
        if skipped:
            print(f"  忽略不匹配参数: {len(skipped)}")
        model.load_state_dict(transfer, strict=False)
        # 训练期间 logit_bias 统一保持 0，早停/评估由外部 bias_t 显式叠加，
        # 避免从 checkpoint 带来的先验偏置被重复计算。
        if hasattr(model, "logit_bias"):
            model.logit_bias.zero_()
    n_params = count_parameters(model)
    print(f"模型: {model_name}  参数量: {n_params:,}  (约 {n_params * 4 / 1024:.1f} KB fp32)")

    # 类别权重（缓解不平衡）。
    # 数据已均匀过采样（augbal/bal）时类别频率一致，损失用 uniform 权重；
    # 仅在 "aug"（原始分布）时按类别补偿。
    # weight_mode:
    #   "cb"    -> 有效样本数权重（Cui 2019），对极端不平衡平滑且上限有界
    #   "sqrt"  -> sqrt(max/count)，温和补偿，保留多数类准确率
    #   "inv"   -> max/count，强补偿（容易过度补偿少数类）
    # weight_cap: 仅对 sqrt/inv 生效的权重上限，避免少数类权重过大导致多数类被误判。
    class_counts = np.bincount(y_tr_idx, minlength=len(CLASSES)).astype(np.float32)
    # 显式覆盖（消融用）：--class-weight "0.19,2.41,0.62,0.35,0.36"
    if class_weight:
        w = np.asarray([float(v) for v in class_weight.split(",")], dtype=np.float32)
        if len(w) != len(CLASSES):
            raise ValueError(f"class_weight 需 {len(CLASSES)} 个数，实得 {len(w)}")
    elif sampler in ("augov", "augbal", "bal"):
        w = np.ones(len(CLASSES), dtype=np.float32)
    elif weight_mode == "cb":
        w = class_balanced_weights(class_counts)
    elif weight_mode == "sqrt":
        w = np.sqrt(class_counts.max() / (class_counts + 1e-6))
        w = np.minimum(w, weight_cap)
    else:
        w = class_counts.max() / (class_counts + 1e-6)
        w = np.minimum(w, weight_cap)
    weights = torch.tensor(w, dtype=torch.float32).to(device)
    print("类别权重: " + "  ".join(f"{c}={w[i]:.3f}" for i, c in enumerate(CLASSES)))

    # 先验漂移校正：由 train/val 标签统计得出（不触碰测试集），作为常数加在
    # logits 上。训练期间模型侧 buffer 保持 0、评估时外部叠加，避免梯度把它抵消；
    # 训练结束后写入 checkpoint 的 logit_bias，部署时随模型一起生效。
    bias_np = prior_drift_bias(effective_class_counts(train_ds, y_tr_idx, len(CLASSES)),
                               np.bincount(y_va_idx, minlength=len(CLASSES)),
                               len(CLASSES))
    bias_t = torch.tensor(bias_np, dtype=torch.float32).to(device)
    print("先验校正偏置: " + "  ".join(f"{c}={bias_np[i]:+.3f}" for i, c in enumerate(CLASSES)))

    if loss == "focal":
        criterion = lambda logits, targets: focal_loss(logits, targets, weights, gamma)
    else:
        criterion = nn.CrossEntropyLoss(weight=weights, label_smoothing=smooth)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_score = 0.0
    best_f1 = 0.0
    best_acc = 0.0
    best_state = None
    bad_epochs = 0
    history = {"train_loss": [], "val_acc": [], "val_f1": []}
    t0 = time.time()
    for ep in range(1, epochs + 1):
        model.train()
        losses = []
        for x, rr, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            if _has_rr(model):
                rr = rr.to(device)
                loss = criterion(model(x, rr), y)
            else:
                loss = criterion(model(x), y)
            loss.backward()
            optimizer.step()
            losses.append(loss.item())
        scheduler.step()

        preds, trues = evaluate(model, val_loader, device, bias_t)
        acc = float((preds == trues).mean())
        f1 = float(f1_score(trues, preds, average="macro", zero_division=0))
        score = _selection_score(acc, f1, metric)
        history["train_loss"].append(float(np.mean(losses)))
        history["val_acc"].append(acc)
        history["val_f1"].append(f1)
        print(f"epoch {ep:3d}/{epochs}  loss={np.mean(losses):.4f}  "
              f"val_acc={acc:.4f}  val_f1={f1:.4f}  [{time.time() - t0:.0f}s]")

        if score > best_score:
            best_score = score
            best_acc = acc
            best_f1 = f1
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            bad_epochs = 0
        else:
            bad_epochs += 1
            if bad_epochs >= patience:
                print(f"早停：连续 {patience} 个 epoch 无提升")
                break

    # 训练后 logit 偏置校准：在验证集上微调先验漂移偏置，进一步对齐模型选择
    # 指标（acc / macro F1）。校准只使用验证集，不接触测试集。
    if calibrate and best_state is not None:
        model.load_state_dict(best_state)
        val_logits, val_trues = collect_logits(model, val_loader, device)
        bias_np = calibrate_logit_bias(val_logits, val_trues, bias_np,
                                       metric=metric)
        print("校准后偏置: " + "  ".join(
            f"{c}={bias_np[i]:+.3f}" for i, c in enumerate(CLASSES)))
        # 重新按校准偏置计算最佳验证指标，供日志/指标记录；不再改变已选权重。
        calibrated_bias_t = torch.tensor(bias_np, dtype=torch.float32).to(device)
        cal_preds, cal_trues = evaluate(model, val_loader, device,
                                        bias=calibrated_bias_t)
        cal_acc = float((cal_preds == cal_trues).mean())
        cal_f1 = float(f1_score(cal_trues, cal_preds,
                                average="macro", zero_division=0))
        best_score = _selection_score(cal_acc, cal_f1, metric)
        best_acc = cal_acc
        best_f1 = cal_f1

    suffix = f"_{tag}" if tag else ""
    ckpt = MODEL_DIR / f"{model_name}{suffix}_best.pt"
    if best_state is not None and "logit_bias" in best_state:
        best_state["logit_bias"] = torch.tensor(bias_np, dtype=torch.float32)
    torch.save(best_state, ckpt)
    print(f"训练完成，耗时 {time.time() - t0:.1f}s，最佳 val_{metric}={best_score:.4f} "
          f"(acc={best_acc:.4f}, f1={best_f1:.4f})")

    # 在测试集上评估最佳模型
    model.load_state_dict(torch.load(ckpt, map_location=device, weights_only=True))
    X_te, y_te, rr_te = load_split("test", processed_path)
    y_te_idx = y_te.astype(np.int64)
    test_loader = DataLoader(BeatDataset(X_te, y_te_idx, rr_te), batch_size=BATCH_SIZE,
                             shuffle=False, num_workers=0)
    preds, trues = evaluate(model, test_loader, device)
    test_acc = float((preds == trues).mean())

    from sklearn.metrics import classification_report, confusion_matrix, precision_score, recall_score
    # labels= 显式列出全部类别：某类在测试集缺样时（如过滤数据/小切片）sklearn 会因
    # 类别数与 target_names 不符抛 ValueError；显式声明后缺类按 0 计。
    report = classification_report(trues, preds, labels=np.arange(len(CLASSES)),
                                   target_names=CLASSES, digits=4,
                                   output_dict=True, zero_division=0)
    metrics = {
        "model": model_name,
        "n_params": n_params,
        "best_val_acc": best_acc,
        "best_val_f1": best_f1,
        "test_acc": test_acc,
        "test_macro_f1": float(f1_score(trues, preds, average="macro", zero_division=0)),
        "test_macro_precision": float(precision_score(trues, preds, average="macro", zero_division=0)),
        "test_macro_recall": float(recall_score(trues, preds, average="macro", zero_division=0)),
        "confusion_matrix": confusion_matrix(trues, preds).tolist(),
        "per_class": {c: report[c] for c in CLASSES},
        "prior_bias": [float(v) for v in bias_np],
        "history": history,
    }
    metrics_path = MODEL_DIR / f"{model_name}{suffix}_metrics.json"
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    print(f"测试集 acc={test_acc:.4f}  macro_f1={metrics['test_macro_f1']:.4f}")
    print(f"指标已保存至 {metrics_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="res_se_cnn",
                    choices=["res_se_cnn", "res_se_cnn_rr4", "ds_cnn",
                             "std_cnn", "binary_cnn"])
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--patience", type=int, default=15,
                    help="早停 patience（默认 15）")
    ap.add_argument("--sampler", default="augov", choices=["augov", "augbal", "bal", "aug"],
                    help="采样策略：augov 少数类垫高+增强（默认）/ augbal 均匀过采样 / bal 过采样 / aug 原始分布")
    ap.add_argument("--target", type=int, default=12000,
                    help="augbal 下每类采样目标数")
    ap.add_argument("--floor", type=int, default=8000,
                    help="augov 下少数类垫高到的数量")
    ap.add_argument("--weight-mode", default="cb", choices=["cb", "sqrt", "inv"],
                    help="类别权重策略（仅 sampler=aug 生效）：cb / sqrt / inv")
    ap.add_argument("--metric", default="f1", choices=["acc", "f1", "balanced"],
                    help="早停/选模/校准指标：acc / f1 / balanced(0.6acc+0.4f1)")
    ap.add_argument("--weight-cap", type=float, default=3.0,
                    help="类别权重上限（仅 sqrt/inv 生效）")
    ap.add_argument("--loss", default="focal", choices=["ce", "focal"],
                    help="损失函数：focal 缓解极端不平衡 / ce 交叉熵")
    ap.add_argument("--gamma", type=float, default=2.0,
                    help="Focal Loss 的 gamma（loss=focal 时生效）")
    ap.add_argument("--class-weight", default=None,
                    help="显式逐类权重，逗号分隔 5 个数（覆盖 cb/sqrt/inv）")
    ap.add_argument("--smooth", type=float, default=0.0,
                    help="CE 标签平滑系数（loss=ce 时生效）")
    ap.add_argument("--calibrate", dest="calibrate", action="store_true",
                    help="训练后在验证集上校准 logit 偏置（可选，默认关闭）")
    ap.add_argument("--init-weights", default=None,
                    help="从已有 checkpoint 初始化权重后继续训练")
    ap.add_argument("--lr", type=float, default=LR,
                    help="Adam 初始学习率（默认 1e-3）")
    ap.add_argument("--processed-dir", default=str(PROCESSED_DIR),
                    help="预处理 numpy 缓存目录（默认 processed）")
    ap.add_argument("--tag", default=None,
                    help="输出权重/指标文件名后缀，避免并行实验互相覆盖")
    args = ap.parse_args()
    train(args.model, sampler=args.sampler, epochs=args.epochs,
          patience=args.patience,
          weight_mode=args.weight_mode, metric=args.metric,
          weight_cap=args.weight_cap, loss=args.loss, gamma=args.gamma,
          target=args.target, floor=args.floor,
          class_weight=args.class_weight, smooth=args.smooth, tag=args.tag,
          calibrate=args.calibrate, init_weights=args.init_weights, lr=args.lr,
          processed_dir=args.processed_dir)
