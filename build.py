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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_env import (available_memory_mb, check_compiler_cache,  # noqa: E402
                       ensure_compiler_archive, ensure_temp_space,
                       memory_aware_jobs)

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



def check_compiler_cache():
    """构建前扫一遍 Nuitka 的下载缓存，删掉损坏的压缩包。

    事故背景：编译器包（约 255MB）下载中断时会留下一个"看起来有几十 MB、
    实际是半截"的 zip。Nuitka 解压失败后 scons 会**一直挂着**——不报错、
    不退出、CPU 也是 0，比直接失败难查得多（实测卡了 24 分钟没动静）。
    这里提前发现并删除，让 Nuitka 重新下载。
    """
    roots = []
    env_dir = os.environ.get("NUITKA_CACHE_DIR")
    if env_dir:
        roots.append(Path(env_dir))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Nuitka" / "Nuitka" / "Cache")
    cleaned = 0
    for root in roots:
        downloads = root / "downloads"
        if not downloads.is_dir():
            continue
        for archive_path in downloads.rglob("*.zip"):
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    if zf.testzip() is not None:
                        raise zipfile.BadZipFile("CRC check failed")
            except (zipfile.BadZipFile, OSError, EOFError):
                size_mb = archive_path.stat().st_size / 1048576
                print(f"  [WARN] 编译器缓存损坏（{size_mb:.1f}MB，下载不完整），已删除:")
                print(f"         {archive_path}")
                print("         Nuitka 会在构建时重新下载（约 255MB，请保持网络稳定）")
                try:
                    archive_path.unlink()
                    cleaned += 1
                except OSError:
                    pass
    if cleaned:
        print(f"  [OK] 已清理 {cleaned} 个损坏的缓存包\n")
    return cleaned

def ensure_temp_space(required_mb=2000):
    """确保编译有足够的临时空间，不足时自动把 TEMP 切到其他盘。

    事故背景：gcc 链接大量目标文件时要写"响应文件"，临时盘写满时报错是
        gcc.exe: fatal error: could not write to temporary response file ...
    完全看不出是磁盘满导致的（实测 C 盘只剩 6.5MB 时，772 个 C 文件的链接
    就死在这一步）。这里提前检查并切换，避免这种误导性失败。
    """
    import string
    import tempfile

    tmp = tempfile.gettempdir()
    try:
        free_mb = shutil.disk_usage(tmp).free // (1024 * 1024)
    except OSError:
        free_mb = -1
    if free_mb >= required_mb:
        print(f"  [OK] 临时目录 {tmp}（剩余 {free_mb} MB）")
        return None

    print(f"  [WARN] 临时目录空间不足：{tmp} 仅剩 {free_mb} MB，"
          f"编译约需 {required_mb} MB")
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.exists(drive):
            continue
        try:
            free = shutil.disk_usage(drive).free // (1024 * 1024)
        except OSError:
            continue
        if free < required_mb:
            continue
        new_tmp = os.path.join(drive, "fd_build_temp")
        try:
            os.makedirs(new_tmp, exist_ok=True)
            probe = os.path.join(new_tmp, ".write_test")
            with open(probe, "wb") as f:
                f.write(b"ok")
            os.remove(probe)
        except OSError:
            continue
        os.environ["TMP"] = new_tmp
        os.environ["TEMP"] = new_tmp
        print(f"  [OK] 编译临时目录已切换到 {new_tmp}（剩余 {free} MB）")
        print("       （gcc/Nuitka 的中间文件都会落在那里，构建完可手动删除）")
        return new_tmp

    print("  [FAIL] 没有找到空间足够（≥%d MB）的磁盘，请先清理磁盘再构建" % required_mb)
    return None

def metadata_flags():
    """Windows 版本资源（产品名/公司/版本/版权/图标）。

    没有这些元数据的 exe 在杀软眼里就是"来源不明的可执行文件"，
    启发式评分更高；补上元数据是最省事、最有效的一步（比加壳有效得多）。
    """
    sys.path.insert(0, str(ROOT))
    try:
        from version import __version__
        raw = (__version__.split('.') + ['0', '0', '0', '0'])[:4]
        ver4 = '.'.join(raw)
    except Exception:
        ver4 = '0.0.0.0'
    flags = (
        ' --company-name="FastDownloader"'
        ' --product-name="Fast Downloader Pro"'
        ' --file-description="Fast Downloader Pro - Multi-threaded Downloader"'
        f' --file-version={ver4}'
        f' --product-version={ver4}'
        ' --copyright="MIT License"'
    )
    icon = ROOT / 'assets' / 'app.ico'
    if icon.exists():
        flags += f' --windows-icon-from-ico="{icon}"'
    return flags

def build_main():
    """
    编译 main.py 为独立 exe（内嵌全部项目模块与第三方依赖，免安装 Python）。
    Nuitka --standalone 会把所有被 import 的模块一起打包进 main.dist/，
    因此产物是完整可运行的，不需要额外的 dll/ 目录。
    """
    print("=== Building main.exe (standalone, all modules embedded) ===")
    check_compiler_cache()
    if not ensure_compiler_archive():
        print("\n  [FAIL] 编译器包未能下载完整，请重试（会从断点继续）\n")
        sys.exit(1)
    ensure_temp_space()
    DIST_MAIN.mkdir(parents=True, exist_ok=True)

    cmd = (
        f'"{PYTHON}" -m nuitka'
        f' --standalone --assume-yes-for-downloads'
        f' --enable-plugin=pyqt5'
        f' --windows-console-mode=disable'
        f' --output-dir="{DIST}"'
        f'{metadata_flags()}'
        f' --jobs={memory_aware_jobs()}'
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



README_NOTE_NAME = "安装说明（被杀软拦截请看这里）.txt"


def write_readme_note(target_dir):
    """把白名单/误报说明放进发布包：用户解压后第一眼就能看到。

    注意：只是放一份说明文本，程序自身不会去碰杀软设置。
    """
    note = (target_dir / "安装说明（首次运行必读）.txt")
    src = ROOT / "packaging" / "FIRST_RUN.txt"
    if src.exists():
        shutil.copy2(src, note)
        print(f"  [OK] Shipped first-run note -> {note.name}")
    else:
        print("  [WARN] packaging/FIRST_RUN.txt missing, skipped")

def make_zip():
    zip_path = ROOT / ZIP_NAME
    print(f"=== Packaging -> {ZIP_NAME} ===")
    write_readme_note(DIST_MAIN)
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
