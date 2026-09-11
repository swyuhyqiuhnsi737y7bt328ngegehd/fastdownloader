from curl_cffi import requests as curl_requests
import threading
import queue
import time
import os
import json
import re
import errno
import random
import shutil
import traceback
import urllib.parse

from utils import format_size
from collections import deque
from browser_cookies import apply_cookies_to_session
from playwright_handler import is_available as pw_available, resolve_cookies, apply_playwright_cookies, download_via_playwright as pw_download

from paths import data_file

LOG_FILE = data_file('download.log')
def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')
    except:
        pass

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

class RangeNotHonored(Exception):
    """服务器未按请求返回 Range 区间：重试也不会成功，直接向上报告"""


class DiskFullError(Exception):
    """写入时磁盘写满：重试无意义，直接报告并保留 .part 以便清理后续传"""


def _unique_path(path, limit=9999):
    """返回不冲突的路径：原路径可用就返回原路径，否则 name (1).ext / name (2).ext …"""
    if not os.path.exists(path) and not os.path.exists(path + '.part'):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, limit):
        candidate = f'{base} ({i}){ext}'
        if not os.path.exists(candidate) and not os.path.exists(candidate + '.part'):
            return candidate
    return f'{base} ({int(time.time())}){ext}'


class TokenBucket:
    """令牌桶限速器（任务内所有线程共享）。

    相比按块 sleep，令牌桶允许小突发但长期速率精确，且支持运行中改速率。
    rate <= 0 表示不限速。"""

    def __init__(self, rate_bps=0, burst=None):
        self.rate = float(rate_bps or 0)
        self.capacity = float(burst if burst else max(self.rate, 65536))
        self._tokens = self.capacity
        self._last = time.time()
        self._lock = threading.Lock()

    def set_rate(self, rate_bps):
        with self._lock:
            self.rate = float(rate_bps or 0)
            self.capacity = max(self.rate, 65536)
            self._tokens = min(self._tokens, self.capacity)

    def consume(self, amount, stop_check=None):
        """取走 amount 个令牌，不足则等待；stop_check() 为假时提前返回 False"""
        if self.rate <= 0:
            return True
        while True:
            with self._lock:
                now = time.time()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= amount:
                    self._tokens -= amount
                    return True
                need = (amount - self._tokens) / self.rate
            if stop_check is not None and not stop_check():
                return False
            time.sleep(min(need, 0.2))


def _is_loopback(url):
    """判断是否指向本机（127.0.0.0/8 / localhost / ::1）"""
    try:
        host = urllib.parse.urlsplit(url).hostname or ''
    except ValueError:
        return False
    host = host.lower().strip('[]')
    if host in ('localhost', '::1', '0.0.0.0'):
        return True
    return host.startswith('127.')


def _req_headers(url, extra=None, custom=None):
    origin = '/'.join(url.split('/')[:3]) + '/'
    h = {**HEADERS, 'Referer': origin}
    if custom:
        # 自定义请求头优先级最高，允许覆盖 Referer/User-Agent 等默认值
        h.update({str(k): str(v) for k, v in custom.items() if k})
    if extra:
        h.update(extra)
    return h

class DownloadTask:
    def __init__(self, task_id, url, save_path, num_threads=8, speed_limit=0, overwrite=True,
                 proxy='', headers=None, retry_count=3, retry_backoff=1.0,
                 connect_timeout=15, read_timeout=30, verify_ssl=False,
                 conflict_policy='rename', check_disk_space=True, min_free_mb=100):
        self.task_id = task_id
        self.url = url
        self.save_path = save_path
        self.num_threads = max(1, int(num_threads))  # 防止 0/负数导致 ZeroDivisionError
        self.speed_limit = speed_limit * 1024
        self.overwrite = overwrite
        # ---- 网络配置 ----
        self.proxy = (proxy or '').strip()
        self.custom_headers = {str(k): str(v) for k, v in (headers or {}).items() if k}
        self.retry_count = max(0, int(retry_count))      # 每个分片的最大重试次数
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.connect_timeout = max(1, int(connect_timeout))
        self.read_timeout = max(1, int(read_timeout))
        self.verify_ssl = bool(verify_ssl)
        # 'rename' 自动改名 / 'overwrite' 覆盖 / 'skip' 跳过已存在文件
        policy = str(conflict_policy or 'rename').lower()
        self.conflict_policy = policy if policy in ('rename', 'overwrite', 'skip') else 'rename'
        self.check_disk_space = bool(check_disk_space)
        self.min_free_mb = max(0, int(min_free_mb))
        self.space_check_interval = 10.0   # 运行中磁盘检查间隔（秒）
        # 开始时目标文件是否已存在（覆盖策略或断点续传时，它就是我们要替换的文件）
        self._target_owned = False
        self.total_size = 0
        self.downloaded = 0
        self.status = 'ready'
        self.speed = 0.0
        self.threads = []
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.queue = queue.Queue()
        self.file = None
        self._speed_window = deque(maxlen=5)
        self._last_speed_update = time.time()
        self._bytes_since_update = 0
        self._callback = None
        self._notified = False
        self._final_path = None
        self._error_msg = ''
        self._supports_range = True
        self._use_playwright = False
        self._thread_stats = {}
        self._generation = 0  # 每轮 start/pause/stop 递增，用于作废旧线程
        self._spans = []      # 已写入的完整区间 [[a,b],...]，用于安全断点续传
        self._session = curl_requests.Session()
        self._session.headers.update(_req_headers(url, custom=self.custom_headers))
        if self.proxy:
            # curl_cffi/libcurl 支持 http:// https:// socks4:// socks5:// socks5h://
            self._session.proxies = {'http': self.proxy, 'https': self.proxy}
        # 令牌桶：run 期间由 set_speed_limit() 动态调整
        self._limiter = TokenBucket(self.speed_limit)

    # ---- 断点区间元数据（.part.meta）----
    # 多线程下载中途 .part 的“前缀”并不连续（各线程只写了各自区间的开头），
    # 因此不能按文件大小续传，必须记录实际写过的区间，只补下空洞。
    @staticmethod
    def _merge_spans(spans):
        if not spans:
            return []
        spans = sorted([s for s in spans if s[1] >= s[0]])
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return merged

    def _collect_spans(self):
        spans = list(self._spans)
        for s in self._thread_stats.values():
            ss, se = s.get('span_start'), s.get('span_end')
            if ss is not None and se is not None:
                spans.append([ss, se])
        if self.total_size > 0:
            spans = [[a, min(b, self.total_size - 1)] for a, b in spans]
        return self._merge_spans(spans)

    def _meta_path(self):
        return self.save_path + '.part.meta'

    def _save_spans(self):
        try:
            meta = self._meta_path()
            with open(meta + '.tmp', 'w', encoding='utf-8') as f:
                json.dump({'total': self.total_size, 'spans': self._spans}, f)
            os.replace(meta + '.tmp', meta)
        except Exception:
            pass

    def _load_spans(self):
        try:
            meta = self._meta_path()
            if not os.path.exists(meta):
                return None
            with open(meta, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('total') == self.total_size:
                return self._merge_spans(data.get('spans', []))
        except Exception:
            pass
        return None

    @staticmethod
    def _gaps(spans, lo, hi):
        """在 [lo, hi] 内找出所有未被 spans 覆盖的区间"""
        gaps = []
        cur = lo
        for a, b in spans:
            if b < cur:
                continue
            if a > cur:
                gaps.append((cur, min(a - 1, hi)))
            cur = max(cur, b + 1)
            if cur > hi:
                break
        if cur <= hi:
            gaps.append((cur, hi))
        return gaps

    # ---- 文件名冲突策略 ----

    def _resolve_conflict(self):
        """目标文件已存在时按策略处理。返回 False 表示本轮不应继续下载。"""
        path = self.save_path
        if not os.path.exists(path):
            return True
        self._target_owned = True   # 目标本来就在，完成时由我们替换
        if os.path.exists(path + '.part'):
            # 有未完成的 .part：这是我们自己的续传，保持原路径，
            # 否则会把续传进度丢在一个新名字上，等于从头再下。
            log(f'目标已存在但有 .part，按断点续传处理: {path}')
            return True
        if self.conflict_policy == 'overwrite':
            log(f'目标已存在，按策略覆盖: {path}')
            return True
        if self.conflict_policy == 'skip':
            log(f'目标已存在，按策略跳过: {path}')
            self.status = 'skipped'
            self._final_path = path
            self._notify('skipped', path)
            return False
        # rename：自动换一个不冲突的名字
        new_path = _unique_path(path)
        log(f'目标已存在，自动重命名: {path} -> {new_path}')
        self.save_path = new_path
        return True

    # ---- 磁盘空间 ----

    def _free_space(self):
        """目标目录所在磁盘的剩余字节；取不到返回 None"""
        try:
            target = os.path.dirname(self.save_path) or '.'
            return shutil.disk_usage(target).free
        except OSError:
            return None

    def _check_disk_space(self, need_bytes):
        """空间够返回 None，不够返回给用户看的错误消息"""
        if not self.check_disk_space or need_bytes <= 0:
            return None
        free = self._free_space()
        if free is None:
            return None
        reserve = self.min_free_mb * 1024 * 1024
        if free < need_bytes + reserve:
            return (f'磁盘空间不足：还需要 {format_size(need_bytes)}，'
                    f'当前可用 {format_size(free)}（保留 {self.min_free_mb} MB 安全余量）')
        return None

    def _head(self, url, **kw):
        kw.setdefault('timeout', (self.connect_timeout, self.read_timeout))
        return self._session.head(url, impersonate='chrome120', verify=self.verify_ssl, **kw)

    def _get(self, url, **kw):
        kw.setdefault('timeout', (self.connect_timeout, self.read_timeout))
        return self._session.get(url, impersonate='chrome120', verify=self.verify_ssl, **kw)

    def set_speed_limit(self, kb_per_sec):
        """运行中修改限速（KB/s，0=不限），立即生效"""
        self.speed_limit = max(0, int(kb_per_sec or 0)) * 1024
        self._limiter.set_rate(self.speed_limit)

    def set_callback(self, func):
        self._callback = func

    def _get_remote_size(self):
        self._supports_range = False
        log(f'获取文件大小: {self.url}')
        try:
            resp = self._head(self.url, allow_redirects=True, timeout=10)
            log(f'HEAD 响应: {resp.status_code}')
            if resp.status_code == 200:
                length = resp.headers.get('Content-Length')
                log(f'Content-Length: {length}')
                if length:
                    size = int(length)
                    resp2 = self._get(self.url, headers={'Range': 'bytes=0-0'}, timeout=10)
                    log(f'Range 探测响应: {resp2.status_code}')
                    self._supports_range = (resp2.status_code == 206)
                    return size
            elif resp.status_code == 403:
                log('HEAD 403，尝试 Playwright 浏览器处理...')
                pw_result = resolve_cookies(self.url) if pw_available() else None
                if pw_result:
                    apply_playwright_cookies(self._session, pw_result)
                    resp = self._head(self.url, allow_redirects=True, timeout=10)
                    log(f'Playwright 后 HEAD: {resp.status_code}')
                    if resp.status_code == 200:
                        length = resp.headers.get('Content-Length')
                        if length:
                            size = int(length)
                            resp2 = self._get(self.url, headers={'Range': 'bytes=0-0'}, timeout=10)
                            self._supports_range = (resp2.status_code == 206)
                            return size
                if pw_available():
                    self._use_playwright = True
                    return 0  # Will download via Playwright directly
                self._error_msg = '服务器拒绝了访问，已尝试浏览器 cookie 但仍无法下载'
                return -1
            resp = self._get(self.url, headers={'Range': 'bytes=0-0'}, timeout=10)
            log(f'GET(Range) 响应: {resp.status_code}')
            if resp.status_code == 206:
                cr = resp.headers.get('Content-Range', '')
                if '/' in cr:
                    self._supports_range = True
                    return int(cr.split('/')[-1])
            elif resp.status_code == 403:
                log('Range 403，尝试 Playwright 浏览器处理...')
                pw_result = resolve_cookies(self.url) if pw_available() else None
                if pw_result:
                    apply_playwright_cookies(self._session, pw_result)
                    resp = self._get(self.url, headers={'Range': 'bytes=0-0'}, timeout=10)
                    log(f'Playwright 后 Range: {resp.status_code}')
                    if resp.status_code == 206:
                        cr = resp.headers.get('Content-Range', '')
                        if '/' in cr:
                            self._supports_range = True
                            return int(cr.split('/')[-1])
                    if pw_available():
                        self._use_playwright = True
                        return 0
                self._error_msg = '服务器拒绝了访问，已尝试浏览器模拟但仍无法下载'
                return -1
        except curl_requests.exceptions.MissingSchema:
            self._error_msg = '链接格式错误，请确认以 http:// 或 https:// 开头'
        except curl_requests.exceptions.ConnectionError:
            self._error_msg = '无法连接到服务器，请检查网络连接'
        except curl_requests.exceptions.Timeout:
            self._error_msg = '连接服务器超时，请检查网络或重试'
        except Exception as e:
            self._error_msg = f'无法获取文件大小: {e}'
            log(f'获取文件大小异常: {traceback.format_exc()}')
        return -1

    def start(self):
        log(f'start() 调用: {self.url} -> {self.save_path}')
        with self.state_lock:
            if self.status == 'running':
                log('start() 忽略: 已在运行')
                return
            self.status = 'running'
            self.speed = 0.0
            self._bytes_since_update = 0
            self._last_speed_update = time.time()
            self._notified = False
            self._final_path = None
            self._error_msg = ''

        # 自动导入浏览器 cookie（回环地址不需要，跳过可省去数秒的浏览器数据库扫描）
        if _is_loopback(self.url):
            log('本机地址，跳过浏览器 cookie')
        else:
            try:
                n = apply_cookies_to_session(self._session, self.url)
                if n > 0:
                    log(f'成功导入 {n} 个 cookie')
            except Exception as e:
                log(f'cookie 导入失败: {e}')

        # 如果保存路径是目录，自动生成文件名
        if os.path.isdir(self.save_path) or self.save_path.endswith(('\\', '/')):
            filename = self.url.rstrip('/').split('/')[-1].split('?')[0] or 'download'
            self.save_path = os.path.join(self.save_path, filename)
            log(f'路径是目录，自动补全文件名: {self.save_path}')

        # 检查并创建保存目录
        save_dir = os.path.dirname(self.save_path)
        log(f'保存目录: "{save_dir}", 存在={os.path.exists(save_dir) if save_dir else "N/A"}')
        if save_dir and not os.path.exists(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except PermissionError:
                self.status = 'error'
                self._notify('error', f'权限不足，无法创建目录，请以管理员身份运行: {save_dir}')
                log(f'创建目录权限不足: {save_dir}')
                return
            except Exception as e:
                self.status = 'error'
                self._notify('error', f'无法创建目录: {e}')
                log(f'创建目录异常: {traceback.format_exc()}')
                return

        # 文件名冲突：按策略处理（重命名 / 覆盖 / 跳过）
        # 注意 overwrite 是旧接口参数，默认 True；它不再改写策略，
        # 否则用户选的"自动重命名"会被默认值悄悄变成"覆盖"。
        if not self._resolve_conflict():
            return

        # 旧文件在最终 rename 时才删除（finalize 内），
        # 避免下载失败时把用户原来的文件也弄丢

        self.total_size = self._get_remote_size()
        log(f'文件大小={self.total_size}, 支持Range={self._supports_range}')
        if self.total_size == -1 and self._error_msg:
            self.status = 'error'
            self._notify('error', self._error_msg)
            log(f'获取大小失败: {self._error_msg}')
            return

        if self._use_playwright:
            log('使用 Playwright 直接下载...')
            ok = pw_download(self.url, self.save_path, timeout=120)
            if ok:
                if os.path.exists(self.save_path):
                    self.total_size = os.path.getsize(self.save_path)
                    self.downloaded = self.total_size
                    self._final_path = self.save_path
                    self.status = 'completed'
                    self._notify('completed', self._final_path)
                else:
                    self.status = 'error'
                    self._notify('error', 'Playwright 下载完成但文件未找到')
            else:
                self.status = 'error'
                self._notify('error', 'Playwright 下载失败')
            return

        if self.total_size <= 0:
            self.num_threads = 1
        elif not self._supports_range:
            log('服务器不支持Range，降级为单线程')
            self.num_threads = 1

        temp = self.save_path + '.part'
        existing = os.path.getsize(temp) if os.path.exists(temp) else 0
        log(f'临时文件: {temp}, 已存在={existing}')

        # ---- 断点续传规划 ----
        self._spans = []
        if self.total_size > 0 and os.path.exists(temp):
            self._spans = self._load_spans() or []
            if not self._spans and existing > 0:
                # 没有区间元数据（旧版本/异常退出留下的 .part）：
                # 无法判断哪些字节有效，安全起见从头下载，避免空洞损坏
                log('无断点元数据，重新开始下载（截断旧 .part）')
                existing = 0
                try:
                    open(temp, 'wb').close()
                except OSError:
                    pass
            if self._spans:
                total_span = sum(b - a + 1 for a, b in self._spans)
                self.downloaded = total_span
                gaps = self._gaps(self._spans, 0, self.total_size - 1)
                if not gaps:
                    log('断点区间已完整，直接完成')
                    self._finalize_completion()
                    return
                log(f'断点续传: 已写 {total_span}/{self.total_size} 字节, 待补 {len(gaps)} 段')
                ranges = gaps
                self.num_threads = len(ranges)  # monitor 按实际线程数计数，避免提前完成判定
            else:
                self.downloaded = 0
        else:
            # 大小未知（服务器没给 Content-Length）：无法安全续传，
            # 若已有 .part 则截断从头下载，避免服务器忽略 Range 时数据错位
            if existing > 0:
                log('文件大小未知，截断旧 .part 从头下载')
                existing = 0
                try:
                    open(temp, 'wb').close()
                except OSError:
                    pass
            self.downloaded = 0

        if not self._spans:
            # 全量下载（无断点或已截断）：按 total 均匀分段
            if self.total_size > 0:
                remaining = self.total_size - existing
                part = remaining // self.num_threads
                ranges = []
                for i in range(self.num_threads):
                    s = existing + i * part
                    e = existing + (i + 1) * part - 1 if i < self.num_threads - 1 else self.total_size - 1
                    if s <= e:
                        ranges.append((s, e))
                self.num_threads = len(ranges)
            else:
                ranges = [(existing, -1)]
                self.num_threads = 1

        # ---- 磁盘空间预检查 ----
        # 续传时只需要补空洞的字节数，不能按整个文件大小判断，否则会误报空间不足
        need = self.total_size - self.downloaded if self.total_size > 0 else 0
        disk_err = self._check_disk_space(need)
        if disk_err:
            self.status = 'error'
            self._error_msg = disk_err
            self._notify('error', disk_err)
            log(f'磁盘空间不足，拒绝开始下载: {disk_err}')
            return
        if need > 0:
            log(f'磁盘检查通过: 需要 {format_size(need)}, 可用 {format_size(self._free_space() or 0)}')

        try:
            if not os.path.exists(temp):
                open(temp, 'wb').close()
            self.file = open(temp, 'rb+')
            log(f'打开临时文件成功: {temp}')
        except PermissionError:
            self.status = 'error'
            self._notify('error', '无法写入临时文件，请检查目录权限，或以管理员身份运行')
            log(f'打开临时文件权限不足: {temp}')
            return
        except Exception as e:
            self.status = 'error'
            self._notify('error', f'无法创建临时文件: {e}')
            log(f'打开临时文件异常: {traceback.format_exc()}')
            return

        # 全局限速由任务级令牌桶统一分配（总速率 ≈ speed_limit，与线程数无关）
        self._limiter.set_rate(self.speed_limit)

        # 新的一轮下载：作废旧上一轮可能还存活的线程；
        # 每轮用独立的队列，避免旧 monitor 复活后与新 monitor 抢消息
        self._generation += 1
        gen = self._generation
        q = queue.Queue()
        self.queue = q

        self.threads = []
        for idx, (s, e) in enumerate(ranges):
            t = threading.Thread(target=self._download_part, args=(idx, s, e, gen, q), daemon=True)
            t.start()
            self.threads.append(t)

        threading.Thread(target=self._monitor, args=(gen, q), daemon=True).start()

    # ---- 重试策略 ----
    # 4xx 多为不可恢复（认证/权限/不存在），5xx 与网络抖动才值得退避重试
    _RETRYABLE_HTTP = (408, 425, 429, 500, 502, 503, 504, 507, 509)

    def _should_retry(self, exc):
        if self.retry_count <= 0:
            return False
        if isinstance(exc, (RangeNotHonored, DiskFullError)):
            return False  # 区间问题/磁盘写满：重试只会再次失败
        if isinstance(exc, curl_requests.exceptions.HTTPError):
            code = 0
            resp = getattr(exc, 'response', None)
            if resp is not None:
                code = getattr(resp, 'status_code', 0) or 0
            if not code:
                m = re.search(r'HTTP Error (\d{3})', str(exc))
                code = int(m.group(1)) if m else 0
            return code in self._RETRYABLE_HTTP
        # 传输层故障（连接重置/超时/响应体截断/解码失败）一律可重试
        return True
    def _retry_delay(self, attempt):
        """指数退避 + 抖动，上限 30 秒"""
        base = self.retry_backoff * (2 ** attempt)
        return min(base, 30.0) * (0.5 + random.random())

    def _sleep_interruptible(self, seconds, gen):
        """可被暂停/停止打断的等待；返回 False 表示本轮已作废"""
        deadline = time.time() + seconds
        while True:
            if self.status != 'running' or gen != self._generation:
                return False
            remain = deadline - time.time()
            if remain <= 0:
                return True
            time.sleep(min(0.2, remain))

    def _check_range_response(self, resp, headers, expected_start, idx):
        """校验服务器确实按请求返回了区间（否则写入会错位损坏文件）"""
        if 'Range' not in headers:
            return
        if resp.status_code == 200:
            if expected_start == 0:
                # 从 0 开始写全量响应是安全的（数据没有错位），继续即可
                return
            raise RangeNotHonored('服务器不支持 Range 请求，为避免文件损坏已中止下载')
        if resp.status_code == 206:
            cr = resp.headers.get('Content-Range', '')
            m = re.match(r'bytes\s+(\d+)-', cr or '')
            if m and int(m.group(1)) != expected_start:
                raise RangeNotHonored(
                    f'服务器返回的区间与请求不符（期望从 {expected_start} 开始，实际从 {m.group(1)} 开始），'
                    f'为避免文件损坏已中止下载')
            if not m:
                log(f'线程{idx} 206 但 Content-Range 无法解析: {cr!r}')

    def _pump(self, idx, resp, pos, end, stat, gen):
        """把响应体写入文件；pos 为 [当前位置] 可变容器（异常中断时也能保留进度）"""
        for chunk in resp.iter_content():
            cur = pos[0]
            if self.status != 'running' or gen != self._generation:
                stat['status'] = 'stopped'
                log(f'线程{idx} 停止: status={self.status}, gen={gen}')
                break
            if end >= 0 and cur > end:
                # 服务器返回的数据超出请求区间：停止写入，防止覆盖相邻线程的数据
                log(f'线程{idx} 数据超出区间 end={end}, 停止')
                break
            if not chunk:
                continue
            # 令牌桶限速（任务内所有线程共享配额）
            if self.speed_limit > 0:
                if not self._limiter.consume(len(chunk),
                                             stop_check=lambda: self.status == 'running' and gen == self._generation):
                    stat['status'] = 'stopped'
                    break
            with self.lock:
                if self.status != 'running' or gen != self._generation:
                    stat['status'] = 'stopped'
                    break
                try:
                    self.file.seek(cur)
                    self.file.write(chunk)
                    self.file.flush()
                except OSError as e:
                    if getattr(e, 'errno', None) == errno.ENOSPC or 'space' in str(e).lower():
                        raise DiskFullError(
                            '磁盘空间已满，下载已停止；清理磁盘后点"继续"可断点续传')
                    raise
                self.downloaded += len(chunk)
                self._bytes_since_update += len(chunk)
                stat['downloaded'] += len(chunk)
                stat['bytes_since'] += len(chunk)
                if stat['span_start'] is None:
                    stat['span_start'] = cur
                stat['span_end'] = cur + len(chunk) - 1
                cur += len(chunk)
                pos[0] = cur
            now = time.time()
            if now - self._last_speed_update > 0.5:
                inst_speed = self._bytes_since_update / (now - self._last_speed_update)
                self._speed_window.append(inst_speed)
                if self._speed_window:
                    self.speed = sum(self._speed_window) / len(self._speed_window)
                self._bytes_since_update = 0
                self._last_speed_update = now
            if now - stat['last_update'] > 0.5:
                ds = stat['bytes_since']
                dt = now - stat['last_update']
                stat['speed'] = ds / dt if dt > 0 else 0
                stat['bytes_since'] = 0
                stat['last_update'] = now
        pos[0] = cur
        return cur

    def _download_part(self, idx, start, end, gen, q):
        stat = {
            'status': 'running', 'start': start, 'end': end,
            'downloaded': 0, 'speed': 0.0, 'error': '',
            'last_update': time.time(), 'bytes_since': 0,
            'span_start': None, 'span_end': None, 'retries': 0,
        }
        self._thread_stats[idx] = stat
        pos = [start]   # 可变当前位置：重试时从上次真正写到的偏移继续
        attempt = 0
        try:
            while True:
                cur = pos[0]
                headers = {}
                if self._supports_range:
                    if end >= 0:
                        headers['Range'] = f'bytes={cur}-{end}'
                    elif cur > 0:
                        # 未知大小续传：从 cur 开始；从头下载则不带 Range（空文件服务器会回 416）
                        headers['Range'] = f'bytes={cur}-'
                # 服务器已确认不支持 Range：整文件单线程下载，不发 Range 头
                log(f'线程{idx} 请求: bytes={cur}-{end}' + (f' (重试第{attempt}次)' if attempt else ''))
                resp = None
                try:
                    resp = self._get(self.url, headers=headers, stream=True)
                    log(f'线程{idx} HTTP={resp.status_code}')
                    if resp.status_code in (401, 403):
                        raise curl_requests.exceptions.HTTPError(
                            '服务器拒绝了访问，请更换下载源（如 GitHub Release）或使用浏览器下载', 0, resp)
                    self._check_range_response(resp, headers, pos[0], idx)
                    resp.raise_for_status()
                    self._pump(idx, resp, pos, end, stat, gen)
                    resp.close()
                    resp = None
                    break  # 本分片完成
                except Exception as e:
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    stopped = self.status != 'running' or gen != self._generation
                    if stopped or attempt >= self.retry_count or not self._should_retry(e):
                        raise
                    delay = self._retry_delay(attempt)
                    attempt += 1
                    stat['retries'] = attempt
                    stat['status'] = 'retrying'
                    log(f'线程{idx} 第{attempt}次重试，{delay:.1f}s 后从 {cur} 继续: {e}')
                    if not self._sleep_interruptible(delay, gen):
                        raise
                    stat['status'] = 'running'
            stat['status'] = 'completed' if self.status == 'running' else stat['status']
            if gen == self._generation:
                q.put(('part_done', idx))
            log(f'线程{idx} 完成')
        except Exception as e:
            stat['status'] = 'error'
            stat['error'] = str(e)
            log(f'线程{idx} 异常: {traceback.format_exc()}')
            if gen == self._generation:
                q.put(('error', str(e)))

    def _monitor(self, gen, q):
        done = 0
        last_meta_save = time.time()
        last_space_check = time.time()
        while self.status == 'running' and gen == self._generation:
            try:
                msg = q.get(timeout=1.0)
            except queue.Empty:
                if gen != self._generation:
                    return  # 旧 monitor 已作废（pause/stop 后）：直接退出，不得改动状态
                if all(not t.is_alive() for t in self.threads):
                    if done < self.num_threads:
                        self.status = 'error'
                        self._error_msg = '下载线程意外终止'
                        with self.lock:
                            self._spans = self._collect_spans()
                            self._close_file_locked()
                        self._save_spans()
                        self._notify('error', self._error_msg)
                    return
                now = time.time()
                # 磁盘保护：剩余空间低于安全线时自动暂停，避免写满系统盘
                if self.min_free_mb > 0 and now - last_space_check > self.space_check_interval:
                    last_space_check = now
                    free = self._free_space()
                    if free is not None and free < self.min_free_mb * 1024 * 1024:
                        msg = (f'磁盘剩余空间不足 {self.min_free_mb} MB'
                               f'（当前 {format_size(free)}），已自动暂停任务，清理后可继续')
                        log(msg)
                        self._error_msg = msg
                        self._notify('warning', msg)
                        self.pause()
                        return
                # 周期性落盘断点区间（崩溃后可续传）
                if now - last_meta_save > 2:
                    with self.lock:
                        self._spans = self._collect_spans()
                    self._save_spans()
                    last_meta_save = now
                continue
            if gen != self._generation:
                return  # 旧 monitor：本轮已被作废，丢弃消息并退出
            if msg[0] == 'part_done':
                done += 1
                if done == self.num_threads:
                    self._finalize_completion()
                    return
            elif msg[0] == 'error':
                self.status = 'error'
                self._error_msg = msg[1]
                with self.lock:
                    self._spans = self._collect_spans()
                    # 失败后必须释放 .part 句柄：否则 Windows 上文件被锁住，
                    # 用户无法删除/移动，重试也可能与之冲突
                    self._close_file_locked()
                self._save_spans()
                self._notify('error', msg[1])
                return

    def _finalize_completion(self):
        log('finalize: 开始完成处理')
        event = None
        with self.state_lock:
            if self.status != 'running':
                log(f'finalize 跳过: status={self.status}')
                return
            try:
                if self.file and not self.file.closed:
                    self.file.flush()
                    try:
                        os.fsync(self.file.fileno())
                    except:
                        pass
                    self.file.close()
                    self.file = None
                    log('finalize: 文件已关闭')

                temp = self.save_path + '.part'
                if not os.path.exists(temp):
                    raise RuntimeError('临时文件丢失')

                # 大小校验
                if self.total_size > 0:
                    actual = os.path.getsize(temp)
                    log(f'finalize: 大小校验 期望={self.total_size} 实际={actual}')
                    if actual != self.total_size:
                        raise RuntimeError(
                            f'文件不完整：期望{self.total_size}字节，实际{actual}字节')

                # 目标已存在时：属于我们自己的（覆盖策略 / 断点续传）就替换它，
                # 是下载期间被别人新建的则另存，别把别人的文件删了。
                if os.path.exists(self.save_path):
                    if self.conflict_policy == 'overwrite' or self._target_owned:
                        log(f'finalize: 删除旧文件 {self.save_path}')
                        if os.path.isdir(self.save_path):
                            raise RuntimeError(f'目标路径是目录，无法写入: {self.save_path}')
                        os.remove(self.save_path)
                    else:
                        new_path = _unique_path(self.save_path)
                        log(f'finalize: 目标被占用，改存 {new_path}')
                        self.save_path = new_path

                # 重命名为最终文件
                os.rename(temp, self.save_path)
                self._final_path = self.save_path
                self.status = 'completed'  # 只有重命名成功才算完成
                event = ('completed', self._final_path)
                log(f'finalize: 重命名成功 -> {self.save_path}')
            except PermissionError as e:
                log(f'finalize: 权限不足: {e}')
                self.status = 'error'
                self._error_msg = f'权限不足，文件可能被占用，或以管理员身份运行: {e}'
                event = ('error', self._error_msg)
            except Exception as e:
                log(f'finalize: 失败: {e}')
                self.status = 'error'
                self._error_msg = str(e)
                event = ('error', self._error_msg)

        if event is None:
            return
        if event[0] == 'completed':
            meta = self._meta_path()
            if os.path.exists(meta):
                try:
                    os.remove(meta)
                except:
                    pass
        self._notify(event[0], event[1])

    def pause(self):
        with self.state_lock:
            if self.status != 'running':
                return
            self.status = 'paused'
            self._generation += 1
        for t in self.threads:
            t.join(timeout=0.5)
        self.threads.clear()
        with self.lock:
            self._close_file_locked()
            self._spans = self._collect_spans()
        self._save_spans()
        self._notify('paused')

    def stop(self):
        with self.state_lock:
            if self.status == 'completed':
                return  # 已完成的任务不允许被停止覆盖
            self.status = 'stopped'
            self._generation += 1
        for t in self.threads:
            t.join(timeout=0.5)
        self.threads.clear()
        with self.lock:
            self._close_file_locked()
            self._spans = self._collect_spans()
        self._save_spans()
        temp = self.save_path + '.part'
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except:
                pass
        meta = self._meta_path()
        if os.path.exists(meta):
            try:
                os.remove(meta)
            except:
                pass
        self._spans = []
        self.downloaded = 0
        self._notify('stopped')

    def _close_file_locked(self):
        """关闭临时文件（调用方必须已持有 self.lock）"""
        if self.file and not self.file.closed:
            try:
                self.file.flush()
            except Exception:
                pass
            try:
                self.file.close()
            except Exception:
                pass
        self.file = None

    def get_thread_stats(self):
        return dict(self._thread_stats) if hasattr(self, '_thread_stats') else {}

    def get_info(self):
        if self.total_size > 0:
            pct = (self.downloaded / self.total_size) * 100 if self.total_size else 0
        else:
            pct = 0
        return {
            'percent': pct,
            'downloaded': self.downloaded,
            'total': self.total_size,
            'speed': self.speed,
            'status': self.status,
            'final_path': self._final_path
        }

    def _notify(self, event, data=None):
        log(f'通知: task={self.task_id} event={event} data={data}')
        if self._callback:
            self._callback(self.task_id, event, data)
