# -*- coding: utf-8 -*-
"""生成综合设计报告的插图（输出到 docs/report/figures/）。

用法（项目根目录）:
    ./runtime/python/python.exe tools/make_figures.py

生成：
    图1  系统总体框图
    图2  训练收敛曲线（来自 models/res_se_cnn_rr4_metrics.json 的真实 history，报告图3）
    图3  跨患者测试集混淆矩阵（报告图9）
    图4  GUI 实时监测界面截图（PySide6 离屏渲染，驱动真实 StreamingEngine）
    图5  端侧固件状态机与页面流转（报告图8）
    图6  因果流式处理时序（报告图5）

排版约束（本轮重做重点）
  - 字号唯一：图内所有文字（标题 / 坐标轴标签 / 刻度 / 图例 / 方框文字 / 注释）
    一律 10.5 pt（= 五号），脚本末尾会统计并校验「非 10.5 pt 文字」。
  - 字体：西文与数字 Times New Roman（C:/Windows/Fonts/times.ttf），
    中文宋体（C:/Windows/Fonts/simsun.ttc）。用 addfont 注册后，
    rcParams["font.serif"] = ["Times New Roman", "SimSun"]、rcParams["font.family"] = "serif"
    （matplotlib 里 "Times New Roman"/"SimSun" 这类具体字体名不能直接写进 font.family，
     写进去会被判为未知族名并回退到 DejaVu Sans；因此写进 serif 族列表、
     由 serif 族驱动 "Times New Roman" 在前、"SimSun" 兜底中文）。
    数学文本走 mathtext custom，rm / it / bf 全部指向 Times New Roman。
  - 画布物理尺寸 = 文档显示尺寸：figsize=(宽cm/2.54, 高cm/2.54)，dpi=目标像素宽/宽(inch)，
    这样 10.5 pt 落到文档里就是真正的五号；保存后再用 PIL 精确校正到目标像素。
  - 内容占满画布：所有坐标都按「物理厘米 → 数据坐标」换算后显式书写，
    文字宽度用渲染器实测（不靠估算），方框尺寸由实测文字包围盒反推。

实现要点（因为生成环境无法目视预览，全部靠几何量测保证）：
  - 框图文字先绘制、量出实际包围盒，再按包围盒反推方框尺寸 —— 文字不会超出边框；
  - 箭头只走正交走廊，并逐段做「线段—矩形相交」校验 —— 连线不会横跨方框；
  - 标题 / 副标题 / 页脚等自由文字逐一对「方框 + 其它文字」做重叠校验；
  - 图内文字逐字做「方框字」自检（与私用区无字形字符位图比对）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "docs" / "report" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

# ---------------------------------------------------------------------------
# 字体：Times New Roman（西文/数字）+ SimSun 宋体（中文）
# ---------------------------------------------------------------------------
TIMES_TTF = "C:/Windows/Fonts/times.ttf"
SIMSUN_TTC = "C:/Windows/Fonts/simsun.ttc"
# 兜底候选（times.ttf 缺失时才使用）
TIMES_FALLBACKS = ["C:/Windows/Fonts/timesbd.ttf", "C:/Windows/Fonts/georgia.ttf",
                   "C:/Windows/Fonts/constan.ttf"]
CJK_FONT_CANDIDATES = ["C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/simhei.ttf",
                       "C:/Windows/Fonts/msyh.ttc"]

FS = 10.5           # 全图唯一字号：五号（10.5 pt）
lw_box = 1.1        # 方框描边
lw_line = 1.1       # 连线
# 报告配色（浅色底、蓝灰主色、砖红仅用于报警/强调）
C_BLUE, C_GREEN, C_RED, C_PURPLE, C_GRAY = "#2c6fbb", "#27803f", "#c0392b", "#7d3cbb", "#8a9099"
C_TEXT, C_MUTED = "#333333", "#555555"
FC_BLUE, FC_GREEN, FC_RED, FC_GRAY, FC_PURPLE = "#eaf2fb", "#eef7ee", "#fdeeee", "#f2f2f2", "#f4eefb"


def _squash(s: str) -> str:
    """压缩多余空白（连续空格合并、去掉“ ”两侧空格），文字更紧凑。"""
    s = (s.replace("\u3000", " ").replace("\u2009", " ").replace("\u202f", " ")
         .replace("\xa0", " "))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\s*/\s*", " / ", s)
    return s.strip()

PROBLEMS: list[str] = []
FONT_SIZES: list[tuple[str, float]] = []


def _register_fonts() -> tuple[str, str]:
    """注册 Times New Roman 与宋体，返回 (西文字体文件, 中文字体文件)。"""
    latin = TIMES_TTF if Path(TIMES_TTF).exists() else next(
        (f for f in TIMES_FALLBACKS if Path(f).exists()), None)
    if latin is None:
        raise SystemExit("找不到 Times 系列西文字体文件")
    cjk = next((f for f in CJK_FONT_CANDIDATES if Path(f).exists()), None)
    if cjk is None:
        raise SystemExit("找不到中文字体文件（simsun.ttc / simhei.ttf / msyh.ttc）")
    latin_name = font_manager.FontProperties(fname=latin).get_name()
    cjk_name = font_manager.FontProperties(fname=cjk).get_name()
    font_manager.fontManager.addfont(latin)
    font_manager.fontManager.addfont(cjk)

    # matplotlib 的 font.family 只认 generic 族名 + 已安装族名；具体族名写在族列表里，
    # 由 serif 族解析：先 Times New Roman（西文/数字），缺字形的中文回退 SimSun。
    # 注意：matplotlib 只有在 font.family 里写「具体族名列表」时才会做缺字形回退；
    # 写成 font.family=["serif"] + font.serif=[...] 时中文不会回退，会渲染成方框（已实测）。
    matplotlib.rcParams["font.family"] = [latin_name, cjk_name, "DejaVu Serif"]
    matplotlib.rcParams["font.serif"] = [latin_name, cjk_name, "DejaVu Serif"]
    matplotlib.rcParams["font.sans-serif"] = [latin_name, cjk_name, "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    matplotlib.rcParams["font.size"] = FS
    matplotlib.rcParams["mathtext.fontset"] = "custom"
    matplotlib.rcParams["mathtext.rm"] = latin_name
    matplotlib.rcParams["mathtext.it"] = f"{latin_name}:italic"
    matplotlib.rcParams["mathtext.bf"] = f"{latin_name}:bold"

    # 实测校验：西文解析到 Times，中文解析到宋体（避免静默回退 DejaVu）
    probe_latin = font_manager.findfont(font_manager.FontProperties(family=[latin_name]))
    probe_cjk = font_manager.findfont(font_manager.FontProperties(family=[cjk_name]))
    if Path(probe_latin).name.lower() != Path(latin).name.lower():
        PROBLEMS.append(f"字体: 西文未解析到 {latin}，而是 {probe_latin}")
    if Path(probe_cjk).name.lower() != Path(cjk).name.lower():
        PROBLEMS.append(f"字体: 中文未解析到 {cjk}，而是 {probe_cjk}")
    print(f"字体: 西文={latin_name} ({latin}) / 中文={cjk_name} ({cjk}) / 字号={FS} pt")
    return latin, cjk


LATIN_FONT, CJK_FONT = _register_fonts()

# 报告图2/图3 采用主线模型的实测指标。必须与 docs/tools/fill_report.py 的 MAIN_MODEL
# 保持一致，否则正文数字与插图会取自不同的模型。
MODEL_NAME = "res_se_cnn_rr4"
METRICS = json.loads((ROOT / "models" / f"{MODEL_NAME}_metrics.json").read_text(encoding="utf-8"))
CLASSES = ["N", "S", "V", "F", "Q"]
N_PARAMS = int(METRICS["n_params"])                    # 37,445
TEST_ACC = float(METRICS["test_acc"]) * 100            # 94.30 %
MACRO_F1 = float(METRICS["test_macro_f1"]) * 100       # 43.31 %
DEPLOY_KB = round(N_PARAMS * 4 * 0.2652 / 1024, 2)     # 38.79 KB（与报告表 12 一致）
INT8_BYTES = 36127 + 3592                              # int8 权重 + float 偏置 = 39,719 B


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def text_size(ax, fig, lines, fontsize=FS, linespacing=1.35, weight="normal"):
    """实测文字在数据坐标下的宽度、高度（不留下残留文字）。"""
    if isinstance(lines, (list, tuple)):
        s = "\n".join(_squash(x) for x in lines)
    else:
        s = _squash(lines)
    t = ax.text(0, 0, s, ha="center", va="center", fontsize=fontsize,
                linespacing=linespacing, fontweight=weight, zorder=5)
    fig.canvas.draw()
    bb = t.get_window_extent(renderer=fig.canvas.get_renderer()).transformed(
        ax.transData.inverted())
    t.remove()
    return bb.width, bb.height


def draw_box(ax, fig, cx, cy, lines, fc, ec, pad_x=2.2, pad_y=1.0, linespacing=1.35):
    """画一个恰好包住实测文字的圆角框，返回 (x0, y0, w, h)。"""
    lines = [_squash(x) for x in lines]
    w, h = text_size(ax, fig, lines, linespacing=linespacing)
    x0, y0 = cx - w / 2 - pad_x, cy - h / 2 - pad_y
    bw, bh = w + 2 * pad_x, h + 2 * pad_y
    ax.add_patch(FancyBboxPatch((x0, y0), bw, bh,
                                boxstyle="round,pad=0,rounding_size=1.0",
                                linewidth=lw_box, facecolor=fc, edgecolor=ec, zorder=3))
    t = ax.text(cx, cy, "\n".join(lines), ha="center", va="center", fontsize=FS,
                linespacing=linespacing, color=C_TEXT, zorder=5)
    note_size(t)
    return (x0, y0, bw, bh)


def note_size(txt) -> None:
    FONT_SIZES.append((txt.get_text()[:24].replace("\n", "⏎"), float(txt.get_fontsize())))


def seg_hits_rect(p1, p2, rect, shrink=0.6):
    """线段是否穿过矩形内部（矩形四周各内缩 shrink，避免贴边误判）。"""
    x0, y0, w, h = rect
    x0, y0, x1, y1 = x0 + shrink, y0 + shrink, x0 + w - shrink, y0 + h - shrink
    if x1 <= x0 or y1 <= y0:
        return False
    (ax_, ay), (bx, by) = p1, p2
    dx, dy = bx - ax_, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax_ - x0), (dx, x1 - ax_), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return False
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return False
                if r < t1:
                    t1 = r
    return True


def rects_overlap(a, b, tol=0.0):
    return not (a[0] + a[2] <= b[0] + tol or b[0] + b[2] <= a[0] + tol or
                a[1] + a[3] <= b[1] + tol or b[1] + b[3] <= a[1] + tol)


def check_text_inside_figure(fig, name):
    """校验所有文字都在画布内（防止被裁切），并登记字号。"""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    fb = fig.bbox
    for t in fig.findobj(matplotlib.text.Text):
        s = t.get_text()
        if not s.strip():
            continue
        note_size(t)
        try:
            bb = t.get_window_extent(renderer=r)
        except Exception:
            continue
        if bb.x0 < -1 or bb.y0 < -1 or bb.x1 > fb.x1 + 1 or bb.y1 > fb.y1 + 1:
            PROBLEMS.append(f"{name}: 文字被裁切 -> {s[:24]!r}")


def check_boxes_inside(ax, name, boxes, xlim, ylim):
    """校验方框整体（含描边）在数据坐标范围内。"""
    for nm, (x0, y0, w, h) in boxes:
        if x0 < xlim[0] or y0 < ylim[0] or x0 + w > xlim[1] or y0 + h > ylim[1]:
            PROBLEMS.append(f"{name}: 方框 {nm} 超出画布 ({x0:.2f},{y0:.2f},{w:.2f},{h:.2f})")


class FreeText:
    """收集自由文字，校验：不与方框重叠、彼此不重叠、不被连线穿过。"""

    def __init__(self, ax, fig, name):
        self.ax, self.fig, self.name = ax, fig, name
        self.items = []

    def add(self, txt):
        self.fig.canvas.draw()
        bb = txt.get_window_extent(renderer=self.fig.canvas.get_renderer()).transformed(
            self.ax.transData.inverted())
        self.items.append((txt.get_text()[:20], (bb.x0, bb.y0, bb.width, bb.height)))
        note_size(txt)
        return txt

    def text(self, x, y, s, **kw):
        """登记自由文字（自动压缩空白）。"""
        kw.setdefault("fontsize", FS)
        return self.add(self.ax.text(x, y, _squash(s), **kw))

    def check(self, boxes=(), segs=()):
        for label, r in self.items:
            for nm, box in boxes:
                if rects_overlap(r, box, tol=-0.3):
                    PROBLEMS.append(f"{self.name}: 文字「{label}」与方框 {nm} 重叠")
            for a, b in segs:
                if seg_hits_rect(a, b, r, shrink=0.05):
                    PROBLEMS.append(f"{self.name}: 文字「{label}」被连线 {a}->{b} 穿过")
        for i in range(len(self.items)):
            for j in range(i + 1, len(self.items)):
                li, ri = self.items[i]
                lj, rj = self.items[j]
                if rects_overlap(ri, rj, tol=-0.3):
                    PROBLEMS.append(f"{self.name}: 文字「{li}」与「{lj}」互相重叠")


def check_no_overlap(name, boxes):
    """方框之间不得重叠。"""
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            ni, ri = boxes[i]
            nj, rj = boxes[j]
            if rects_overlap(ri, rj, tol=-0.2):
                PROBLEMS.append(f"{name}: 方框 {ni} 与 {nj} 重叠")


def save_exact(fig, path: Path, px_w: int, px_h: int):
    """保存为精确像素尺寸的 PNG（必要时用 PIL LANCZOS 校正）。"""
    tmp = path.with_name("_" + path.name)
    fig.savefig(tmp)
    plt.close(fig)
    from PIL import Image
    im = Image.open(tmp)
    if im.size != (px_w, px_h):
        im = im.convert("RGB").resize((px_w, px_h), Image.LANCZOS)
    else:
        im = im.convert("RGB")
    im.save(path)
    tmp.unlink(missing_ok=True)
    print(f"  -> {path.name}  {im.size[0]}x{im.size[1]} px")


def paper_axes(fig, w_cm, h_cm, left, right, bottom, top):
    """按物理厘米新建坐标区：left/right/bottom/top 单位 cm（原点在左下）。"""
    ax = fig.add_axes((left / w_cm, bottom / h_cm,
                       1 - (left + right) / w_cm, 1 - (bottom + top) / h_cm))
    return ax


def font_check_boxes(fig, name, texts):
    """逐字「方框字」自检：与私用区字符（必定无字形）的位图比对。

    同时把每个字渲染到较小画布，字形缺失时 matplotlib 会画 .notdef 方框，
    其位图与私用区字符完全一致。
    """
    from matplotlib.textpath import TextPath
    bad = []
    seen = set()
    for s in texts:
        for ch in s:
            if ch in " \n\t" or ch in seen:
                continue
            seen.add(ch)
            try:
                tp = TextPath((0, 0), ch, size=30,
                              prop=font_manager.FontProperties(
                                  family=matplotlib.rcParams["font.serif"]))
                if len(tp.vertices) <= 5:      # 只剩 .notdef 的空框
                    bad.append(f"{ch!r}(U+{ord(ch):04X})")
            except Exception:
                bad.append(f"{ch!r}(U+{ord(ch):04X})")
    if bad:
        PROBLEMS.append(f"{name}: 下列字符缺字形（会显示为方框）-> {sorted(set(bad))}")
    return sorted(set(bad))


# ---------------------------------------------------------------------------
# 图1 系统总体框图（15.00 cm × 7.50 cm → 2880 × 1440 px）
# ---------------------------------------------------------------------------
FIG1_W_CM, FIG1_H_CM, FIG1_PX = 15.00, 7.50, (2880, 1440)


def _layout_row(ax, fig, put, items, y, x_lo, x_hi, min_gap, tag):
    """把 items 在一行内按实测宽度均布；返回各框矩形。"""
    ws = [text_size(ax, fig, ln)[0] + 2.0 for _, ln in items]
    n = len(items)
    gap = (x_hi - x_lo - sum(ws)) / (n - 1)
    if gap < min_gap:
        PROBLEMS.append(f"图1: {tag} 行间距不足（{gap:.2f} < {min_gap}）")
    rects, cx = [], x_lo
    for (nm, ln), w in zip(items, ws):
        rects.append(put(cx + w / 2, y, ln, FC_BLUE, C_BLUE, nm))
        cx += w + gap
    return rects


def fig_block_diagram():
    fig = plt.figure(figsize=(FIG1_W_CM / 2.54, FIG1_H_CM / 2.54),
                     dpi=FIG1_PX[0] / (FIG1_W_CM / 2.54))
    ax = fig.add_axes((0, 0, 1, 1))
    X, Y = FIG1_W_CM * 10, FIG1_H_CM * 10              # 数据坐标 = 0.1 cm
    ax.set_xlim(0, X); ax.set_ylim(0, Y); ax.axis("off")
    boxes = []
    lc = FreeText(ax, fig, "图1")
    segs = []

    def put(cx, cy, lines, fc, ec, name, pad_x=1.0, pad_y=0.8):
        r = draw_box(ax, fig, cx, cy, lines, fc, ec, pad_x=pad_x, pad_y=pad_y)
        boxes.append((name, r))
        return r

    # 第一行：信号链五段（主色蓝）；第二行：下游三项（辅色绿）
    row1 = [("源", ["ECG 信号源", "MIT-BIH / 端侧采集"]),
            ("预处理", ["0.5–45 Hz 带通", "+50 Hz 陷波"]),
            ("R峰检测", ["Pan-Tompkins", "因果流式检测"]),
            ("分割", ["心拍分割 187 点", "逐拍归一化"]),
            ("CNN", ["轻量 1D CNN", "int8 38.79 KB"])]
    row2 = [("分类结果", ["AAMI 五类", "N / S / V / F / Q"]),
            ("心率报警", ["R-R 间期", ">100 / <50 bpm"]),
            ("显示/端侧", ["GUI + 端侧显示", "波形 / 标注 / 触摸"])]
    # 左列五段纵向信号链，右列三个下游框（两列错开，横线走 y=48/26/4 三条走廊）
    LX, LW = 2.0, 56.0
    RX, RW = 90.0, 51.0
    r1, ys1 = [], [66.0, 54.0, 40.0, 26.0, 12.0]
    for (nm, ln), yy in zip(row1, ys1):
        w = text_size(ax, fig, ln)[0] + 2.0
        if w > LW:
            PROBLEMS.append(f"图1: 方框 {nm} 宽 {w:.2f} 超出左列宽 {LW}")
        r1.append(put(LX + LW / 2, yy, ln, FC_BLUE, C_BLUE, nm))
    r2 = []
    for (nm, ln), yy in zip(row2, [64.0, 42.0, 20.0]):
        w = text_size(ax, fig, ln)[0] + 2.0
        if w > RW:
            PROBLEMS.append(f"图1: 方框 {nm} 宽 {w:.2f} 超出右列宽 {RW}")
        r2.append(put(RX + RW / 2, yy, ln, FC_GREEN, C_GREEN, nm))

    # 左列内部纵向推进
    for a, b in zip(r1[:-1], r1[1:]):
        seg = ((a[0] + a[2] / 2, a[1]), (b[0] + b[2] / 2, b[1] + b[3]))
        segs.append(seg)
        ax.add_patch(FancyArrowPatch(*seg, arrowstyle="-|>", mutation_scale=11,
                                     linewidth=lw_line, color="#444", zorder=4))
    # 右列内部纵向推进
    for a, b in zip(r2[:-1], r2[1:]):
        seg = ((a[0] + a[2] / 2, a[1]), (b[0] + b[2] / 2, b[1] + b[3]))
        segs.append(seg)
        ax.add_patch(FancyArrowPatch(*seg, arrowstyle="-|>", mutation_scale=11,
                                     linewidth=lw_line, color="#444", zorder=4))

    # 分发：左列首框（源）→ 竖直干线 → 三条横线 → 右列三个下游框
    src = r1[0]
    trunk_x = 78.0
    src_y = src[1] + src[3] / 2
    for nm, rect in boxes:
        if seg_hits_rect((src[0] + src[2], src_y), (trunk_x, src_y), rect):
            PROBLEMS.append(f"图1: 分发（源）横线穿过 {nm}")
        if seg_hits_rect((trunk_x, src_y), (trunk_x, r2[-1][1] + r2[-1][3] / 2), rect):
            PROBLEMS.append(f"图1: 分发竖直干线穿过 {nm}")
    ax.plot([src[0] + src[2], trunk_x], [src_y, src_y], color="#444", lw=lw_line, zorder=2)
    ax.plot([trunk_x, trunk_x], [src_y, r2[-1][1] + r2[-1][3] / 2],
            color="#444", lw=lw_line, zorder=2)
    segs.append(((src[0] + src[2], src_y), (trunk_x, src_y)))
    for r in r2:
        ty = r[1] + r[3] / 2
        segs.append(((trunk_x, min(ty, src_y)), (trunk_x, ty)))
        segs.append(((trunk_x, ty), (r[0], ty)))
        ax.add_patch(FancyArrowPatch((trunk_x, ty), (r[0], ty), arrowstyle="-|>",
                                     mutation_scale=11, linewidth=lw_line, color="#444",
                                     zorder=4))
        for nm, rect in boxes:
            if rect is r or rect is src:
                continue
            if seg_hits_rect((trunk_x, ty), (r[0], ty), rect):
                PROBLEMS.append(f"图1: 分发横线穿过 {nm}")
    lc.text(6.0, Y - 3.4, "系统总体框图", ha="left", va="center", color=C_TEXT)
    lc.text(X / 2, 2.8, "离线训练 → 量化压缩 → 实时推理 → GUI 可视化 → 端侧部署",
            ha="center", va="center", color=C_MUTED)
    check_no_overlap("图1", boxes)
    check_boxes_inside(ax, "图1", boxes, (0, X), (0, Y))
    lc.check(boxes=boxes, segs=segs)
    check_text_inside_figure(fig, "图1")
    font_check_boxes(fig, "图1", [t for _, ln in row1 + row2 for t in ln] +
                     ["系统总体框图", "离线训练 → 量化压缩 → 实时推理 → GUI 可视化 → 端侧部署"])
    save_exact(fig, OUT / "fig1_system.png", *FIG1_PX)
    return boxes


# ---------------------------------------------------------------------------
# 图2 训练收敛曲线（13.50 cm × 8.03 cm → 2220 × 1320 px）
# ---------------------------------------------------------------------------
FIG2_W_CM, FIG2_H_CM, FIG2_PX = 13.50, 8.03, (2220, 1320)


def fig_training_curves():
    h = METRICS["history"]
    ep = np.arange(1, len(h["train_loss"]) + 1)
    loss = np.array(h["train_loss"]) * 100
    acc = np.array(h["val_acc"]) * 100

    fig = plt.figure(figsize=(FIG2_W_CM / 2.54, FIG2_H_CM / 2.54),
                     dpi=FIG2_PX[0] / (FIG2_W_CM / 2.54))
    ax1 = paper_axes(fig, FIG2_W_CM, FIG2_H_CM, left=1.70, right=1.75, bottom=1.20, top=0.90)
    l1, = ax1.plot(ep, loss, color=C_BLUE, marker="o", ms=3.6, lw=1.4, label="训练损失 (×100)")
    ax1.set_xlabel("训练轮次 (epoch)", fontsize=FS)
    ax1.set_ylabel("训练损失 (×100)", color=C_BLUE, fontsize=FS)
    ax1.tick_params(axis="y", labelcolor=C_BLUE, labelsize=FS)
    ax1.tick_params(axis="x", labelsize=FS)
    ax1.set_ylim(1.4, 13.6)
    ax1.set_yticks([2, 4, 6, 8, 10, 12])
    ax1.set_xticks(np.arange(1, 16, 2))
    ax1.set_xlim(-1.6, len(ep) + 1.0)
    ax1.grid(alpha=0.3, linestyle="--")

    ax2 = ax1.twinx()
    l2, = ax2.plot(ep, acc, color=C_RED, marker="s", ms=3.6, lw=1.4, label="验证准确率")
    ax2.set_ylabel("验证准确率 (%)", color=C_RED, fontsize=FS)
    ax2.tick_params(axis="y", labelcolor=C_RED, labelsize=FS)
    ax2.set_ylim(86.0, 98.0)
    ax2.set_yticks([87, 89, 91, 93, 95, 97])

    best_i = int(np.argmax(acc))
    best_ep, best = int(ep[best_i]), float(acc[best_i])
    ax2.axvline(best_ep, color="#999", ls=":", lw=1.1, zorder=1)
    ax2.plot([best_ep], [best], marker="*", ms=13, color=C_RED, zorder=6)
    ann = ax2.text(best_ep + 0.35, best - 2.6, f"峰值 {best:.2f}%", fontsize=FS, color=C_RED,
                   ha="left", va="center", zorder=6)
    fig.canvas.draw()
    ab = ann.get_window_extent(renderer=fig.canvas.get_renderer()).transformed(
        ax2.transData.inverted())
    axb = ax2.get_window_extent().transformed(ax2.transData.inverted())
    if ab.x0 < axb.x0 or ab.x1 > axb.x1 or ab.y0 < axb.y0 or ab.y1 > axb.y1:
        PROBLEMS.append("图2: 峰值标签超出坐标区")
    # 标签置于峰值点右下方空白处：与验证曲线（x ≥ best_ep）不重叠
    seg = acc[(ep >= ab.x0 - 0.25) & (ep <= ab.x1 + 0.25)]
    if len(seg) and ab.y1 > seg.min() - 0.35:
        PROBLEMS.append("图2: 峰值标签与验证曲线重叠")

    ax1.legend([l1, l2], [l1.get_label(), l2.get_label()], loc="lower center",
               bbox_to_anchor=(0.5, 1.01), ncol=2, frameon=False, fontsize=FS,
               handlelength=1.8, columnspacing=1.6, borderaxespad=0.0)
    check_text_inside_figure(fig, "图2")
    txt = [f"峰值 {best:.2f}%", "训练轮次 (epoch)", "训练损失 (×100)", "验证准确率 (%)",
           "训练损失 (×100)", "验证准确率"]
    font_check_boxes(fig, "图2", txt)
    save_exact(fig, OUT / "fig2_training.png", *FIG2_PX)


# ---------------------------------------------------------------------------
# 图3 混淆矩阵（12.00 cm × 10.67 cm → 1721 × 1530 px）
# ---------------------------------------------------------------------------
FIG3_W_CM, FIG3_H_CM, FIG3_PX = 12.00, 10.67, (1721, 1530)


def fig_confusion():
    cm = np.array(METRICS["confusion_matrix"], dtype=float)
    row = cm.sum(axis=1, keepdims=True)
    norm = np.divide(cm, row, out=np.zeros_like(cm), where=row > 0)

    fig = plt.figure(figsize=(FIG3_W_CM / 2.54, FIG3_H_CM / 2.54),
                     dpi=FIG3_PX[0] / (FIG3_W_CM / 2.54))
    W, H = FIG3_W_CM, FIG3_H_CM
    ax = fig.add_axes((1.32 / W, 2.30 / H, 6.00 / W, 6.00 / H))
    im = ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(5), CLASSES, fontsize=FS)
    ax.set_yticks(range(5), CLASSES, fontsize=FS)
    ax.tick_params(length=3)
    ax.set_xlabel("预测类别", fontsize=FS)
    ax.set_ylabel("真实类别", fontsize=FS)
    for i in range(5):
        for j in range(5):
            v = norm[i, j]
            ax.text(j, i, f"{int(cm[i, j])}\n{v * 100:.1f}%", ha="center", va="center",
                    fontsize=FS, color="white" if v > 0.55 else "#20304a",
                    linespacing=1.25, zorder=5)
    ax.set_title(f"测试集准确率 {TEST_ACC:.2f}%（跨患者）", fontsize=FS, pad=6)
    cax = fig.add_axes((8.15 / W, 2.30 / H, 0.50 / W, 6.00 / H))
    cb = fig.colorbar(im, cax=cax)
    cb.set_label("按行归一化比例", fontsize=FS)
    cb.ax.tick_params(labelsize=FS)
    lc = FreeText(ax, fig, "图3")
    lc.text(2.5, -2.3, f"宏平均 F1 {MACRO_F1:.2f}% · 按行归一化（每行 100%）",
            ha="center", va="center", color=C_MUTED)
    font_check_boxes(fig, "图3", ["预测类别", "真实类别", "按行归一化比例",
                                  f"测试集准确率 {TEST_ACC:.2f}%（跨患者）",
                                  f"宏平均 F1 {MACRO_F1:.2f}% · 按行归一化（每行 100%）"] + CLASSES)
    check_text_inside_figure(fig, "图3")
    save_exact(fig, OUT / "fig3_confusion.png", *FIG3_PX)


# ---------------------------------------------------------------------------
# 图4 GUI 离屏截图（含中文字体加载 + 方框字自检）—— 本轮不改
# ---------------------------------------------------------------------------
def _install_cjk_font(app):
    """装中文字体并设置字形回退链，返回 (family, 字体文件)。

    注意：▶ ⏸ ⚠ ⏳ 这几个图标在 msyh/simhei/simsun/simkai 里**都没有字形**，
    只靠中文字体会渲染成方框；Segoe UI Symbol 覆盖它们，故加入回退链。
    """
    from PySide6.QtGui import QFont, QFontDatabase

    fam, path = None, None
    for f in CJK_FONT_CANDIDATES:
        if not Path(f).exists():
            continue
        fid = QFontDatabase.addApplicationFont(f)
        fams = QFontDatabase.applicationFontFamilies(fid)
        if fid != -1 and fams:
            fam, path = fams[0], f
            break
    if fam:
        # 关键：▶ ⏸ ⚠ ⏳ 在中文字体里全部缺字形；必须显式注册 Segoe UI Symbol，
        # 否则 Qt 字体库里根本没有该 family，回退链形同虚设、字符渲染成方框。
        for sp in ("C:/Windows/Fonts/seguisym.ttf",):
            if Path(sp).exists():
                QFontDatabase.addApplicationFont(sp)
        font = QFont(fam, 10)
        font.setFamilies([fam, "Segoe UI Symbol", "Segoe UI Emoji"])
        app.setFont(font)
    return fam, path


def _glyph_sig(font, ch, box=28):
    """把单个字符渲染成小位图，返回像素字节（用于判断是否为方框字）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    img = QImage(box, box, QImage.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setFont(font)
    p.setPen(QColor("black"))
    p.drawText(img.rect(), Qt.AlignCenter, ch)
    p.end()
    return bytes(img.constBits())


def _check_widget_glyphs(win, app):
    """遍历窗口内所有控件文本，逐字符判断是否为「方框字」。

    判据：该字符的渲染位图与「私用区字符（必定无字形）」的位图完全相同 ——
    说明它落到了 .notdef 字形，即显示为方框。
    """
    from PySide6.QtWidgets import QLabel, QPushButton

    widgets = win.findChildren(QLabel) + win.findChildren(QPushButton)
    texts = [w.text() for w in widgets]
    # 播放过程中才会出现的动态文案（按钮/状态栏会被改写）
    texts += ["▶ 实时监测中（4× 速度，窗口 3.0s）", "⏸ 暂停",
              "▶ 继续", "▶ 重新播放", "⚠ 未找到可用数据集",
              "⏳ 正在加载 MIT-BIH 记录 200 …",
              "播放结束：共 151 拍　⚠ 报警未确认", "报警 心动过速"]
    font = widgets[0].font() if widgets else app.font()
    notdef = _glyph_sig(font, "\ue000")     # 私用区字符：必定无字形，作为「方框」基准
    seen, bad = set(), []
    for t in texts:
        for ch in t:
            if ch in " \n\t" or ch in seen:
                continue
            seen.add(ch)
            if _glyph_sig(font, ch) == notdef:
                bad.append(f"{ch!r}(U+{ord(ch):04X})")
    return sorted(set(bad))


def fig_gui_screenshot(record: int = 200, ticks: int = 1400, size=(1000, 720)):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from config import MODEL_DIR
    from datasets import available_datasets
    from gui import MainWindow
    from realtime import BeatClassifier, StreamingEngine

    model_path = MODEL_DIR / f"{MODEL_NAME}_best.pt"
    if not model_path.exists():
        print("跳过图4：未找到", model_path)
        return

    app = QApplication.instance() or QApplication(["make_figures"])
    fam, fpath = _install_cjk_font(app)
    print(f"图4 字体: family={fam!r} file={fpath}")

    clf = BeatClassifier(MODEL_NAME, model_path)
    engine = StreamingEngine(clf, speed=4)
    win = MainWindow(engine=engine, datasets=available_datasets())
    win.setFont(app.font())
    win.resize(*size)
    win.show()

    bad = _check_widget_glyphs(win, app)
    if bad:
        PROBLEMS.append(f"图4: 下列字符无字形、会显示为方框 -> {bad}")
        print(f"图4 方框字自检: 失败 -> {bad}")
    else:
        print("图4 方框字自检: 窗口内全部字符均有字形（通过）")

    label = f"MIT-BIH 记录 {record}"
    idx = win.combo.findText(label)
    if idx < 0:
        print(f"跳过图4：数据集列表中没有「{label}」")
        return
    win.combo.setCurrentIndex(idx)

    win._load_selected()
    import time as _t
    for _ in range(2000):
        app.processEvents()
        if win.worker is None or not win.worker.isRunning():
            break
        _t.sleep(0.01)
    app.processEvents()
    if win.filtered is None:
        print("跳过图4：后台准备未完成")
        return

    win.timer.stop()
    win.playing = True
    win.paused = False
    for _ in range(ticks):
        win._tick()
    app.processEvents()

    path = OUT / "fig4_gui.png"
    win.grab().save(str(path))
    print(f"图4 -> {path}  ({ticks} tick, 心拍 {len(win.beats)}, 报警={win.latched_alarm})")
    win.close()


# ---------------------------------------------------------------------------
# 图5 端侧固件状态机与页面流转（15.00 cm × 8.30 cm → 2820 × 1560 px）
# ---------------------------------------------------------------------------
FIG5_W_CM, FIG5_H_CM, FIG5_PX = 15.00, 8.30, (2820, 1560)


def fig_state_machine():
    """横向主干五状态 + 下方回流走廊；正交连线逐段做不穿框校验。"""
    fig = plt.figure(figsize=(FIG5_W_CM / 2.54, FIG5_H_CM / 2.54),
                     dpi=FIG5_PX[0] / (FIG5_W_CM / 2.54))
    ax = fig.add_axes((0, 0, 1, 1))
    X, Y = FIG5_W_CM * 10, FIG5_H_CM * 10              # 数据坐标 = 0.1 cm
    ax.set_xlim(0, X); ax.set_ylim(0, Y); ax.axis("off")
    boxes, segs = [], []
    lc = FreeText(ax, fig, "图5")

    def put(cx, cy, lines, fc, ec, name, wmax=None, pad_x=1.2, pad_y=0.8):
        """按实测文字宽度画框；wmax 给定时校验不超宽。"""
        w = text_size(ax, fig, lines)[0] + 2 * pad_x
        if wmax is not None and w > wmax + 0.01:
            PROBLEMS.append(f"图5: 方框 {name} 宽 {w:.2f} 超出可用宽度 {wmax:.2f}")
        r = draw_box(ax, fig, cx, cy, lines, fc, ec, pad_x=pad_x, pad_y=pad_y)
        boxes.append((name, r))
        return r

    def arrow(pts, color="#444", ls="-"):
        for a, b in zip(pts[:-1], pts[1:]):
            if a == b:
                continue
            for nm, rect in boxes:
                if seg_hits_rect(a, b, rect):
                    PROBLEMS.append(f"图5: 连线 {a}->{b} 穿过方框 {nm}")
            segs.append((a, b))
        for a, b in zip(pts[:-1], pts[1:]):
            if a == b:
                continue
            ax.plot([a[0], b[0]], [a[1], b[1]], color=color, lw=lw_line, ls=ls,
                    zorder=4, solid_capstyle="butt")
        ax.add_patch(FancyArrowPatch(pts[-2], pts[-1], arrowstyle="-|>",
                                     mutation_scale=11, linewidth=lw_line,
                                     color=color, linestyle=ls, zorder=4))

    def er(r): return (r[0] + r[2], r[1] + r[3] / 2)
    def el(r): return (r[0], r[1] + r[3] / 2)
    def et(r): return (r[0] + r[2] / 2, r[1] + r[3])
    def eb(r): return (r[0] + r[2] / 2, r[1])

    ty = Y - 3.4
    lc.text(X / 2, ty, "端侧固件状态机（单 app_main 大循环，20 ms tick 驱动）",
            ha="center", va="center", color=C_TEXT)

    # 主干两行三列（左→右、再折回），右侧上下挂两个分支，底部为回流走廊
    y1, y2 = 66.0, 38.0
    C1, C2 = 54.0, 20.0                      # 两条横向走廊
    # 每个框的水平区间（按实测文字宽度预留，保证不重叠、不越界）
    spans = {"ST_BOOT": (2.0, 25.5), "ST_MODE": (30.0, 52.0), "ST_DEMO_MENU": (56.5, 89.0),
             "ST_PLAY": (26.0, 45.0), "ST_ALARM": (48.0, 77.0),
             "ST_SETTINGS": (114.0, 150.0), "ST_REALTIME": (114.0, 150.0)}

    def box(name, cy, lines, fc, ec):
        x0, x1 = spans[name]
        return put((x0 + x1) / 2, cy, lines, fc, ec, name, wmax=x1 - x0)

    boot = box("ST_BOOT", y1, ["ST_BOOT", "开机动画 2 s"], FC_GRAY, C_GRAY)
    mode = box("ST_MODE", y1, ["ST_MODE", "模式选择页"], FC_BLUE, C_BLUE)
    demo = box("ST_DEMO_MENU", y1, ["ST_DEMO_MENU", "演示样本列表"], FC_GREEN, C_GREEN)
    play = box("ST_PLAY", y2, ["ST_PLAY", "监测页"], FC_GREEN, C_GREEN)
    alarm = box("ST_ALARM", y2, ["ST_ALARM", "报警页（锁存）"], FC_RED, C_RED)
    settings = box("ST_SETTINGS", y1, ["ST_SETTINGS", "语言 / 亮度（NVS）"],
                   FC_PURPLE, C_PURPLE)
    realtime = box("ST_REALTIME", y2, ["ST_REALTIME", "实时模式（待接入）"],
                   FC_GRAY, C_GRAY)

    # 主干推进：ST_BOOT → ST_MODE → ST_DEMO_MENU → ST_PLAY → ST_ALARM
    arrow([er(boot), el(mode)])
    arrow([er(mode), el(demo)])
    arrow([eb(demo), (eb(demo)[0], C1), (eb(play)[0], C1), et(play)])
    arrow([er(play), el(alarm)])
    lc.text((er(boot)[0] + el(mode)[0]) / 2, et(boot)[1] + 1.6, "开机完成",
            ha="center", va="bottom", color=C_MUTED)
    lc.text((er(mode)[0] + el(demo)[0]) / 2, et(mode)[1] + 1.6, "演示模式",
            ha="center", va="bottom", color=C_MUTED)
    lc.text(78.0, C1 - 4.0, "开始回放", ha="center", va="center", color=C_MUTED)
    lc.text((er(play)[0] + el(alarm)[0]) / 2, et(play)[1] + 1.6, "心率越界",
            ha="center", va="bottom", color=C_RED)

    # 分支：设置（右上）与实时模式（右下），走廊分别走 C1 与 C2
    arrow([eb(boot), (eb(boot)[0], C1), (eb(settings)[0], C1), eb(settings)])
    lc.text(eb(settings)[0], et(settings)[1] + 3.4, "设置",
            ha="center", va="center", color=C_MUTED)
    arrow([eb(alarm), (eb(alarm)[0], C2), (eb(realtime)[0], C2), eb(realtime)])
    lc.text(eb(realtime)[0], et(realtime)[1] + 3.4, "实时模式",
            ha="center", va="center", color=C_MUTED)

    # 回流：报警页 → 演示列表（从报警页右边引出，经 x=100 竖廊兜回演示列表底部）
    # 回流：报警页 → 演示列表（从报警页右边引出，绕最右侧竖廊与底部走廊回演示列表）
    arrow([er(alarm), (104.0, er(alarm)[1]), (104.0, 1.5),
           (91.5, 1.5), (91.5, eb(demo)[1] - 2.0)],
          color=C_RED, ls="--")
    lc.text(55.0, 16.0, "触摸确认 → 停止回放并返回列表",
            ha="center", va="center", color=C_RED)

    check_no_overlap("图5", boxes)
    check_boxes_inside(ax, "图5", boxes, (0, X), (0, Y))
    lc.check(boxes=boxes, segs=segs)
    check_text_inside_figure(fig, "图5")
    font_check_boxes(fig, "图5", ["端侧固件状态机（单 app_main 大循环，20 ms tick 驱动）",
                                  "ST_BOOT", "开机动画 2 s", "ST_MODE", "模式选择页",
                                  "ST_DEMO_MENU", "演示样本列表", "ST_PLAY", "监测页",
                                  "ST_ALARM", "报警页（锁存）", "ST_SETTINGS",
                                  "语言 / 亮度（NVS）", "ST_REALTIME",
                                  "实时模式（待接入）", "开机完成", "演示模式",
                                  "开始回放", "心率越界", "设置", "实时模式",
                                  "触摸确认 → 停止回放并返回列表"])
    save_exact(fig, OUT / "fig5_state_machine.png", *FIG5_PX)
    return boxes


# ---------------------------------------------------------------------------
# 图6 因果流式处理时序（15.00 cm × 6.14 cm → 1760 × 720 px）
# ---------------------------------------------------------------------------
FIG6_W_CM, FIG6_H_CM, FIG6_PX = 15.00, 6.14, (1760, 720)


def fig_streaming_timing():
    """共享时间轴的双泳道图：泳道 A 心率/报警（立即），泳道 B 逐拍分类（晚一拍）。"""
    fig = plt.figure(figsize=(FIG6_W_CM / 2.54, FIG6_H_CM / 2.54),
                     dpi=FIG6_PX[0] / (FIG6_W_CM / 2.54))
    ax = fig.add_axes((0, 0, 1, 1))
    X, Y = FIG6_W_CM * 10, FIG6_H_CM * 10              # 数据坐标 = 0.1 cm
    ax.set_xlim(0, X); ax.set_ylim(0, Y); ax.axis("off")
    segs = []
    lc = FreeText(ax, fig, "图6")

    def line(x1, y1, x2, y2, **kw):
        ax.plot([x1, x2], [y1, y2], **kw)
        segs.append(((x1, y1), (x2, y2)))

    peaks = [18.0, 42.0, 66.0, 90.0, 114.0]
    X0, X1 = 6.0, 112.0
    y_time, y_cls, y_hr = 12.0, 28.0, 38.0

    # 顶部图例（与泳道同色）
    lc.text(X0, 57.5, "绿色：心率 / 报警 —— R 峰确认即更新", ha="left", va="center", color=C_GREEN)
    lc.text(X0, 51.5, "红色：逐拍分类 —— 需 post-RR，晚 R(k+1) 一拍输出",
            ha="left", va="center", color=C_RED)

    # 泳道 A：心率 / 报警（R 峰确认即更新）
    line(X0, y_hr, X1, y_hr, color="#9aa0a6", lw=1.0)
    for i, x in enumerate(peaks):
        line(x, y_hr, x, y_hr + 6.0, color=C_GREEN, lw=1.4)
        ax.add_patch(FancyArrowPatch((x, y_hr + 5.4), (x, y_hr + 6.1), arrowstyle="-|>",
                                     mutation_scale=10, lw=1.4, color=C_GREEN, zorder=4))
        lc.text(x, y_hr + 9.0, f"HR{i + 1}", ha="center", va="center", color=C_GREEN)

    # 泳道 B：逐拍分类（每段 = 该拍类别的输出区间；首拍无输出）
    line(X0, y_cls, X1, y_cls, color="#9aa0a6", lw=1.0)
    for k in range(len(peaks) - 1):
        a = peaks[k + 1]
        b = peaks[k + 2] if k + 2 < len(peaks) else peaks[k + 1] + 22.0
        line(a, y_cls, b, y_cls, color=C_RED, lw=2.6)
        lc.text((a + b) / 2, y_cls - 2.6, f"class(R{k + 1})", ha="center",
                va="top", color=C_RED)

    # 「延迟一拍」示意：R1 确认时刻 → class(R1) 的输出时刻 R2（"晚一拍"已在图例说明）
    line(peaks[0] + 0.8, y_hr - 0.8, peaks[1] - 1.0, y_cls + 0.8,
         color="#7f8c8d", lw=1.2, ls="--")
    ax.add_patch(FancyArrowPatch((peaks[1] - 2.2, y_cls + 0.6), (peaks[1] - 0.9, y_cls + 0.6),
                                 arrowstyle="-|>", mutation_scale=11, lw=1.2,
                                 color="#7f8c8d", linestyle="--", zorder=4))
    lc.text(3.0, y_cls + 3.6, "首拍无输出", ha="left", va="center", color="#7f8c8d")
    lc.text(24.0, 16.2, "延迟 1 拍", ha="left", va="center", color="#5d6d7e")

    # 底部时间轴：R 峰刻度
    line(X0, y_time, X1, y_time, color="#333", lw=1.2)
    lc.text(X1 + 2.0, y_time, "时间", ha="left", va="center", color=C_TEXT)
    for i, x in enumerate(peaks):
        line(x, y_time, x, y_time + 4.6, color=C_BLUE, lw=1.2)
        ax.plot([x], [y_time], marker="v", ms=7, color=C_BLUE, zorder=4)
        lc.text(x, y_time - 2.8, f"R{i + 1}", ha="center", va="top", color=C_BLUE)

    # 用竖直虚线把「R(k+1) 时刻」与分类输出起点对齐
    for x in peaks[1:]:
        line(x, y_time + 4.6, x, y_cls, color=C_BLUE, lw=0.8, ls=":")

    lc.check(segs=segs)
    check_text_inside_figure(fig, "图6")
    font_check_boxes(fig, "图6", ["绿色：心率 / 报警 —— R 峰确认即更新",
                                  "红色：逐拍分类 —— 需 post-RR，晚 R(k+1) 一拍输出",
                                  "HR1", "HR2", "HR3", "HR4", "HR5", "class(R1)", "class(R4)",
                                  "首拍无输出", "延迟 1 拍", "时间", "R1", "R5"])
    save_exact(fig, OUT / "fig6_timing.png", *FIG6_PX)


# ---------------------------------------------------------------------------
# 统一自检：字号必须全部为 10.5 pt
# ---------------------------------------------------------------------------
def audit_font_sizes():
    bad = [(s, f) for s, f in FONT_SIZES if abs(f - FS) > 1e-9]
    uniq = sorted({round(f, 3) for _, f in FONT_SIZES})
    print(f"\n字号统计：共 {len(FONT_SIZES)} 处文字，出现过的字号 = {uniq}")
    if bad:
        for s, f in bad[:20]:
            PROBLEMS.append(f"字号不统一: 「{s}」= {f} pt（应为 {FS}）")


if __name__ == "__main__":
    fig_block_diagram()
    fig_training_curves()
    fig_confusion()
    fig_gui_screenshot()
    fig_state_machine()
    fig_streaming_timing()
    audit_font_sizes()

    print("\n=== 自动几何校验 ===")
    if PROBLEMS:
        for p in PROBLEMS:
            print("  [问题]", p)
        sys.exit(1)
    print("  未检出文字越界 / 连线穿框 / 文字裁切 / 文字重叠 / 字号不统一 / 方框字")
