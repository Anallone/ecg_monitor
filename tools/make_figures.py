# -*- coding: utf-8 -*-
"""生成综合设计报告的插图（输出到 docs/report/figures/）。

用法（项目根目录）:
    ./runtime/python/python.exe tools/make_figures.py

生成：
    图1  系统总体框图
    图2  训练收敛曲线（来自 models/res_se_cnn_metrics.json 的真实 history）
    图3  跨患者测试集混淆矩阵
    图4  GUI 实时监测界面截图（PySide6 离屏渲染，驱动真实 StreamingEngine）
    图5  端侧固件状态机与页面流转
    图6  因果流式处理时序

实现要点（因为生成环境无法目视预览，全部靠几何量测保证）：
  - 框图文字先绘制、量出实际包围盒，再按包围盒反推方框尺寸 —— 文字不会超出边框；
  - 箭头只走正交走廊，并逐段做「线段—矩形相交」校验 —— 连线不会横跨方框；
  - 图2 图例外置于坐标区上方，并显式设定四周边距 —— 不与曲线重叠、内容居中；
  - 图4 显式加载系统中文字体，并做「方框字」自检（逐字位图比对）。
"""
from __future__ import annotations

import json
import os
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

CJK_FONT_CANDIDATES = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                       "C:/Windows/Fonts/simsun.ttc"]
for _f in CJK_FONT_CANDIDATES:
    if Path(_f).exists():
        font_manager.fontManager.addfont(_f)
        matplotlib.rcParams["font.family"] = font_manager.FontProperties(fname=_f).get_name()
        CJK_FONT_FILE = _f
        break
matplotlib.rcParams["axes.unicode_minus"] = False

# 报告图2/图3 采用主线模型的实测指标。必须与 docs/tools/fill_report.py 的 MAIN_MODEL
# 保持一致，否则正文数字与插图会取自不同的模型。
MODEL_NAME = "res_se_cnn_rr4"
METRICS = json.loads((ROOT / "models" / f"{MODEL_NAME}_metrics.json").read_text(encoding="utf-8"))
CLASSES = ["N", "S", "V", "F", "Q"]

PROBLEMS: list[str] = []


# ---------------------------------------------------------------------------
# 通用：按文字实测尺寸画方框；正交连线并校验不穿框
# ---------------------------------------------------------------------------
def measure_text(ax, fig, cx, cy, lines, fontsize, linespacing=1.45):
    """返回文字在数据坐标下的 (x0, y0, w, h)。"""
    txt = ax.text(cx, cy, "\n".join(lines), ha="center", va="center",
                  fontsize=fontsize, linespacing=linespacing, zorder=5)
    fig.canvas.draw()
    bb = txt.get_window_extent(renderer=fig.canvas.get_renderer())
    bb = bb.transformed(ax.transData.inverted())
    return txt, (bb.x0, bb.y0, bb.width, bb.height)


def probe_width(ax, fig, lines, fontsize, linespacing=1.45):
    """临时绘制再删除，用于量出文字宽度（不留残留文字）。"""
    txt, (_, _, w, _) = measure_text(ax, fig, 0, 0, lines, fontsize, linespacing)
    txt.remove()
    return w


def draw_box(ax, fig, cx, cy, lines, fontsize=9.5, pad_x=1.2, pad_y=1.0,
             fc="#eaf2fb", ec="#2c6fbb", bold=False):
    """画一个恰好包住文字（含内边距）的圆角框，返回 (x0, y0, w, h)。"""
    txt, (tx, ty, tw, th) = measure_text(ax, fig, cx, cy, lines, fontsize)
    if bold and txt.get_weight() != "bold":
        txt.set_fontweight("bold")
        fig.canvas.draw()
        bb = txt.get_window_extent(renderer=fig.canvas.get_renderer()).transformed(
            ax.transData.inverted())
        tx, ty, tw, th = bb.x0, bb.y0, bb.width, bb.height
    x0, y0 = tx - pad_x, ty - pad_y
    w, h = tw + 2 * pad_x, th + 2 * pad_y
    ax.add_patch(FancyBboxPatch((x0, y0), w, h,
                                boxstyle="round,pad=0,rounding_size=1.0",
                                linewidth=1.1, facecolor=fc, edgecolor=ec, zorder=3))
    return (x0, y0, w, h)


def seg_hits_rect(p1, p2, rect, shrink=0.6):
    """线段是否穿过矩形内部（矩形四周各内缩 shrink，避免贴边误判）。"""
    x0, y0, w, h = rect
    x0, y0, x1, y1 = x0 + shrink, y0 + shrink, x0 + w - shrink, y0 + h - shrink
    if x1 <= x0 or y1 <= y0:
        return False
    (ax_, ay), (bx, by) = p1, p2
    # Liang-Barsky
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


def ortho_arrow(ax, p_from, p_to, boxes, color="#444", label="", label_offset=(0, 1.0),
                label_fs=8.5, mid=None):
    """从 p_from 经 mid（缺省取中点拐一次）到 p_to 的正交连线，校验不穿框。

    boxes: [(name, rect), ...]，允许穿过起点/终点所在框。
    """
    if mid is None:
        mid = (p_to[0], p_from[1])
    pts = [p_from, mid, p_to]
    for a, b in zip(pts[:-1], pts[1:]):
        if a == b:
            continue
        for name, rect in boxes:
            if seg_hits_rect(a, b, rect):
                PROBLEMS.append(f"连线 {a}->{b} 穿过方框 {name}")
    for a, b in zip(pts[:-1], pts[1:]):
        ax.plot([a[0], b[0]], [a[1], b[1]], color=color, lw=1.1, zorder=4,
                solid_capstyle="butt")
    ax.add_patch(FancyArrowPatch(pts[-2], pts[-1], arrowstyle="-|>", mutation_scale=11,
                                 linewidth=1.1, color=color, zorder=4))
    if label:
        lx = (pts[0][0] + pts[1][0]) / 2 + label_offset[0]
        ly = (pts[0][1] + pts[1][1]) / 2 + label_offset[1]
        ax.text(lx, ly, label, ha="center", va="bottom", fontsize=label_fs,
                color="#333", zorder=6)


def check_text_inside_figure(fig, name):
    """校验所有文字都在画布内（防止被裁切）。"""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    fb = fig.bbox
    for t in fig.findobj(matplotlib.text.Text):
        s = t.get_text()
        if not s.strip():
            continue
        try:
            bb = t.get_window_extent(renderer=r)
        except Exception:
            continue
        if bb.x0 < -1 or bb.y0 < -1 or bb.x1 > fb.x1 + 1 or bb.y1 > fb.y1 + 1:
            PROBLEMS.append(f"{name}: 文字被裁切 -> {s[:24]!r}")


# ---------------------------------------------------------------------------
# 图1 系统总体框图
# ---------------------------------------------------------------------------
def fig_block_diagram():
    fig, ax = plt.subplots(figsize=(9.6, 4.8), dpi=300)
    ax.set_xlim(0, 150); ax.set_ylim(0, 68); ax.axis("off")
    boxes = []

    def add(name, rect):
        boxes.append((name, rect))
        return rect

    # 第一行：信号链（按实测宽度顺序排布，绝不重叠）
    row1 = [
        ("源", "ECG 信号源", "MIT-BIH / 端侧采集"),
        ("预处理", "预处理", "0.5–45 Hz 带通", "+50 Hz 陷波"),
        ("R峰检测", "R 峰检测", "Pan-Tompkins", "（因果流式）"),
        ("分割", "心拍分割", "187 点窗口", "逐拍归一化"),
        ("CNN", "轻量 1D CNN", "深度可分离卷积", "int8 · 7.21 KB"),
    ]
    gap = 6.0
    # 先量宽度
    widths = []
    for name, *lines in row1:
        widths.append(probe_width(ax, fig, lines, 9.5) + 2 * 1.2)   # 含 pad_x
    total = sum(widths) + gap * (len(row1) - 1)
    x = (150 - total) / 2
    row1_rects = []
    for (name, *lines), w in zip(row1, widths):
        cx = x + w / 2
        rect = draw_box(ax, fig, cx, 52, lines, 9.5)
        add(name, rect)
        row1_rects.append(rect)
        x += w + gap

    # 第一行内部箭头（同一水平线，必然不穿框）
    for a, b in zip(row1_rects[:-1], row1_rects[1:]):
        ax.add_patch(FancyArrowPatch((a[0] + a[2], a[1] + a[3] / 2),
                                     (b[0], b[1] + b[3] / 2),
                                     arrowstyle="-|>", mutation_scale=11,
                                     linewidth=1.1, color="#444", zorder=4))

    # 第二行：下游（置于第一行下方，用正交总线连接）
    row2 = [
        ("分类结果", "AAMI 五类", "N / S / V / F / Q"),
        ("心率报警", "心率与报警", "R-R 间期，>100 / <50 bpm"),
        ("显示/端侧", "GUI 与端侧显示", "波形 + 标注 + 触摸确认"),
    ]
    widths2 = []
    for name, *lines in row2:
        widths2.append(probe_width(ax, fig, lines, 9.5) + 2 * 1.2)
    gap2 = 8.0
    total2 = sum(widths2) + gap2 * (len(row2) - 1)
    x = (150 - total2) / 2
    row2_rects = []
    for (name, *lines), w in zip(row2, widths2):
        cx = x + w / 2
        rect = draw_box(ax, fig, cx, 18, lines, 9.5, fc="#eef7ee", ec="#27803f")
        add(name, rect)
        row2_rects.append(rect)
        x += w + gap2

    # 从第一行末端向下到总线，再由总线分发到第二行各项
    src = row1_rects[-1]
    src_x = src[0] + src[2] / 2
    bus_y = 34.0
    ax.plot([src_x, src_x], [src[1], bus_y], color="#444", lw=1.1, zorder=2)
    bus_x0 = min(r[0] + r[2] / 2 for r in row2_rects + [src])
    bus_x1 = max(r[0] + r[2] / 2 for r in row2_rects + [src])
    ax.plot([bus_x0, bus_x1], [bus_y, bus_y], color="#444", lw=1.1, zorder=2)
    for r in row2_rects:
        tx = r[0] + r[2] / 2
        ax.plot([tx, tx], [bus_y, r[1] + r[3]], color="#444", lw=1.1, zorder=2)
        ax.add_patch(FancyArrowPatch((tx, bus_y), (tx, r[1] + r[3]),
                                     arrowstyle="-|>", mutation_scale=11,
                                     linewidth=1.1, color="#444", zorder=4))
    # 干线/总线/分发竖线的穿框校验
    for nm, rect in boxes:
        if rect is not src and seg_hits_rect((src_x, src[1]), (src_x, bus_y), rect):
            PROBLEMS.append(f"图1: 竖直干线穿过 {nm}")
        if seg_hits_rect((bus_x0, bus_y), (bus_x1, bus_y), rect):
            PROBLEMS.append(f"图1: 水平总线穿过 {nm}")
    for r in row2_rects:
        tx = r[0] + r[2] / 2
        for nm2, rr in boxes:
            if rr is r:
                continue
            if seg_hits_rect((tx, bus_y), (tx, r[1] + r[3]), rr):
                PROBLEMS.append(f"图1: 分发竖线穿过 {nm2}")

    txt = ax.text(75, 4.5, "离线训练 → 量化压缩 → 模拟实时推理 → GUI 可视化 → 端侧部署",
                  ha="center", fontsize=9.5, color="#555", zorder=5)
    ax.text(75, 62.5, "系统总体技术路线", ha="center", fontsize=11, color="#333", zorder=5)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.97, bottom=0.03)
    check_text_inside_figure(fig, "图1")
    fig.savefig(OUT / "fig1_system.png")
    plt.close(fig)
    print("图1 ->", OUT / "fig1_system.png")


# ---------------------------------------------------------------------------
# 图2 训练收敛曲线
# ---------------------------------------------------------------------------
def fig_training_curves():
    h = METRICS["history"]
    ep = np.arange(1, len(h["train_loss"]) + 1)
    fig, ax1 = plt.subplots(figsize=(7.4, 4.4), dpi=300)

    ax1.plot(ep, h["train_loss"], color="#2c6fbb", marker="o", ms=3.4, lw=1.4, label="训练损失")
    ax1.set_xlabel("训练轮次 (epoch)")
    ax1.set_ylabel("训练损失", color="#2c6fbb")
    ax1.tick_params(axis="y", labelcolor="#2c6fbb")
    ax1.set_ylim(0.0, 1.18)
    ax1.grid(alpha=0.3, linestyle="--")

    ax2 = ax1.twinx()
    ax2.plot(ep, [v * 100 for v in h["val_acc"]], color="#c0392b", marker="s", ms=3.4, lw=1.4,
             label="验证准确率")
    ax2.set_ylabel("验证准确率 (%)", color="#c0392b")
    ax2.tick_params(axis="y", labelcolor="#c0392b")
    ax2.set_ylim(88.5, 101.0)

    best_ep = int(np.argmax(h["val_acc"])) + 1
    best = max(h["val_acc"]) * 100
    ax2.axvline(best_ep, color="#999", ls=":", lw=1.1)
    # 峰值用星标 + 短标签标出：不做跨曲线箭头，标签置于曲线以上的空白区
    ax2.plot([best_ep], [best], marker="*", ms=13, color="#c0392b", zorder=6)
    ann = ax2.text(best_ep + 0.6, 99.2, f"峰值 {best:.2f}%", fontsize=9, color="#c0392b",
                   ha="left", va="center", zorder=6)
    # 标签必须落在坐标区内，且不与验证曲线相交
    fig.canvas.draw()
    ab = ann.get_window_extent(renderer=fig.canvas.get_renderer()).transformed(
        ax2.transData.inverted())
    axb = ax2.get_window_extent().transformed(ax2.transData.inverted())
    if ab.x0 < axb.x0 or ab.x1 > axb.x1 or ab.y0 < axb.y0 or ab.y1 > axb.y1:
        PROBLEMS.append("图2: 峰值标签超出坐标区")
    acc = np.array(h["val_acc"]) * 100
    seg = acc[(ep >= ab.x0) & (ep <= ab.x1)]
    if len(seg) and ab.y0 < seg.max() + 0.25:
        PROBLEMS.append("图2: 峰值标签与验证曲线重叠")

    # 图例外置到坐标区上方，彻底避免与曲线重叠
    lines = ax1.get_lines() + ax2.get_lines()
    ax1.legend(lines, [l.get_label() for l in lines], loc="lower center",
               bbox_to_anchor=(0.5, 1.005), ncol=2, frameon=False, fontsize=9.5)
    ax1.set_xlim(0, len(ep) + 1)
    # 对称边距 → 内容居中；右侧留够，避免右轴标签被裁
    fig.subplots_adjust(left=0.105, right=0.895, top=0.855, bottom=0.135)
    check_text_inside_figure(fig, "图2")
    fig.savefig(OUT / "fig2_training.png")
    plt.close(fig)
    print("图2 ->", OUT / "fig2_training.png")


# ---------------------------------------------------------------------------
# 图3 混淆矩阵
# ---------------------------------------------------------------------------
def fig_confusion():
    cm = np.array(METRICS["confusion_matrix"], dtype=float)
    row = cm.sum(axis=1, keepdims=True)
    norm = np.divide(cm, row, out=np.zeros_like(cm), where=row > 0)

    fig, ax = plt.subplots(figsize=(6.2, 5.2), dpi=300)
    im = ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(5), CLASSES)
    ax.set_yticks(range(5), CLASSES)
    ax.set_xlabel("预测类别")
    ax.set_ylabel("真实类别")
    for i in range(5):
        for j in range(5):
            v = norm[i, j]
            ax.text(j, i, f"{int(cm[i, j])}\n{v*100:.1f}%", ha="center", va="center",
                    fontsize=8, color="white" if v > 0.55 else "#20304a", linespacing=1.3)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03, label="按行归一化比例")
    ax.set_title(f"测试集准确率 {METRICS['test_acc']*100:.2f}%（DS2，跨患者）", fontsize=10.5)
    fig.tight_layout()
    check_text_inside_figure(fig, "图3")
    fig.savefig(OUT / "fig3_confusion.png", bbox_inches="tight")
    plt.close(fig)
    print("图3 ->", OUT / "fig3_confusion.png")


# ---------------------------------------------------------------------------
# 图4 GUI 离屏截图（含中文字体加载 + 方框字自检）
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
    notdef = _glyph_sig(font, "")     # 私用区字符：必定无字形，作为「方框」基准
    seen, bad = set(), []
    for t in texts:
        for ch in t:
            if ch in " \n\t" or ch in seen:
                continue
            seen.add(ch)
            if _glyph_sig(font, ch) == notdef:
                bad.append(f"{ch!r}(U+{ord(ch):04X})")
    return sorted(set(bad))


class _LabelCheck:
    """收集图内自由文字，校验：不与方框重叠、标签之间互不重叠。"""

    def __init__(self, ax, fig, boxes, name):
        self.ax, self.fig, self.boxes, self.name = ax, fig, boxes, name
        self.items = []

    def add(self, txt, label=""):
        self.fig.canvas.draw()
        bb = txt.get_window_extent(renderer=self.fig.canvas.get_renderer())
        self.items.append((label or txt.get_text()[:12],
                           bb.transformed(self.ax.transData.inverted())))
        return txt

    @staticmethod
    def _overlap(a, b, tol=0.15):
        return not (a[0] + a[2] <= b[0] + tol or b[0] + b[2] <= a[0] + tol or
                    a[1] + a[3] <= b[1] + tol or b[1] + b[3] <= a[1] + tol)

    def report(self):
        for label, r in self.items:
            for nm, box in self.boxes:
                if self._overlap((r.x0, r.y0, r.width, r.height), box):
                    PROBLEMS.append(f"{self.name}: 标签「{label}」与方框 {nm} 重叠")
        for i in range(len(self.items)):
            for j in range(i + 1, len(self.items)):
                li, ri = self.items[i]
                lj, rj = self.items[j]
                if self._overlap((ri.x0, ri.y0, ri.width, ri.height),
                                 (rj.x0, rj.y0, rj.width, rj.height)):
                    PROBLEMS.append(f"{self.name}: 标签「{li}」与「{lj}」互相重叠")


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
# 图5 端侧固件状态机（正交连线 + 不穿框校验）
# ---------------------------------------------------------------------------
def fig_state_machine():
    """端侧固件状态机：单列主干 + 右侧分支 + 右侧/下方回流走廊。

    所有连线均为正交折线，并逐段校验不穿方框；所有标签校验不与方框、
    不与其他标签重叠。
    """
    fig, ax = plt.subplots(figsize=(9.4, 5.2), dpi=300)
    ax.set_xlim(0, 165); ax.set_ylim(0, 94); ax.axis("off")
    boxes = []
    lc = _LabelCheck(ax, fig, boxes, "图5")

    def put(cx, cy, lines, fc, ec, name):
        r = draw_box(ax, fig, cx, cy, lines, 9.3, pad_x=1.6, pad_y=1.2, fc=fc, ec=ec)
        boxes.append((name, r))
        return r

    CXL, CXR = 32, 118
    boot = put(CXL, 78, ["ST_BOOT", "开机动画 100 帧 / 2 s"], "#f2f2f2", "#777", "ST_BOOT")
    mode = put(CXL, 60, ["ST_MODE", "模式选择页"], "#eaf2fb", "#2c6fbb", "ST_MODE")
    demo = put(CXL, 42, ["ST_DEMO_MENU", "演示样本列表（可滚动）"],
               "#eef7ee", "#27803f", "ST_DEMO_MENU")
    play = put(CXL, 24, ["ST_PLAY", "监测页（与报警页共用版式）"],
               "#eef7ee", "#27803f", "ST_PLAY")
    alarm = put(CXL, 7, ["ST_ALARM", "报警页（锁存，触摸确认）"],
                "#fdeeee", "#c0392b", "ST_ALARM")
    settings = put(CXR, 78, ["ST_SETTINGS", "语言 / 亮度（NVS 持久化）"],
                   "#f4eefb", "#7d3cbb", "ST_SETTINGS")
    live = put(CXR, 60, ["ST_REALTIME", "实时模式（前端待接入）"],
               "#f2f2f2", "#777", "ST_REALTIME")

    def er(r): return (r[0] + r[2], r[1] + r[3] / 2)
    def el(r): return (r[0], r[1] + r[3] / 2)
    def et(r): return (r[0] + r[2] / 2, r[1] + r[3])
    def eb(r): return (r[0] + r[2] / 2, r[1])

    def arrow(pts, color="#444", ls="-"):
        for a, b in zip(pts[:-1], pts[1:]):
            if a == b:
                continue
            for nm, rect in boxes:
                if seg_hits_rect(a, b, rect):
                    PROBLEMS.append(f"图5: 连线 {a}->{b} 穿过方框 {nm}")
        for a, b in zip(pts[:-1], pts[1:]):
            ax.plot([a[0], b[0]], [a[1], b[1]], color=color, lw=1.1, ls=ls,
                    zorder=4, solid_capstyle="butt")
        ax.add_patch(FancyArrowPatch(pts[-2], pts[-1], arrowstyle="-|>",
                                     mutation_scale=11, linewidth=1.1,
                                     color=color, linestyle=ls, zorder=4))

    # 主干（单列竖直推进）
    arrow([eb(boot), et(mode)])
    arrow([eb(mode), et(demo)])
    arrow([eb(demo), et(play)])
    arrow([eb(play), et(alarm)])

    # 右侧分支
    arrow([er(mode), el(live)])                                   # 实时模式
    x_elbow = er(mode)[0] + 34
    arrow([er(mode), (x_elbow, er(mode)[1]), (x_elbow, er(settings)[1]), el(settings)])

    # 回流：报警页 → 演示列表（走最右走廊，再沿 y=demo 中线折回）
    x_ret = 156.0
    arrow([er(alarm), (x_ret, er(alarm)[1]), (x_ret, er(demo)[1]), er(demo)],
          color="#c0392b", ls="--")

    # 标签（全部置于方框之间的空隙，逐一校验）
    lc.add(ax.text(34, 68.5, "开机完成", ha="left", va="center", fontsize=8.5,
                   color="#333", zorder=6))
    lc.add(ax.text(34, 50.5, "演示模式", ha="left", va="center", fontsize=8.5,
                   color="#333", zorder=6))
    lc.add(ax.text(34, 32.5, "开始回放", ha="left", va="center", fontsize=8.5,
                   color="#333", zorder=6))
    lc.add(ax.text(34, 14.5, "心率越界", ha="left", va="center", fontsize=8.5,
                   color="#333", zorder=6))
    lc.add(ax.text(76, 61.5, "实时模式", ha="center", va="bottom", fontsize=8.5,
                   color="#333", zorder=6))
    lc.add(ax.text(x_elbow + 1.5, 69.0, "设置", ha="left", va="center", fontsize=8.5,
                   color="#333", zorder=6))
    lc.add(ax.text(96, 3.2, "触摸确认 → 停止回放并返回列表", ha="center", va="center",
                   fontsize=8.5, color="#c0392b", zorder=6))
    lc.report()

    ax.text(82, 90, "端侧固件状态机（单 app_main 大循环，20 ms tick 驱动）",
            ha="center", fontsize=11, color="#333", zorder=5)
    fig.subplots_adjust(left=0.01, right=0.99, top=0.97, bottom=0.02)
    check_text_inside_figure(fig, "图5")
    fig.savefig(OUT / "fig5_state_machine.png")
    plt.close(fig)
    print("图5 ->", OUT / "fig5_state_machine.png")


# ---------------------------------------------------------------------------
# 图6 因果流式时序
# ---------------------------------------------------------------------------
def fig_streaming_timing():
    """因果流式时序：共享时间轴的双泳道图。

    泳道A（绿）心率/报警 —— 每个 R 峰确认即更新，无延迟；
    泳道B（红）逐拍分类 —— 每拍的类别在下一拍 R(k+1) 时刻才输出。
    两条泳道共用同一时间轴，因此「延迟一拍」在图上直接可读。
    """
    peaks = [1.7, 3.6, 5.5, 7.4, 9.3]
    segs = []          # 连线，用于文字压线检测
    texts = []         # 自由文字，用于重叠检测

    fig, ax = plt.subplots(figsize=(8.8, 3.6), dpi=200)
    ax.set_xlim(0, 11.5); ax.set_ylim(0, 8.6); ax.axis("off")

    def line(x1, y1, x2, y2, **kw):
        ax.plot([x1, x2], [y1, y2], **kw)
        segs.append(((x1, y1), (x2, y2)))

    def note(x, y, s, **kw):
        t = ax.text(x, y, s, **kw)
        texts.append((s, t))
        return t

    GREEN, RED, BLUE, GRAY = "#27803f", "#c0392b", "#2c6fbb", "#9aa0a6"
    X0, X1 = 0.6, 10.8

    # ---- 顶部说明（颜色与泳道一致）----
    note(0.4, 8.15, "绿色箭头：心率 / 报警 —— R 峰确认即更新（不延迟）",
         ha="left", va="center", fontsize=9.5, color=GREEN)
    note(0.4, 7.55, "红色线段：逐拍分类 —— 需 post-RR，故晚 R(k+1) 一拍输出",
         ha="left", va="center", fontsize=9.5, color=RED)

    # ---- 泳道A：心率 / 报警 ----
    line(X0, 5.6, X1, 5.6, color=GRAY, lw=1.0)
    for i, t in enumerate(peaks):
        line(t, 5.6, t, 6.5, color=GREEN, lw=1.4)
        ax.add_patch(FancyArrowPatch((t, 6.30), (t, 6.52), arrowstyle="-|>",
                                     mutation_scale=10, lw=1.4, color=GREEN, zorder=4))
        note(t, 6.82, f"HR{i+1}", ha="center", va="center", fontsize=9, color=GREEN)

    # ---- 泳道B：逐拍分类（每段 = 该拍类别的输出区间）----
    line(X0, 2.95, X1, 2.95, color=GRAY, lw=1.0)
    for k in range(len(peaks) - 1):
        a, b = peaks[k + 1], peaks[k + 2] if k + 2 < len(peaks) else peaks[k + 1] + 1.9
        line(a, 2.95, b, 2.95, color=RED, lw=2.6)
        note((a + b) / 2, 2.62, f"class(R{k+1})", ha="center", va="top",
             fontsize=9, color=RED)
    note(0.7, 3.30, "首拍无输出", ha="left", va="center", fontsize=8.5, color=GRAY)

    # ---- 泳道之间的空白处：用一条直虚线示意「延迟一拍」 ----
    # 从 R1 时刻（检测到该拍）指向 class(R1) 的输出时刻 R2，语义与图注一致。
    line(peaks[0] + 0.05, 5.52, peaks[1] - 0.06, 3.02,
         color="#7f8c8d", lw=1.2, ls="--")
    ax.add_patch(FancyArrowPatch((peaks[1] - 0.18, 3.02), (peaks[1] - 0.04, 3.02),
                                 arrowstyle="-|>", mutation_scale=11, lw=1.2,
                                 color="#7f8c8d", linestyle="--", zorder=4))
    note(1.0, 4.30, "延迟 1 拍", ha="left", va="center", fontsize=9, color="#5d6d7e")

    # ---- 底部时间轴：R 峰刻度 ----
    line(X0, 0.95, X1, 0.95, color="#333", lw=1.2)
    note(X1 + 0.12, 0.95, "时间", ha="left", va="center", fontsize=9, color="#333")
    for i, t in enumerate(peaks):
        line(t, 0.95, t, 1.62, color=BLUE, lw=1.2)
        ax.plot([t], [0.95], marker="v", ms=7, color=BLUE, zorder=4)
        note(t, 0.52, f"R{i+1}", ha="center", va="top", fontsize=9, color=BLUE)

    # ---- 用竖直虚线把「R(k+1) 时刻」与分类输出起点对齐 ----
    for k in range(len(peaks) - 1):
        x = peaks[k + 1]
        line(x, 2.95, x, 1.62, color=BLUE, lw=0.8, ls=":")
        line(x, 5.6, x, 6.5, color=BLUE, lw=0.8, ls=":")

    # ---- 校验：文字不压线、文字互不重叠、不越界 ----
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    boxes = []
    for s, t in texts:
        bb = t.get_window_extent(renderer=r).transformed(ax.transData.inverted())
        boxes.append((s, (bb.x0, bb.y0, bb.width, bb.height)))
    for s, (x0, y0, w, h) in boxes:
        for a, b in segs:
            if seg_hits_rect(a, b, (x0, y0, w, h), shrink=0.02):
                PROBLEMS.append(f"图6: 文字「{s}」被连线穿过")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            n1, r1 = boxes[i]; n2, r2 = boxes[j]
            if not (r1[0]+r1[2] <= r2[0] or r2[0]+r2[2] <= r1[0] or
                    r1[1]+r1[3] <= r2[1] or r2[1]+r2[3] <= r1[1]):
                PROBLEMS.append(f"图6: 文字「{n1}」与「{n2}」重叠")

    fig.subplots_adjust(left=0.015, right=0.985, top=0.97, bottom=0.03)
    check_text_inside_figure(fig, "图6")
    fig.savefig(OUT / "fig6_timing.png")
    plt.close(fig)
    print("图6 ->", OUT / "fig6_timing.png")


if __name__ == "__main__":
    fig_block_diagram()
    fig_training_curves()
    fig_confusion()
    fig_gui_screenshot()
    fig_state_machine()
    fig_streaming_timing()

    print("\n=== 自动几何校验 ===")
    if PROBLEMS:
        for p in PROBLEMS:
            print("  [问题]", p)
        sys.exit(1)
    print("  未检出文字越界 / 连线穿框 / 文字裁切 / 方框字")
