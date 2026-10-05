"""生成设计报告图表：混淆矩阵、精度-体积曲线、训练曲线。

用法:
    ./runtime/python/python.exe src/make_report.py
读取 models/ 下的 *_metrics.json 与 *_summary.json，输出 PNG 到 docs/report/。
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from config import CLASSES, MODEL_DIR, REPORT_DIR  # noqa: E402

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def load_json(name: str):
    p = MODEL_DIR / name
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def plot_confusion_matrix(model_name: str):
    m = load_json(f"{model_name}_metrics.json")
    if m is None:
        return
    cm = np.asarray(m["confusion_matrix"])
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(CLASSES)), CLASSES)
    ax.set_yticks(range(len(CLASSES)), CLASSES)
    ax.set_xlabel("预测类别")
    ax.set_ylabel("真实类别")
    ax.set_title(f"混淆矩阵 — {model_name} (acc={m['test_acc']:.4f})")
    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(j, i, str(cm[i, j]), ha="center", va="center",
                    color="white" if cm[i, j] > cm.max() / 2 else "black")
    fig.colorbar(im)
    fig.tight_layout()
    out = REPORT_DIR / f"{model_name}_confusion_matrix.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"已生成 {out}")


def plot_training_curve(model_name: str):
    m = load_json(f"{model_name}_metrics.json")
    if m is None:
        return
    h = m.get("history", {})
    if not h:
        return
    fig, ax1 = plt.subplots(figsize=(7, 4))
    ax1.plot(h["train_loss"], color="#e74c3c", label="训练 loss")
    ax1.set_xlabel("epoch")
    ax1.set_ylabel("loss", color="#e74c3c")
    ax2 = ax1.twinx()
    ax2.plot(h["val_acc"], color="#2ecc71", label="验证准确率")
    ax2.set_ylabel("acc", color="#2ecc71")
    ax1.set_title(f"训练曲线 — {model_name}")
    fig.tight_layout()
    out = REPORT_DIR / f"{model_name}_training.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"已生成 {out}")


def plot_model_comparison():
    models = ["res_se_cnn", "ds_cnn", "std_cnn", "binary_cnn"]
    names, params, accs, sizes, lats, flops = [], [], [], [], [], []
    for name in models:
        s = load_json(f"{name}_summary.json")
        m = load_json(f"{name}_metrics.json")
        if s is None and m is None:
            continue
        names.append(name)
        params.append((s or m).get("n_params", 0))
        accs.append((s or m).get("test_acc", (s or {}).get("best_val_acc", 0)))
        sizes.append((s or {}).get("int8_onnx_kb") or (s or {}).get("fp32_onnx_kb") or 0)
        lats.append((s or {}).get("latency_ms", 0))
        flops.append((s or {}).get("flops", 0))

    if not names:
        print("无模型指标，跳过对比图")
        return

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    x = np.arange(len(names))
    axes[0].bar(x - 0.2, accs, 0.4, label="准确率", color="#3498db")
    axes[0].set_xticks(x, names)
    axes[0].set_ylabel("准确率")
    axes[0].set_title("模型准确率对比")
    axes[0].set_ylim(0, 1)
    axes[0].axhline(0.9, color="#e74c3c", ls="--", label="目标 90%")
    axes[0].legend()

    axes[1].bar(x - 0.2, sizes, 0.4, label="int8 体积(KB)", color="#2ecc71")
    axes[1].set_xticks(x, names)
    axes[1].set_ylabel("模型体积 (KB)")
    axes[1].set_title("模型体积对比（目标 <50KB）")
    axes[1].axhline(50, color="#e74c3c", ls="--", label="目标 50KB")
    axes[1].legend()
    fig.tight_layout()
    out = REPORT_DIR / "model_comparison.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"已生成 {out}")


if __name__ == "__main__":
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    for m in ["res_se_cnn", "ds_cnn", "std_cnn", "binary_cnn"]:
        plot_confusion_matrix(m)
        plot_training_curve(m)
    plot_model_comparison()
    print("报告图表生成完成")
