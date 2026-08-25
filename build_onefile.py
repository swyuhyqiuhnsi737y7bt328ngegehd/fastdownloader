#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Fast Downloader Pro - Single-File Build Script
================================================
    打包为单个 .exe 文件，Nuitka --onefile 模式。
    运行时自动解压到临时目录运行，用完自动清理。

Usage:
    python build_onefile.py                    # 标准优化（推荐）
    python build_onefile.py --tiny             # 标准 + 额外排除极少用的包
    python build_onefile.py --safe             # 安全模式（优化最少，兜底用）
    python build_onefile.py --standalone-upx   # standalone 目录 + UPX 压缩（需安装 UPX）
    python build_onefile.py --clean            # 清理所有构建产物

原理:
    Nuitka --onefile 会把 Python 代码编译成 C，再编译成单个 .exe。
    这个 .exe 实际上是一个压缩包 + 一个轻量加载器，
    运行时加载器把内容解压到 %TEMP%\\onefile_XXXX 目录，执行完后自动清理。

体积优化层次:
    --safe           : 基础 onefile，去掉控制台窗口，不做激进裁剪
    (默认)           : 排除测试框架/IPython/Dask/Numba 等无用包，启用 LTO
    --tiny           : 在默认基础上额外排除极少用到的标准库包（收益有限，
                       onefile 的 payload 已被 zstd 压缩，再压空间不大）
    --standalone-upx : 生成 standalone 目录后用 UPX 压缩 .pyd/.dll（需安装 UPX）
"""

import os
import sys
import shutil
import subprocess
import platform
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Nuitka 内部 scons 会读取 PROCESSOR_ARCHITECTURE，缺失时直接崩溃。
    # 在受限环境（IDE / 任务计划程序 / 某些 CI）里该变量可能为空，这里兜底。
    os.environ.setdefault("PROCESSOR_ARCHITECTURE", platform.machine())

# ─── 配置 ───────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
OUTPUT_EXE = DIST / "FastDownloader.exe"
# Nuitka 编译缓存放项目本地，避免写入用户目录（权限/磁盘空间问题），也方便清理。
# Nuitka 4.x 没有 --cache-dir 选项，通过环境变量 NUITKA_CACHE_DIR 指定缓存根目录。
CACHE_DIR = ROOT / ".nuitka_cache"
os.environ.setdefault("NUITKA_CACHE_DIR", str(CACHE_DIR))
PYTHON = sys.executable

# 需要强制包含的包（动态加载/Nuitka 可能检测不到的）
FORCE_INCLUDE_PACKAGES = [
    "curl_cffi",          # 内部用 ctypes 加载 .dll，Nuitka 可能漏
    "Crypto",             # pycryptodome 的 Python 包名
    "playwright",         # 顶层 import 已静态可见，这里保险起见再显式包含
]

# 需要随包分发数据文件的包（playwright 的 driver 二进制必须带上，否则浏览器降级不可用）
PACKAGE_DATA = [
    "playwright",
]

# 需要插件处理的框架
ENABLE_PLUGINS = [
    "pyqt5",              # Qt 二进制、平台插件、sip 模块
]

# ─── 排除列表（减小体积）─────────────────────────────────
# Nuitka 4.x anti-bloat 支持的 --noinclude-*-mode 选项（只有这 8 个）
# 每个选项可选值: error | warning | nofollow | allow
STD_EXCLUDE_MODES = [
    "--noinclude-setuptools-mode=nofollow",
    "--noinclude-pytest-mode=nofollow",
    "--noinclude-unittest-mode=nofollow",
    "--noinclude-pydoc-mode=nofollow",
    "--noinclude-IPython-mode=nofollow",
    "--noinclude-dask-mode=nofollow",
    "--noinclude-numba-mode=nofollow",
]

# 按包名排除（项目未引用、但可能挂在 Python 环境里的包）。
# 注意：Nuitka 4.x 不接受 --noinclude-custom-mode=包名:nofollow（会 FATAL），
# 必须用 --nofollow-import-to=包名。
STD_EXCLUDE_CUSTOM = [
    "customtkinter", "pydantic", "gevent", "matplotlib", "scipy",
    "pandas", "PIL", "cv2", "torch", "tensorflow", "sklearn",
    "transformers", "nltk", "sqlalchemy", "jinja2",
]

# 极限模式额外排除（仅 --tiny 使用）。
# 只排除项目确实未引用的包：排除 pywintypes 会弄坏 Cookie 解密（win32crypt 依赖），
# 排除 asyncio 会弄坏 Playwright 降级（playwright.sync_api 依赖），因此绝不能排除。
TINY_EXCLUDE_CUSTOM = [
    "multiprocessing", "tkinter",
]

# UPX 选项（仅 --standalone-upx 模式，需安装 UPX）
UPX_COMPRESSION_LEVEL = "--best"  # --best / -9 / -8 / ...


# ─── 核心函数 ───────────────────────────────────────────

def run(cmd, label=""):
    """执行命令，实时打印关键输出"""
    prefix = f"[{label}] " if label else ""
    # 截断超长命令
    if len(cmd) > 200:
        display = cmd[:200] + "..."
    else:
        display = cmd
    print(f"  {prefix}{display}")

    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
    except OSError as e:
        print(f"  [FAIL] 无法启动命令: {e}")
        sys.exit(1)

    # 实时输出，方便看进度
    output_lines = []
    for line in proc.stdout or []:
        line = line.rstrip()
        output_lines.append(line)
        # 只打印关键行，减少噪音
        if any(kw in line for kw in (
            "Successfully", "Error", "FATAL", "Working on",
            "Compiling", "Linking", "Onefile payload",
            "UPX", "compressed", "Nuitka",
        )):
            print(f"    {line}")

    proc.wait()

    if proc.returncode != 0:
        print(f"\n  [FAIL] Compilation failed (exit code {proc.returncode})")
        # 打印最后 50 行用于调试
        print("  Last 50 lines of output:")
        for line in output_lines[-50:]:
            print(f"    {line}")
        sys.exit(1)

    return True


def check_upx():
    """检查 UPX 是否可用，返回路径或 None"""
    try:
        result = subprocess.run(
            ["upx", "--version"], capture_output=True,
            text=True, timeout=5
        )
        if result.returncode == 0:
            ver = result.stdout.strip().splitlines()[0]
            print(f"  [OK] Found UPX: {ver}")
            return "upx"
    except Exception:
        pass

    # 常见安装位置
    for path in [
        r"E:\upx-5.2.0-win64\upx.exe",
        r"E:\upx\upx.exe",
        r"C:\upx\upx.exe",
        r"C:\Program Files\upx\upx.exe",
        str(ROOT / "tools" / "upx.exe"),
    ]:
        if os.path.exists(path):
            print(f"  [OK] Found UPX at: {path}")
            return path

    return None


def compress_with_upx(dist_dir, upx_path):
    """用 UPX 压缩 dist 目录下所有 .pyd/.dll 和主 exe 以外的文件"""
    count = 0
    saved_total = 0

    for ext in [".pyd", ".dll"]:
        for f in dist_dir.rglob(f"*{ext}"):
            # 跳过 PyQt5/Qt 的 DLL（UPX 对 Qt DLL 可能不稳定）
            if "Qt5" in f.name or "PyQt5" in f.name:
                continue

            size_before = f.stat().st_size
            try:
                subprocess.run(
                    [upx_path, UPX_COMPRESSION_LEVEL, "-q", str(f)],
                    capture_output=True, text=True, timeout=30,
                    check=False,
                )
                size_after = f.stat().st_size
                saved = size_before - size_after
                if saved > 0:
                    saved_total += saved
                    count += 1
            except Exception:
                pass

    if count > 0:
        print(f"  [UPX] Compressed {count} files, saved {saved_total / 1024:.0f} KB")
    else:
        print(f"  [UPX] No files compressed (all skipped or already compressed)")

    return saved_total


def clean():
    """清理构建产物"""
    print("=== Cleaning build artifacts ===")
    # 只清理构建产物；不碰项目根目录下的 *.pyd/*.dll/*.a（可能是用户自己的文件）
    patterns = [
        "dist", "*.build", "build_c", "build_nuitka",
        ".nuitka_cache", "__pycache__",
    ]
    for pattern in patterns:
        for p in ROOT.glob(pattern):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    for p in ROOT.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    print("  [OK] Cleaned\n")


def _exclude_flags(packages):
    return " ".join(f"--nofollow-import-to={p}" for p in packages)


def build_onefile(mode="std"):
    """
    构建单文件 exe。

    mode:
        "safe"  - 基础 onefile，最小优化
        "std"   - 标准优化（默认）：排除无用包 + LTO
        "tiny"  - 标准 + 额外排除极少用到的标准库包
    """
    print(f"=== Building Single-File EXE (mode: {mode}) ===\n")

    # ── 基础参数（所有模式共用）─────────────────────────
    plugin_flags = " ".join(
        f"--enable-plugin={p}" for p in ENABLE_PLUGINS
    )
    include_flags = " ".join(
        f"--include-package={p}" for p in FORCE_INCLUDE_PACKAGES
    )
    data_flags = " ".join(
        f"--include-package-data={p}" for p in PACKAGE_DATA
    )

    base_flags = (
        f'"{PYTHON}" -m nuitka'
        f' --standalone'
        f' --onefile'
        f' --assume-yes-for-downloads'
        f' --windows-console-mode=disable'
        f' --output-dir="{DIST}"'
        f' {plugin_flags}'
        f' {include_flags}'
        f' {data_flags}'
    )

    # ── 优化参数 ────────────────────────────────────────
    opt_flags = ""

    # LTO：链接时优化，让 C 编译器做全局内联和死代码消除
    # 显著减小体积（通常 -10%~-30%），但编译时间更长
    if mode in ("std", "tiny"):
        opt_flags += " --lto=yes"

    # 排除无用包的 anti-bloat 检测
    if mode in ("std", "tiny"):
        for flag in STD_EXCLUDE_MODES:
            opt_flags += f" {flag}"
        opt_flags += " " + _exclude_flags(STD_EXCLUDE_CUSTOM)
        if mode == "tiny":
            opt_flags += " " + _exclude_flags(TINY_EXCLUDE_CUSTOM)

    # ── 组装完整命令 ────────────────────────────────────
    cmd = f"{base_flags} {opt_flags} \"{ROOT / 'main.py'}\""
    run(cmd, "onefile")

    # ── 定位输出文件 ─────────────────────────────────────
    # Nuitka --onefile 默认输出到 dist/main.exe
    output = DIST / "main.exe"
    if not output.exists():
        # 兼容某些版本的输出路径：取最新生成的 exe，避免误选旧产物
        candidates = sorted(DIST.glob("*.exe"), key=lambda f: f.stat().st_mtime, reverse=True)
        if candidates:
            output = candidates[0]
            print(f"  [INFO] Found output at: {output}")
        else:
            print(f"  [FAIL] Cannot find output .exe in {DIST}")
            sys.exit(1)

    # 重命名
    if output != OUTPUT_EXE:
        if OUTPUT_EXE.exists():
            OUTPUT_EXE.unlink()
        output.rename(OUTPUT_EXE)

    # ── 清理构建残留 ────────────────────────────────────
    for pattern in ["*.build", "main.build", "__pycache__"]:
        for p in ROOT.glob(pattern):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    for d in [DIST / "main.build", DIST / "main.dist", DIST / "main.onefile-build"]:
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    # ── 打印结果 ────────────────────────────────────────
    size_mb = OUTPUT_EXE.stat().st_size / (1024 * 1024)
    print()
    print("=" * 50)
    print("  BUILD COMPLETE (Single File)")
    print("=" * 50)
    print(f"  Output:  {OUTPUT_EXE}")
    print(f"  Size:    {size_mb:.1f} MB")
    print(f"  Mode:    {mode}")
    print()
    print(f"  Run:     {OUTPUT_EXE}")

    # 提示
    onefile_dir = Path(os.environ.get("TEMP", "C:\\Windows\\Temp"))
    print(f"  Runtime: extracts to {onefile_dir}\\onefile_XXXX\\")
    print(f"           (auto-cleaned on exit)")
    print()


def build_standalone_upx():
    """
    极限体积方案：standalone 编译 + UPX 压缩所有 .pyd/.dll
    最后手动打包为自解压 zip，达到类似 onefile 但更小的效果。

    原理:
        1. Nuitka --standalone 生成一个 dist/main.dist/ 目录
        2. UPX 压缩目录内所有 .pyd 和第三方 .dll
        3. 整个目录就是一个免安装的绿色软件
        4. 如需"单文件"，可以用 WinRAR/7z 做成自解压 exe
    """
    print("=== Building Standalone + UPX (Ultra-Small) ===\n")

    # 检查 UPX
    upx_path = check_upx()
    if not upx_path:
        print("  [FAIL] UPX not found!")
        print("  Install: choco install upx   or download from https://upx.github.io/")
        print("  Then put upx.exe on PATH or in tools/upx.exe")
        sys.exit(1)

    # ── standalone 编译 ──────────────────────────────────
    plugin_flags = " ".join(f"--enable-plugin={p}" for p in ENABLE_PLUGINS)
    include_flags = " ".join(f"--include-package={p}" for p in FORCE_INCLUDE_PACKAGES)
    data_flags = " ".join(f"--include-package-data={p}" for p in PACKAGE_DATA)

    exclude_flags = ""
    for flag in STD_EXCLUDE_MODES:
        exclude_flags += f" {flag}"
    exclude_flags += " " + _exclude_flags(STD_EXCLUDE_CUSTOM)

    cmd = (
        f'"{PYTHON}" -m nuitka'
        f' --standalone'
        f' --assume-yes-for-downloads'
        f' --windows-console-mode=disable'
        f' --lto=yes'
        f' --output-dir="{DIST}"'
        f' {plugin_flags}'
        f' {include_flags}'
        f' {data_flags}'
        f' {exclude_flags}'
        f' "{ROOT / "main.py"}"'
    )
    run(cmd, "standalone")

    # ── UPX 压缩 ─────────────────────────────────────────
    dist_dir = DIST / "main.dist"
    if not dist_dir.exists():
        print(f"  [FAIL] {dist_dir} not found")
        sys.exit(1)

    print("\n=== Running UPX compression ===")
    saved = compress_with_upx(dist_dir, upx_path)

    # ── 统计 ────────────────────────────────────────────
    total_size = sum(
        f.stat().st_size for f in dist_dir.rglob("*")
        if f.is_file() and "__pycache__" not in str(f)
    )
    total_mb = total_size / (1024 * 1024)

    print()
    print("=" * 50)
    print("  BUILD COMPLETE (Standalone + UPX)")
    print("=" * 50)
    print(f"  Output:  {dist_dir}")
    print(f"  Total:   {total_mb:.1f} MB")
    if saved > 0:
        print(f"  Saved:   {saved / 1024:.0f} KB (UPX)")
    print()
    print(f"  Run:     {dist_dir}\\main.exe")
    print(f"  Tip:     Use 7z/WinRAR to create self-extracting exe")
    print(f"           7z a -sfx -mmt=on FastDownloader.exe {dist_dir}\\*")
    print()

    # 清理 build 残留
    for p in ROOT.glob("*.build"):
        shutil.rmtree(p, ignore_errors=True)
    for p in DIST.glob("*.build"):
        shutil.rmtree(p, ignore_errors=True)


# ─── 入口 ───────────────────────────────────────────────

def main():
    args = set(sys.argv[1:])

    if "--clean" in args:
        clean()
        return

    if "--standalone-upx" in args:
        clean()
        build_standalone_upx()
        return

    if "--tiny" in args:
        clean()
        build_onefile(mode="tiny")
        return

    if "--safe" in args:
        clean()
        build_onefile(mode="safe")
        return

    # 默认：标准优化模式
    clean()
    build_onefile(mode="std")


if __name__ == "__main__":
    main()
