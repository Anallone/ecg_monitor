"""模型量化、体积测量与导出（ONNX / TFLite int8）。

包含：
- PyTorch -> ONNX
- ONNX -> TFLite（含 int8 后训练量化）
- 参数量 / FLOPs / 单拍推理时延 / 模型文件体积测量
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np
import torch

from config import EXPORT_DIR, MODEL_DIR, MODEL_INPUT_LEN, NUM_CLASSES
from models import build_model, count_parameters

CALIB_SAMPLES = 1024  # int8 量化校准样本数


def _dummy_input(batch: int = 1):
    return torch.randn(batch, 1, MODEL_INPUT_LEN)


def _dummy_rr(batch: int = 1):
    return torch.ones(batch, 2)


def export_onnx(model_name: str, dynamic_batch: bool = False):
    import onnx

    model = build_model(model_name)
    model.load_state_dict(torch.load(MODEL_DIR / f"{model_name}_best.pt", map_location="cpu", weights_only=True))
    model.eval()

    path = EXPORT_DIR / f"{model_name}.onnx"
    use_rr = bool(getattr(model, "rr_features", False))
    args = (_dummy_input(1),)
    input_names = ["input"]
    if use_rr:
        args = args + (_dummy_rr(1),)
        input_names.append("rr")
    dynamic_axes = None
    if dynamic_batch:
        dynamic_axes = {n: {0: "batch"} for n in input_names}
        dynamic_axes["output"] = {0: "batch"}
    torch.onnx.export(model, args, str(path), input_names=input_names,
                      output_names=["output"], dynamic_axes=dynamic_axes,
                      opset_version=13)
    onnx_model = onnx.load(str(path))
    onnx.checker.check_model(onnx_model)
    size_kb = path.stat().st_size / 1024
    print(f"ONNX 导出完成: {path}  ({size_kb:.2f} KB)")
    return path


def export_tflite_int8(model_name: str):
    """通过 onnx -> tflite 并做 int8 后训练量化。

    依赖 tensorflow + onnx-tf/onnx2tf 生态；此处采用 onnxruntime 量化到 int8 的
    通用路径，输出 .onnx 量化版并估算体积；真实 TFLite 转换见 deploy 脚本。
    """
    import onnx
    from onnxruntime.quantization import quantize_dynamic, QuantType

    src = EXPORT_DIR / f"{model_name}.onnx"
    if not src.exists():
        export_onnx(model_name)
    dst = EXPORT_DIR / f"{model_name}_int8.onnx"
    quantize_dynamic(str(src), str(dst), weight_type=QuantType.QInt8)
    size_kb = dst.stat().st_size / 1024
    print(f"int8 量化 ONNX 完成: {dst}  ({size_kb:.2f} KB)")
    return dst


def measure_flops(model: torch.nn.Module) -> int:
    """粗略 FLOPs 统计：按 blocks 顺序跟踪长度/通道，乘加 MAC 计 1。"""
    from models import (BinaryConv1d, _ResSepBlock, _SE1d, _SepConvBlock,
                        _StdConvBlock, conv1d_out_len)

    def conv1d_macs(in_len: int, cin: int, cout: int, k: int,
                    stride: int, groups: int) -> tuple[int, int]:
        out_len = conv1d_out_len(in_len, k, stride=stride, pad=k // 2)
        macs = (cin // groups) * cout * k * out_len
        return macs, out_len

    total = 0
    cur_len = MODEL_INPUT_LEN
    cur_ch = 1

    # ResSECNN 的 stem 在 blocks 之外，需要一并统计；其他模型没有 stem 时忽略。
    stem = getattr(model, "stem", None)
    seq = ([stem] if stem is not None else []) + list(
        getattr(model, "blocks", []) or [])

    for module in seq:
        if isinstance(module, (_SepConvBlock, _ResSepBlock)):
            k = module.depthwise.kernel_size[0]
            stride = module.depthwise.stride[0]
            cin = cur_ch
            # depthwise: groups=cin
            macs_dw, out_len = conv1d_macs(cur_len, cin, cin, k, stride, cin)
            # pointwise 1x1
            cout = module.pointwise.out_channels
            macs_pw = cin * cout * out_len
            total += macs_dw + macs_pw
            cur_len, cur_ch = out_len, cout
            # SE 的两个 1x1 全连接在时间维上无滑动，仅计入通道间乘加。
            se = getattr(module, "se", None)
            if isinstance(se, _SE1d):
                hidden = se.fc1.out_channels
                total += 2 * cout * hidden
        elif isinstance(module, _StdConvBlock):
            k = module.conv.kernel_size[0]
            stride = module.conv.stride[0]
            cout = module.conv.out_channels
            macs, out_len = conv1d_macs(cur_len, cur_ch, cout, k, stride,
                                        module.conv.groups)
            total += macs
            cur_len, cur_ch = out_len, cout
        elif isinstance(module, BinaryConv1d):
            k = module.conv.kernel_size[0]
            stride = module.conv.stride[0]
            cout = module.conv.out_channels
            macs, out_len = conv1d_macs(cur_len, cur_ch, cout, k, stride,
                                        module.conv.groups)
            total += macs
            cur_len, cur_ch = out_len, cout
        elif isinstance(module, torch.nn.MaxPool1d):
            k = module.kernel_size
            s = module.stride
            cur_len = (cur_len - k) // s + 1
    return int(total)


def measure_latency(model: torch.nn.Module, iters: int = 200) -> float:
    """单拍 CPU 推理时延（ms），取中位数。"""
    model.eval()
    x = _dummy_input(1)
    # 带 RR 特征的模型必须传 rr 才算完整前向（否则走 NULL 回退分支，少掉 rr_head）
    use_rr = bool(getattr(model, "rr_features", False))
    args = (x, _dummy_rr(1)) if use_rr else (x,)
    with torch.no_grad():
        # warmup
        for _ in range(20):
            model(*args)
        ts = []
        for _ in range(iters):
            t0 = time.perf_counter()
            model(*args)
            ts.append((time.perf_counter() - t0) * 1000)
    return float(np.median(ts))


def measure_model(model_name: str):
    """汇总指标并写入 JSON。"""
    import onnx

    model = build_model(model_name)
    model.load_state_dict(torch.load(MODEL_DIR / f"{model_name}_best.pt", map_location="cpu", weights_only=True))
    model.eval()

    n_params = count_parameters(model)
    flops = measure_flops(model)
    latency = measure_latency(model)

    fp32_path = EXPORT_DIR / f"{model_name}.onnx"
    int8_path = EXPORT_DIR / f"{model_name}_int8.onnx"
    fp32_kb = fp32_path.stat().st_size / 1024 if fp32_path.exists() else None
    int8_kb = int8_path.stat().st_size / 1024 if int8_path.exists() else None

    # 读取训练指标
    metrics_path = MODEL_DIR / f"{model_name}_metrics.json"
    base = {}
    if metrics_path.exists():
        with open(metrics_path, "r", encoding="utf-8") as f:
            base = json.load(f)

    summary = {
        **base,
        "flops": flops,
        "latency_ms": latency,
        "fp32_onnx_kb": fp32_kb,
        "int8_onnx_kb": int8_kb,
        "params_bytes": n_params * 4,
    }
    with open(MODEL_DIR / f"{model_name}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    def _kb(v):
        return f"{v:.2f} KB" if v is not None else "N/A"

    print(f"模型 {model_name}: 参数量 {n_params:,}, FLOPs {flops:,}, "
          f"单拍时延 {latency:.3f} ms, fp32 {_kb(fp32_kb)}, int8 {_kb(int8_kb)}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="res_se_cnn_rr4",
                    choices=["res_se_cnn", "res_se_cnn_rr4", "ds_cnn",
                             "std_cnn", "binary_cnn"])
    ap.add_argument("--export", action="store_true", help="导出 ONNX / int8")
    ap.add_argument("--measure", action="store_true", help="测量并汇总指标")
    args = ap.parse_args()

    if args.export or not args.measure:
        export_onnx(args.model)
        try:
            export_tflite_int8(args.model)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] int8 量化失败（可后续补齐）: {exc}")
    if args.measure:
        measure_model(args.model)
