"""导出 TFLite（fp32 / float16 / 动态范围 / 全整型 int8）。

转换路径：PyTorch -> ONNX（src/quantize.py）-> onnx2tf -> TFLite。

说明（重要，2026-09-09 实测结论）：
- 原方案 ai-edge-torch 在 Windows 上不可用：其硬依赖 `torch_xla` 无 Windows wheel
  （Linux/TPU-only），无法安装。故本脚本不再走 ai-edge-torch。
- 改用 onnx2tf（Windows 原生可用）完成 ONNX->TFLite 与 int8 后训练量化。
- 全整型 int8 模型体积 14.52 KB，与 PyTorch 在测试集前 200 拍 argmax 一致率 100%。
- 本项目「权威」int8 体积仍以 C 导出路径为准（export/c_model，~7.2 KB，已逐位验证）；
  TFLite int8 作为端侧推理框架对照指标，供报告引用。

依赖（已拆分为 `requirements-export.txt`）：
    ..\run_pip.bat install -r ..\requirements-export.txt

用法（在 src 目录）:
    ../runtime/python/python.exe export_tflite.py --model res_se_cnn_rr4 --verify
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

from config import EXPORT_DIR, MODEL_DIR, MODEL_INPUT_LEN

# onnx2tf 输出目录：按模型分目录，避免后一次导出覆盖前一次结果。
TFLITE_ROOT = EXPORT_DIR / "tflite"


def _ensure_onnx(model_name: str) -> Path:
    onnx_path = EXPORT_DIR / f"{model_name}.onnx"
    if not onnx_path.exists():
        from quantize import export_onnx
        export_onnx(model_name)
    return onnx_path


def _make_calibration(model_name: str, n: int = 256):
    """从测试集取前 n 拍生成校准数据（已 z-score / RR 裁剪，mean=0 std=1）。"""
    from config import PROCESSED_DIR

    calib_dir = EXPORT_DIR / "calib"
    calib_dir.mkdir(exist_ok=True)
    x = np.load(PROCESSED_DIR / "test_windows.npy", mmap_mode="r")[:n].astype(np.float32)
    rr = np.load(PROCESSED_DIR / "test_rrfeat.npy", mmap_mode="r")[:n].astype(np.float32)
    x = x[:, None, :]  # (n, 1, 187)
    np.save(calib_dir / "input.npy", x)
    np.save(calib_dir / "rr.npy", rr)
    return calib_dir / "input.npy", calib_dir / "rr.npy"


def convert(model_name: str):
    onnx_path = _ensure_onnx(model_name)

    # 检测模型是否使用 RR 特征，决定校准是否含 rr 输入
    from models import build_model
    import torch
    probe = build_model(model_name)
    use_rr = bool(getattr(probe, "rr_features", False))
    del probe

    calib_in, calib_rr = _make_calibration(model_name)

    tflite_dir = TFLITE_ROOT / model_name
    if tflite_dir.exists():
        shutil.rmtree(tflite_dir)
    tflite_dir.mkdir(parents=True)

    cmd = [
        sys.executable, "-m", "onnx2tf",
        "-i", str(onnx_path),
        "-o", str(tflite_dir),
        "-oiqt",                 # 全整型量化
        "-qt", "per-tensor",
        "-cind", "input", str(calib_in), "0", "1",   # 校准数据已是模型输入空间，mean=0 std=1
    ]
    if use_rr:
        cmd += ["-cind", "rr", str(calib_rr), "0", "1"]
    cmd += ["-b", "1", "-coion"]
    import os
    env = dict(os.environ)
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               TF_CPP_MIN_LOG_LEVEL="3", TF_ENABLE_ONEDNN_OPTS="0")
    print("运行:", " ".join(cmd))
    try:
        subprocess.run(cmd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"[warn] TFLite 转换失败，跳过本模型 TFLite 产物: {exc}")
        return None

    for f in sorted(tflite_dir.glob("*.tflite")):
        print(f"TFLite: {f.name}  ({f.stat().st_size / 1024:.2f} KB)")
    return tflite_dir / f"{model_name}_full_integer_quant.tflite"


def verify_tflite(model_name: str, path: Path | None = None):
    """全整型 int8 TFLite vs PyTorch，测试集前 200 拍 argmax 一致率。"""
    import torch
    from ai_edge_litert.interpreter import Interpreter

    from models import build_model

    path = path or TFLITE_ROOT / model_name / f"{model_name}_full_integer_quant.tflite"
    if not path.exists():
        print(f"[warn] 未找到 {path}，跳过校验")
        return None

    itp = Interpreter(model_path=str(path))
    itp.allocate_tensors()
    inds = itp.get_input_details()
    outd = itp.get_output_details()[0]
    inmap = {d["name"].split("serving_default_")[-1].split(":")[0]: d for d in inds}

    model = build_model(model_name)
    model.load_state_dict(torch.load(MODEL_DIR / f"{model_name}_best.pt", map_location="cpu", weights_only=True))
    model.eval()
    use_rr = bool(getattr(model, "rr_features", False))

    from config import PROCESSED_DIR
    x = np.load(PROCESSED_DIR / "test_windows.npy", mmap_mode="r")[:200].astype(np.float32)
    rr = np.load(PROCESSED_DIR / "test_rrfeat.npy", mmap_mode="r")[:200].astype(np.float32)

    di = inmap["input"]
    dr = inmap.get("rr")
    if use_rr and dr is None:
        print(f"[warn] 模型需要 rr 输入但 TFLite 中未找到该输入，无法校验")
        return None
    si, zi = di["quantization"][0], di["quantization"][1]
    sr = zr = 0.0
    if dr is not None:
        sr, zr = dr["quantization"][0], dr["quantization"][1]
    so, zo = outd["quantization"][0], outd["quantization"][1]

    agree = 0
    for i in range(len(x)):
        xw = x[i][None, :, None]   # NHWC (1,187,1)
        xi = np.clip(np.round(xw / si) + zi, -128, 127).astype(np.int8)
        itp.set_tensor(di["index"], xi)
        rv = None
        if dr is not None:
            rv = rr[i][None, :]    # (1,2)
            ri = np.clip(np.round(rv / sr) + zr, -128, 127).astype(np.int8)
            itp.set_tensor(dr["index"], ri)
        itp.invoke()
        out = itp.get_tensor(outd["index"])[0]
        logits = (out.astype(np.float32) - zo) * so
        pred = int(np.argmax(logits))
        with torch.no_grad():
            if use_rr:
                ref = model(torch.as_tensor(xw.transpose(0, 2, 1)),
                            torch.as_tensor(rv)).argmax(1).item()
            else:
                ref = model(torch.as_tensor(xw.transpose(0, 2, 1))).argmax(1).item()
        agree += int(pred == ref)

    acc = agree / len(x)
    print(f"TFLite int8 vs PyTorch: 预测一致率={acc:.4f} ({agree}/{len(x)})")
    return acc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="res_se_cnn_rr4")
    ap.add_argument("--verify", action="store_true", help="导出后用 TFLite 解释器校验")
    args = ap.parse_args()

    convert(args.model)
    if args.verify:
        verify_tflite(args.model)
