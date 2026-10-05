"""把轻量心电模型导出为 int8 权重 C 数组 + 逐层描述表，供 ESP32-S3 手写前向推理。

策略：
- BatchNorm 折叠进 pointwise 卷积（float 阶段完成），再 per-tensor 对称量化到 int8；
  通道注意力（SE）的两个 1x1 卷积权重同样量化，bias 保留 float。
- MCU 端反量化回 float 后用 FPU 做 float32 卷积，避免手写 int8 累加/重量化。
- 生成「逐层描述表」，使固件侧可以用一个通用循环支持不同结构
  （深度可分离卷积 / 残差连接 / 通道注意力），无需为每种模型改 C 代码。

支持的结构：
  - DepthwiseSeparableCNN（基线，三层 SepConv）
  - ResSECNN（残差 + SE + 更深主干）

输出 export/c_model/：
  - model_data.h      int8 权重 + float bias 的 C 数组，以及逐层描述表 g_model_layers
  - model_config.h    维度常量与工作缓冲容量
  - model_params.json 结构与量化参数（便于复核）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from config import EXPORT_DIR, PROCESSED_DIR
from models import (_ResSepBlock, _SE1d, _SepConvBlock, build_model)


# ---------------------------------------------------------------------------
# 量化与格式化
# ---------------------------------------------------------------------------
def _fold_bn(pw_weight: torch.Tensor, bn) -> tuple[np.ndarray, np.ndarray]:
    """把 BN 折叠进 pointwise 卷积：返回 (folded_w_float, bias_float)。"""
    gamma = bn.weight.detach().float().numpy()
    beta = bn.bias.detach().float().numpy()
    mean = bn.running_mean.detach().float().numpy()
    var = bn.running_var.detach().float().numpy()
    scale = gamma / np.sqrt(var + bn.eps)
    shift = beta - mean * scale
    w = pw_weight.detach().float().numpy()
    return w * scale[:, None, None], shift


def _quantize(t: np.ndarray) -> tuple[np.ndarray, float]:
    """per-tensor int8 对称量化。返回 (q_int8, scale)。"""
    t = np.asarray(t, dtype=np.float32)
    amax = float(np.max(np.abs(t)))
    scale = amax / 127.0 if amax > 1e-9 else 1e-6
    q = np.clip(np.round(t / scale), -127, 127).astype(np.int8)
    return q, scale


def _carr(arr) -> str:
    return ", ".join(str(int(x)) for x in np.asarray(arr).flatten())


def _flit(v: float) -> str:
    """格式化为合法的 C float 字面量（必须有小数点，否则 0 会变成非法的 `0f`）。"""
    s = f"{float(v):.9g}"
    if "e" not in s and "E" not in s and "." not in s:
        s += ".0"
    return s + "f"


def _farr(arr) -> str:
    return ", ".join(_flit(x) for x in np.asarray(arr).flatten())


# ---------------------------------------------------------------------------
# 按前向顺序抽取层（兼容基线与 ResSECNN）
# ---------------------------------------------------------------------------
def _iter_feature_modules(model):
    """按前向顺序产出各层模块，跳过 Dropout 等无参模块。"""
    stem = getattr(model, "stem", None)
    if stem is not None:                      # ResSECNN 的输入层
        yield stem
    for module in getattr(model, "blocks", []):
        if isinstance(module, torch.nn.Dropout):
            continue
        yield module


def extract_layers(model) -> list[dict]:
    """把模型拍平成「sepconv（可含 SE / 残差）」与「maxpool」的序列。"""
    layers: list[dict] = []
    for module in _iter_feature_modules(model):
        if isinstance(module, torch.nn.MaxPool1d):
            # kernel_size/stride 可能是 int 也可能是单元素元组
            ksz = module.kernel_size
            st = module.stride
            layers.append({"type": "maxpool",
                           "kernel": int(ksz[0] if isinstance(ksz, (tuple, list)) else ksz),
                           "stride": int(st[0] if isinstance(st, (tuple, list)) else st)})
            continue
        if not isinstance(module, (_SepConvBlock, _ResSepBlock)):
            raise TypeError(f"不支持的层类型: {type(module).__name__}")

        dw_w = module.depthwise.weight.detach().float().numpy()
        dw_q, dw_scale = _quantize(dw_w)
        pw_folded, pw_bias = _fold_bn(module.pointwise.weight, module.bn)
        pw_q, pw_scale = _quantize(pw_folded)

        layer = {
            "type": "sepconv",
            "stride": int(module.depthwise.stride[0]),
            "pad": int(module.depthwise.padding[0]),
            "dw_q": dw_q, "dw_scale": dw_scale,
            "pw_q": pw_q, "pw_scale": pw_scale,
            "pw_bias": pw_bias,
            "residual": bool(getattr(module, "residual", False)),
            "se": False,
        }
        se = getattr(module, "se", None)
        if isinstance(se, _SE1d):
            # Conv1d 权重形状为 (out, in, 1)，压成 (out, in) 便于固件按行主序索引
            w1 = se.fc1.weight.detach().float().numpy()[:, :, 0]
            w2 = se.fc2.weight.detach().float().numpy()[:, :, 0]
            q1, s1 = _quantize(w1)
            q2, s2 = _quantize(w2)
            layer.update({
                "se": True,
                "se_q1": q1, "se_scale1": s1,
                "se_b1": se.fc1.bias.detach().float().numpy(),
                "se_q2": q2, "se_scale2": s2,
                "se_b2": se.fc2.bias.detach().float().numpy(),
                "se_h": int(w1.shape[0]),
            })
        layers.append(layer)

    # 标记每个 sepconv 之后是否紧跟一次最大池化，并记录池化参数
    for i, layer in enumerate(layers):
        layer["do_pool"] = False
        for nxt in layers[i + 1:]:
            if nxt["type"] == "maxpool":
                layer["do_pool"] = True
                layer["pool_k"] = nxt["kernel"]
                layer["pool_s"] = nxt["stride"]
            break
    return [l for l in layers if l["type"] == "sepconv"]


def _shapes(layers: list[dict], in_len: int) -> dict:
    """按 stride=1 卷积 + 可选池化，推算各层张量尺寸与缓冲容量。"""
    cur_len, cur_ch = in_len, 1
    max_act = in_len
    max_dw = 0
    max_ch = 1
    max_hid = 1
    for l in layers:
        act = l["pw_q"].shape[0] * cur_len
        max_act = max(max_act, act)
        max_dw = max(max_dw, l["dw_q"].shape[0] * cur_len)
        max_ch = max(max_ch, l["pw_q"].shape[0])
        if l["se"]:
            max_hid = max(max_hid, l["se_h"])
        if l["do_pool"]:
            cur_len = (cur_len - l["pool_k"]) // l["pool_s"] + 1
            max_act = max(max_act, l["pw_q"].shape[0] * cur_len)
        cur_ch = l["pw_q"].shape[0]
    return {"max_act": max_act, "max_dw": max_dw, "max_ch": max_ch,
            "max_hid": max_hid, "final_ch": cur_ch, "final_len": cur_len}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export(model, out_dir: Path, in_len: int = 187, num_classes: int = 5):
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = extract_layers(model)

    linear = [m for m in model.head if isinstance(m, torch.nn.Linear)][0]
    fc_w = linear.weight.detach().float().numpy()
    fc_b = (linear.bias.detach().float().numpy() if linear.bias is not None
            else np.zeros(num_classes, dtype=np.float32))
    # 先验漂移校正偏置折进 fc_b：与 ecg_infer.c 的 logits = feat·fc_w + fc_b 等价，
    # 这样固件侧无需新增任何代码，Python 与 C 也天然一致。
    logit_bias = getattr(model, "logit_bias", None)
    if logit_bias is not None:
        fc_b = fc_b + logit_bias.detach().float().numpy()
    fc_q, fc_scale = _quantize(fc_w)

    # RR 间期旁路（可选）：Linear(2,16)+ReLU+Linear(16,5)，输出直接加到主 FC
    # logits 上。早搏（S/V）形态接近 N，RR 是唯一可靠判据，给它一条直达 logits
    # 的旁路，避免被形态特征淹没。与 ecg_infer.c 的 rr_head 实现严格对应。
    rr_head = getattr(model, "rr_head", None)
    rr_w1 = rr_b1 = rr_w2 = rr_b2 = None
    rr_w1_q = rr_w2_q = None
    rr_w1_scale = rr_w2_scale = 0.0
    rr_h = 0
    if rr_head is not None:
        rr_w1 = rr_head[0].weight.detach().float().numpy()          # (16, 2)
        rr_b1 = rr_head[0].bias.detach().float().numpy()            # (16,)
        rr_w2 = rr_head[2].weight.detach().float().numpy()          # (5, 16)
        rr_b2 = rr_head[2].bias.detach().float().numpy()            # (5,)
        rr_w1_q, rr_w1_scale = _quantize(rr_w1)
        rr_w2_q, rr_w2_scale = _quantize(rr_w2)
        rr_h = int(rr_w1.shape[0])

    n_rr = getattr(model, "n_rr", 2) if getattr(model, "rr_features", False) else 0
    dims = _shapes(layers, in_len)

    # ---------------- model_data.h ----------------
    d = ["/* 自动生成：勿手动编辑。由 src/export_c_model.py 生成。 */",
         "#ifndef MODEL_DATA_H", "#define MODEL_DATA_H", "",
         "#include <stdint.h>", "",
         "/* 逐层描述：固件侧用一个通用循环即可支持不同结构 */",
         "typedef struct {",
         "  const int8_t  *dw;      float dw_scale;",
         "  const int8_t  *pw;      float pw_scale;",
         "  const float   *bias;",
         "  const int8_t  *se_q1;   float se_scale1;  const float *se_b1;",
         "  const int8_t  *se_q2;   float se_scale2;  const float *se_b2;",
         "  uint16_t c_in, c_out, k, pad, se_h;",
         "  uint8_t  residual, se, do_pool;",
         "  uint16_t pool_k, pool_s;",
         "} ecg_layer_t;",
         ""]
    for i, l in enumerate(layers):
        d.append(f"static const int8_t l{i}_dw[{l['dw_q'].size}] = {{{_carr(l['dw_q'])}}};")
        d.append(f"static const int8_t l{i}_pw[{l['pw_q'].size}] = {{{_carr(l['pw_q'])}}};")
        d.append(f"static const float l{i}_bias[{l['pw_bias'].size}] = {{{_farr(l['pw_bias'])}}};")
        if l["se"]:
            d.append(f"static const int8_t l{i}_se1[{l['se_q1'].size}] = {{{_carr(l['se_q1'])}}};")
            d.append(f"static const float l{i}_se1b[{l['se_b1'].size}] = {{{_farr(l['se_b1'])}}};")
            d.append(f"static const int8_t l{i}_se2[{l['se_q2'].size}] = {{{_carr(l['se_q2'])}}};")
            d.append(f"static const float l{i}_se2b[{l['se_b2'].size}] = {{{_farr(l['se_b2'])}}};")
        d.append("")
    d.append(f"static const int8_t fc_w[{fc_q.size}] = {{{_carr(fc_q)}}};")
    d.append(f"static const float fc_b[{fc_b.size}] = {{{_farr(fc_b)}}};")
    d.append("")
    if rr_head is not None:
        d.append(f"static const int8_t rr_w1[{rr_w1_q.size}] = {{{_carr(rr_w1_q)}}};")
        d.append(f"static const float rr_b1[{rr_b1.size}] = {{{_farr(rr_b1)}}};")
        d.append(f"static const int8_t rr_w2[{rr_w2_q.size}] = {{{_carr(rr_w2_q)}}};")
        d.append(f"static const float rr_b2[{rr_b2.size}] = {{{_farr(rr_b2)}}};")
        d.append("")
    d.append("static const ecg_layer_t g_model_layers[] = {")
    for i, l in enumerate(layers):
        se_q1 = f"l{i}_se1" if l["se"] else "0"
        se_b1 = f"l{i}_se1b" if l["se"] else "0"
        se_q2 = f"l{i}_se2" if l["se"] else "0"
        se_b2 = f"l{i}_se2b" if l["se"] else "0"
        d.append(
            f"  {{ l{i}_dw, {_flit(l['dw_scale'])}, l{i}_pw, {_flit(l['pw_scale'])}, l{i}_bias,"
            f" {se_q1}, {_flit(l.get('se_scale1', 0.0))}, {se_b1},"
            f" {se_q2}, {_flit(l.get('se_scale2', 0.0))}, {se_b2},"
            f" {l['dw_q'].shape[0]}, {l['pw_q'].shape[0]}, {l['dw_q'].shape[2]}, {l['pad']},"
            f" {l.get('se_h', 0)}, {int(l['residual'])}, {int(l['se'])}, {int(l['do_pool'])},"
            f" {l.get('pool_k', 0)}, {l.get('pool_s', 0)} }},")
    d.append("};")
    d.append(f"#define MODEL_N_SEP {len(layers)}")
    d.append("#endif")
    (out_dir / "model_data.h").write_text("\n".join(d) + "\n", encoding="utf-8")

    # ---------------- model_config.h ----------------
    c = ["/* 自动生成：勿手动编辑。由 src/export_c_model.py 生成。 */",
         "#ifndef MODEL_CONFIG_H", "#define MODEL_CONFIG_H", "",
         f"#define MODEL_INPUT_LEN {in_len}",
         f"#define MODEL_NUM_CLASSES {num_classes}",
         f"#define MODEL_N_RR_FEATURES {n_rr}",
         f"#define MODEL_HAS_RR_HEAD {1 if rr_head is not None else 0}",
         "",
         "/* RR 间期旁路维度与量化 scale（MODEL_HAS_RR_HEAD=1 时有效） */"]
    if rr_head is not None:
        c += [f"#define RR_H {rr_h}",
              f"#define RR_W1_SCALE {_flit(rr_w1_scale)}",
              f"#define RR_W2_SCALE {_flit(rr_w2_scale)}"]
    c += ["",
          "/* 工作缓冲容量（由导出脚本按结构推算，保证不越界） */",
          f"#define MODEL_MAX_ACT {dims['max_act']}",
          f"#define MODEL_MAX_DW  {dims['max_dw']}",
          f"#define MODEL_MAX_CH  {dims['max_ch']}",
          f"#define MODEL_MAX_HID {dims['max_hid']}",
          f"#define MODEL_MAX_POOL {dims['max_ch'] + max(n_rr, 1)}",
          "",
          f"#define FC_IN {fc_q.shape[1]}",
          f"#define FC_OUT {fc_q.shape[0]}",
          f"#define FC_W_SCALE {_flit(fc_scale)}",
          "#endif"]
    (out_dir / "model_config.h").write_text("\n".join(c) + "\n", encoding="utf-8")

    # ---------------- model_params.json ----------------
    params = {
        "input_len": in_len, "num_classes": num_classes, "n_rr_features": n_rr,
        "has_rr_head": rr_head is not None,
        "n_params": int(sum(p.numel() for p in model.parameters() if p.requires_grad)),
        "layers": [{
            "c_in": int(l["dw_q"].shape[0]), "c_out": int(l["pw_q"].shape[0]),
            "k": int(l["dw_q"].shape[2]), "pad": int(l["pad"]),
            "residual": l["residual"], "se": l["se"], "se_h": l.get("se_h", 0),
            "do_pool": l["do_pool"], "pool_k": l.get("pool_k"),
            "dw_scale": l["dw_scale"], "pw_scale": l["pw_scale"],
        } for l in layers],
        "fc_scale": fc_scale,
        "buffers": dims,
    }
    if rr_head is not None:
        params["rr_head"] = {"h": rr_h, "w1_scale": rr_w1_scale,
                             "w2_scale": rr_w2_scale}
    if logit_bias is not None:
        params["logit_bias"] = [float(v) for v in logit_bias.detach().float().numpy()]
        params["logit_bias_folded_into"] = "fc_b"
    (out_dir / "model_params.json").write_text(
        json.dumps(params, indent=2), encoding="utf-8")

    total = sum(l["dw_q"].nbytes + l["pw_q"].nbytes + l["pw_bias"].nbytes
                + (l["se_q1"].nbytes + l["se_b1"].nbytes
                   + l["se_q2"].nbytes + l["se_b2"].nbytes if l["se"] else 0)
                for l in layers)
    total += fc_q.nbytes + fc_b.nbytes
    if rr_head is not None:
        total += rr_w1_q.nbytes + rr_b1.nbytes + rr_w2_q.nbytes + rr_b2.nbytes
    print(f"导出完成: {out_dir}")
    print(f"  层数 {len(layers)}，参数量 {params['n_params']:,}")
    print(f"  int8 权重 + float bias 总字节数: {total} B ({total/1024:.2f} KB)")
    print(f"  工作缓冲: ACT={dims['max_act']} DW={dims['max_dw']} "
          f"CH={dims['max_ch']} HID={dims['max_hid']}")
    return params


# ---------------------------------------------------------------------------
# 参考前向：严格复刻 C 端算法，用于验证导出数据与语义
# ---------------------------------------------------------------------------
def numpy_reference(model, x: np.ndarray, rr: np.ndarray | None = None) -> np.ndarray:
    """numpy 版前向，与 ecg_infer.c 的算法逐行对应（量化存储 + float 反量化）。"""
    layers = extract_layers(model)
    linear = [m for m in model.head if isinstance(m, torch.nn.Linear)][0]
    fc_q, fc_scale = _quantize(linear.weight.detach().float().numpy())
    fc_b = linear.bias.detach().float().numpy()
    logit_bias = getattr(model, "logit_bias", None)
    if logit_bias is not None:
        fc_b = fc_b + logit_bias.detach().float().numpy()
    use_rr = bool(getattr(model, "rr_features", False))
    n_rr = getattr(model, "n_rr", 2) if use_rr else 0

    x = np.asarray(x, dtype=np.float32)
    for l in layers:
        dw = l["dw_q"].astype(np.float32) * l["dw_scale"]
        pw = l["pw_q"].astype(np.float32) * l["pw_scale"]
        pad = l["pad"]
        c_in, c_out, k = dw.shape[0], pw.shape[0], dw.shape[2]
        n = x.shape[0]; L = x.shape[2]
        # depthwise
        xp = np.pad(x, ((0, 0), (0, 0), (pad, pad)))
        dw_out = np.zeros((n, c_in, L), dtype=np.float32)
        for c in range(c_in):
            for t in range(L):
                dw_out[:, c, t] = np.sum(xp[:, c, t:t + k] * dw[c, 0], axis=1)
        # pointwise + bias + ReLU
        y = np.zeros((n, c_out, L), dtype=np.float32)
        for co in range(c_out):
            acc = np.full((n, L), l["pw_bias"][co], dtype=np.float32)
            for ci in range(c_in):
                acc += pw[co, ci, 0] * dw_out[:, ci, :]
            y[:, co, :] = acc
        y = np.maximum(y, 0.0)
        # 通道注意力
        if l["se"]:
            g = y.mean(axis=2)                                  # (n, c_out)
            w1 = l["se_q1"].astype(np.float32) * l["se_scale1"]
            w2 = l["se_q2"].astype(np.float32) * l["se_scale2"]
            h = np.maximum(g @ w1.T + l["se_b1"], 0.0)           # (n, se_h)
            gate = 1.0 / (1.0 + np.exp(-(h @ w2.T + l["se_b2"])))  # (n, c_out)
            y = y * gate[:, :, None]
        # 残差
        if l["residual"]:
            y = np.maximum(y + x, 0.0)
        x = y
        # 池化
        if l["do_pool"]:
            kk, ss = l["pool_k"], l["pool_s"]
            out_len = (L - kk) // ss + 1
            out = np.zeros((n, c_out, out_len), dtype=np.float32)
            for t in range(out_len):
                out[:, :, t] = np.max(x[:, :, t * ss:t * ss + kk], axis=2)
            x = out

    x = x.mean(axis=2)
    rr_missing = rr is None
    if use_rr:
        if rr is None:
            # 与固件 ecg_infer.c 的 NULL 回退一致：rr_ext 全部置 1.0
            # （= 平均 RR 归一化值，训练侧缺邻接 RR 同样回退 1.0）
            rr = np.ones((x.shape[0], n_rr), dtype=np.float32)
        rr = np.asarray(rr, dtype=np.float32)
        if n_rr == 4 and rr.shape[1] == 2:
            pre = rr[:, 0:1]
            post = rr[:, 1:2]
            ratio = np.clip(pre / (post + 1e-6), 0.3, 3.0)
            rr = np.concatenate([rr, ratio, post - pre], axis=1)
        x = np.concatenate([x, rr], axis=1)
    logits = x @ (fc_q.astype(np.float32) * fc_scale).T + fc_b

    # RR 间期旁路（与 PyTorch / ecg_infer.c 一致）：logits += rr_head(rr)。
    # 固件在 rr_feat==NULL 时跳过旁路，这里同样只在 rr 实际提供时计算。
    rr_head = getattr(model, "rr_head", None)
    if rr_head is not None and not rr_missing:
        w1 = rr_head[0].weight.detach().float().numpy()
        b1 = rr_head[0].bias.detach().float().numpy()
        w2 = rr_head[2].weight.detach().float().numpy()
        b2 = rr_head[2].bias.detach().float().numpy()
        h = np.maximum(rr @ w1.T + b1, 0.0)
        logits = logits + (h @ w2.T + b2)
    return logits


def load_weights(model, path) -> None:
    """加载权重，容忍旧 checkpoint 缺少 `logit_bias`（先验校正 buffer）。

    该 buffer 不是学出来的参数，而是由 train/val 标签统计推出的常数偏置；早期
    checkpoint 没有它，缺失时回退为 0（即不做先验校正），其余键必须完全匹配——
    只放行这一个已知的可选项，避免掩盖真正的结构不一致。
    """
    sd = torch.load(path, map_location="cpu", weights_only=True)
    missing, unexpected = model.load_state_dict(sd, strict=False)
    extra = set(missing) - {"logit_bias"}
    if extra or unexpected:
        raise RuntimeError(
            f"权重与模型结构不匹配: 缺失 {sorted(extra)}，多余 {sorted(unexpected)}")


def verify_quantized(model_name: str, n_beats: int = 200):
    """量化导出的前向 与 PyTorch 原模型逐拍比对。"""
    from config import MODEL_DIR

    model = build_model(model_name)
    load_weights(model, MODEL_DIR / f"{model_name}_best.pt")
    model.eval()

    te = np.load(PROCESSED_DIR / "test_windows.npy")[:n_beats][:, None, :]
    rr = None
    rr_path = PROCESSED_DIR / "test_rrfeat.npy"
    if rr_path.exists() and getattr(model, "rr_features", False):
        rr = np.load(rr_path)[:n_beats]
    with torch.no_grad():
        ref = model(torch.as_tensor(te), torch.as_tensor(rr)) if rr is not None \
            else model(torch.as_tensor(te))
        ref = ref.numpy()
    got = numpy_reference(model, te, rr)
    max_err = float(np.max(np.abs(ref - got)))
    agree = float((ref.argmax(1) == got.argmax(1)).mean())
    print(f"[{model_name}] 量化前向 vs PyTorch: max_err={max_err:.6f}, argmax 一致率={agree:.4f}")
    return max_err, agree


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="res_se_cnn_rr4")
    ap.add_argument("--weights", default=None)
    args = ap.parse_args()

    from config import MODEL_DIR
    weights = args.weights or str(MODEL_DIR / f"{args.model}_best.pt")
    m = build_model(args.model)
    load_weights(m, weights)
    m.eval()
    export(m, EXPORT_DIR / "c_model")
    if (PROCESSED_DIR / "test_windows.npy").exists():
        verify_quantized(args.model)
