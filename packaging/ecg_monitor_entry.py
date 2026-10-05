"""PyInstaller 打包入口：等价于 `run.py gui`（主线模型 res_se_cnn_rr4）。

打包时用 --paths src 让 PyInstaller 解析 src/ 下的模块；
开发模式下直接运行本文件也能启动 GUI（与 run.py gui 行为一致）。
"""
from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def main() -> int:
    from config import MODEL_DIR
    from datasets import available_datasets
    from gui import run_gui
    from realtime import BeatClassifier, StreamingEngine

    model_name = "res_se_cnn_rr4"
    model_path = MODEL_DIR / f"{model_name}_best.pt"
    if not model_path.exists():
        print(f"未找到模型权重 {model_path}")
        return 1

    clf = BeatClassifier(model_name, model_path)
    # 与端侧对齐：用因果流式引擎（固件 rt_tick 的同构实现），4 倍速、20ms tick
    engine = StreamingEngine(clf, speed=4)
    # GUI 里用下拉框选择数据集（MIT-BIH 记录 + sdcard/ 导出的 .BIN），
    # 与端侧「演示模式」的样本列表对应。
    datasets = available_datasets()
    if not datasets:
        print("[警告] 未找到可用数据集：data/ 下需有 MIT-BIH 记录，或 sdcard/ 下有 .BIN")
    return run_gui(engine=engine, datasets=datasets,
                   initial_label="MIT-BIH 记录 100")


if __name__ == "__main__":
    sys.exit(main() or 0)
