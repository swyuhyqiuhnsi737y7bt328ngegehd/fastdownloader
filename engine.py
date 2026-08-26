from curl_cffi import requests as curl_requests
import threading
import queue
import time
import os
import json
import re
import traceback
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

def _req_headers(url, extra=None):
    origin = '/'.join(url.split('/')[:3]) + '/'
    h = {**HEADERS, 'Referer': origin}
    if extra:
        h.update(extra)
    return h

class DownloadTask:
    def __init__(self, task_id, url, save_path, num_threads=8, speed_limit=0, overwrite=True):
        self.task_id = task_id
        self.url = url
        self.save_path = save_path
        self.num_threads = max(1, int(num_threads))  # 防止 0/负数导致 ZeroDivisionError
        self.speed_limit = speed_limit * 1024
        self.overwrite = overwrite
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
        self._session.headers.update(_req_headers(url))

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

    def _head(self, url, **kw):
        return self._session.head(url, impersonate='chrome120', verify=False, **kw)

    def _get(self, url, **kw):
        return self._session.get(url, impersonate='chrome120', verify=False, **kw)

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

        # 自动导入浏览器 cookie
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

        # 不覆盖模式下目标已存在：直接报错返回（Windows 下 os.rename 无法覆盖已存在文件）
        if not self.overwrite and os.path.exists(self.save_path):
            self.status = 'error'
            self._error_msg = f'文件已存在，未覆盖: {self.save_path}'
            self._notify('error', self._error_msg)
            log(f'overwrite=False 且文件已存在: {self.save_path}')
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

        # 全局限速分摊到每个线程：总速度 ≈ speed_limit，而不是 limit × 线程数
        self._thread_speed_limit = self.speed_limit / self.num_threads if self.num_threads else 0

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

    def _download_part(self, idx, start, end, gen, q):
        stat = {
            'status': 'running', 'start': start, 'end': end,
            'downloaded': 0, 'speed': 0.0, 'error': '',
            'last_update': time.time(), 'bytes_since': 0,
            'span_start': None, 'span_end': None,
        }
        self._thread_stats[idx] = stat
        try:
            headers = {}
            if end >= 0:
                headers['Range'] = f'bytes={start}-{end}'
            elif start > 0:
                # 未知大小续传：从 start 开始；从头下载则不带 Range（空文件服务器会回 416）
                headers['Range'] = f'bytes={start}-'
            log(f'线程{idx} 开始: bytes={start}-{end}')
            resp = self._get(self.url, headers=headers, stream=True, timeout=30)
            log(f'线程{idx} HTTP={resp.status_code}')
            if resp.status_code in (403, 503, 429):
                raise Exception('服务器拒绝了访问，请更换下载源（如 GitHub Release）或使用浏览器下载')
            if 'Range' in headers:
                # 服务器可能忽略/篡改 Range：直接按请求偏移写入会把数据写错位，
                # 且大小校验不一定能发现（总长度可能恰好一致），必须显式校验。
                if resp.status_code == 200:
                    raise Exception('服务器不支持 Range 请求，为避免文件损坏已中止下载')
                if resp.status_code == 206:
                    cr = resp.headers.get('Content-Range', '')
                    m = re.match(r'bytes\s+(\d+)-', cr or '')
                    if m and int(m.group(1)) != start:
                        raise Exception(
                            f'服务器返回的区间与请求不符（期望从 {start} 开始，实际从 {m.group(1)} 开始），'
                            f'为避免文件损坏已中止下载')
                    if not m:
                        log(f'线程{idx} 206 但 Content-Range 无法解析: {cr!r}')
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=8192):
                if self.status != 'running' or gen != self._generation:
                    stat['status'] = 'stopped'
                    log(f'线程{idx} 停止: status={self.status}, gen={gen}')
                    break
                if end >= 0 and start > end:
                    # 服务器返回的数据超出请求区间：停止写入，防止覆盖相邻线程的数据
                    log(f'线程{idx} 数据超出区间 end={end}, 停止')
                    break
                if self._thread_speed_limit > 0:
                    time.sleep(len(chunk) / self._thread_speed_limit * 0.95)
                with self.lock:
                    if self.status != 'running' or gen != self._generation:
                        stat['status'] = 'stopped'
                        break
                    self.file.seek(start)
                    self.file.write(chunk)
                    self.file.flush()
                    self.downloaded += len(chunk)
                    self._bytes_since_update += len(chunk)
                    stat['downloaded'] += len(chunk)
                    stat['bytes_since'] += len(chunk)
                    if stat['span_start'] is None:
                        stat['span_start'] = start
                    stat['span_end'] = start + len(chunk) - 1
                    start += len(chunk)
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
            resp.close()
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
                        self._notify('error', self._error_msg)
                    return
                # 周期性落盘断点区间（崩溃后可续传）
                now = time.time()
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

                # 覆盖模式下，重命名前才删除旧文件（避免下载失败丢旧文件）
                if self.overwrite and os.path.exists(self.save_path):
                    log(f'finalize: 删除旧文件 {self.save_path}')
                    os.remove(self.save_path)

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
            if self.file and not self.file.closed:
                self.file.close()
                self.file = None
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
            if self.file and not self.file.closed:
                self.file.close()
                self.file = None
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
