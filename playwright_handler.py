import os
import time
import json

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, 'download.log')

def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime("%H:%M:%S")}] [pw] {msg}\n')
    except:
        pass

_available = None          # 探测结果: channel 或 False
_available_at = 0.0        # 探测时间（TTL 缓存，避免装好浏览器后永久 False）

# 顶层受保护的导入：让 Nuitka/pyinstaller 打包时能扫描到 playwright 依赖
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# 默认无头模式；部分反爬站点会检测无头浏览器并拒绝，
# 设环境变量 FD_PW_HEADLESS=0 可强制弹出真实浏览器窗口。
HEADLESS = os.environ.get('FD_PW_HEADLESS', '1') != '0'


def _launch_browser(p, channel, timeout=15000):
    """优先无头启动；失败时退回有头模式。返回 browser 或 None。"""
    attempts = (True, False) if HEADLESS else (False,)
    for headless in attempts:
        try:
            return p.chromium.launch(channel=channel, headless=headless, timeout=timeout)
        except Exception as e:
            log(f'launch headless={headless} 失败: {e}')
    return None


def is_available():
    """探测可用浏览器 channel；结果缓存 60 秒（TTL），
    避免浏览器刚装好/机器繁忙时探测失败导致永久不可用。"""
    global _available, _available_at
    now = time.time()
    if _available is not None and now - _available_at < 60:
        return _available
    if sync_playwright is None:
        _available = False
        _available_at = now
        log('playwright 库未安装')
        return _available
    try:
        with sync_playwright() as p:
            for ch in ('msedge', 'chrome', None):
                try:
                    kw = {'channel': ch} if ch else {}
                    browser = p.chromium.launch(headless=True, timeout=10000, **kw)
                    browser.close()
                    _available = ch or 'chromium'
                    _available_at = now
                    log(f'Playwright 可用 (channel={_available})')
                    return _available
                except:
                    continue
        _available = False
        _available_at = now
    except Exception as e:
        _available = False
        _available_at = now
        log(f'Playwright 检测异常: {e}')
    return _available

def _get_best_channel(p):
    for ch in ('msedge', 'chrome', None):
        try:
            kw = {'channel': ch} if ch else {}
            browser = p.chromium.launch(headless=True, timeout=5000, **kw)
            browser.close()
            return ch
        except:
            continue
    return None

def resolve_cookies(url, timeout=30):
    if sync_playwright is None:
        log('playwright 未安装')
        return None

    log(f'启动 Playwright 浏览器: {url}')
    browser = None
    try:
        with sync_playwright() as p:
            channel = _get_best_channel(p)
            log(f'使用浏览器 channel={channel}')

            browser = _launch_browser(p, channel)
            if not browser:
                log('浏览器启动失败')
                return None
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, timeout=timeout * 1000)

            # Handle JS challenge: wait for page to reload/redirect
            for _ in range(6):
                title = page.title()
                cur = page.url
                log(f'  检查: title="{title}" url={cur}')
                if '403' not in title and 'Verifying' not in title and 'verify' not in title.lower():
                    break
                time.sleep(2)

            cookies = context.cookies()
            log(f'获取到 {len(cookies)} 个 cookie')

            return {
                'cookies': cookies,
                'final_url': page.url,
            }
    except Exception as e:
        log(f'Playwright 异常: {e}')
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

def apply_playwright_cookies(session, pw_result):
    for c in pw_result.get('cookies', []):
        name = c.get('name')
        value = c.get('value')
        domain = c.get('domain', '')
        path = c.get('path', '/')
        if name and value:
            session.cookies.set(name, value, domain=domain, path=path)
    log(f'已注入 {len(pw_result.get("cookies", []))} 个 cookie')
    return len(pw_result.get('cookies', []))

def download_via_playwright(url, save_path, timeout=120):
    if sync_playwright is None:
        return False

    log(f'Playwright 直接下载: {url} -> {save_path}')
    browser = None
    try:
        with sync_playwright() as p:
            channel = _get_best_channel(p)
            browser = _launch_browser(p, channel)
            if not browser:
                log('浏览器启动失败')
                return False
            page = browser.new_page()

            download = None
            def on_download(dl):
                nonlocal download
                download = dl
            page.on('download', on_download)

            result = page.goto(url, timeout=timeout * 1000)

            # Wait for challenge to resolve + download to start
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(2)
                if download:
                    log('下载事件已触发')
                    break

            if download:
                download.save_as(save_path)
                log(f'Playwright 下载保存: {save_path}')
                return os.path.exists(save_path) and os.path.getsize(save_path) > 0

            # No download event - try URL again (post-challenge)
            log('尝试二次请求...')
            result = page.goto(url, timeout=timeout * 1000)
            if result:
                headers = result.headers
                ct = headers.get('content-type', '')
                if 'text/html' not in ct:
                    log(f'二次请求成功: {ct}')
                    resp = page.request.fetch(url)
                    with open(save_path, 'wb') as f:
                        f.write(resp.body())
                    return os.path.exists(save_path) and os.path.getsize(save_path) > 0

            return False
    except Exception as e:
        log(f'Playwright 下载失败: {e}')
        return False
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
