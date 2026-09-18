# build_env.py - 构建环境自检与准备（build.py / build_onefile.py 共用）
#
# 这里的每一段都对应一次真实踩过的坑，报错本身都看不出原因：
#   * 临时盘写满   -> gcc 报 "could not write to temporary response file"
#   * 内存不足     -> gcc 报 "cannot execute as.exe: CreateProcess: No such file"
#   * 编译器包截断 -> scons 无输出、无错误、CPU 0，一直挂着
#   * Nuitka 的下载器没有断点续传/重试/校验，255MB 的包在慢网下几乎必然中断
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def available_memory_mb():
    """当前可用物理内存（MB）；取不到返回 None"""
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.ullAvailPhys // (1024 * 1024)
    except Exception:
        return None


def memory_aware_jobs(per_job_mb=900, reserve_ratio=0.75):
    """按可用内存决定并行编译数（默认按 CPU 核数会把内存吃光）"""
    cpus = os.cpu_count() or 4
    avail = available_memory_mb()
    if not avail:
        print(f"  [OK] 并行编译数: {cpus}")
        return cpus
    jobs = max(1, min(cpus, max(1, int(avail * reserve_ratio) // per_job_mb)))
    note = "" if jobs == cpus else "（按可用内存下调，避免编译时内存耗尽）"
    print(f"  [OK] 并行编译数: {jobs}{note}　[CPU {cpus} 核 / 可用内存 {avail} MB]")
    return jobs


def ensure_temp_space(required_mb=2000):
    """临时目录空间不足时自动切到其他盘"""
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

    print(f"  [WARN] 临时目录空间不足：{tmp} 仅剩 {free_mb} MB，编译约需 {required_mb} MB")
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
        return new_tmp

    print(f"  [FAIL] 没有空间足够（≥{required_mb} MB）的磁盘，请先清理磁盘再构建")
    return None


def nuitka_cache_dir():
    """Nuitka 的缓存根目录。

    查找顺序：NUITKA_CACHE_DIR（构建脚本会设置）-> %LOCALAPPDATA%（环境变量，
    某些受限 shell 里取不到）-> Windows API 直接问系统。
    """
    env_dir = os.environ.get("NUITKA_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    local = os.environ.get("LOCALAPPDATA")
    if not local and os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_LOCAL_APPDATA = 0x001c
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x001c, None, 0, buf) == 0:
                local = buf.value
        except Exception:
            local = None
    if local:
        return Path(local) / "Nuitka" / "Nuitka" / "Cache"
    return None


def _archive_is_ok(path):
    if not path.exists():
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return zf.testzip() is None
    except (zipfile.BadZipFile, OSError, EOFError):
        return False


def nuitka_mingw_url():
    """从 Nuitka 源码里读出它要用的 MinGW 包地址（跟随 Nuitka 版本）"""
    try:
        import inspect
        import nuitka.utils.Download as dl
        src = inspect.getsource(dl)
        for url in re.findall(r'"(https://github\.com/brechtsanders/winlibs_mingw[^"]+)"', src):
            if "x86_64" in url and url.endswith(".zip"):
                return url
    except Exception:
        pass
    return ""


def check_compiler_cache():
    """删掉缓存里损坏的编译器包"""
    root = nuitka_cache_dir()
    if not root:
        return 0
    cleaned = 0
    downloads = root / "downloads"
    if downloads.is_dir():
        for archive in downloads.rglob("*.zip"):
            if not _archive_is_ok(archive):
                size_mb = archive.stat().st_size / 1048576
                print(f"  [WARN] 编译器缓存损坏（{size_mb:.1f}MB，下载不完整），已删除:")
                print(f"         {archive}")
                try:
                    archive.unlink()
                    cleaned += 1
                except OSError:
                    pass
    return cleaned


def _other_cache_roots():
    """其他可能存有同一份编译器包的缓存位置（复用可省一次 255MB 下载）"""
    roots = []
    local = os.environ.get("LOCALAPPDATA")
    if not local and os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x001c, None, 0, buf) == 0:
                local = buf.value
        except Exception:
            local = None
    if local:
        roots.append(Path(local) / "Nuitka" / "Nuitka" / "Cache")
    if os.environ.get("NUITKA_CACHE_DIR"):
        roots.append(Path(os.environ["NUITKA_CACHE_DIR"]))
    return roots


def reuse_existing_compiler_archive(target, version, filename):
    """如果别的缓存位置已经有完整同版本包，直接复制过来，省掉 255MB 下载"""
    for root in _other_cache_roots():
        candidate = root / "downloads" / "gcc" / "x86_64" / version / filename
        try:
            if candidate.resolve() == target.resolve():
                continue
        except OSError:
            pass
        if _archive_is_ok(candidate):
            print(f"  [OK] 复用已有的编译器包（{candidate.stat().st_size / 1048576:.0f}MB）:")
            print(f"       {candidate}")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, target)
                return True
            except OSError as e:
                print(f"  [WARN] 复制失败（{e}），改为重新下载")
    return False


def ensure_compiler_archive():
    """确保 Nuitka 需要的 MinGW 编译器压缩包已经**完整**下载。

    为什么不让 Nuitka 自己下：它的下载器没有断点续传、没有重试、也不校验
    完整性。winlibs 包有 255MB，在慢速网络下（实测 0.1MB/s，需 40 分钟）
    几乎必然中断，留下半截 zip 之后 scons 会一直挂着——没有输出、没有报错、
    CPU 也是 0，极难排查。这里用 curl 断点续传 + 多次重试 + CRC 校验，
    下载到 Nuitka 期望的位置，它随后直接解压使用。
    """
    root = nuitka_cache_dir()
    if not root:
        return True
    url = nuitka_mingw_url()
    if not url:
        print("  [WARN] 未能从 Nuitka 源码解析出 MinGW 下载地址，交给 Nuitka 自行下载")
        return True

    version = url.rstrip("/").split("/")[-2]
    filename = url.rstrip("/").split("/")[-1]
    target = root / "downloads" / "gcc" / "x86_64" / version / filename

    if _archive_is_ok(target):
        print(f"  [OK] 编译器包已就绪（{target.stat().st_size / 1048576:.0f}MB）")
        return True

    if reuse_existing_compiler_archive(target, version, filename):
        return True

    print("  [INFO] 编译器包缺失或不完整，开始下载（约 255MB，支持断点续传）")
    print(f"         {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    curl = shutil.which("curl") or "curl"
    cmd = [curl, "-L", "--retry", "20", "--retry-delay", "3", "--retry-all-errors",
           "-C", "-", "--connect-timeout", "30", "--progress-bar", "-o", str(target), url]
    try:
        subprocess.run(cmd, check=False)
    except OSError as e:
        print(f"  [WARN] 调用 curl 失败（{e}），交给 Nuitka 自行下载")
        return True

    if _archive_is_ok(target):
        print(f"  [OK] 编译器包下载完成并校验通过（{target.stat().st_size / 1048576:.0f}MB）")
        return True

    print("  [WARN] 下载后校验仍未通过（网络可能仍不稳定）。")
    print("         可以重复执行本构建命令——curl 会接着上次的进度继续下。")
    try:
        target.unlink()
    except OSError:
        pass
    return False


# ---------------------------------------------------------------- 产物占用检查

def running_processes(names):
    """列出正在运行的同名进程（Windows），如 ['FastDownloader.exe (PID 1234)']。

    为什么要在构建前查：程序还开着的时候，它的 exe 会被锁住，替换必然失败。
    """
    if os.name != 'nt':
        return []
    wanted = {str(n).lower() for n in names}
    try:
        result = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=20,
            encoding='utf-8', errors='replace',
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in (result.stdout or '').splitlines():
        fields = [f.strip().strip('"') for f in line.split('","')]
        if len(fields) >= 2 and fields[0].lower() in wanted:
            found.append(f"{fields[0]} (PID {fields[1]})")
    return sorted(set(found))


def locked_reason(path):
    """路径能否被替换；不能则返回一句人话，能则返回 None。

    为什么需要：clean() 用的是 shutil.rmtree(ignore_errors=True) 和
    unlink(missing_ok=True)——删不掉也**不报错**。旧产物于是留在 dist 里，
    等二十分钟编译结束、最后一步 os.replace() 才抛 PermissionError，
    用户看到的只是"跑了半天没有结果"。提前试着重命名一次就能提前发现。
    """
    target = Path(path)
    if not target.exists():
        return None
    probe = target.with_name(target.name + '.fdlock')
    try:
        os.replace(target, probe)
        os.replace(probe, target)
        return None
    except OSError as exc:
        try:
            if probe.exists():
                os.replace(probe, target)
        except OSError:
            pass
        return f"{path} 无法替换（{exc.strerror or exc}）"


def check_output_available(paths, process_names=()):
    """构建开始前的占用自检。返回 True 可以继续；False 说明应先关掉程序。"""
    problems = [msg for msg in (locked_reason(p) for p in paths) if msg]
    running = running_processes(process_names)
    if not problems and not running:
        return True
    print()
    print("  [FAIL] 打包目标正被占用 —— 先关掉它们再重新构建，否则会白跑一趟：")
    for item in running:
        print(f"         - 程序还在运行：{item}")
    for msg in problems:
        print(f"         - {msg}")
    print("         提示：刚关闭程序后杀毒软件可能仍在扫描该文件，等几秒再试。")
    print()
    return False
