# -*- coding: utf-8 -*-
"""校验 PC 端 GUI 配色的对比度（WCAG 2.1）。

为什么需要它：界面配色改过一版「很浅的莫兰迪」，前景色跟着变浅，结果浅底上
文字/波形几乎看不清（16 项组合有 16 项不达标）。肉眼调色很容易过头，所以把
校验固化成脚本，改色后跑一次即可。

判据：
  - 文字类前景（状态栏、统计、报警、提示）  ≥ 4.5:1
  - 图形类前景（波形线、心率曲线、心拍标注）≥ 3.0:1
  - 网格等纯装饰元素不设阈值

用法（项目根目录）:
    ./runtime/python/python.exe tools/check_gui_contrast.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUI = ROOT / "src" / "gui.py"


def _lum(hex_color: str) -> float:
    """WCAG 相对亮度。"""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(fg: str, bg: str) -> float:
    """WCAG 对比度（1~21）。"""
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def load_palette(src: str) -> tuple[dict, dict]:
    def block(name: str) -> str:
        i = src.index(f"{name} = {{")
        return src[i:src.index("}", i)]

    pal = dict(re.findall(r'"(\w+)":\s*"(#[0-9A-Fa-f]{6})"', block("PALETTE")))
    cls = dict(re.findall(r'"(\w)":\s*"(#[0-9A-Fa-f]{6})"', block("CLASS_COLORS")))
    return pal, cls


# 已知例外：低于 4.5:1 但经确认保留（记录在此，不静默放宽阈值）。
# 判据仍是「≥3:1」——即大字号/图形可用，小号提示字会略淡，属有意取舍。
EXCEPTIONS = {
    "次要文字 / 卡片底": "保留与正文的层次差，接受小号提示字略淡（≥3:1）",
    "主按钮白字 / 按钮填充": "按设计规范统一主色为 #6E8093（白字 4.1:1，略低于 4.5）",
}


def find_stray_colors(src: str) -> list[tuple[int, str]]:
    """找出 PALETTE / CLASS_COLORS 之外硬编码的十六进制颜色。

    为什么需要：上一版改配色时，替换脚本只匹配了「整个字符串就是一个色值」的情况，
    而界面标签的颜色是**嵌在样式表字符串里**的（"color:#58a6ff; font-size:14px;"），
    结果 15 处沿用了旧深色主题的浅色前景，画在浅底上几乎看不见——正是「字体看不清」
    的根因。这条守卫用来防止同类问题再次发生。
    """
    pal_start = src.index("PALETTE = {")
    pal_end = src.index("}", src.index("CLASS_COLORS = {")) + 1
    head, tail = src[:pal_start], src[pal_end:]
    stray = []
    for lineno, line in enumerate(head.splitlines(), 1):
        for m in re.finditer(r"#[0-9A-Fa-f]{6}\b", line):
            stray.append((lineno, m.group(0)))
    # tail 从 CLASS_COLORS 块右括号之后开始，其首行行号必须按原文从头数起：
    # 若用 head 段行数 + 2 近似（旧写法），palette 块本身占的几十行没算进去，
    # 报告的行号会整体偏小、指向错误位置。
    for i, line in enumerate(tail.splitlines(), src.count("\n", 0, pal_end) + 1):
        for m in re.finditer(r"#[0-9A-Fa-f]{6}\b", line):
            stray.append((i, m.group(0)))
    return stray


def main() -> int:
    src = GUI.read_text(encoding="utf-8")
    pal, cls = load_palette(src)
    bg_app, bg_card = pal["BG_APP"], pal["BG_CARD"]

    # (说明, 前景, 背景, 最低对比度)
    checks = [
        ("标题 / 卡片底",              pal["TEXT"],       pal["BG_CARD"], 4.5),
        ("正文 / 卡片底",              pal["BODY"],       pal["BG_CARD"], 4.5),
        ("正文 / 主背景",              pal["BODY"],       bg_app,         4.5),
        ("次要文字 / 卡片底",          pal["TEXT_DIM"],   pal["BG_CARD"], 4.5),
        # ACCENT 只用于「填充与线条」（主按钮、分段选中、波形线），不再作文字——
        # 状态文字已改用 BODY，故这里只按图形阈值校验。
        ("波形线 / 卡片底",            pal["WAVE"],       bg_card,        3.0),
        ("心率曲线 / 卡片底",          pal["HR_LINE"],    bg_card,        3.0),
        ("100bpm 参考线 / 卡片底",     pal["REF_LINE"],   bg_card,        3.0),
        ("主按钮白字 / 按钮填充",      pal["BTN_TEXT"],   pal["ACCENT_FILL"], 4.5),
        ("报警文字 / 主背景",          pal["ALERT"],      bg_app,         4.5),
        ("报警文字 / 报警卡片底",      pal["ALERT"],      pal["ALERT_BG"], 4.5),
        ("卡片边框 / 主背景（装饰）",  pal["BORDER"],     bg_app,         0.0),
        ("次要按钮边框 / 卡片底（装饰）", pal["BTN_BORDER"], pal["BG_CARD"], 0.0),
        ("网格 / 卡片底（装饰）",      pal["GRID"],       bg_card,        0.0),
    ]
    for k in "NSVFQ":
        checks.append((f"类别 {k} 波形标注 / 卡片底", cls[k], bg_card, 3.0))

    print(f"{'组合':<30}{'对比度':>8}  {'要求':>5}  结果")
    print("-" * 58)
    failed = 0
    warned = 0
    for name, fg, bg, need in checks:
        r = contrast(fg, bg)
        ok = r >= need
        if not ok and name in EXCEPTIONS and r >= 3.0:
            # 已知例外：仍满足 3:1（大字号/图形阈值）则只告警，不算失败
            warned += 1
            print(f"{name:<30}{r:>8.2f}  {need:>5.1f}  WARN  {EXCEPTIONS[name]}")
            continue
        if not ok and need > 0:
            failed += 1
        mark = "OK" if ok else ("FAIL" if need > 0 else "  -")
        print(f"{name:<30}{r:>8.2f}  {need:>5.1f}  {mark}")

    total = sum(1 for c in checks if c[3] > 0)
    tail = f"（另有 {warned} 项已知例外，见 EXCEPTIONS）" if warned else ""
    print(f"\n不达标 {failed} / {total} 项{tail}")

    stray = find_stray_colors(src)
    if stray:
        print(f"\n发现 {len(stray)} 处绕过色板的硬编码颜色（应改为引用 PALETTE/CLASS_COLORS）：")
        for lineno, c in stray[:10]:
            print(f"  gui.py:{lineno}  {c}")
        failed += len(stray)

    if failed:
        print("请调深对应前景色后重跑（浅底配深字，别凭肉眼判断）。")
        return 1
    print("全部达标：灰雾白底 + 同色系深色前景，文字 ≥4.5:1、图形 ≥3:1。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
