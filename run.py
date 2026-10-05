"""一键入口：训练 -> 量化 -> GUI / 离线演示。

用法（在项目根目录）:
    ./runtime/python/python.exe run.py demo --record 100      # 离线命令行演示
    ./runtime/python/python.exe run.py gui                     # 启动 GUI（需先训练模型）
    ./runtime/python/python.exe run.py train --model res_se_cnn_rr4 # 训练（主线模型）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def cmd_train(args):
    from train import train
    train(args.model, sampler=args.sampler, epochs=args.epochs,
          weight_mode=args.weight_mode, metric=args.metric,
          weight_cap=args.weight_cap, loss=args.loss, gamma=args.gamma,
          target=args.target, floor=args.floor,
          class_weight=args.class_weight, smooth=args.smooth, tag=args.tag)


def cmd_demo(args):
    from demo import demo
    demo(args.record, args.model)


def cmd_gui(args):
    from config import MODEL_DIR
    from datasets import available_datasets
    from gui import run_gui

    model_path = MODEL_DIR / f"{args.model}_best.pt"
    if not model_path.exists():
        print(f"未找到模型权重 {model_path}，请先运行: run.py train --model {args.model}")
        return 1

    def build_engine():
        # 延迟导入：torch 和模型权重放到 GUI 首帧之后再加载，缩短上位机启动时间。
        from realtime import BeatClassifier, StreamingEngine
        clf = BeatClassifier(args.model, model_path)
        # 与端侧对齐：用因果流式引擎（固件 rt_tick 的同构实现），4 倍速、20ms tick
        return StreamingEngine(clf, speed=4)

    # GUI 里用下拉框选择数据集（MIT-BIH 记录 + sdcard/ 导出的 .BIN），
    # 与端侧「演示模式」的样本列表对应。扫描放到首帧后再做，避免阻塞启动。--record 只用于预选。
    return run_gui(engine_loader=build_engine,
                   datasets_loader=available_datasets,
                   initial_label=f"MIT-BIH 记录 {args.record}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="run.py")
    sub = ap.add_subparsers(dest="cmd")

    p_train = sub.add_parser("train", help="训练模型")
    p_train.add_argument("--model", default="res_se_cnn_rr4",
                        choices=["res_se_cnn_rr4", "res_se_cnn", "ds_cnn",
                                 "std_cnn", "binary_cnn"])
    p_train.add_argument("--epochs", type=int, default=60)
    p_train.add_argument("--sampler", default="augov",
                        choices=["augov", "augbal", "bal", "aug"],
                        help="采样策略：augov 少数类垫高+增强（默认）/ augbal 均匀过采样 / bal 过采样 / aug 原始分布")
    p_train.add_argument("--target", type=int, default=12000,
                        help="augbal 下每类采样目标数")
    p_train.add_argument("--floor", type=int, default=8000,
                        help="augov 下少数类垫高到的数量")
    p_train.add_argument("--weight-mode", default="cb", choices=["cb", "sqrt", "inv"])
    p_train.add_argument("--metric", default="f1", choices=["acc", "f1"])
    p_train.add_argument("--weight-cap", type=float, default=3.0)
    p_train.add_argument("--loss", default="focal", choices=["ce", "focal"])
    p_train.add_argument("--gamma", type=float, default=2.0)
    p_train.set_defaults(func=cmd_train)

    p_demo = sub.add_parser("demo", help="离线命令行演示")
    p_demo.add_argument("--record", type=int, default=100)
    p_demo.add_argument("--model", default="res_se_cnn_rr4")
    p_demo.set_defaults(func=cmd_demo)

    p_gui = sub.add_parser("gui", help="启动 GUI")
    p_gui.add_argument("--record", type=int, default=100)
    p_gui.add_argument("--model", default="res_se_cnn_rr4")
    p_gui.set_defaults(func=cmd_gui)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(0)
    sys.exit(args.func(args) or 0)
