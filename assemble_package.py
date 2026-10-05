"""打包后整理 + 压缩，供 build_exe.bat 调用。

两种用法：
    python assemble_package.py --clean-dist    打包前清理 dist\\ECGMonitor（先清只读位）
    python assemble_package.py                  打包后拷贝数据 + 压缩

打包整理：把 models/（模型权重）、data/（MIT-BIH 演示记录）、sdcard/（SD 样本）
拷进 dist\\ECGMonitor，再把整个目录压缩成 ECGMonitor.zip，放在工程根目录。
数据目录均为「可选」，不存在时跳过并告警（GUI 仍可运行，只是下拉框无数据集）。
本脚本用 UTF-8 处理中文路径，避免 bat 的代码页问题。
"""

import os
import shutil
import stat
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "dist", "ECGMonitor")
ZIP_BASE = os.path.join(ROOT, "ECGMonitor")

# 便携包内随 exe 一起分发的内容（对应 config.ROOT 下的目录，frozen 时从 exe 同级解析）
DATA_DIRS = ("models", "data", "sdcard")


def _clear_readonly(path: str) -> None:
    """递归清除只读位，否则 Windows 上 shutil.rmtree/copytree 会失败。"""
    if not os.path.isdir(path):
        if os.path.exists(path):
            os.chmod(path, stat.S_IWRITE)
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(root, name)
            try:
                os.chmod(full, stat.S_IWRITE)
            except OSError:
                pass


def _copytree(src: str, dst: str) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _clear_readonly(dst)


def clean_dist() -> None:
    """删除 dist\\ECGMonitor，先清只读位，避免只读 DLL 卡死删除。"""
    if os.path.isdir(OUT_DIR):
        _clear_readonly(OUT_DIR)
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    elif os.path.exists(OUT_DIR):
        os.chmod(OUT_DIR, stat.S_IWRITE)
        try:
            os.remove(OUT_DIR)
        except OSError:
            pass


def main() -> int:
    if not os.path.isdir(OUT_DIR):
        print(f"[ERROR] 打包产物不存在：{OUT_DIR}", file=sys.stderr)
        return 1

    for i, name in enumerate(DATA_DIRS, 1):
        src = os.path.join(ROOT, name)
        print(f"[{i}/{len(DATA_DIRS) + 1}] 拷贝 {name}/ ...")
        if os.path.isdir(src):
            _copytree(src, os.path.join(OUT_DIR, name))
        else:
            print(f"  WARN: 未找到 {src}，跳过（GUI 中对应数据集不可用）")

    print(f"[{len(DATA_DIRS) + 1}/{len(DATA_DIRS) + 1}] 压缩 {ZIP_BASE}.zip ...")
    zip_path = ZIP_BASE + ".zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)
    shutil.make_archive(
        ZIP_BASE,
        "zip",
        root_dir=os.path.join(ROOT, "dist"),
        base_dir="ECGMonitor",
    )
    print(f"完成：{zip_path}")
    return 0


if __name__ == "__main__":
    if "--clean-dist" in sys.argv:
        clean_dist()
        sys.exit(0)
    sys.exit(main())
