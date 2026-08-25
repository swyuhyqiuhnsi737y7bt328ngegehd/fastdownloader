#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fast Downloader Pro - One-Click Build Script
Usage:
    python build.py              # Full build (standalone exe, embeds all modules)
    python build.py --clean      # Clean all build artifacts
    python build.py --zip        # Build + package as zip (for GitHub Releases)

Prerequisites:
    pip install nuitka PyQt5 curl_cffi pycryptodome playwright
"""

import os
import sys
import shutil
import subprocess
import zipfile
import platform
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Nuitka 内部 scons 会读取 PROCESSOR_ARCHITECTURE，缺失时直接崩溃，
    # 在受限环境（IDE / 任务计划程序 / 某些 CI）里该变量可能为空，这里兜底。
    os.environ.setdefault("PROCESSOR_ARCHITECTURE", platform.machine())

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DIST_MAIN = DIST / "main.dist"
# Nuitka 编译缓存放项目本地，避免写入用户目录（权限/磁盘空间问题），也方便清理。
# Nuitka 4.x 没有 --cache-dir 选项，通过环境变量 NUITKA_CACHE_DIR 指定缓存根目录。
CACHE_DIR = ROOT / ".nuitka_cache"
os.environ.setdefault("NUITKA_CACHE_DIR", str(CACHE_DIR))
PYTHON = sys.executable

# zip 包名
ZIP_NAME = "fastdownloader.zip"


def run(cmd, label=""):
    prefix = f"[{label}] " if label else ""
    print(f"  {prefix}{cmd[:120]}{'...' if len(cmd) > 120 else ''}")

    result = subprocess.run(
        cmd, shell=True, cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = result.stdout + result.stderr

    for line in output.splitlines():
        if "Successfully" in line:
            print(f"  [OK] {line.strip()}")

    # 成功只看退出码：Nuitka 输出里出现 "Successfully" 不代表整体成功
    if result.returncode != 0:
        print(f"  [FAIL] Compilation failed (exit code {result.returncode})")
        print(output[-800:])
        sys.exit(1)

    return True


def clean():
    print("=== Cleaning build artifacts ===")
    # 只清理构建产物；不碰项目根目录下的 *.pyd/*.dll/*.a（可能是用户自己的文件）
    for pattern in ["dist", "*.build", "build_c", "build_nuitka",
                     ".nuitka_cache", "__pycache__"]:
        for p in ROOT.glob(pattern):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    for p in ROOT.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    print("  [OK] Cleaned\n")


def build_main():
    """
    编译 main.py 为独立 exe（内嵌全部项目模块与第三方依赖，免安装 Python）。
    Nuitka --standalone 会把所有被 import 的模块一起打包进 main.dist/，
    因此产物是完整可运行的，不需要额外的 dll/ 目录。
    """
    print("=== Building main.exe (standalone, all modules embedded) ===")
    DIST_MAIN.mkdir(parents=True, exist_ok=True)

    cmd = (
        f'"{PYTHON}" -m nuitka'
        f' --standalone --assume-yes-for-downloads'
        f' --enable-plugin=pyqt5'
        f' --windows-console-mode=disable'
        f' --output-dir="{DIST}"'
        f' "{ROOT / "main.py"}"'
    )
    run(cmd, "main.exe")
    print()


def cleanup_dist():
    print("=== Cleaning up build residue ===")
    for d in list(DIST_MAIN.glob("*.build")) + list(DIST.glob("*.build")):
        shutil.rmtree(d, ignore_errors=True)
    for d in [DIST / "main.build"]:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    for f in list(DIST_MAIN.glob("*.pyi")) + list(DIST.glob("*.pyi")):
        f.unlink(missing_ok=True)
    print("  [OK] Cleaned\n")


def make_zip():
    zip_path = ROOT / ZIP_NAME
    print(f"=== Packaging -> {ZIP_NAME} ===")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in DIST_MAIN.rglob("*"):
            if f.is_file() and "__pycache__" not in str(f):
                zf.write(f, f.relative_to(DIST_MAIN))
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  [OK] {ZIP_NAME} ({size_mb:.1f} MB)\n")


def print_summary():
    exe = DIST_MAIN / "main.exe"

    print("=" * 45)
    print("  BUILD COMPLETE")
    print("=" * 45)
    print()

    if exe.exists():
        print(f"  main.exe  ({exe.stat().st_size / 1048576:.1f} MB)")
    else:
        print("  [FAIL] main.exe missing!")

    total_mb = sum(f.stat().st_size for f in DIST_MAIN.rglob("*")
                   if f.is_file()) / 1048576
    print(f"  Output:  {DIST_MAIN}")
    print(f"  Total:   {total_mb:.1f} MB")
    print(f"\n  Run:     dist\\main.dist\\main.exe")


def main():
    args = set(sys.argv[1:])

    if "--clean" in args:
        clean()
        return

    if "--zip" in args:
        clean()

    build_main()

    cleanup_dist()

    print_summary()

    if "--zip" in args:
        make_zip()


if __name__ == "__main__":
    main()
