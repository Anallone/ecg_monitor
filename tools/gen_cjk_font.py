"""生成固件用的 16x16 点阵字库（ASCII + 按需汉字子集）。

为什么 ASCII 也要生成：早期实现里 ASCII 用固件内置的 8x8 字库、汉字用 16x16，
中英混排时数字字母明显偏小、基线也对不齐。改为**全部字形都由同一 TTF 渲染成
16x16**，行高统一、每字符步进恒为 16px，排版可以精确计算。

做法：扫描固件里实际会显示在屏上的字符串（各页面模块与 main.c），取出其中用到的
ASCII 可见字符与汉字，逐个渲染成 16x16 1-bit 点阵，输出
firmware/components/lcd/font16.h。

只嵌入界面真正用到的字（几十个，1~2KB），而不是整本 GB2312（267KB）。
改动界面文案后需重新运行本工具。

用法：
    ./runtime/python/python.exe tools/gen_cjk_font.py
    ./runtime/python/python.exe tools/gen_cjk_font.py --font C:/Windows/Fonts/msyh.ttc
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
# 界面文案分散在 application/ 各模块（i18n 集中放双语文案，其余模块有少量
# 内联字符串），main.c 也可能有；全部纳入扫描，避免新增文案漏进字库。
SCAN_FILES = [
    ROOT / "firmware" / "main" / "main.c",
] + sorted((ROOT / "firmware" / "application").rglob("*.c"))
OUT_H = ROOT / "firmware" / "components" / "lcd" / "font16.h"

GLYPH_W = 16
GLYPH_H = 16
BYTES_PER_GLYPH = GLYPH_W * GLYPH_H // 8   # 32

# ASCII 用更大的字号渲染：CJK 字体的汉字会占满整个字身框，而拉丁数字/字母
# 在同字号下只有约 10/16 的视觉高度，混排时数字显得偏小。用更大字号渲染 ASCII、
# 再放进同样的 16x16 格，两者视觉重量才匹配。数值可调（偏大就调小）。
ASCII_PT = 21

# ASCII 直接内置全集：运行时才出现的字符（如 SD 文件名 "S100.BIN" 里的 '.'）
# 无法靠扫描字面量覆盖，缺失就会显示成占位块。ASCII 共 95 字 ≈3KB，可以全带上。
# 汉字才做按需子集（整本 GB2312 要 267KB，太大）。
EXTRA_CHARS = "".join(chr(c) for c in range(0x20, 0x7F))


def collect_chars() -> list[str]:
    """收集「会显示在屏上」的字符（ASCII 可见字符 + 汉字）。

    策略：只扫描 main.c（界面文案都在其中），逐行处理：
      - 跳过整行注释、跳过含 ESP_LOG 的行（串口日志不上屏）
      - 先剥离行尾块注释，避免 /* 未分类不画 */ 这类注释混进来
      - 取该行**所有**字符串字面量中的字符

    为什么不按「lcd_draw_string 行」过滤：文案现在集中在双语表里
        static const char* const T_TITLE[2] = {"心电监测", "ECG MONITOR"};
    这些定义行并不绘制；而且带数字的文案常在 snprintf 行、绘制在下一行。
    按绘制行过滤会大面积漏字（表现为屏上出现实心占位块）。
    非显示字面量（"rb"、"%s/%s"）混进来只是多几个无用字形，代价可忽略。
    """
    chars: set[str] = set(EXTRA_CHARS)
    for path in SCAN_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("*", "/*", "//")):
                continue                        # 整行注释
            if "ESP_LOG" in line:
                continue                        # 串口日志，不上屏
            line = re.sub(r"/\*.*?\*/", "", line)   # 剥离行尾块注释
            for lit in re.findall('"([^"]*)"', line):
                # 剥掉 printf 格式符（%d/%s/%3.0f）：它们不是要显示的字符，
                # 否则会把 d/s/f 之类混进字库。先把 %% 还原成字面 '%'（进度显示要用），
                # 再剥离其余格式符。
                lit = lit.replace("%%", "%")
                lit = re.sub("%[-+ #0-9.]*[diufFeEgGsxXc%]", "", lit)
                for ch in lit:
                    if ch == "\n" or ch == "\t":
                        continue
                    if 0x20 <= ord(ch) <= 0x7E or "\u4e00" <= ch <= "\u9fff":
                        chars.add(ch)
    return sorted(chars)


def _to_bitmap(canvas: Image.Image) -> bytes:
    """16x16 图像转点阵字节（行主序，每行 2 字节，高位在左）。"""
    px = canvas.load()
    out = bytearray(BYTES_PER_GLYPH)
    for row in range(GLYPH_H):
        hi = lo = 0
        for col in range(GLYPH_W):
            if px[col, row]:
                if col < 8:
                    hi |= (0x80 >> col)
                else:
                    lo |= (0x80 >> (col - 8))
        out[row * 2] = hi
        out[row * 2 + 1] = lo
    return bytes(out)


# 渲染基准：所有字符都画在同一原点，保证基线一致
_ORIGIN = 16


def _ascii_band(font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """求 ASCII 字符集的公共竖直区间（保持数字/字母/标点的相对高低）。

    若逐字居中，'.' 会浮到中间、看起来不对；用统一基线区间则 '.' 自然贴底。
    """
    big = Image.new("1", (512, 128), 0)
    d = ImageDraw.Draw(big)
    d.text((_ORIGIN, _ORIGIN),
           "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz./:%()-+<>",
           font=font, fill=1)
    bbox = big.getbbox()
    if bbox is None:
        return _ORIGIN, _ORIGIN + GLYPH_H
    top, bottom = bbox[1], bbox[3]
    center = (top + bottom) // 2
    top = center - GLYPH_H // 2
    return top, top + GLYPH_H


def render_ascii(ch: str, font: ImageFont.FreeTypeFont, band: tuple[int, int]) -> bytes:
    """ASCII：竖直用公共基线区间，水平居中方格；超宽则等比缩小（不裁切，
    否则 M/W 等宽字母会被切掉笔画）。"""
    big = Image.new("1", (64, 64), 0)
    ImageDraw.Draw(big).text((_ORIGIN, _ORIGIN), ch, font=font, fill=1)
    canvas = Image.new("1", (GLYPH_W, GLYPH_H), 0)
    bbox = big.getbbox()
    if bbox is not None:
        glyph = big.crop((bbox[0], band[0], bbox[2], band[1]))
        gw, gh = glyph.size
        if gw > GLYPH_W:                     # 超过格宽则等比缩到格内
            ratio = GLYPH_W / gw
            glyph = glyph.resize((GLYPH_W, max(1, int(gh * ratio))), Image.LANCZOS)
            gw, gh = glyph.size
        canvas.paste(glyph, ((GLYPH_W - gw) // 2, max(0, (GLYPH_H - gh) // 2)))
    return _to_bitmap(canvas)


def render_cjk(ch: str, font: ImageFont.FreeTypeFont) -> bytes:
    """汉字：按实际 bbox 居中放入 16x16（汉字本身占满字身框）。"""
    big = Image.new("1", (64, 64), 0)
    ImageDraw.Draw(big).text((_ORIGIN, _ORIGIN), ch, font=font, fill=1)
    bbox = big.getbbox()
    if bbox is None:
        return bytes(BYTES_PER_GLYPH)        # 字体缺字
    glyph = big.crop(bbox)
    canvas = Image.new("1", (GLYPH_W, GLYPH_H), 0)
    gw, gh = glyph.size
    if gw > GLYPH_W or gh > GLYPH_H:         # 超框则等比缩到框内
        ratio = min(GLYPH_W / gw, GLYPH_H / gh)
        glyph = glyph.resize((max(1, int(gw * ratio)), max(1, int(gh * ratio))),
                             Image.LANCZOS)
        gw, gh = glyph.size
    canvas.paste(glyph, ((GLYPH_W - gw) // 2, (GLYPH_H - gh) // 2))
    return _to_bitmap(canvas)


def main(font_path: str) -> int:
    chars = collect_chars()
    if not chars:
        print("未在源码中找到可显示字符，退出")
        return 1
    try:
        font_cjk = ImageFont.truetype(font_path, GLYPH_H)
        font_ascii = ImageFont.truetype(font_path, ASCII_PT)   # ASCII 用更大字号
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 无法加载字体 {font_path}: {exc}")
        return 1

    band = _ascii_band(font_ascii)
    entries, blanks = [], []
    for ch in chars:
        bmp = (render_ascii(ch, font_ascii, band) if ord(ch) < 0x80
               else render_cjk(ch, font_cjk))
        # 只对汉字报缺字告警：
        #  - 空格本就没有墨迹；
        #  - ASCII 里 '_' 之类位于基线以下，被公共竖直区间裁掉是预期行为（且不上屏）。
        if not any(bmp) and ord(ch) >= 0x80:
            blanks.append(ch)
        entries.append((ord(ch), ch, bmp))

    n_ascii = sum(1 for c, _, _ in entries if c < 0x80)
    n_cjk = len(entries) - n_ascii

    lines = [
        "/* 自动生成：勿手动编辑。由 tools/gen_cjk_font.py 生成。",
        f" * 字体：{Path(font_path).name}  格：{GLYPH_W}x{GLYPH_H} 1-bit"
        f"（汉字 {GLYPH_H}pt / ASCII {ASCII_PT}pt，视觉等重）",
        f" * 覆盖 {len(entries)} 个字形（ASCII {n_ascii} + 汉字 {n_cjk}，按需子集）。",
        " * 全部字形同源同尺寸 -> 中英混排等高，每字符步进恒为 16px。",
        " * 改动界面文案后请重新运行该工具。 */",
        # 守卫名不能叫 FONT16_H——那是字高宏（16）。同名时守卫被定义成空宏，
        # 编译器会报 "FONT16_H redefined"，宏的实际取值还取决于包含顺序。
        "#ifndef FONT16_GEN_H",
        "#define FONT16_GEN_H",
        "",
        "#include <stdint.h>",
        "",
        f"#define FONT16_W {GLYPH_W}",
        f"#define FONT16_H {GLYPH_H}",
        f"#define FONT16_BYTES {BYTES_PER_GLYPH}",
        f"#define FONT16_COUNT {len(entries)}",
        "",
        "typedef struct {",
        "  uint16_t code;               /* Unicode 码点 */",
        "  uint8_t  bmp[FONT16_BYTES];  /* 行主序，每行 2 字节，高位在左 */",
        "} font16_glyph_t;",
        "",
        "/* 按 Unicode 升序排列，供二分查找 */",
        "static const font16_glyph_t g_font16[FONT16_COUNT] = {",
    ]
    for code, ch, bmp in entries:
        body = ", ".join(f"0x{b:02X}" for b in bmp)
        label = ch if ch != " " else "SP"
        lines.append(f"  {{0x{code:04X}, {{{body}}}}},  /* {label} */")
    lines += ["};", "", "#endif /* FONT16_GEN_H */"]
    OUT_H.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"已生成 {OUT_H}")
    print(f"  字形 {len(entries)} 个（ASCII {n_ascii} + 汉字 {n_cjk}），"
          f"点阵 {len(entries) * BYTES_PER_GLYPH} 字节，头文件 {OUT_H.stat().st_size} 字节")
    if blanks:
        print(f"  [警告] {len(blanks)} 个字渲染为空白（字体可能缺字）：{''.join(blanks)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="生成固件用 16x16 点阵字库（ASCII + 汉字子集）")
    ap.add_argument("--font", default="C:/Windows/Fonts/simhei.ttf",
                    help="TTF/TTC 字体路径（默认 simhei.ttf）")
    args = ap.parse_args()
    sys.exit(main(args.font))
