import os
import json
import sqlite3
import base64
import glob
import time
import shutil
import tempfile
from urllib.parse import urlparse

from paths import data_file

# 顶层受保护的导入：Nuitka/pyinstaller 打包时能静态扫描到这些依赖，
# 缺包时优雅降级而不是启动崩溃（win32crypt 需 pywin32）
try:
    import win32crypt
except ImportError:
    win32crypt = None
try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

LOG_FILE = data_file('download.log')
CACHE_FILE = data_file('cookie_cache.json')

def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime("%H:%M:%S")}] [cookie] {msg}\n')
    except:
        pass

_CHROMIUM_PATHS = [
    ('Edge', os.path.expandvars(r'%LOCALAPPDATA%\Microsoft\Edge\User Data')),
    ('Chrome', os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data')),
    ('Brave', os.path.expandvars(r'%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data')),
    ('Chrome Canary', os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome SxS\User Data')),
    ('Chromium', os.path.expandvars(r'%LOCALAPPDATA%\Chromium\User Data')),
    ('Vivaldi', os.path.expandvars(r'%LOCALAPPDATA%\Vivaldi\User Data')),
    ('Opera', os.path.expandvars(r'%APPDATA%\Opera Software\Opera Stable')),
]

def _find_chromium():
    for name, base in _CHROMIUM_PATHS:
        if not os.path.isdir(base):
            continue
        local_state = os.path.join(base, 'Local State')
        if not os.path.exists(local_state):
            continue
        profiles = ['Default']
        try:
            profiles += sorted(
                p for p in os.listdir(base) if p.startswith('Profile ')
            )
        except OSError:
            pass
        for profile in profiles:
            cookie_db = os.path.join(base, profile, 'Network', 'Cookies')
            if os.path.exists(cookie_db):
                yield name, cookie_db, local_state

def _get_chromium_master_key(local_state_path):
    try:
        with open(local_state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        encrypted = state['os_crypt']['encrypted_key']
        encrypted = base64.b64decode(encrypted)
        assert encrypted[:5] == b'DPAPI'
        if win32crypt is None:
            log('pywin32 未安装，无法解密 Chromium 主密钥')
            return None
        key = win32crypt.CryptUnprotectData(encrypted[5:], None, None, None, 0)[1]
        return key
    except Exception as e:
        log(f'获取主密钥失败: {e}')
        return None

def _decrypt_chromium_value(encrypted_value, master_key):
    if not encrypted_value:
        return None
    try:
        if encrypted_value[:3] in (b'v10', b'v11'):
            if AES is None:
                return None
            nonce = encrypted_value[3:15]
            tag = encrypted_value[-16:]
            ciphertext = encrypted_value[15:-16]
            cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ciphertext, tag).decode('utf-8')
    except Exception:
        pass
    try:
        import win32crypt
        data = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1]
        return data.decode('utf-8')
    except:
        return None

def _copy_cookie_db(cookie_db):
    """把 Cookies 数据库复制到临时目录再读取。

    浏览器运行时会对数据库持有锁，直接复制 WAL/SHM 经常失败（WinError 33），
    这里改用 SQLite 的 backup API：对运行中的 WAL 库做一致性快照，
    不需要触碰 -wal/-shm 文件，浏览器开着也能读到最新数据。
    """
    tmp = tempfile.mkdtemp(prefix='fd_cookies_')
    dest = os.path.join(tmp, os.path.basename(cookie_db))
    src = None
    try:
        src = sqlite3.connect(f'file:{cookie_db}?mode=ro', uri=True, timeout=3)
        dst = sqlite3.connect(dest)
        src.backup(dst)
        dst.close()
        return dest
    except Exception:
        # 兜底：直接复制主库（可能缺最近 WAL 帧，但总比没有强）
        try:
            shutil.copy2(cookie_db, dest)
            return dest
        except OSError:
            pass
        return None
    finally:
        if src is not None:
            try:
                src.close()
            except Exception:
                pass


def _get_chromium_cookies(domain, cookie_db, local_state):
    master_key = _get_chromium_master_key(local_state)
    if not master_key:
        return {}
    db_copy = None
    conn = None
    try:
        db_copy = _copy_cookie_db(cookie_db)
        if not db_copy:
            log(f'复制 cookie 数据库失败: {cookie_db}')
            return {}
        conn = sqlite3.connect(f'file:{db_copy}?mode=ro', uri=True, timeout=3)
        conn.execute('PRAGMA query_only=ON')
        conn.text_factory = bytes
        cur = conn.cursor()
        # 只匹配精确域名或 .前缀 子域名（避免 badexample.com 这类误匹配）
        pattern = '%.' + domain.replace('%', '\\%').replace('_', '\\_')
        cur.execute(
            "SELECT host_key, name, path, encrypted_value "
            "FROM cookies WHERE host_key = ? OR host_key LIKE ? ESCAPE '\\'",
            (domain, pattern)
        )
        cookies = {}
        for row in cur.fetchall():
            host = row[0].decode('utf-8') if isinstance(row[0], bytes) else row[0]
            name = row[1].decode('utf-8') if isinstance(row[1], bytes) else row[1]
            path = row[2].decode('utf-8') if isinstance(row[2], bytes) else row[2]
            val = _decrypt_chromium_value(row[3], master_key)
            if val:
                # 以 (name, domain, path) 为键，避免同名 cookie 互相覆盖
                cookies[(name, host, path)] = dict(value=val, domain=host, path=path)
        log(f'找到 {len(cookies)} 个 cookie (domain: {domain})')
        return cookies
    except sqlite3.OperationalError as e:
        if 'locked' in str(e).lower():
            log('数据库被锁定，请关闭浏览器后再试')
        else:
            log(f'读取失败: {e}')
        return None
    except Exception as e:
        log(f'读取失败: {e}')
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if db_copy:
            shutil.rmtree(os.path.dirname(db_copy), ignore_errors=True)

def _get_firefox_cookies(domain):
    profiles_dir = os.path.expandvars(r'%APPDATA%\Mozilla\Firefox\Profiles')
    cookies = {}
    if not os.path.isdir(profiles_dir):
        return cookies
    for profile_dir in os.listdir(profiles_dir):
        pdir = os.path.join(profiles_dir, profile_dir)
        db = os.path.join(pdir, 'cookies.sqlite')
        if not os.path.isdir(pdir) or not os.path.exists(db):
            continue
        try:
            conn = sqlite3.connect(db, timeout=3)
            cur = conn.cursor()
            cur.execute(
                "SELECT host, name, path, value FROM moz_cookies "
                "WHERE host = ? OR host LIKE ?",
                (domain, '%.' + domain)
            )
            for row in cur.fetchall():
                host, name, path, value = row
                cookies[(name, host, path)] = dict(value=value, domain=host, path=path)
            conn.close()
            log(f'Firefox: 找到 {len(cookies)} 个 cookie')
        except Exception as e:
            log(f'Firefox 读取失败: {e}')
    return cookies

def _load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {}

def _save_cache(cookies):
    try:
        cache = _load_cache()
        now = time.time()
        for k, v in cookies.items():
            cache[k] = v
            cache[k]['_cached_at'] = now
        # Remove expired entries (>1 hour)
        expired = [k for k, v in cache.items() if now - v.get('_cached_at', 0) > 3600]
        for k in expired:
            del cache[k]
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
    except:
        pass

def get_cookies_for_url(url):
    domain = urlparse(url).hostname
    if not domain:
        return {}
    log(f'获取 cookie: {url}')

    all_cookies = {}
    browsers_found = False
    db_locked = False

    for browser_name, db, state in _find_chromium():
        browsers_found = True
        log(f'尝试 {browser_name}')
        c = _get_chromium_cookies(domain, db, state)
        if c is None:
            db_locked = True
            log(f'{browser_name}: 数据库被锁定')
        elif c:
            for k, v in c.items():
                all_cookies.setdefault(k, v)

    ff = _get_firefox_cookies(domain)
    if ff:
        browsers_found = True
        for k, v in ff.items():
            all_cookies.setdefault(k, v)

    if not all_cookies and db_locked:
        cache = _load_cache()
        for k, v in cache.items():
            dom = v.get('domain', '')
            if dom == domain or dom.endswith('.' + domain):
                all_cookies[k] = v
        if all_cookies:
            log(f'从缓存加载了 {len(all_cookies)} 个 cookie')

    if all_cookies:
        _save_cache(all_cookies)

    log(f'共 {len(all_cookies)} 个 cookie')
    return all_cookies

def apply_cookies_to_session(session, url):
    cookies = get_cookies_for_url(url)
    for (name, _dom, _path), info in cookies.items():
        session.cookies.set(name, info['value'], domain=info.get('domain', ''), path=info.get('path', '/'))
    return len(cookies)
