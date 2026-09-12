# updater.py - 更新检查与自动更新
#
# 流程：查询 GitHub Releases -> 按当前打包形态挑选资产 -> 下载（校验 sha256）
#       -> 目录版解压 -> 生成后台替换脚本 -> 退出并重启
#
# 为什么需要后台脚本：Windows 上正在运行的 exe 无法被覆盖（文件被占用），
# 所以替换动作必须等本进程退出后由独立进程完成。
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from paths import data_file
from version import __version__, RELEASES_API, RELEASES_PAGE

HERE = os.path.dirname(os.path.abspath(__file__))

# 资产名 -> 安装形态（与 build_onefile.py / build.py 的产物对应）
ASSET_FOR_KIND = {
    'onefile': 'FastDownloader.exe',   # 单文件版：直接替换自身
    'standalone': 'fastdownloader.zip',  # 目录版：解压后覆盖安装目录
}


# ---------------------------------------------------------------- 版本比较

def parse_version(text):
    """'v1.2.3' / '1.2.3-beta' -> (1, 2, 3)；无法解析时返回 ()"""
    if not text:
        return ()
    text = str(text).strip().lstrip('vV')
    # 只取数字和点，忽略 -beta / +build 之类的后缀
    cleaned = []
    for ch in text:
        if ch.isdigit() or ch == '.':
            cleaned.append(ch)
        else:
            break
    parts = [p for p in ''.join(cleaned).split('.') if p != '']
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return ()


def is_newer(remote, local):
    """远程版本是否比本地新（按数字段比较，缺失段按 0 处理）"""
    r, l = parse_version(remote), parse_version(local)
    if not r:
        return False
    if not l:
        return True
    length = max(len(r), len(l))
    r = r + (0,) * (length - len(r))
    l = l + (0,) * (length - len(l))
    return r > l


# ---------------------------------------------------------------- 运行形态

def compiled_info():
    """Nuitka 编译产物会注入 __compiled__；源码运行时为 None"""
    return globals().get('__compiled__')


def detect_install_kind():
    """返回 'onefile' / 'standalone' / 'source'"""
    info = compiled_info()
    if info is not None:
        # Nuitka 直接告诉我们是不是 onefile / standalone
        try:
            if getattr(info, 'onefile', False):
                return 'onefile'
            if getattr(info, 'standalone', False):
                return 'standalone'
        except Exception:
            pass
    if os.environ.get('NUITKA_ONEFILE_BINARY') or os.environ.get('NUITKA_ONEFILE_PARENT'):
        return 'onefile'
    if getattr(sys, 'frozen', False):
        # 其他打包器：没有解压目录特征就当作目录版
        return 'standalone'
    return 'source'


def install_root(kind=None):
    """返回需要被替换的位置：onefile 指 exe 文件，standalone 指目录"""
    kind = kind or detect_install_kind()
    exe = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else sys.argv[0])
    if kind == 'onefile':
        return exe
    return os.path.dirname(exe)


def can_self_update(kind=None):
    """源码运行不支持自我替换（会改坏开发环境）"""
    kind = kind or detect_install_kind()
    if kind == 'source':
        return False, '源码运行模式不支持自动更新，请用 git pull 更新'
    if not os.access(install_root(kind), os.W_OK):
        return False, '程序目录不可写，请用管理员身份运行后再试'
    return True, ''


# ---------------------------------------------------------------- 查询更新

def _looks_like_cert_error(exc):
    text = str(exc).lower()
    return 'certificate' in text or 'ssl' in text or 'curl: (60)' in text or 'curl: (77)' in text


def _request(url, *, timeout=15, stream=False, headers=None, verify=True):
    """带证书回退的请求：企业代理/系统缺少 CA 链时降级为不校验，
    保证"检查更新"不会因为环境问题完全不可用（下载内容仍用 sha256 校验）。"""
    from curl_cffi import requests as curl_requests
    kw = dict(impersonate='chrome120', timeout=timeout, stream=stream,
              allow_redirects=True, verify=verify)
    if headers:
        kw['headers'] = headers
    try:
        resp = curl_requests.get(url, **kw)
        resp.raise_for_status()
        return resp
    except Exception as e:
        if verify and _looks_like_cert_error(e):
            from engine import log
            log(f'更新检查: 证书校验失败（{e}），改用不校验重试')
            kw['verify'] = False
            resp = curl_requests.get(url, **kw)
            resp.raise_for_status()
            return resp
        raise


def _http_get(url, timeout=15):
    return _request(url, timeout=timeout,
                    headers={'Accept': 'application/vnd.github+json'})


def fetch_latest(api_url=None, timeout=15):
    """查询最新 Release。返回 dict；失败抛异常（由调用方决定是否提示）

    api_url 可覆盖默认的 GitHub API 地址（便于镜像/企业环境与测试）。
    """
    resp = _http_get(api_url or RELEASES_API, timeout=timeout)
    data = resp.json()
    if not isinstance(data, dict) or 'tag_name' not in data:
        raise ValueError('GitHub 返回的数据格式不符合预期')
    assets = {}
    for asset in data.get('assets') or []:
        name = asset.get('name')
        if not name:
            continue
        assets[name] = {
            'url': asset.get('browser_download_url'),
            'size': int(asset.get('size') or 0),
            'digest': asset.get('digest') or '',   # 形如 'sha256:...'（新版 API 提供）
        }
    return {
        'tag': data.get('tag_name', ''),
        'version': parse_version(data.get('tag_name', '')),
        'name': data.get('name') or data.get('tag_name', ''),
        'notes': data.get('body') or '',
        'html_url': data.get('html_url') or RELEASES_PAGE,
        'published_at': data.get('published_at') or '',
        'assets': assets,
        'prerelease': bool(data.get('prerelease')),
    }


def check_for_update(current=None, api_url=None, timeout=15):
    """返回 (release, has_update)；current 省略时用本程序版本"""
    current = current or __version__
    release = fetch_latest(api_url=api_url, timeout=timeout)
    return release, is_newer(release.get('tag'), current)


def pick_asset(release, kind=None):
    """按安装形态挑选资产，返回 (name, info)；没有匹配返回 (None, None)"""
    kind = kind or detect_install_kind()
    if kind == 'source':
        return None, None
    wanted = ASSET_FOR_KIND.get(kind)
    assets = release.get('assets') or {}
    if wanted and wanted in assets:
        return wanted, assets[wanted]
    # 兜底：按后缀挑（例如资产改名了）
    suffix = '.exe' if kind == 'onefile' else '.zip'
    for name, info in assets.items():
        if name.lower().endswith(suffix):
            return name, info
    return None, None


# ---------------------------------------------------------------- 下载与解压

def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def download_asset(url, dest, expected_digest='', progress=None, timeout=30):
    """下载资产到 dest；带 sha256 校验（GitHub 提供 digest 时）"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + '.part'
    got = 0
    resp = _request(url, timeout=(15, timeout), stream=True)
    try:
        total = int(resp.headers.get('Content-Length') or 0)
        with open(tmp, 'wb') as f:
            for chunk in resp.iter_content():
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    finally:
        # curl_cffi 的 Response 不是上下文管理器，手动关闭
        try:
            resp.close()
        except Exception:
            pass
    os.replace(tmp, dest)
    if expected_digest:
        algo, _, value = expected_digest.partition(':')
        if algo.lower() == 'sha256' and value:
            actual = sha256_file(dest)
            if actual.lower() != value.lower():
                os.remove(dest)
                raise ValueError('下载文件校验失败（sha256 不匹配），已丢弃')
    return dest


def extract_zip(zip_path, dest_dir):
    """解压到 dest_dir（目录版更新用）"""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        # 防目录穿越
        base = os.path.abspath(dest_dir)
        for member in zf.namelist():
            target = os.path.abspath(os.path.join(base, member))
            if not target.startswith(base + os.sep) and target != base:
                raise ValueError(f'压缩包内含非法路径: {member}')
        zf.extractall(base)
    return dest_dir


def prepare_update(release, kind=None, work_dir=None, progress=None):
    """下载（并在需要时解压）新版本，返回可交给替换脚本的 payload 目录"""
    kind = kind or detect_install_kind()
    name, info = pick_asset(release, kind)
    if not name:
        raise RuntimeError('该 Release 没有适用于当前安装方式的文件')
    work_dir = work_dir or os.path.join(tempfile.gettempdir(), 'fd_update')
    os.makedirs(work_dir, exist_ok=True)
    payload = os.path.join(work_dir, 'payload')
    if os.path.isdir(payload):
        shutil.rmtree(payload, ignore_errors=True)
    os.makedirs(payload, exist_ok=True)

    if kind == 'onefile':
        target = os.path.join(payload, name)
        download_asset(info['url'], target, info.get('digest', ''), progress=progress)
    else:
        zip_path = os.path.join(work_dir, name)
        download_asset(info['url'], zip_path, info.get('digest', ''), progress=progress)
        extract_zip(zip_path, payload)
        os.remove(zip_path)
    return {'kind': kind, 'payload': payload, 'asset': name, 'work_dir': work_dir}


# ---------------------------------------------------------------- 应用更新

def build_update_script(payload, kind=None, target=None, restart=True, log_path=None,
                        timeout_seconds=180):
    """生成后台替换脚本（.bat）。

    脚本流程：等旧进程退出 -> 复制新文件 -> 校验大小 -> 清理 -> 重启。
    正在运行的 exe 在 Windows 上被锁定，所以替换必须等进程结束、由独立进程完成。

    三条硬性约束，每一条都来自实际事故：
      1. 绝不通过写入目标 exe 来试探占用 —— 那会真往程序文件里追加字节；
         改用 tasklist 判断进程是否存在。
      2. 超时后必须放弃，不能强行复制 —— 覆盖正在运行的程序只会留下残缺文件
         （表现为"只复制了几 MB、双击打不开"）。
      3. 复制后校验大小，失败要保留现场且不重启 —— 否则脚本一边报错一边把旧
         版本拉起来，用户看到的是"更新点了没反应、还反复提示更新"。
    """
    kind = kind or detect_install_kind()
    target = target or install_root(kind)
    # Keep the log OUTSIDE the work dir: the script deletes that dir at the end,
    # so a log living inside it would be gone exactly when an update fails.
    log_path = log_path or os.path.join(tempfile.gettempdir(), 'fd_update.log')
    restart_exe = target if kind == 'onefile' else os.path.join(target, 'main.exe')
    work_dir = os.path.dirname(payload)
    exe_name = os.path.basename(restart_exe)

    if kind == 'onefile':
        files = [f for f in os.listdir(payload)
                 if os.path.isfile(os.path.join(payload, f))]
        if not files:
            raise RuntimeError('更新包为空，无法生成替换脚本')
        src_file = os.path.join(payload, files[0])
    else:
        src_file = os.path.join(payload, 'main.exe')

    lines = [
        '@echo off',
        'setlocal enabledelayedexpansion',
        'set "PAYLOAD=' + payload + '"',
        'set "SRC=' + src_file + '"',
        'set "TARGET=' + target + '"',
        'set "WORKDIR=' + work_dir + '"',
        'set "LOG=' + log_path + '"',
        'set "RESTART=' + restart_exe + '"',
        'set "EXENAME=' + exe_name + '"',
        'echo [%date% %time%] update start > "%LOG%"',
        '',
        'rem ---- 1) 等旧进程退出（只查进程，不碰目标文件） ----',
        'set /a WAITED=0',
        ':waitloop',
        'tasklist /fi "IMAGENAME eq %EXENAME%" 2>nul | find /i "%EXENAME%" >nul',
        'if errorlevel 1 goto install',
        'set /a WAITED+=1',
        'if !WAITED! GEQ ' + str(timeout_seconds) + ' goto giveup',
        'ping -n 2 127.0.0.1 >nul',
        'goto waitloop',
        '',
        ':giveup',
        'echo [%date% %time%] 旧进程超时未退出，放弃更新（目标文件未改动） >> "%LOG%"',
        'exit /b 1',
        '',
        ':install',
        'echo [%date% %time%] copying >> "%LOG%"',
    ]

    if kind == 'onefile':
        lines += [
            'rem 源文件大小（后面两处校验都要用）',
            'for %%A in ("%SRC%") do set "SIZE_SRC=%%~zA"',
            'rem 先复制到同目录的临时文件并校验，再用 move /y 原子替换。',
            'rem 直接 copy 到被占用的目标会先把它截断，失败后只剩半个文件 ——',
            'rem 那正是"更新后只能复制几 MB、程序打不开"的原因。',
            'copy /y "%SRC%" "%TARGET%.new" >> "%LOG%" 2>&1',
            'if errorlevel 1 goto failed',
            'for %%A in ("%TARGET%.new") do set "SIZE_NEW=%%~zA"',
            'if not "%SIZE_SRC%"=="%SIZE_NEW%" (',
            '    echo [%date% %time%] 临时文件大小不一致，放弃更新 >> "%LOG%"',
            '    del "%TARGET%.new" >nul 2>&1',
            '    goto failed',
            ')',
            'move /y "%TARGET%.new" "%TARGET%" >> "%LOG%" 2>&1',
            'if errorlevel 1 (',
            '    echo [%date% %time%] 替换失败（目标仍被占用），原文件未改动 >> "%LOG%"',
            '    del "%TARGET%.new" >nul 2>&1',
            '    goto failed',
            ')',
        ]
    else:
        lines += [
            'rem 目录版：整体覆盖安装目录（/e 含子目录、/i 按目录处理、/y 不询问）',
            r'xcopy /y /e /i /q "%PAYLOAD%\*" "%TARGET%\" >> "%LOG%" 2>&1',
            'if errorlevel 1 goto failed',
        ]

    lines += [
        '',
        'rem ---- 2) 校验大小：不一致说明没复制完整 ----',
        'for %%A in ("%SRC%") do set "SIZE_SRC=%%~zA"',
        'for %%A in ("%TARGET%") do set "SIZE_DST=%%~zA"',
        'if not "%SIZE_SRC%"=="%SIZE_DST%" (',
        '    echo [%date% %time%] 大小不一致 %SIZE_SRC% != %SIZE_DST%，更新失败 >> "%LOG%"',
        '    goto failed',
        ')',
        '',
        'echo [%date% %time%] ok >> "%LOG%"',
    ]

    if restart:
        lines += [
            'echo [%date% %time%] restart >> "%LOG%"',
            'start "" "%RESTART%"',
        ]
    else:
        lines.append('echo [%date% %time%] no restart >> "%LOG%"')

    # 清理放在重启之后：脚本不在 WORKDIR 内，清理失败也不影响重启
    lines += [
        'echo %TARGET% | find /i "%WORKDIR%" >nul',
        'if errorlevel 1 rmdir /s /q "%WORKDIR%" >nul 2>&1',
        'exit /b 0',
        '',
        ':failed',
        'rem 失败时保留临时文件、并且不重启：否则旧版本又跑起来、又提示更新，',
        'rem 用户就会陷入"点了没反应、还一直弹窗"的循环。',
        'echo [%date% %time%] 更新失败，临时文件保留在 %WORKDIR% >> "%LOG%"',
        'exit /b 1',
        '',
    ]

    # Script must not live in WORKDIR: it deletes that directory at the end,
    # and a batch file deleting itself leaves cmd unable to read the rest of
    # its lines - the restart never happened.
    script_path = os.path.join(tempfile.gettempdir(), 'fd_apply_update.bat')
    encoding = 'mbcs' if os.name == 'nt' else 'utf-8'
    with open(script_path, 'w', encoding=encoding, errors='replace', newline='\r\n') as f:
        f.write('\n'.join(lines))
    return script_path


def launch_update_script(script_path):
    """以隐藏窗口启动替换脚本（调用方随后应立即退出程序）"""
    if os.name != 'nt':
        raise RuntimeError('自动更新目前仅支持 Windows')
    flags = 0
    for flag_name in ('CREATE_NO_WINDOW', 'DETACHED_PROCESS'):
        flags |= getattr(subprocess, flag_name, 0)
    subprocess.Popen(['cmd', '/c', script_path], close_fds=True,
                     creationflags=flags, cwd=os.path.dirname(script_path))
    return True


UPDATE_ATTEMPT_FILE = data_file('update_attempt.json')


def record_update_attempt(tag):
    """记录"正在尝试更新到 <tag>"。

    替换是交给外部脚本做的：如果它失败了，程序重启后仍然是旧版本——不记一笔的话
    启动检查又会发现"有新版本"，用户就会陷入反复弹窗、反复失败的循环。
    """
    try:
        with open(UPDATE_ATTEMPT_FILE, 'w', encoding='utf-8') as f:
            json.dump({'tag': str(tag), 'at': time.time()}, f)
    except OSError:
        pass


def clear_update_attempt():
    try:
        os.remove(UPDATE_ATTEMPT_FILE)
    except OSError:
        pass


def failed_attempt_tag():
    """上次尝试更新到的版本；若当前版本已经达到它，说明那次更新成功了（顺手清理）。"""
    try:
        with open(UPDATE_ATTEMPT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ''
    tag = str(data.get('tag') or '')
    if not tag:
        return ''
    if is_newer(tag, __version__):
        return tag          # 比当前版本新 -> 说明没换成功
    clear_update_attempt()  # 已经升上去了 -> 记录作废
    return ''


def self_update(release, kind=None, progress=None, restart=True):
    """完整流程：准备 -> 生成脚本 -> 启动脚本。调用方随后应退出程序。"""
    ok, reason = can_self_update(kind)
    if not ok:
        raise RuntimeError(reason)
    prepared = prepare_update(release, kind=kind, progress=progress)
    script = build_update_script(prepared['payload'], kind=prepared['kind'],
                                 restart=restart)
    record_update_attempt(release.get('tag', ''))
    launch_update_script(script)
    return prepared
