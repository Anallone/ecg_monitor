"""校验 ONNX Runtime 与 PyTorch 模型在测试集前 N 拍上 argmax 一致率。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import EXPORT_DIR, MODEL_DIR, PROCESSED_DIR
from models import build_model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="res_se_cnn_rr4")
    ap.add_argument("--beats", type=int, default=200)
    ap.add_argument("--onnx", default=None)
    args = ap.parse_args()

    onnx_path = Path(args.onnx or EXPORT_DIR / f"{args.model}.onnx")
    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    input_names = [inp.name for inp in sess.get_inputs()]
    out_name = sess.get_outputs()[0].name

    model = build_model(args.model)
    model.load_state_dict(torch.load(MODEL_DIR / f"{args.model}_best.pt", map_location="cpu", weights_only=True))
    model.eval()
    use_rr = bool(getattr(model, "rr_features", False))

    x = np.load(PROCESSED_DIR / "test_windows.npy", mmap_mode="r")[:args.beats].astype(np.float32)
    rr = (np.load(PROCESSED_DIR / "test_rrfeat.npy", mmap_mode="r")[:args.beats].astype(np.float32)
          if use_rr else None)

    agree = 0
    max_err = 0.0
    for i in range(len(x)):
        # 输入名取自会话本身（旧写法硬编码 {"input": ...} / {"rr": ...}，
        # 一旦导出脚本改名 ONNX 输入，这里会 KeyError 且与模型真实接口脱节）
        feed = {input_names[0]: x[i][None, None, :]}
        if use_rr:
            feed[input_names[1]] = rr[i][None, :]
        onnx_logits = sess.run([out_name], feed)[0][0]
        with torch.no_grad():
            if use_rr:
                ref = model(torch.as_tensor(x[i][None, None, :]),
                            torch.as_tensor(rr[i][None, :])).numpy()[0]
            else:
                ref = model(torch.as_tensor(x[i][None, None, :])).numpy()[0]
        max_err = max(max_err, float(np.max(np.abs(onnx_logits - ref))))
        agree += int(np.argmax(onnx_logits) == np.argmax(ref))

    acc = agree / len(x)
    print(f"ONNX Runtime vs PyTorch: max_err={max_err:.6f}, "
          f"argmax 一致率={acc:.4f} ({agree}/{len(x)})")
    return acc, max_err


if __name__ == "__main__":
    main()
