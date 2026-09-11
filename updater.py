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

def build_update_script(payload, kind=None, target=None, restart=True, log_path=None):
    """生成后台替换脚本（.bat）。

    脚本做四件事：等本程序退出 -> 复制新文件 -> 清理临时目录 -> 重启新版本。
    之所以不在 Python 里直接替换：正在运行的 exe 在 Windows 上被锁定，
    必须等进程结束后由别的进程来做。
    """
    kind = kind or detect_install_kind()
    target = target or install_root(kind)
    log_path = log_path or os.path.join(os.path.dirname(payload), 'update.log')
    restart_exe = target if kind == 'onefile' else os.path.join(target, 'main.exe')
    work_dir = os.path.dirname(payload)

    lines = [
        '@echo off',
        'setlocal',
        f'set "PAYLOAD={payload}"',
        f'set "TARGET={target}"',
        f'set "WORKDIR={work_dir}"',
        f'set "LOG={log_path}"',
        f'set "RESTART={restart_exe}"',
        'echo [%date% %time%] update start > "%LOG%"',
        '',
        'rem --- 等待本程序退出（尝试写入目标文件，失败说明仍被占用） ---',
        ':wait',
        'set WAITED=0',
        ':waitloop',
        '2>nul (>>"%RESTART%" echo.) && goto copy',
        'set /a WAITED+=1',
        'if %WAITED% GTR 60 goto copy',
        'timeout /t 1 /nobreak >nul',
        'goto waitloop',
        '',
        ':copy',
        'echo [%date% %time%] copying >> "%LOG%"',
    ]
    if kind == 'onefile':
        lines += [
            'copy /y "%PAYLOAD%\\*" "%TARGET%" >> "%LOG%" 2>&1',
        ]
    else:
        lines += [
            'rem 目录版：整体覆盖安装目录（/e 含子目录，/y 不询问）',
            'xcopy /y /e /i /q "%PAYLOAD%\\*" "%TARGET%" >> "%LOG%" 2>&1',
        ]
    lines += [
        '',
        'echo [%date% %time%] cleanup >> "%LOG%"',
        'rmdir /s /q "%WORKDIR%" >nul 2>&1',
        '',
    ]
    if restart:
        lines += [
            'echo [%date% %time%] restart >> "%LOG%"',
            'start "" "%RESTART%"',
        ]
    else:
        lines.append('echo [%date% %time%] no restart >> "%LOG%"')
    lines += [
        'endlocal',
        'exit /b 0',
        '',
    ]
    script_path = os.path.join(work_dir, 'apply_update.bat')
    # 批处理用系统 ANSI 编码（中文系统为 GBK），避免 cmd 解析出错
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


def self_update(release, kind=None, progress=None, restart=True):
    """完整流程：准备 -> 生成脚本 -> 启动脚本。调用方随后应退出程序。"""
    ok, reason = can_self_update(kind)
    if not ok:
        raise RuntimeError(reason)
    prepared = prepare_update(release, kind=kind, progress=progress)
    script = build_update_script(prepared['payload'], kind=prepared['kind'],
                                 restart=restart)
    launch_update_script(script)
    return prepared
