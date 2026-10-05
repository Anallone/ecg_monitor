"""实时推理引擎：读入 ECG -> 滤波 -> R峰检测 -> 滑动窗口逐拍分类 -> 心率/报警。

模拟「真实流式」：逐采样推进，检测到 R 峰即截取前 64 点窗口做推理。
"""
from __future__ import annotations

import numpy as np
import torch

from config import CLASSES, FS, IDX2CLASS, MODEL_INPUT_LEN, PRE_R, POST_R
from models import build_model
from preprocessing import bandpass_filter, notch_filter, pan_tompkins, zscore_normalize

# 心率报警阈值（bpm）
HR_HIGH = 100
HR_LOW = 50


class BeatClassifier:
    """加载训练好的模型，做逐拍分类。"""

    def __init__(self, model_name: str, model_path, device: str = "cpu"):
        self.device = torch.device(device)
        self.model = build_model(model_name)
        self.model.load_state_dict(torch.load(model_path, map_location=self.device,
                                              weights_only=True))
        self.model.eval()
        self.rr_features = bool(getattr(self.model, "rr_features", False))

    @torch.no_grad()
    def predict(self, window: np.ndarray, rr_feat: np.ndarray | None = None) -> tuple[int, str, np.ndarray]:
        """输入归一化后的 187 点窗口（可选 RR 特征），返回 (类别下标, 类别名, 概率分布)。"""
        x = torch.as_tensor(window, dtype=torch.float32).unsqueeze(0).unsqueeze(0).to(self.device)
        if self.rr_features:
            if rr_feat is None:
                # 全链路回退约定：缺 RR 特征用 1.0（= 平均 RR 归一化值，中性），
                # 与固件 ecg_infer.c 的 ones 回退、训练侧缺邻接 RR 的 mean 回退一致。
                rr_feat = np.ones(2, dtype=np.float32)
            rr = torch.as_tensor(rr_feat, dtype=torch.float32).unsqueeze(0).to(self.device)
            logits = self.model(x, rr)[0]
        else:
            logits = self.model(x)[0]
        prob = torch.softmax(logits, dim=0).cpu().numpy()
        idx = int(prob.argmax())
        return idx, IDX2CLASS[idx], prob


def _rr_features_from_peaks(r_peaks: np.ndarray, fs: int = 360) -> np.ndarray:
    """由 R 峰序列计算归一化 pre-RR / post-RR 特征（与训练侧一致，裁剪到 [0.3, 3.0]）。

    与 `data_loader.extract_beats` 对齐：首拍 pre-RR 与末拍 post-RR 用平均 RR 回退
    （归一化后即 1.0），而非用相邻第一个/最后一个实际 RR，保证推理与训练一致。
    """
    peaks = np.asarray(r_peaks, dtype=np.int64)
    n = len(peaks)
    feat = np.ones((n, 2), dtype=np.float32)
    if n == 0:
        return feat
    rr_sec = np.diff(peaks) / fs
    mean_rr = float(np.mean(rr_sec)) if len(rr_sec) else 1.0
    # 首尾回退到平均 RR（与训练侧一致）；n==1 时 rr_sec 为空，pre/post 均为 [mean_rr]
    pre = np.concatenate([[mean_rr], rr_sec])
    post = np.concatenate([rr_sec, [mean_rr]])
    pre_n = np.clip(pre / mean_rr, 0.3, 3.0)
    post_n = np.clip(post / mean_rr, 0.3, 3.0)
    feat[:, 0] = pre_n
    feat[:, 1] = post_n
    return feat


class RealtimeEngine:
    """流式引擎：维护滑动缓冲，逐采样处理并输出事件。

    事件格式:
        {"type": "beat", "r_peak": int, "class": str, "class_idx": int,
         "prob": np.ndarray, "hr": float | None, "alarm": str | None}
    """

    def __init__(self, classifier: BeatClassifier, fs: int = 360,
                 pre: int = PRE_R, post: int = POST_R):
        self.classifier = classifier
        self.fs = fs
        self.pre, self.post = pre, post
        self._reset()

    def _reset(self):
        self.buffer = np.array([], dtype=np.float32)   # 滤波后的信号缓冲
        self.last_r = None

    def reset(self):
        """清空内部状态（供 GUI 每次重新分析前调用，避免缓冲累积）。"""
        self._reset()

    def _update_hr(self, r_peak: int) -> tuple[float | None, str | None]:
        """由 R-R 间期计算瞬时心率，并判定报警。"""
        hr = None
        alarm = None
        if self.last_r is not None:
            rr = (r_peak - self.last_r) / self.fs
            if 0.2 < rr < 3.0:  # 合理范围 20–300 bpm
                hr = 60.0 / rr
                if hr > HR_HIGH:
                    alarm = "high"
                elif hr < HR_LOW:
                    alarm = "low"
        self.last_r = r_peak
        return hr, alarm

    def process(self, raw: np.ndarray) -> list[dict]:
        """处理一段原始采样（整段），返回事件列表。

        每次调用视为一次全新分析：清空缓冲后对整段做「单次」滤波 + R 峰检测 +
        逐拍推理。这是 GUI / 演示用的整段接口。
        """
        self._reset()
        if raw.ndim > 1:
            raw = raw[:, 0]
        sig = raw.astype(np.float64)
        sig = notch_filter(bandpass_filter(sig)).astype(np.float32)
        r_peaks = pan_tompkins(sig, self.fs)
        return self._classify(sig, r_peaks)

    def _classify(self, sig: np.ndarray, r_peaks: np.ndarray) -> list[dict]:
        """对已滤波信号按给定 R 峰逐拍分类（不重复滤波/检测）。"""
        self._reset()
        self.buffer = sig.astype(np.float32)
        events = []
        rr_feats = _rr_features_from_peaks(r_peaks, self.fs)
        for i, r in enumerate(r_peaks):
            start, end = r - self.pre, r + self.post + 1
            if start < 0 or end > len(sig):
                continue
            window = zscore_normalize(sig[start:end])
            idx, name, prob = self.classifier.predict(window, rr_feats[i])
            hr, alarm = self._update_hr(r)
            events.append({
                "type": "beat", "r_peak": int(r), "class": name,
                "class_idx": idx, "prob": prob, "hr": hr, "alarm": alarm,
            })
        return events

    def classify_offline(self, sig: np.ndarray, r_peaks: np.ndarray) -> list[dict]:
        """离线模式：给定「原始」信号与 R 峰，滤波一次后逐拍分类。

        注意：r_peaks 应在同一滤波后的信号上检测（与 process 内部一致）。
        """
        if sig.ndim > 1:
            sig = sig[:, 0]
        sig = sig.astype(np.float64)
        sig = notch_filter(bandpass_filter(sig)).astype(np.float32)
        return self._classify(sig, r_peaks)


# ---------------------------------------------------------------------------
# 因果流式引擎：与端侧 firmware/components/realtime/realtime.c 同构
# ---------------------------------------------------------------------------
# 端侧固件是「游标推进 + 尾随窗检测 + 逐拍分类」，本类按同一套常数与顺序实现，
# 使 PC 端与端侧在同一份样本上给出同一批心拍、同一个心率/报警序列。
#   integ[i] = 尾随 MA_WIN 个 diff^2 的均值（只用过去点，真流式）
#   thr[i]   = 含当前点的尾随 THR_WIN 个 integ 的 mean + 0.5*std
#   R 峰     = integ[i-1] 局部极大且 > thr[i]，峰间至少 MIN_DIST，再向原信号精修
#   分类延迟一拍：post-RR 需要下一个 R 峰；末尾那拍用 mean_rr 回退
MA_WIN = 54          # 因果移动平均窗（0.15s @360Hz）
THR_WIN = 1800       # 滑动阈值窗（5s @360Hz）
WARMUP = 720         # 预热采样数（不足此长度不做检测）
MIN_DIST = 97        # 最小 R 峰间距（≈220bpm 上限）
REFINE_BACK = 30     # 峰位精修：向前搜
REFINE_FWD = 10      # 峰位精修：向后搜


class StreamingEngine:
    """因果流式引擎：load() 预计算积分/阈值曲线，tick() 逐 tick 推进游标。

    与 RealtimeEngine 的区别：不做整段滤波与整段 R 峰检测（那是非因果的批处理），
    而是像固件一样「推到哪算到哪」——R 峰一确认就更新心率/报警，分类比检测晚一拍。

    用法：
        eng = StreamingEngine(BeatClassifier(...))
        eng.load(filtered_sig)          # 已滤波信号（端侧样本本身就是预滤波的）
        while not eng.finished:
            st = eng.tick(20)           # 20ms 一个 tick，speed=4 即 4 倍速
    """

    def __init__(self, classifier: BeatClassifier, fs: int = FS, speed: int = 4,
                 pre: int = PRE_R, post: int = POST_R):
        self.classifier = classifier
        self.fs = fs
        # 允许小数倍速（PC 端 GUI 提供 0.5×~8× 档位）；固件侧仍用整数倍速。
        self.speed = max(0.1, float(speed))
        self.pre, self.post = pre, post
        self.sig: np.ndarray | None = None
        self.integ = np.zeros(0, dtype=np.float64)
        self.thr = np.zeros(0, dtype=np.float64)
        # 流式模式：feed() 边到边算，用于 BLE 等不一次性给全的源。
        # False 时按批处理语义（load 一整段，末尾 tick 冲刷最后一拍）。
        self.streaming = False
        self._reset_curve_state()
        self.reset()

    # ---- 曲线预计算的滚动状态（feed 增量维护）----
    def _reset_curve_state(self) -> None:
        """清零 integ/thr 增量计算的滚动累加器（不涉及播放游标）。"""
        self._acc = 0.0                                # 尾随 MA_WIN 个 diff² 之和
        self._thr_buf = np.zeros(THR_WIN, dtype=np.float64)
        self._thr_idx = 0
        self._thr_cnt = 0
        self._thr_sum = 0.0
        self._thr_sumsq = 0.0

    # ---- 生命期 ----
    def load(self, sig: np.ndarray) -> None:
        """装载一整段**已滤波**信号并预计算检测曲线（对应固件 rt_init 的预处理）。

        预计算只用过去点，因此不违反因果性：固件同样在 rt_init 里一次性算好整段 integ。
        """
        sig = np.asarray(sig, dtype=np.float32).ravel()
        self.sig = sig
        self.streaming = False
        self._reset_curve_state()
        n = len(sig)

        # 积分信号：diff^2 的尾随 MA_WIN 点均值（i < MA_WIN 时按已有点数取均值）
        d = np.zeros(n, dtype=np.float64)
        if n > 1:
            d[1:] = np.diff(sig.astype(np.float64))
        sq = d * d
        c = np.cumsum(sq)
        integ = np.zeros(n, dtype=np.float64)
        if n > MA_WIN:
            integ[MA_WIN:] = (c[MA_WIN:] - c[:-MA_WIN]) / MA_WIN
        m = min(n, MA_WIN)
        integ[:m] = c[:m] / (np.arange(m) + 1.0)

        # 阈值：含当前点的尾随 THR_WIN 点 mean + 0.5*std（固件滑动阈值窗的等价实现）
        c1 = np.cumsum(integ)
        c2 = np.cumsum(integ * integ)
        idx = np.arange(n)
        cnt = np.minimum(idx + 1, THR_WIN)
        start = idx - THR_WIN                         # 窗口为 integ[idx-W+1 .. idx]，共 cnt 点
        s1, s2 = c1.copy(), c2.copy()
        mask = start >= 0
        s1[mask] -= c1[start[mask]]
        s2[mask] -= c2[start[mask]]
        mean = s1 / cnt
        var = np.maximum(s2 / cnt - mean * mean, 0.0)
        thr = np.where(cnt < 2, 0.0, mean + 0.5 * np.sqrt(var))

        self.integ, self.thr = integ, thr
        self.reset()                                  # 预计算不算推进，游标归零

    def begin_stream(self) -> None:
        """开始一个流式会话：清空缓冲与曲线，等待 feed()。"""
        self.sig = np.zeros(0, dtype=np.float32)
        self.integ = np.zeros(0, dtype=np.float64)
        self.thr = np.zeros(0, dtype=np.float64)
        self.streaming = True
        self._reset_curve_state()
        self.reset()

    def feed(self, samples: np.ndarray) -> None:
        """增量喂入一段**已滤波**样本（流式源用；未 begin_stream 会自动开始）。

        integ / thr 用滚动和增量维护，逐点复刻固件 realtime.c 的
        build_integrated() 与 thr_win_push()：因此「分块 feed」与「load 整段」
        得到的曲线一致（见 tests 里的一致性断言）。
        """
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if samples.size == 0:
            return
        if not self.streaming or self.sig is None:
            self.begin_stream()

        n0 = len(self.sig)
        self.sig = np.concatenate([self.sig, samples])
        m = samples.size
        integ_new = np.empty(m, dtype=np.float64)
        thr_new = np.empty(m, dtype=np.float64)

        for j in range(m):
            i = n0 + j
            cur = float(samples[j])
            prev = float(self.sig[i - 1]) if i > 0 else cur
            d = cur - prev
            self._acc += d * d
            if i >= MA_WIN:
                k = i - MA_WIN
                kprev = float(self.sig[k - 1]) if k > 0 else float(self.sig[k])
                dout = float(self.sig[k]) - kprev
                self._acc -= dout * dout
                integ = self._acc / MA_WIN
            else:
                integ = self._acc / (i + 1)
            integ_new[j] = integ

            # 阈值窗：含当前点的尾随 THR_WIN 点 mean + 0.5*std（环形增量维护）
            if self._thr_cnt == THR_WIN:
                old = self._thr_buf[self._thr_idx]
                self._thr_sum -= old
                self._thr_sumsq -= old * old
            else:
                self._thr_cnt += 1
            self._thr_buf[self._thr_idx] = integ
            self._thr_sum += integ
            self._thr_sumsq += integ * integ
            self._thr_idx = (self._thr_idx + 1) % THR_WIN

            if self._thr_cnt < 2:
                thr_new[j] = 0.0
            else:
                mean = self._thr_sum / self._thr_cnt
                var = self._thr_sumsq / self._thr_cnt - mean * mean
                thr_new[j] = mean + 0.5 * np.sqrt(var if var > 0.0 else 0.0)

        self.integ = np.concatenate([self.integ, integ_new])
        self.thr = np.concatenate([self.thr, thr_new])

    def end_stream(self) -> list[dict]:
        """流结束：把尾段推完并按「已到末尾」冲刷最后一拍（与 load() 路径一致）。

        返回尾段新分类的心拍（供 GUI 补画标记）。
        """
        self.streaming = False
        out: list[dict] = []
        while self.sig is not None and self.pos < len(self.sig):
            out += self.tick(20)["new_beats"]
        out += self._flush_classify(tail=True)
        return out

    def reset(self) -> None:
        """游标与统计复位（不重算预计算曲线），用于重播。"""
        self.pos = 0
        self.last_r = -1
        self.beats: list[dict] = []      # {"r": int, "class_idx": int|-1, "hr": float|None, "alarm": int}
        self.n_classified = 0
        self.rr_sum = 0.0
        self.rr_cnt = 0
        self.mean_rr = 1.0               # 与训练侧「无邻接 RR 回退 1.0」一致
        self.hr = 0.0
        self.alarm = 0
        self.counts = [0] * len(CLASSES)

    # ---- 查询 ----
    @property
    def finished(self) -> bool:
        return self.sig is not None and self.pos >= len(self.sig)

    @property
    def progress(self) -> float:
        n = 0 if self.sig is None else len(self.sig)
        return (self.pos / n) if n else 0.0

    def view(self, points: int) -> tuple[np.ndarray, int]:
        """当前显示窗口：返回 (窗口内的信号, 窗口左端对应的全局下标)。"""
        if self.sig is None or len(self.sig) == 0:
            return np.zeros(0, dtype=np.float32), 0
        to = min(self.pos, len(self.sig))
        frm = max(0, to - points)
        return self.sig[frm:to], frm

    # ---- 推进（对应固件 rt_tick）----
    def tick(self, tick_ms: int = 20) -> dict:
        """推进一个 tick，返回本 tick 新分类的心拍与当前状态。"""
        if self.sig is None or len(self.sig) == 0:
            # 流式会话开始时 sig 是空的（begin_stream 置零缓冲、等 feed），此时 tick
            # 仍要返回与正常路径同构的字典，否则 GUI 里 st["alarm"]/st["hr"] 会 KeyError。
            return {"new_beats": [], "pos": 0, "hr": self.hr, "alarm": self.alarm,
                    "counts": list(self.counts), "finished": True}

        n = len(self.sig)
        step = max(1, int(self.speed * self.fs * tick_ms / 1000))
        # 流式会话里，峰位精修要看 cand+REFINE_FWD 个采样。若这些点还没喂进来，
        # 此刻处理会让精修窗口被截断、择出不同的峰位。故只推进到「还留够前瞻」的位置，
        # 尾段留给 end_stream() 处理（与批处理在样本末尾的截断行为一致）。
        limit = max(0, n - REFINE_FWD) if self.streaming else n
        frm = self.pos
        to = min(self.pos + step, limit)

        if frm < to:
            integ, thr, sig = self.integ, self.thr, self.sig
            for i in range(frm, to):
                if i < WARMUP:
                    continue
                pm = integ[i - 1]
                # 确认 i-1 是否为峰：需要 i 作为右邻（1 点延迟，因果）
                if not (pm > thr[i] and pm >= integ[i] and pm >= integ[i - 2]):
                    continue
                cand = i - 1
                if self.last_r >= 0 and (cand - self.last_r) < MIN_DIST:
                    continue
                lo = max(0, cand - REFINE_BACK)
                hi = min(n - 1, cand + REFINE_FWD)
                best = int(np.argmax(sig[lo:hi + 1])) + lo
                if self.last_r >= 0 and (best - self.last_r) < MIN_DIST:
                    continue
                self._on_r(best)
            self.pos = to

        # 流式会话里 pos 追平缓冲是常态（等新数据），此时不能当「已到末尾」冲刷。
        tail = self.finished and not self.streaming
        new_beats = self._flush_classify(tail=tail)
        return {
            "new_beats": new_beats,
            "pos": self.pos,
            "hr": self.hr,
            "alarm": self.alarm,
            "counts": list(self.counts),
            "finished": self.finished,
        }

    # ---- 内部 ----
    def _on_r(self, r: int) -> None:
        """R 峰确认：更新心率/报警（不延迟）并登记心拍。"""
        if self.last_r >= 0:
            rr = (r - self.last_r) / self.fs
            if 0.2 < rr < 3.0:                       # 20–300 bpm
                self.rr_sum += rr
                self.rr_cnt += 1
                self.mean_rr = self.rr_sum / self.rr_cnt
                self.hr = 60.0 / rr
                self.alarm = 1 if self.hr > HR_HIGH else (2 if self.hr < HR_LOW else 0)
        self.beats.append({"r": r, "class_idx": -1, "hr": self.hr, "alarm": self.alarm})
        self.last_r = r

    def _classify_beat(self, i: int) -> None:
        """对第 i 拍分类（调用前保证 next-RR 可得或已到末尾）。"""
        sig = self.sig
        r = self.beats[i]["r"]
        s, e = r - self.pre, r + self.post + 1
        if s < 0 or e > len(sig):
            self.n_classified = i + 1
            return                                    # 边界拍：不出结果（与固件一致）
        window = zscore_normalize(sig[s:e])

        mrr = self.mean_rr if self.mean_rr > 1e-3 else 1.0
        pre_sec = (r - self.beats[i - 1]["r"]) / self.fs if i > 0 else self.mean_rr
        post_sec = ((self.beats[i + 1]["r"] - r) / self.fs
                    if i + 1 < len(self.beats) else self.mean_rr)
        rr_feat = np.array([np.clip(pre_sec / mrr, 0.3, 3.0),
                            np.clip(post_sec / mrr, 0.3, 3.0)], dtype=np.float32)
        idx, _name, _prob = self.classifier.predict(window, rr_feat)
        self.beats[i]["class_idx"] = int(idx)
        self.counts[int(idx)] += 1
        self.n_classified = i + 1

    def _flush_classify(self, tail: bool) -> list[dict]:
        """推进待分类游标：下一拍已知、或已到末尾时才分类（分类比检测晚一拍）。"""
        out: list[dict] = []
        while self.n_classified < len(self.beats):
            i = self.n_classified
            if not (i + 1 < len(self.beats)) and not tail:
                break
            self._classify_beat(i)
            b = self.beats[i]
            out.append({"r_peak": b["r"], "class_idx": b["class_idx"],
                        "class": IDX2CLASS[b["class_idx"]] if b["class_idx"] >= 0 else "--",
                        "hr": b["hr"], "alarm": b["alarm"]})
        return out
