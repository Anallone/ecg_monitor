# -*- coding: utf-8 -*-
"""校验 export/c_model 下生成的 C 头文件：数据正确 + 算法语义与 PyTorch 一致。

因为没有主机 C 编译器，这里不编译 C，而是**直接解析生成的 model_data.h /
model_config.h**，用 Python 严格复刻 ecg_infer.c 的算法（含深度可分离卷积、
通道注意力、残差、原地最大池化、全局平均池化、全连接），再与 PyTorch 原模型
逐拍比对。这样验证的是「导出数据 + C 算法」这一整条链路。

局限（重要）：本脚本模拟的是**算法语义**，用的是规整的 numpy 数组，因此
**看不到 C 实现的内存布局问题**——例如原地池化后通道步长没收紧，导致后续层
逐通道读错位（这类 bug 会让本脚本通过、而端侧结果全错）。凡是改动
ecg_infer.c 的缓冲/索引逻辑，必须在真机上对照逐层中间值复核（见
firmware/README.md 的「端侧推理核对」）。

用法（项目根目录）:
    ./runtime/python/python.exe tools/verify_c_export.py --model res_se_cnn
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CMODEL = ROOT / "export" / "c_model"


# ---------------------------------------------------------------------------
# 解析生成的 C 头文件
# ---------------------------------------------------------------------------
def parse_defines(text: str) -> dict:
    out = {}
    for m in re.finditer(r"#define\s+(\w+)\s+([-\d.]+)f?", text):
        try:
            out[m.group(1)] = float(m.group(2))
        except ValueError:
            pass
    return out


def parse_arrays(text: str) -> dict:
    """解析 static const <type> <name>[n] = { ... }; 形式的所有数组。"""
    out = {}
    pat = re.compile(r"static const (int8_t|float)\s+(\w+)\[(\d+)\]\s*=\s*\{([^}]*)\};")
    for m in pat.finditer(text):
        typ, name, n, body = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        vals = [v.strip() for v in body.split(",") if v.strip()]
        assert len(vals) == n, f"{name}: 声明 {n} 个，实得 {len(vals)} 个"
        arr = np.array([float(v.rstrip("f")) for v in vals], dtype=np.float32)
        if typ == "int8_t":
            arr = arr.astype(np.int8)
        out[name] = arr
    return out


def parse_layer_table(text: str) -> list[dict]:
    """解析 g_model_layers 描述表。"""
    m = re.search(r"g_model_layers\[\]\s*=\s*\{(.*?)\n\};", text, re.S)
    if not m:
        raise RuntimeError("未找到 g_model_layers 表")
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        f = [x.strip() for x in line.strip("{},").split(",")]
        assert len(f) == 21, f"描述表字段数异常: {len(f)} -> {f}"
        rows.append({
            "dw": f[0], "dw_scale": float(f[1].rstrip("f")),
            "pw": f[2], "pw_scale": float(f[3].rstrip("f")),
            "bias": f[4],
            "se_q1": f[5], "se_scale1": float(f[6].rstrip("f")), "se_b1": f[7],
            "se_q2": f[8], "se_scale2": float(f[9].rstrip("f")), "se_b2": f[10],
            "c_in": int(f[11]), "c_out": int(f[12]), "k": int(f[13]),
            "pad": int(f[14]), "se_h": int(f[15]),
            "residual": int(f[16]), "se": int(f[17]), "do_pool": int(f[18]),
            "pool_k": int(f[19]), "pool_s": int(f[20]),
        })
    return rows


# ---------------------------------------------------------------------------
# 严格复刻 ecg_infer.c 的算法
# ---------------------------------------------------------------------------
def c_forward(x: np.ndarray, rr: np.ndarray | None, cfg: dict, arrays: dict,
              layers: list[dict]) -> np.ndarray:
    in_len = int(cfg["MODEL_INPUT_LEN"])
    n_rr = int(cfg["MODEL_N_RR_FEATURES"])

    def relu(a):
        return np.maximum(a, 0.0)

    logits = np.zeros((x.shape[0], int(cfg["FC_OUT"])), dtype=np.float32)
    for bi in range(x.shape[0]):
        cur = np.asarray(x[bi, 0], dtype=np.float32).copy()   # (len,)
        c_in, length = 1, in_len
        for L in layers:
            dw = arrays[L["dw"]].astype(np.float32) * np.float32(L["dw_scale"])
            pw = arrays[L["pw"]].astype(np.float32) * np.float32(L["pw_scale"])
            bias = arrays[L["bias"]].astype(np.float32)
            k, pad, c_out = L["k"], L["pad"], L["c_out"]

            # depthwise（零填充，逐通道）
            rows = cur.reshape(c_in, length)
            dw_out = np.zeros((c_in, length), dtype=np.float32)
            for c in range(c_in):
                for t in range(length):
                    acc = 0.0
                    for j in range(k):
                        s = t - pad + j
                        if 0 <= s < length:
                            acc += rows[c, s] * dw[c * k + j]
                    dw_out[c, t] = acc

            # pointwise + bias + ReLU
            out = np.zeros((c_out, length), dtype=np.float32)
            for co in range(c_out):
                acc = np.full(length, bias[co], dtype=np.float32)
                for ci in range(c_in):
                    acc += dw_out[ci] * pw[co * c_in + ci]
                out[co] = acc
            out = relu(out)

            # 通道注意力
            if L["se"]:
                g = out.mean(axis=1)                                  # (c_out,)
                w1 = arrays[L["se_q1"]].astype(np.float32) * np.float32(L["se_scale1"])
                b1 = arrays[L["se_b1"]].astype(np.float32)
                hid = relu(g @ w1.reshape(L["se_h"], c_out).T + b1)    # (se_h,)
                w2 = arrays[L["se_q2"]].astype(np.float32) * np.float32(L["se_scale2"])
                b2 = arrays[L["se_b2"]].astype(np.float32)
                gate = 1.0 / (1.0 + np.exp(-(hid @ w2.reshape(c_out, L["se_h"]).T + b2)))
                out = out * gate[:, None].astype(np.float32)

            # 残差
            if L["residual"]:
                out = relu(out + rows)

            # 原地最大池化
            if L["do_pool"]:
                kk, ss = L["pool_k"], L["pool_s"]
                nl = (length - kk) // ss + 1
                pooled = np.zeros((c_out, nl), dtype=np.float32)
                for t in range(nl):
                    pooled[:, t] = out[:, t * ss:t * ss + kk].max(axis=1)
                out, length = pooled, nl

            cur = out.reshape(-1)
            c_in = c_out

        feat = out.mean(axis=1)                                        # (c_in,)
        if n_rr:
            r = rr[bi] if rr is not None else np.ones(n_rr, dtype=np.float32)
            r = np.asarray(r, dtype=np.float32)
            if n_rr == 4 and r.shape[0] == 2:
                pre = r[0:1]
                post = r[1:2]
                ratio = np.clip(pre / (post + 1e-6), 0.3, 3.0)
                r = np.concatenate([r, ratio, post - pre])
            feat = np.concatenate([feat, r])
        fc_w = arrays["fc_w"].astype(np.float32) * np.float32(cfg["FC_W_SCALE"])
        fc_b = arrays["fc_b"].astype(np.float32)
        logits_bi = feat @ fc_w.reshape(int(cfg["FC_OUT"]), int(cfg["FC_IN"])).T + fc_b

        # RR 间期旁路（与 ecg_infer.c 一致）：logits += Linear(2,16)+ReLU+Linear(16,5)
        if int(cfg.get("MODEL_HAS_RR_HEAD", 0)):
            rr_h = int(cfg["RR_H"])
            r = rr[bi] if rr is not None else np.ones(n_rr, dtype=np.float32)
            r = np.asarray(r, dtype=np.float32)
            if n_rr == 4 and r.shape[0] == 2:
                pre = r[0:1]
                post = r[1:2]
                ratio = np.clip(pre / (post + 1e-6), 0.3, 3.0)
                r = np.concatenate([r, ratio, post - pre])
            w1 = arrays["rr_w1"].astype(np.float32) * np.float32(cfg["RR_W1_SCALE"])
            b1 = arrays["rr_b1"].astype(np.float32)
            w2 = arrays["rr_w2"].astype(np.float32) * np.float32(cfg["RR_W2_SCALE"])
            b2 = arrays["rr_b2"].astype(np.float32)
            h = relu(r @ w1.reshape(rr_h, n_rr).T + b1)
            logits_bi = logits_bi + (h @ w2.reshape(int(cfg["FC_OUT"]), rr_h).T + b2)
        logits[bi] = logits_bi
    return logits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="res_se_cnn")
    ap.add_argument("--beats", type=int, default=200)
    args = ap.parse_args()

    from config import MODEL_DIR, PROCESSED_DIR
    from export_c_model import load_weights
    from models import build_model

    data = (CMODEL / "model_data.h").read_text(encoding="utf-8")
    cfg = parse_defines((CMODEL / "model_config.h").read_text(encoding="utf-8"))
    arrays = parse_arrays(data)
    layers = parse_layer_table(data)
    print(f"解析到 {len(layers)} 层、{len(arrays)} 个数组")

    # 结构自洽性检查
    ch = 1
    for i, L in enumerate(layers):
        assert L["c_in"] == ch, f"第{i}层 c_in={L['c_in']} 与上一层 c_out={ch} 不符"
        ch = L["c_out"]
    print(f"通道链自洽，最终通道数 {ch}，FC_IN={cfg['FC_IN']}（应等于 {ch}+RR）")
    assert int(cfg["FC_IN"]) == ch + int(cfg["MODEL_N_RR_FEATURES"])

    # 缓冲容量检查：按层表真实轨迹推演长度（与 export_c_model.py::_shapes 同口径）。
    # 旧写法用「前 3 层每层都减半」的启发式近似长度轨迹，池化层位置一旦变动，
    # 会算出偏小的需求而漏报（假通过）或偏大而误报（假拒绝）。
    cur_len = int(cfg["MODEL_INPUT_LEN"])
    need_act = max(cur_len, layers[0]["c_in"] * cur_len)   # 输入 1×L 与 stem 输出
    for L in layers:
        need_act = max(need_act, L["c_out"] * cur_len)          # 池化前缓冲
        if L["do_pool"]:
            cur_len = (cur_len - L["pool_k"]) // L["pool_s"] + 1
            need_act = max(need_act, L["c_out"] * cur_len)      # 池化后缓冲
    assert cfg["MODEL_MAX_ACT"] >= need_act, (
        f"MODEL_MAX_ACT 偏小：按层表推演需 {need_act}，配置为 {int(cfg['MODEL_MAX_ACT'])}")
    print(f"缓冲容量: ACT={cfg['MODEL_MAX_ACT']} DW={cfg['MODEL_MAX_DW']} "
          f"CH={cfg['MODEL_MAX_CH']} HID={cfg['MODEL_MAX_HID']}")

    # 与 PyTorch 对比
    model = build_model(args.model)
    load_weights(model, MODEL_DIR / f"{args.model}_best.pt")
    model.eval()
    te = np.load(PROCESSED_DIR / "test_windows.npy")[:args.beats][:, None, :]
    rr = None
    if getattr(model, "rr_features", False):
        rr = np.load(PROCESSED_DIR / "test_rrfeat.npy")[:args.beats]
    with torch.no_grad():
        ref = (model(torch.as_tensor(te), torch.as_tensor(rr)) if rr is not None
               else model(torch.as_tensor(te))).numpy()

    got = c_forward(te, rr, cfg, arrays, layers)
    max_err = float(np.max(np.abs(ref - got)))
    agree = float((ref.argmax(1) == got.argmax(1)).mean())
    print(f"\nC 头文件 + C 算法  vs PyTorch：max_err={max_err:.6f}，"
          f"argmax 一致率={agree:.4f}（{args.beats} 拍）")
    if agree == 1.0:
        print("结论：导出数据与 C 算法语义与 PyTorch 完全一致 [OK]")
    else:
        print("结论：存在不一致，需排查 [FAIL]")
        sys.exit(1)


if __name__ == "__main__":
    main()
