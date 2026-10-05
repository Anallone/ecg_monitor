"""PySide6 GUI：ECG **实时滚动**波形 + 心拍分类标注 + 心率曲线 + 类别统计 + 报警。

与端侧对齐（关键约定，改这里前先看 firmware/main/main.c 与 realtime.c）：
- 显示语义：窗口默认 VIEW_POINTS 点（= 端侧 DISP_POINTS 1080 = 3s，可在工具栏切换
  2/3/5/10s），右端为「当前时刻」，
  波形随游标推进向左滚出；只画已经检测到的心拍，绝不提前显示未来。
- 检测语义：用 StreamingEngine（与固件 rt_tick 同构的因果引擎），20ms 一个 tick、
  默认 4 倍速推进（= 端侧 PLAY_SPEED_MULT，可在工具栏切换 0.5×~8×）。
  R 峰一确认就更新心率/报警，分类比检测晚一拍。
- 报警锁存：心率越界即锁存报警状态，必须点「确认报警」才解除（= 端侧触摸确认）。
- 后台线程只做「一次性的滤波 + 预计算检测曲线」（对应端侧 start_play 时的 rt_init），
  逐拍推理在播放过程中实时进行，主线程不会被整段分析阻塞。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from ble_client import MODE_DEMO, MODE_LIVE
from config import CLASSES, FS, ROOT
from preprocessing import bandpass_filter, notch_filter

try:
    from PySide6.QtCore import QPointF, Qt, QThread, QTimer, Signal
    from PySide6.QtGui import (QColor, QIcon, QKeySequence, QPainter, QPen,
                               QPolygonF, QShortcut)
    from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                                   QLabel, QMainWindow, QPushButton, QVBoxLayout,
                                   QWidget)
    HAS_QT = True
except Exception:  # noqa: BLE001
    HAS_QT = False


# 全站配色：灰雾白底 + 去饱和前景，「灰蓝为主色、红只留给报警」
#
# 所有前景/背景组合按 WCAG 2.1 校验（tools/check_gui_contrast.py）：
#   文字 ≥ 4.5:1，图形 ≥ 3:1。改色后请跑该脚本，别凭肉眼调。
PALETTE = {
    "BG_APP":      "#F2F0EC",   # 主背景：暖灰白
    "BG_CARD":     "#FAF8F5",   # 卡片 / 绘图区：比背景稍亮
    "BORDER":      "#E0DCD7",   # 卡片边框
    "PLOT_BORDER": "#D8D4CE",   # 绘图区边框（中性灰；砖红只在报警期出现，
                              #   这样「边框变红」本身就是报警信号）
    "BTN_BORDER":  "#D8D4CE",   # 次要按钮边框
    "GRID":        "#E8E4DF",   # 绘图网格（装饰性）
    "TEXT":        "#4A4543",   # 标题 / 主要文字        8.3:1
    "BODY":        "#6B6663",   # 正文 / 五类计数        5.0:1
    "TEXT_DIM":    "#7D7871",   # 次要 / 占位文字        3.8:1（已知例外）
    "ACCENT":      "#6E8093",   # 灰蓝主色：仅用于「填充/线条」——主按钮、分段选中、波形线
                              #   全界面唯一主色（白字 4.1:1，见校验脚本的已知例外）
    "ACCENT_FILL": "#6E8093",   # 主按钮/分段选中填充（与 ACCENT 同色，全界面一个主色）
    "BTN_TEXT":    "#FFFFFF",
    "HR_LINE":     "#54677A",   # 心率曲线（同主按钮）
    "WAVE":        "#46586B",   # 波形线（与标题栏同族的灰蓝）
    "REF_LINE":    "#9E827E",   # 参考线（100bpm），需 ≥3:1 才「一眼可见」
    "ALERT":       "#985955",   # 砖红灰：报警 / V 类     4.7:1
    "ALERT_BG":    "#F6E9E5",   # 报警卡片底色（极淡红，对报警文字 4.6:1）
}
# 波形上的心拍标注需要区分类别，故保留各自色相（均 ≥3:1，见校验脚本）
CLASS_COLORS = {
    # 按「灰绿/灰青/砖红/奶茶/灰蓝」色相，但加深到 ≥3:1（波形上的标注属图形，
    # 原始色号偏淡只有 2.4~2.6:1，在浅底上几乎看不出竖线）
    "N": "#6E8069",
    "S": "#5E8287",
    "V": "#B0716E",
    "F": "#8A7458",
    "Q": "#66809C",
}

# 与端侧一致的播放参数（见 firmware/main/main.c）
TICK_MS = 20             # 每 tick 20ms（端侧 TICK_MS）
# 回放参数：离散档位用分段按钮切换（比滑块精确、比输入框快，符合医疗设备
# 「快速确认参数」的交互习惯）。默认 4× / 3s 与端侧 PLAY_SPEED_MULT / DISP_POINTS 一致。
SPEED_OPTIONS = (0.5, 1, 2, 4, 8)
WINDOW_OPTIONS_S = (2, 3, 5, 10)
DEFAULT_SPEED = 4
DEFAULT_WINDOW_S = 3
VIEW_POINTS = DEFAULT_WINDOW_S * FS      # 默认窗口点数（360*3 = 1080）
CONTROL_H = 32                            # 工具栏控件统一高度（下拉框/按钮/分段按钮对齐）


class ECGPlot(QWidget):
    """自绘 ECG 实时滚动波形 + 心拍标注。

    右端永远是「当前时刻」（cursor），窗口为 [cursor-window, cursor)。
    与端侧监测页一致：波形向左滚出、心拍用类别色竖线标注并带类别字母。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.signal = np.array([], dtype=np.float32)   # 已滤波的整段信号
        self.beats = []          # [(r_peak, class_name)]，只画 <= cursor 的（不剧透）
        self.cursor = 0          # 当前播放游标（采样点）
        self.window = VIEW_POINTS
        self.alarm = 0           # 0/1/2：报警时把外框画成红色（呼应端侧整屏红）
        self.phase = False       # 报警闪烁相位（由主窗口翻转）
        self.placeholder = "请选择数据集 / 导入 ECG 文件"   # 空态提示（蓝牙源会换文案）
        self.setMinimumHeight(260)

    def set_source(self, sig):
        """换一份新信号：游标与心拍清空。"""
        self.signal = np.asarray(sig, dtype=np.float32).ravel()
        self.beats = []
        self.cursor = 0
        self.alarm = 0
        self.update()

    def set_signal_live(self, sig):
        """流式源专用：只替换信号缓冲，不清游标/心拍。

        本地回放用 set_source()（整段一次性换）；蓝牙流式每 tick 喂一点、
        缓冲不断增长，若每次 set_source 会把已画的心拍与游标一并清掉。
        """
        self.signal = np.asarray(sig, dtype=np.float32).ravel()

    def add_beat(self, r_peak, cls):
        """实时追加一个「已检测到」的心拍（只会在 cursor 之后出现，天然不剧透）。"""
        self.beats.append((int(r_peak), cls))

    def set_cursor(self, cursor, alarm=0, phase=False):
        self.cursor = int(cursor)
        self.alarm = int(alarm)
        self.phase = bool(phase)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(PALETTE["BG_CARD"]))
        w, h = self.width(), self.height()

        # 外框：平时 1px 浅灰描边。
        # 报警时**不铺底色**（铺满会让波形判读疲劳），只让边框在砖红/浅灰间闪烁，
        # 报警状态由边框 + 报警卡片共同表达——临床软件常用这种更克制的做法。
        if self.alarm:
            border = QColor(PALETTE["ALERT"]) if self.phase else QColor(PALETTE["PLOT_BORDER"])
        else:
            border = QColor(PALETTE["PLOT_BORDER"])
        p.setPen(QPen(border, 2))
        p.drawRect(1, 1, w - 3, h - 3)

        if len(self.signal) == 0:
            p.setPen(QColor(PALETTE["TEXT_DIM"]))
            p.drawText(self.rect(), Qt.AlignCenter, self.placeholder)
            return

        # 当前显示窗口：右端 = 游标所在时刻
        to = min(self.cursor, len(self.signal))
        frm = max(0, to - self.window)
        seg = self.signal[frm:to]
        if len(seg) < 2:
            p.setPen(QColor(PALETTE["TEXT_DIM"]))
            p.drawText(self.rect(), Qt.AlignCenter, "等待信号…")
            return
        # 缓冲不足一屏：先画已有部分，并在右上角提示（不留白、也不假装画满）
        if len(seg) < self.window:
            p.setPen(QColor(PALETTE["TEXT_DIM"]))
            p.drawText(self.rect().adjusted(0, 6, -8, 0),
                       Qt.AlignRight | Qt.AlignTop, "缓冲中")

        lo, hi = float(seg.min()), float(seg.max())
        rng = (hi - lo) or 1.0
        pad = rng * 0.15
        lo, hi = lo - pad, hi + pad

        # 网格
        p.setPen(QPen(QColor(PALETTE["GRID"]), 1))
        for gx in range(0, w, 50):
            p.drawLine(gx, 0, gx, h)
        for gy in range(0, h, 40):
            p.drawLine(0, gy, w, gy)

        # 波形：一次性折线，避免逐点 drawLine 的调用开销
        n = len(seg)
        xs = np.linspace(0, w, n)
        ys = (h - ((seg - lo) / (hi - lo) * h)).astype(np.float32)
        p.setPen(QPen(QColor(PALETTE["WAVE"]), 1.2))
        pts = [QPointF(float(xs[i]), float(ys[i])) for i in range(n)]
        p.drawPolyline(QPolygonF(pts))

        # 心拍标注：只画窗口内、且已推进到的心拍（右端=现在，未来不剧透）
        span = max(1, to - frm)
        for r_peak, cls in self.beats:
            if r_peak < frm or r_peak >= to:
                continue
            x = int((r_peak - frm) / span * w)
            color = QColor(CLASS_COLORS.get(cls, PALETTE["TEXT_DIM"]))
            p.setPen(QPen(color, 2))
            p.drawLine(x, 0, x, h)
            p.setPen(color)
            p.drawText(x + 2, 14, cls)


class HRPlot(QWidget):
    """心率趋势小图：固定高度，无数据时给提示而不是留一大片空白。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hr_values = []
        self.setFixedHeight(96)
        self.setStyleSheet(f"border:1px solid {PALETTE['BORDER']};")

    def set_hr(self, values):
        self.hr_values = values
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(PALETTE["BG_CARD"]))
        w, h = self.width(), self.height()

        f = p.font()
        f.setPixelSize(12)                 # 与卡片标签字号一致
        p.setFont(f)
        p.setPen(QColor(PALETTE["BODY"]))
        p.drawText(8, 16, "心率趋势 (bpm)")

        vals = np.asarray(self.hr_values, dtype=np.float64) if self.hr_values else np.zeros(0)
        vals = np.nan_to_num(vals, nan=0.0)
        vals = vals[vals > 0]
        if len(vals) < 2:
            p.setPen(QColor(PALETTE["TEXT_DIM"]))
            p.drawText(self.rect(), Qt.AlignCenter, "加载数据后显示心率趋势")
            return

        top, bot = 22, h - 8          # 顶部留给标签
        # 纵轴范围至少覆盖 96~104，保证 100 bpm 参考线**始终可见**——
        # 否则心率全在 100 以上或以下时参考线不画，就失去「一眼可见」的意义了。
        lo = min(max(0.0, float(vals.min()) - 10), 96.0)
        hi = max(float(vals.max()) + 10, 104.0)
        hi = max(hi, lo + 10)

        # 显示层做 5 点滑动平均（边缘用端点复制），减轻心率抖动造成的锯齿；
        # 只影响绘制，原始数值与统计不受影响。
        if len(vals) >= 5:
            win = 5
            pad = win // 2
            kern = np.ones(win) / win
            disp = np.convolve(np.pad(vals, pad, mode="edge"), kern, mode="valid")
        else:
            disp = vals

        p.setPen(QPen(QColor(PALETTE["GRID"]), 1))
        for gy in range(top, bot, 18):
            p.drawLine(0, gy, w, gy)

        def y_of(v):
            return bot - (v - lo) / (hi - lo) * (bot - top)

        # 100 bpm 参考线：先画（压在曲线下层），心动过速时段不用看数值即可感知
        y100 = int(y_of(100))
        p.setPen(QPen(QColor(PALETTE["REF_LINE"]), 1, Qt.DashLine))
        p.drawLine(0, y100, w, y100)
        p.setPen(QColor(PALETTE["TEXT_DIM"]))
        p.drawText(4, y100 - 3, "100")

        p.setPen(QPen(QColor(PALETTE["HR_LINE"]), 1.6))
        xs = np.linspace(0, w, len(disp))
        ys = y_of(disp)
        p.drawPolyline(QPolygonF([QPointF(float(xs[i]), float(ys[i]))
                                  for i in range(len(disp))]))


class PrepareWorker(QThread):
    """后台准备线程：整段滤波 + 预计算检测曲线（对应端侧 start_play 里的 rt_init）。

    逐拍推理不在这里做——那一步放到播放过程中实时进行，否则就不是「实时」了。
    """

    finished = Signal(object)   # 携带滤波后的信号
    failed = Signal(str)

    def __init__(self, engine, signal, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.signal = signal

    def run(self):
        try:
            raw = np.asarray(self.signal, dtype=np.float64)
            if raw.ndim > 1:
                raw = raw[:, 0]
            filtered = notch_filter(bandpass_filter(raw)).astype(np.float32)
            self.engine.load(filtered)      # 预计算 integ / thr（一次性，O(n)）
            self.finished.emit(filtered)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{exc}\n{traceback.format_exc(limit=3)}")


class SegmentedControl(QWidget):
    """分段按钮：一组互斥的离散选项，点一下即生效。

    选中项灰蓝填充白字、未选中白底浅边框；用于「速度」「窗口」这类档位选择。
    """

    changed = Signal(object)

    def __init__(self, options, current, fmt, parent=None, height=0):
        super().__init__(parent)
        self._options = list(options)
        self._value = current
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._buttons = []
        for opt in self._options:
            b = QPushButton(fmt(opt))
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)      # 不抢键盘焦点，快捷键才有效
            if height:
                b.setFixedHeight(height)      # 与工具栏按钮/下拉框等高
            b.clicked.connect(lambda _=False, o=opt: self.set_value(o))
            lay.addWidget(b)
            self._buttons.append(b)
        self._restyle()

    def value(self):
        return self._value

    def set_value(self, v, emit=True):
        if v not in self._options:
            return
        self._value = v
        self._restyle()
        if emit:
            self.changed.emit(v)

    def step(self, direction):
        """按档位前后移动（快捷键用）。"""
        i = self._options.index(self._value) + direction
        if 0 <= i < len(self._options):
            self.set_value(self._options[i])

    def _restyle(self):
        sel_qss = _seg_qss(True)
        unsel_qss = _seg_qss(False)
        for opt, b in zip(self._options, self._buttons):
            on = (opt == self._value)
            b.setChecked(on)
            b.setStyleSheet(sel_qss if on else unsel_qss)


def _seg_qss(selected: bool) -> str:
    """分段按钮样式：选中=灰蓝填充白字，未选中=白底浅边框。"""
    off = (f"QPushButton:disabled{{background:{PALETTE['BG_APP']};"
           f" color:{PALETTE['TEXT_DIM']}; border-color:{PALETTE['BORDER']};}}")
    if selected:
        return (f"QPushButton{{background:{PALETTE['ACCENT_FILL']}; color:{PALETTE['BTN_TEXT']};"
                f" border:1px solid {PALETTE['ACCENT_FILL']}; border-radius:5px;"
                f" padding:2px 6px; font-size:12px;}}" + off)
    return (f"QPushButton{{background:{PALETTE['BG_CARD']}; color:{PALETTE['BODY']};"
            f" border:1px solid {PALETTE['BTN_BORDER']}; border-radius:5px;"
            f" padding:2px 6px; font-size:12px;}}"
            f"QPushButton:hover{{border-color:{PALETTE['ACCENT_FILL']};}}" + off)


def _card(title: str, value_label: QLabel) -> QFrame:
    """把「小标题 + 值」包成一张卡片（浅底、1px 边框、圆角）。"""
    box = QFrame()
    box.setStyleSheet(
        f"QFrame{{background:{PALETTE['BG_CARD']}; border:1px solid {PALETTE['BORDER']};"
        f" border-radius:8px;}}")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(12, 8, 12, 8)
    lay.setSpacing(2)
    cap = QLabel(title)
    cap.setStyleSheet(f"color:{PALETTE['TEXT_DIM']}; font-size:12px; border:none;")
    value_label.setStyleSheet(
        f"color:{PALETTE['BODY']}; font-size:16px; font-weight:bold; border:none;")
    lay.addWidget(cap)
    lay.addWidget(value_label)
    return box


def _vsep() -> QFrame:
    """竖向分隔线（按钮分组用）。"""
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setStyleSheet(f"color:{PALETTE['BORDER']}; background:{PALETTE['BORDER']};")
    line.setFixedWidth(1)
    return line


def _primary_button_qss() -> str:
    """主按钮：灰蓝填充 + 白字（核心动作）。"""
    return (f"QPushButton{{background:{PALETTE['ACCENT_FILL']}; color:{PALETTE['BTN_TEXT']};"
            f" border:1px solid {PALETTE['ACCENT_FILL']}; border-radius:6px;"
            f" padding:5px 11px; font-size:14px;}}"
            f"QPushButton:disabled{{background:{PALETTE['BORDER']}; color:{PALETTE['BG_APP']};"
            f" border-color:{PALETTE['BORDER']};}}")


def _alert_button_qss() -> str:
    """报警确认按钮：砖红填充 + 白字（报警时把注意力引向「确认」）。"""
    return (f"QPushButton{{background:{PALETTE['ALERT']}; color:{PALETTE['BTN_TEXT']};"
            f" border:1px solid {PALETTE['ALERT']}; border-radius:6px;"
            f" padding:6px 14px; font-size:14px; font-weight:bold;}}"
            f"QPushButton:disabled{{background:{PALETTE['BORDER']}; color:{PALETTE['BG_APP']};"
            f" border-color:{PALETTE['BORDER']};}}")


def _secondary_button_qss() -> str:
    """次要按钮：白底 + 浅边框（数据操作类）。"""
    return (f"QPushButton{{background:{PALETTE['BG_CARD']}; color:{PALETTE['BODY']};"
            f" border:1px solid {PALETTE['BTN_BORDER']}; border-radius:6px;"
            f" padding:5px 11px; font-size:14px;}}"
            f"QPushButton:disabled{{color:{PALETTE['TEXT_DIM']};"
            f" border-color:{PALETTE['BORDER']};}}")


_ARROW_PNG = None


def _down_arrow_png() -> str:
    """主题灰色下拉箭头（chevron）的 PNG 路径，首次调用时用 QPainter 生成一次。

    QSS 的 QComboBox::down-arrow 只认图片资源；运行时画一个 14×10 的深灰箭头
    落盘到临时目录，避免额外引入资源文件。用 TEXT（比 BODY 更深）保证箭头可辨。
    """
    global _ARROW_PNG
    if _ARROW_PNG is not None:
        return _ARROW_PNG
    from PySide6.QtGui import QImage
    img = QImage(14, 10, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(PALETTE["TEXT"]), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline(QPolygonF([QPointF(2.0, 3.0), QPointF(7.0, 7.0), QPointF(12.0, 3.0)]))
    p.end()
    path = os.path.join(tempfile.gettempdir(), "ecg_gui_down_arrow.png")
    img.save(path)
    _ARROW_PNG = path.replace("\\", "/")
    return _ARROW_PNG


def _combo_qss() -> str:
    """下拉框样式（数据来源 / 数据集两处共用，保持一致）。"""
    arrow = _down_arrow_png()
    return (f"QComboBox{{color:{PALETTE['BODY']}; background:{PALETTE['BG_CARD']};"
            f" border:1px solid {PALETTE['BTN_BORDER']}; border-radius:6px;"
            f" padding:5px 8px; font-size:14px;}}"
            f"QComboBox:hover{{border-color:{PALETTE['ACCENT_FILL']};}}"
            f"QComboBox:disabled{{color:{PALETTE['TEXT_DIM']};"
            f" border-color:{PALETTE['BORDER']};}}"
            f"QComboBox::drop-down{{border:none; background:transparent; width:22px;"
            f" border-top-right-radius:6px; border-bottom-right-radius:6px;}}"
            f"QComboBox::down-arrow{{image:url(\"{arrow}\"); width:14px; height:10px;}}"
            f"QComboBox QAbstractItemView{{color:{PALETTE['BODY']};"
            f" background:{PALETTE['BG_CARD']}; selection-background-color:{PALETTE['BORDER']};"
            f" selection-color:{PALETTE['TEXT']}; border:1px solid {PALETTE['BTN_BORDER']};"
            f" padding:4px; outline:0;}}")


def _light_title_bar(win) -> None:
    """Windows 深色模式下把标题栏也设为浅色，避免「深色标题栏 + 浅色主体」割裂。

    仅 Windows 有效；非 Windows 或调用失败时静默跳过（保持系统默认）。
    """
    try:
        import ctypes
        from ctypes import wintypes
        value = ctypes.c_int(0)          # 0 = 浅色标题栏
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(int(win.winId())), 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:  # noqa: BLE001
        pass


class MainWindow(QMainWindow):
    def __init__(self, engine=None, datasets=None):
        super().__init__()
        self.setWindowTitle("轻量级可穿戴 ECG 实时心律失常监测系统")
        self.resize(1000, 720)
        self.engine = engine
        self.datasets = list(datasets or [])   # [(显示名, 加载函数)]
        self.worker = None

        central = QWidget()
        central.setStyleSheet(f"background-color: {PALETTE['BG_APP']};")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 14)     # 四周留白，内容不贴边
        root.setSpacing(10)

        # ── 工具栏两行：第一行「数据从哪来」，第二行「怎么跑」──
        # 组内间距 8px、组间竖线 + 16px；两行之间由 root 的 10px 间距分隔。
        bar1 = QHBoxLayout()
        bar1.setSpacing(8)

        # 数据来源：本地数据集 / 蓝牙设备。选蓝牙时「加载选中数据集」变「连接设备」，
        # 数据集下拉与「导入 ECG 文件」隐藏（蓝牙源的数据由设备推来，不吃本地文件）。
        self.combo_src = QComboBox()
        self.combo_src.addItems(["本地数据集", "蓝牙设备"])
        self.combo_src.setMinimumWidth(104)
        self.combo_src.setFixedHeight(CONTROL_H)
        self.combo_src.setStyleSheet(_combo_qss())
        lbl_src = QLabel("来源")
        lbl_src.setStyleSheet(f"color:{PALETTE['TEXT_DIM']}; font-size:12px;")

        self.combo = QComboBox()
        self.combo.setMinimumWidth(190)
        self.combo.setFixedHeight(CONTROL_H)
        self.combo.setStyleSheet(_combo_qss())
        for label, _ in self.datasets:
            self.combo.addItem(label)

        self.btn_load = QPushButton("加载选中数据集")
        self.btn_open = QPushButton("导入 ECG 文件")
        self.btn_play = QPushButton("▶ 开始实时分析")
        self.btn_ack = QPushButton("确认报警")
        self.btn_save = QPushButton("💾 保存接收数据")   # 次要按钮 + 图标，与主操作拉开层级
        self.btn_save.setCheckable(True)
        self.btn_save.setChecked(True)          # 默认开启：无线采集的数据默认落盘
        self.btn_save.setToolTip("把蓝牙收到的样本按 ECG1 格式存到 sdcard/，便于离线复现")
        for b in (self.btn_load, self.btn_open, self.btn_ack, self.btn_save):
            b.setStyleSheet(_secondary_button_qss())
            b.setFixedHeight(CONTROL_H)
        self.btn_play.setStyleSheet(_primary_button_qss())   # 核心动作：主色填充
        self.btn_play.setFixedHeight(CONTROL_H)
        self.btn_play.setEnabled(False)
        self.btn_ack.setEnabled(False)                       # 平时置灰，报警时才可用
        has_ds = bool(self.datasets)
        self.combo.setEnabled(has_ds)
        self.btn_load.setEnabled(has_ds)

        bar1.addWidget(lbl_src)
        bar1.addWidget(self.combo_src)
        bar1.addWidget(self.combo)
        bar1.addWidget(self.btn_load)
        bar1.addWidget(self.btn_open)
        bar1.addSpacing(8)
        bar1.addWidget(_vsep())
        bar1.addSpacing(8)
        bar1.addWidget(self.btn_save)          # 仅蓝牙源可见（_on_source_changed 控制）
        bar1.addStretch(1)
        root.addLayout(bar1)

        # 第二行：运行控制（启停 / 报警确认）｜ 回放参数（速度 / 窗口）
        bar2 = QHBoxLayout()
        bar2.setSpacing(8)
        self.seg_speed = SegmentedControl(SPEED_OPTIONS, DEFAULT_SPEED,
                                          lambda v: f"{v:g}×", height=CONTROL_H)
        self.seg_window = SegmentedControl(WINDOW_OPTIONS_S, DEFAULT_WINDOW_S,
                                           lambda v: f"{v:g}s", height=CONTROL_H)
        # 初始一律置灰：与「开始实时分析」同基准——**加载数据后**才可用，
        # 否则点了也没有数据可放，只会让用户困惑。
        self._set_playback_controls_enabled(False)

        bar2.addWidget(self.btn_play)
        bar2.addWidget(self.btn_ack)
        bar2.addSpacing(8)
        bar2.addWidget(_vsep())
        bar2.addSpacing(8)
        for cap, ctl in (("速度", self.seg_speed), ("窗口", self.seg_window)):
            lbl = QLabel(cap)
            lbl.setStyleSheet(f"color:{PALETTE['TEXT_DIM']}; font-size:12px;")
            bar2.addWidget(lbl)
            bar2.addWidget(ctl)
            bar2.addSpacing(4)
        bar2.addStretch(1)
        root.addLayout(bar2)

        # ── 状态卡片行：状态 / 心率 / 心拍 / 报警（报警卡片触发时变砖红）──
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.lbl_status = QLabel("就绪")
        self.lbl_hr = QLabel("-- 次/分")
        self.lbl_beat = QLabel("--")
        self.lbl_alarm = QLabel("无")
        self.card_status = _card("状态", self.lbl_status)
        # 状态卡第二行小字：蓝牙连接 / 丢包 / 电量（本地源时隐藏，不占版式）
        self.lbl_status_sub = QLabel("")
        self.lbl_status_sub.setStyleSheet(
            f"color:{PALETTE['TEXT_DIM']}; font-size:11px; border:none;")
        self.lbl_status_sub.setVisible(False)
        self.card_status.layout().addWidget(self.lbl_status_sub)
        self.card_hr = _card("心率", self.lbl_hr)
        self.card_beat = _card("心拍", self.lbl_beat)
        self.card_alarm = _card("报警", self.lbl_alarm)
        for c in (self.card_status, self.card_hr, self.card_beat, self.card_alarm):
            cards.addWidget(c)
        cards.addStretch(1)
        self.lbl_progress = QLabel("0%")
        self.lbl_progress.setStyleSheet(f"color:{PALETTE['TEXT_DIM']}; font-size:14px;")
        cards.addWidget(self.lbl_progress)
        root.addLayout(cards)

        # ── ECG 波形（1px 浅灰边框，与卡片一致）──
        self.ecg_plot = ECGPlot()
        root.addWidget(self.ecg_plot, 3)

        # ── 心率趋势（固定小高度，无数据时给提示）──
        self.hr_plot = HRPlot()
        root.addWidget(self.hr_plot)

        # ── 五类计数：统一正文灰，仅 V 用砖红加粗（需要时才突出）──
        stats = QHBoxLayout()
        stats.setSpacing(20)
        self.stat_labels = {}
        for c in CLASSES:
            lbl = QLabel(f"{c}: 0")
            color = PALETTE["ALERT"] if c == "V" else PALETTE["BODY"]
            weight = "bold" if c == "V" else "normal"
            lbl.setStyleSheet(f"color:{color}; font-weight:{weight}; font-size:15px;")
            stats.addWidget(lbl)
            self.stat_labels[c] = lbl
        stats.addStretch(1)
        # 蓝牙丢包率（次要色小字，与 N/S/V/F/Q 同行右侧对齐；仅蓝牙会话显示）
        self.lbl_ble = QLabel("")
        self.lbl_ble.setStyleSheet(f"color:{PALETTE['TEXT_DIM']}; font-size:12px;")
        stats.addWidget(self.lbl_ble)
        root.addLayout(stats)

        self._window_s = DEFAULT_WINDOW_S
        self.seg_speed.changed.connect(self._on_speed_changed)
        self.seg_window.changed.connect(self._on_window_changed)
        self._install_shortcuts()

        self.btn_open.clicked.connect(self._open)
        self.btn_load.clicked.connect(self._on_load_clicked)   # 本地=加载，蓝牙=连接
        self.btn_play.clicked.connect(self._play)
        self.btn_ack.clicked.connect(self._ack_alarm)
        self.combo_src.currentIndexChanged.connect(self._on_source_changed)

        self.signal = None
        self.beats = []
        self.hr_values = []

        # ── 蓝牙状态 ──
        self.ble = None              # BleClientWorker（连接中/已连接时非空）
        self.ble_active = False      # 正在接收波形流
        self.ble_connected = False
        self.ble_mode = None         # MODE_DEMO / MODE_LIVE
        self.ble_pending = []        # 待喂给引擎的样本块（主线程消费）
        self.ble_sos = None          # BLE 实时会话的因果滤波 SOS
        self.ble_zi = None           # BLE 实时滤波器的跨块状态
        self.ble_save_chunks = []    # 落盘缓冲（int16 原生值）
        self.ble_loss = 0.0
        self.ble_battery = -1
        self.ble_save_seq = 0

        # 实时回放（与端侧同一套节拍：20ms tick、4 倍速）
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)
        self.playing = False
        self.paused = False
        self.filtered = None
        self.latched_alarm = 0          # 报警锁存：确认前一直显示
        self.alarm_hr_extreme = 0.0     # 锁存期间录入的极值心率（过速记峰值/过缓记最低）
        self.alarm_phase = False        # 报警闪烁相位
        self.alarm_next_flip = 0
        self.letter = "--"

        self._on_source_changed(0)   # 初始化控件可用性（默认本地数据集；需 self.filtered 已就绪）

        # 两行工具栏后最宽的是「数据通路」那一行，按自然宽度设定最小宽度，
        # 避免用户拉窄后控件被压缩换行或重叠；窗口默认就开到这个宽度。
        _w = max(1000, self.sizeHint().width())
        self.setMinimumWidth(_w)
        self.resize(_w, 720)

        _light_title_bar(self)      # Windows 深色模式下标题栏也转浅色

        if not has_ds:
            self.lbl_status.setText(
                "⚠ 未找到可用数据集：data/ 下需有 MIT-BIH 记录，或 sdcard/ 下有 .BIN 样本")

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------
    def _open(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 ECG 文件",
                                              "", "Numpy (*.npy);;所有文件 (*)")
        if not path:
            return
        try:
            arr = np.load(path)
        except Exception as exc:  # noqa: BLE001
            self.lbl_status.setText(f"⚠ 文件读取失败：{exc}")
            return
        self._load(arr, Path(path).name)

    def _load_selected(self):
        """加载下拉框当前选中的数据（延迟加载：选中项只在此时才读盘）。"""
        idx = self.combo.currentIndex()
        if idx < 0 or idx >= len(self.datasets):
            return
        label, loader = self.datasets[idx]
        self.lbl_status.setText(f"⏳ 正在加载 {label} …")
        self.lbl_status.setStyleSheet(f"color:{PALETTE['ALERT']}; font-size:14px;")
        # 处理事件让上面的提示先渲染出来（读一条 MIT-BIH 记录约 1 秒）
        QApplication.processEvents()
        try:
            sig = loader()
        except Exception as exc:  # noqa: BLE001
            self.lbl_status.setText(f"⚠ 加载 {label} 失败：{exc}")
            self.lbl_status.setStyleSheet(f"color:{PALETTE['ALERT']}; font-size:14px;")
            return
        self._load(sig, label)

    def _load(self, sig, label=None):
        sig = np.asarray(sig, dtype=np.float32)
        if sig.ndim > 1:
            sig = sig[:, 0]
        self.signal = sig
        self.filtered = None
        self.beats = []
        self.hr_values = []
        self.latched_alarm = 0
        self.alarm_hr_extreme = 0.0
        self.letter = "--"
        self.ecg_plot.set_source(np.zeros(0, dtype=np.float32))
        self.hr_plot.set_hr([])
        for c in CLASSES:
            self.stat_labels[c].setText(f"{c}: 0")
        name = f"{label}：" if label else ""
        self.lbl_status.setText(f"已加载 {name}{len(sig)} 采样点 ({len(sig)/FS:.1f} 秒)")
        self.lbl_status.setStyleSheet(f"color:{PALETTE['BODY']}; font-size:14px;")
        self.btn_play.setEnabled(True)
        self._set_playback_controls_enabled(True)   # 有数据了，回放参数才可用
        self._update_readouts()
        # 与端侧一致：选中样本即开始播放（先后台滤波 + 预计算检测曲线）
        self._prepare_and_play()

    # ------------------------------------------------------------------
    # 数据来源：本地数据集 / 蓝牙设备
    # ------------------------------------------------------------------
    def _on_source_changed(self, idx):
        """切换来源：控件语义与可用性跟着变（蓝牙时不吃本地文件）。"""
        ble_src = (idx == 1)

        # 切到蓝牙先停掉本地回放，避免两个源同时推数据；切回本地则断开蓝牙。
        if ble_src:
            self._stop_local_playback()
        elif self.ble_connected or self.ble is not None:
            self._disconnect_ble()

        self.combo.setVisible(not ble_src)
        self.btn_open.setVisible(not ble_src)
        self.btn_save.setVisible(ble_src)              # 保存接收数据只在蓝牙源出现
        # 切源后波形区清空：新来源是全新数据流，旧来源的波形/心拍/游标不能残留。
        self.ecg_plot.placeholder = "等待设备数据流…" if ble_src else "请选择数据集 / 导入 ECG 文件"
        self.ecg_plot.set_source(np.zeros(0, dtype=np.float32))

        if ble_src:
            self.btn_load.setText("连接设备")
            self.seg_speed.setEnabled(False)          # 蓝牙是实时流，恒 1×
            self._update_status_sub()
        else:
            self.btn_load.setText("加载选中数据集")
            self.lbl_status_sub.setVisible(False)
            self.lbl_ble.setText("")
            self._set_playback_controls_enabled(self.filtered is not None)
        self._refresh_data_path_enabled()

    def _on_load_clicked(self):
        """「加载/连接」按钮：按当前来源分派。"""
        if self.combo_src.currentIndex() == 1:
            if self.ble_connected or self.ble is not None:
                self._disconnect_ble()
            else:
                self._connect_ble()
        else:
            self._load_selected()

    # ---- 连接生命周期 ----
    def _connect_ble(self):
        if self.engine is None:
            self._ble_status_text("⚠ 无可用分析引擎（请用 run.py gui 启动）", alert=True)
            return
        try:
            from ble_client import BleClientWorker
        except Exception as exc:  # noqa: BLE001
            self._ble_status_text(f"⚠ 蓝牙模块不可用：{exc}", alert=True)
            return

        self._ble_status_text("⏳ 正在扫描并连接设备…", alert=True)
        self.btn_load.setText("连接中…")

        self.ble = BleClientWorker()
        self.ble.connected.connect(self._on_ble_connected)
        self.ble.disconnected.connect(self._on_ble_disconnected)
        self.ble.failed.connect(self._on_ble_failed)
        self.ble.session_started.connect(self._on_ble_session)
        self.ble.session_ended.connect(self._on_ble_session_end)
        self.ble.samples.connect(self._on_ble_samples)
        self.ble.status.connect(self._on_ble_status)
        self.ble.start()
        self._refresh_data_path_enabled()   # 连接中：来源/加载等锁定，连接按钮也置灰

    def _disconnect_ble(self):
        """主动断开（按钮 / 切回本地源 / 关闭窗口）。"""
        worker, self.ble = self.ble, None
        if worker is not None:
            worker.stop()
        self._retire_thread(worker)
        self._flush_ble_save()
        self._on_ble_disconnected()

    def _on_ble_connected(self, name):
        self.ble_connected = True
        self.btn_load.setText("断开设备")
        self._refresh_data_path_enabled()
        self._ble_status_text(f"蓝牙已连接：{name}")
        self._update_status_sub()

    def _on_ble_disconnected(self):
        was_active = self.ble_active
        self.ble_connected = False
        self.ble_active = False
        self.timer.stop()
        self.playing = False
        self.paused = False
        self.btn_load.setText("连接设备")
        self._refresh_data_path_enabled()
        self.btn_play.setText("▶ 开始实时分析")
        self.btn_play.setEnabled(False)
        self.lbl_status_sub.setVisible(False)
        self.lbl_ble.setText("")
        if was_active:
            self._ble_status_text("蓝牙已断开，接收停止")
        else:
            self._ble_status_text("就绪")

    def _on_ble_failed(self, msg):
        # failed 是在 run() 内部发射的，此时线程尚未返回；若直接丢掉引用，
        # Python GC 会在 QThread 仍在运行时销毁它（Qt 会告警且行为未定义）。
        # 必须先等线程真正结束再放手。
        worker, self.ble = self.ble, None
        self._retire_thread(worker)
        self.ble_connected = False
        self.ble_active = False
        self.timer.stop()
        self.playing = False
        self.btn_load.setText("连接设备")
        self._refresh_data_path_enabled()
        self._ble_status_text(f"⚠ {msg.splitlines()[0]}", alert=True)

    # ---- 会话（设备端开始/结束推流）----
    def _on_ble_session(self, mode, name):
        """设备端一个推流会话开始：重置引擎与显示，按真实 1× 接收。"""
        self.ble_mode = int(mode)
        self.ble_active = True
        self.ble_pending = []
        self.ble_sos = None
        self.ble_zi = None
        self.ble_save_chunks = []
        self.ble_save_seq = 0
        self.ble_loss = 0.0

        # 演示会话的样本在设备端已滤波+z-score，直接喂；实时会话是原始值，由上位机因果滤波。
        if self.ble_mode == MODE_LIVE:
            sos_bp = butter(4, [0.5, 30.0], btype="band", fs=FS, output="sos")
            sos_notch = butter(2, [49.0, 51.0], btype="bandstop", fs=FS, output="sos")
            self.ble_sos = np.vstack([sos_bp, sos_notch])
            self.ble_zi = sosfilt_zi(self.ble_sos)

        self.engine.speed = 1.0
        self.seg_speed.set_value(1)
        self.engine.begin_stream()

        self._reset_stream_view()
        label = name or "设备样本"
        if self.ble_mode == MODE_DEMO:
            self._ble_status_text(f"▶ 实时接收中：{label}（1×）")
        else:
            self._ble_status_text(f"▶ 实时接收中：{label}（上位机已滤波）")

        self.playing = True
        self._refresh_data_path_enabled()   # 接收开始：锁定数据通路（连接/断开按钮除外）
        self.paused = False
        self.btn_play.setText("● 接收中")
        self.btn_play.setEnabled(False)      # 实时流不提供暂停（暂停会让缓冲无限堆积）
        self.seg_window.setEnabled(True)
        self._update_status_sub()
        self.timer.start()

    def _on_ble_session_end(self):
        """推流结束：把尾段分类冲刷掉，停止定时器。"""
        if not self.ble_active:
            return
        self.ble_active = False
        self.timer.stop()
        if self.engine is not None:
            self._consume_new_beats(self.engine.end_stream())
            self._update_readouts()
        self.playing = False
        self._refresh_data_path_enabled()
        self.btn_play.setText("▶ 开始实时分析")
        self.btn_play.setEnabled(False)
        saved = self._flush_ble_save()
        tail = f"，已存 {saved}" if saved else ""
        self._ble_status_text(f"设备端推流结束：共 {len(self.beats)} 拍{tail}")

    # ---- 接收回调（主线程）----
    def _on_ble_samples(self, arr):
        if not self.ble_active:
            return
        self.ble_pending.append(arr)
        if self.btn_save.isChecked():
            self.ble_save_chunks.append(np.rint(arr * 2000.0).astype("<i2"))

    def _on_ble_status(self, battery, sent, loss):
        self.ble_battery = int(battery)
        self.ble_loss = float(loss)
        self._update_status_sub()
        self.lbl_ble.setText(f"BLE: {loss * 100:.1f}%")
        color = PALETTE["ALERT"] if loss > 0.05 else PALETTE["TEXT_DIM"]
        self.lbl_ble.setStyleSheet(f"color:{color}; font-size:12px;")
        _ = sent

    def _update_status_sub(self):
        """状态卡第二行小字：连接 / 丢包 / 电量（仅蓝牙源显示）。"""
        if not self.ble_connected:
            self.lbl_status_sub.setVisible(False)
            return
        parts = ["蓝牙已连接"]
        if self.ble_active:
            parts.append("实时接收中")
        parts.append(f"丢包 {self.ble_loss * 100:.1f}%")
        if 0 <= self.ble_battery <= 100:
            parts.append(f"电量 {self.ble_battery}%")
        self.lbl_status_sub.setText(" · ".join(parts))
        color = PALETTE["ALERT"] if self.ble_loss > 0.05 else PALETTE["TEXT_DIM"]
        self.lbl_status_sub.setStyleSheet(f"color:{color}; font-size:11px; border:none;")
        self.lbl_status_sub.setVisible(True)

    def _ble_status_text(self, text, alert=False):
        self.lbl_status.setText(text)
        color = PALETTE["ALERT"] if alert else PALETTE["BODY"]
        self.lbl_status.setStyleSheet(f"color:{color}; font-size:14px;")

    def _reset_stream_view(self):
        """蓝牙新会话：清空显示与统计（不清引擎——引擎刚 begin_stream）。"""
        self.beats = []
        self.hr_values = []
        self.letter = "--"
        self.latched_alarm = 0
        self.alarm_hr_extreme = 0.0
        self.btn_ack.setEnabled(False)
        self.ecg_plot.set_source(np.zeros(0, dtype=np.float32))
        self.ecg_plot.window = int(self._window_s * FS)
        self.hr_plot.set_hr([])
        for c in CLASSES:
            self.stat_labels[c].setText(f"{c}: 0")
        self._style_alarm_card(False)
        self._update_readouts()

    def _consume_new_beats(self, new_beats):
        """把新分类的心拍并入显示与统计（本地回放与蓝牙接收共用）。"""
        for b in new_beats:
            self.ecg_plot.add_beat(b["r_peak"], b["class"])
            self.beats.append((b["r_peak"], b["class"]))
            self.hr_values.append(b["hr"] if b["hr"] else np.nan)
            self.letter = b["class"]
        if new_beats:
            self.hr_plot.set_hr(self.hr_values)

    def _flush_ble_save(self):
        """把本次蓝牙会话收到的样本按 ECG1 格式落盘（sdcard/）。返回文件名或 None。"""
        chunks, self.ble_save_chunks = self.ble_save_chunks, []
        if not chunks:
            return None
        try:
            data = np.concatenate(chunks).astype("<i2")
        except Exception:  # noqa: BLE001
            return None
        if data.size == 0:
            return None
        import struct
        out_dir = Path(__file__).resolve().parent.parent / "sdcard"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            name = f"ble_{time.strftime('%Y%m%d_%H%M%S')}_{self.ble_save_seq:03d}.BIN"
            self.ble_save_seq += 1
            with open(out_dir / name, "wb") as f:
                f.write(b"ECG1")
                f.write(struct.pack("<ii", int(data.size), FS))
                f.write(data.tobytes())
            return name
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 准备（后台线程）+ 实时回放
    # ------------------------------------------------------------------
    def _prepare_and_play(self):
        if self.signal is None or self.engine is None:
            return
        if self.worker is not None and self.worker.isRunning():
            # 上一次准备尚未结束：给出可见反馈，而不是静默忽略本次请求
            self.lbl_status.setText("⏳ 上一次准备仍在进行，请稍候再试…")
            self.lbl_status.setStyleSheet(f"color:{PALETTE['ALERT']}; font-size:14px; font-weight:bold;")
            return
        self.timer.stop()
        self.playing = False
        self.paused = False
        self.btn_play.setText("▶ 开始实时分析")
        self.btn_play.setEnabled(False)
        self.lbl_status.setText("⏳ 准备中：滤波 + 预计算检测曲线…")
        self.lbl_status.setStyleSheet(f"color:{PALETTE['ALERT']}; font-size:14px; font-weight:bold;")

        self.worker = PrepareWorker(self.engine, self.signal)
        self.worker.finished.connect(self._on_ready)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_ready(self, filtered):
        """后台准备完成：显示波形并立刻开始 4 倍速实时回放。"""
        self.filtered = filtered
        self.ecg_plot.set_source(filtered)
        self.hr_plot.set_hr([])
        self.hr_values = []
        self.beats = []
        self.letter = "--"
        self.latched_alarm = 0
        self.alarm_hr_extreme = 0.0
        self.btn_ack.setEnabled(False)
        self.playing = True
        self._refresh_data_path_enabled()   # 监测开始：锁定数据通路
        self.paused = False
        self.btn_play.setText("⏸ 暂停")
        self.btn_play.setEnabled(True)
        self.lbl_status.setText(self._running_text())
        self.lbl_status.setStyleSheet(f"color:{PALETTE['BODY']}; font-size:14px;")
        self.timer.start()

    def _play(self):
        """按钮：暂停 / 继续；未准备时先准备。"""
        if self.filtered is None:
            self._prepare_and_play()
            return
        if self.paused:
            self.paused = False
            self.btn_play.setText("⏸ 暂停")
            self.timer.start()
        else:
            self.paused = True
            self.btn_play.setText("▶ 继续")
            self.timer.stop()

    def _ack_alarm(self):
        """确认报警：解除锁存（对应端侧触摸确认）。"""
        self.latched_alarm = 0
        self.alarm_hr_extreme = 0.0
        self.btn_ack.setEnabled(False)
        self._update_readouts()

    def _tick(self):
        """一个播放节拍：推进游标 -> 取新分类的心拍 -> 刷新显示。"""
        if self.engine is None or not self.playing or self.paused:
            return
        # 蓝牙：先把收到的样本喂进引擎（流式增量），再按 20ms 推进
        if self.ble_pending:
            chunks, self.ble_pending = self.ble_pending, []
            arr = np.concatenate(chunks)
            if self.ble_mode == MODE_LIVE and self.ble_sos is not None:
                arr, self.ble_zi = sosfilt(self.ble_sos, arr, zi=self.ble_zi)
            self.engine.feed(arr)
        st = self.engine.tick(TICK_MS)
        self._consume_new_beats(st["new_beats"])

        # 报警锁存：越界即锁存，直到点「确认报警」。
        # 判据是引擎给的**瞬时心率**（60 / 最近一个 RR），因此单个早搏的短 RR
        # 就足以越界；锁存后即使心率回落也继续显示，故记录极值以便用户理解成因。
        if st["alarm"] and not self.latched_alarm:
            self.latched_alarm = int(st["alarm"])
            self.alarm_hr_extreme = float(st.get("hr") or 0.0)
            self.btn_ack.setEnabled(True)
        elif self.latched_alarm:
            hr_now = float(st.get("hr") or 0.0)
            if self.latched_alarm == 1:
                self.alarm_hr_extreme = max(self.alarm_hr_extreme, hr_now)
            else:
                self.alarm_hr_extreme = (hr_now if self.alarm_hr_extreme <= 0
                                         else min(self.alarm_hr_extreme, hr_now))
        if self.latched_alarm:
            now = int(time.monotonic() * 1000)      # 毫秒单调时钟（报警闪烁相位用）
            if now >= self.alarm_next_flip:
                self.alarm_phase = not self.alarm_phase
                self.alarm_next_flip = now + 250        # ~2Hz，与端侧闪烁一致

        # 蓝牙流式：缓冲是边收边长的，波形画布持有的还是会话开始时的空信号，
        # 得每个 tick 把引擎里的最新缓冲换进去（只换信号，不清游标/心拍）。
        if getattr(self.engine, "streaming", False) and self.engine.sig is not None:
            self.ecg_plot.set_signal_live(self.engine.sig)
        self.ecg_plot.set_cursor(st["pos"], self.latched_alarm, self.alarm_phase)
        cnt = st.get("counts") or [0] * len(CLASSES)
        for i, c in enumerate(CLASSES):
            self.stat_labels[c].setText(f"{c}: {cnt[i]}")
        self._update_readouts(hr=st["hr"], progress=self.engine.progress)

        if st["finished"] and not self.engine.streaming:
            self.timer.stop()
            self.playing = False
            self._refresh_data_path_enabled()
            self.btn_play.setText("▶ 重新播放")
            self.btn_play.setEnabled(True)
            self.lbl_status.setText(f"播放结束：共 {len(self.beats)} 拍"
                                    + ("　⚠ 报警未确认" if self.latched_alarm else ""))
            self.lbl_status.setStyleSheet(f"color:{PALETTE['BODY']}; font-size:14px;")

    def _refresh_data_path_enabled(self):
        """数据通路在监测进行中锁定：数据集/导入/加载不可改，防止运行中误换数据。

        来源下拉始终可用——切换来源是顶层动作，会先停掉当前回放/接收再切换，
        因此不必锁；蓝牙源的「连接/断开」按钮也是停止接收的唯一出口，连接中才置灰。
        """
        busy = self.playing or self.ble_active
        ble_src = self.combo_src.currentIndex() == 1
        connecting = ble_src and (self.ble is not None and not self.ble_connected)
        if ble_src:
            self.combo.setEnabled(False)
            self.btn_open.setEnabled(False)
            self.btn_load.setEnabled(not connecting)
        else:
            # 本地回放中允许切换数据集/导入新文件：加载新数据会先停掉当前回放。
            self.combo.setEnabled(bool(self.datasets))
            self.btn_open.setEnabled(True)
            self.btn_load.setEnabled(bool(self.datasets))

    def _stop_local_playback(self):
        """停本地回放（切到蓝牙源时用；不碰 BLE 连接状态）。"""
        self.timer.stop()
        self.playing = False
        self.paused = False
        self.btn_play.setText("▶ 开始实时分析")
        self.btn_play.setEnabled(False)

    def _set_playback_controls_enabled(self, on: bool):
        """回放参数控件的可用性跟着「有无数据」走，与主按钮联动。

        空闲时点了也没有数据可放，置灰可避免「点了没反应」的困惑。
        """
        self.seg_speed.setEnabled(on)
        self.seg_window.setEnabled(on)

    def _running_text(self) -> str:
        """运行状态文案（同时作为回放参数的反馈，用户不必回头看工具栏）。"""
        if self.engine is not None and getattr(self.engine, "streaming", False):
            return "▶ 实时接收中（蓝牙设备，1× 速度）"
        return (f"▶ 实时监测中（{self.seg_speed.value():g}× 速度，"
                f"窗口 {self._window_s:g}s）")

    def _on_speed_changed(self, v):
        """改速度：只改引擎「每 tick 前进的采样点数」，不重置数据流，下一拍即生效。"""
        if self.engine is not None:
            self.engine.speed = float(v)
        if self.playing:
            self.lbl_status.setText(self._running_text())

    def _on_window_changed(self, v):
        """改窗口：只改显示点数并重绘当前段，不动引擎游标与已缓存数据。"""
        self._window_s = v
        self.ecg_plot.window = int(v * FS)
        self.ecg_plot.update()
        if self.playing:
            self.lbl_status.setText(self._running_text())

    def _install_shortcuts(self):
        """快捷键：+/- 调速度，]/[ 调窗口（均为离散档位切换）。"""
        for keys, ctl, delta in ((("+", "="), self.seg_speed, +1),
                                 (("-", "_"), self.seg_speed, -1),
                                 (("]", "}"), self.seg_window, +1),
                                 (("[", "{"), self.seg_window, -1)):
            for k in keys:
                QShortcut(QKeySequence(k), self,
                          activated=lambda c=ctl, d=delta: c.step(d))

    def _update_readouts(self, hr=None, progress=None):
        """刷新四张状态卡片（状态 / 心率 / 心拍 / 报警）与进度。"""
        if hr is None:
            hr = getattr(self.engine, "hr", 0.0) if self.engine is not None else 0.0
        if progress is None:
            progress = getattr(self.engine, "progress", 0.0) if self.engine is not None else 0.0

        # 卡片已带标题（心率/心拍/报警），值里不再重复前缀
        self.lbl_hr.setText(f"{hr:.0f} 次/分" if hr else "-- 次/分")
        self.lbl_beat.setText(self.letter if self.letter != "--" else "--")

        if self.latched_alarm == 1:
            self.lbl_alarm.setText(f"心动过速 · 峰值 {self.alarm_hr_extreme:.0f}")
        elif self.latched_alarm == 2:
            self.lbl_alarm.setText(f"心动过缓 · 最低 {self.alarm_hr_extreme:.0f}")
        else:
            self.lbl_alarm.setText("无")
        self._style_alarm_card(bool(self.latched_alarm))
        if self.engine is not None and getattr(self.engine, "streaming", False):
            self.lbl_progress.setText("实时")          # 流式无「进度」概念
        else:
            self.lbl_progress.setText(f"{int(progress * 100)}%")

    def _apply_button_mode(self, alarming):
        """按钮强调跟着状态走：报警时把实心强调让给「确认报警」。

        正常：「继续/暂停」实心灰蓝，「确认报警」次要且置灰；
        报警：「确认报警」变实心砖红（注意力应被引向确认），「继续」退回次要样式。
        """
        if alarming:
            self.btn_ack.setStyleSheet(_alert_button_qss())
            self.btn_play.setStyleSheet(_secondary_button_qss())
        else:
            self.btn_ack.setStyleSheet(_secondary_button_qss())
            self.btn_play.setStyleSheet(_primary_button_qss())

    def _style_alarm_card(self, on):
        """报警卡片：平时与其它卡片同色，触发时整卡变淡砖红以一眼可辨。"""
        self._apply_button_mode(on)
        if on:
            self.card_alarm.setStyleSheet(
                f"QFrame{{background:{PALETTE['ALERT_BG']};"
                f" border:1px solid {PALETTE['ALERT']}; border-radius:8px;}}")
            self.lbl_alarm.setStyleSheet(
                f"color:{PALETTE['ALERT']}; font-size:16px; font-weight:bold; border:none;")
        else:
            self.card_alarm.setStyleSheet(
                f"QFrame{{background:{PALETTE['BG_CARD']};"
                f" border:1px solid {PALETTE['BORDER']}; border-radius:8px;}}")
            self.lbl_alarm.setStyleSheet(
                f"color:{PALETTE['BODY']}; font-size:16px; font-weight:bold; border:none;")

    def _on_failed(self, msg):
        self.lbl_status.setText(f"⚠ 分析失败：{msg.splitlines()[0]}")
        self.lbl_status.setStyleSheet(f"color:{PALETTE['ALERT']}; font-size:12px; font-weight:bold;")
        print("[GUI] 分析失败：\n", msg, file=sys.stderr)
        self.btn_play.setEnabled(True)

    def _retire_thread(self, t):
        """安全回收后台 QThread：等待其结束，超时则终止。

        绝不能直接丢弃「仍在运行」的线程引用——Python GC 随后销毁运行中的 QThread
        会直接崩溃（Qt 明确警告 "QThread: Destroyed while thread is still running"）。
        故先 wait，仍未结束则 terminate 兜底，确保放手时线程已停止。
        """
        if t is None:
            return
        if t.isRunning():
            t.wait(3000)
        if t.isRunning():
            t.terminate()
            t.wait(1000)

    def closeEvent(self, event):  # noqa: N802
        # 先停回放定时器，再等后台线程结束，避免进程退出时线程仍在写导致崩溃
        self.timer.stop()
        self.playing = False
        if self.ble is not None:
            self.ble.stop()
            self._retire_thread(self.ble)
            self.ble = None
        self._retire_thread(self.worker)
        super().closeEvent(event)


def _normalize_app_font(app):
    """确保应用基准字体使用「点字号」，消除 QFont::setPointSize 告警。

    部分 Windows 环境下系统默认字体以像素为单位，此时 QFont.pointSize() 返回
    哨兵值 -1；Qt 的主题/原生样式引擎随后按点字号换算时会打印
    "QFont::setPointSize: Point size <= 0 (-1), must be greater than 0"。
    仅在检测到基准字体确无点字号时才补一个（按 96dpi 折算），
    已是点字号则不改动，避免影响既有排版。
    """
    f = app.font()
    if f.pointSize() > 0:
        return
    px = f.pixelSize()
    f.setPointSize(max(1, round(px * 72 / 96)) if px > 0 else 9)
    app.setFont(f)


_qt_prev_handler = None


def _is_benign_qt_msg(msg: str) -> bool:
    """Qt 已知无害的一条告警：像素字号字体经原生样式引擎换算时打印的
    "QFont::setPointSize: Point size <= 0 (-1) ..."。不影响功能与外观。"""
    return "QFont::setPointSize" in msg and "Point size" in msg


def _install_qt_warning_filter():
    """静默上述已知无害告警，其余 Qt 消息原样放行（不掩盖真实问题）。"""
    from PySide6.QtCore import qInstallMessageHandler

    def handler(mode, ctx, msg):
        if _is_benign_qt_msg(msg):
            return
        if _qt_prev_handler is not None:
            _qt_prev_handler(mode, ctx, msg)
        else:
            sys.stderr.write(msg + "\n")

    global _qt_prev_handler
    _qt_prev_handler = qInstallMessageHandler(handler)


def _load_app_icon() -> "QIcon | None":
    """应用图标：打包后从 PyInstaller 解包目录取，源码运行从 packaging/ 取。

    两处相对路径一致（packaging/ecg.ico），由 build_exe.bat 的 --add-data 保证。
    """
    base = Path(getattr(sys, "_MEIPASS", None) or ROOT)
    path = base / "packaging" / "ecg.ico"
    if path.is_file():
        return QIcon(str(path))
    return None


def run_gui(engine=None, datasets=None, initial_label=None):
    """启动 GUI。

    @param datasets       [(显示名, 加载函数)]，见 src/datasets.py
    @param initial_label  若给出且能在 datasets 中匹配到，则预选该项
    """
    if not HAS_QT:
        print("PySide6 未安装，无法启动 GUI")
        return 1
    # 只传程序名，避免 Qt 解析 argparse 残留参数（--record 等）产生告警
    # 先装过滤器，覆盖 QApplication 创建阶段的 Qt 消息
    _install_qt_warning_filter()
    app = QApplication([sys.argv[0]])
    icon = _load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)     # 所有窗口继承，含任务栏
    _normalize_app_font(app)
    win = MainWindow(engine=engine, datasets=datasets)
    if initial_label:
        idx = win.combo.findText(initial_label)
        if idx >= 0:
            win.combo.setCurrentIndex(idx)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run_gui())
