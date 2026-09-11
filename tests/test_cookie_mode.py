# tests/test_cookie_mode.py - 浏览器 Cookie 读取策略测试
#
# 重点：默认(auto)模式下普通下载绝不能读取浏览器的 Cookies 数据库，
# 只有服务器明确要求认证(401/403)时才读取并重试。
import base64
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine                                      # noqa: E402
from engine import DownloadTask                    # noqa: E402
from fault_server import FaultServer               # noqa: E402

BASIC = 'Basic ' + base64.b64encode(b'user:pass').decode()


def wait(task, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.status in ('completed', 'error', 'stopped', 'skipped', 'paused'):
            return task.status
        time.sleep(0.02)
    return task.status


class CookieModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_cookie_')
        self.calls = []
        self._orig = engine.apply_cookies_to_session
        # 测试服务器跑在 127.0.0.1 上，而引擎对回环地址会跳过浏览器 Cookie
        # （这是刻意设计），这里临时关掉该优化以便验证 Cookie 逻辑本身。
        self._orig_loopback = engine._is_loopback
        engine._is_loopback = lambda url: False

    def tearDown(self):
        engine.apply_cookies_to_session = self._orig
        engine._is_loopback = self._orig_loopback
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install_cookie_source(self, header=None):
        """替换浏览器 Cookie 读取：记录调用，可选注入认证头（模拟登录态）"""
        def fake(session, url):
            self.calls.append(url)
            if header:
                session.headers[header[0]] = header[1]
                return 1
            return 0
        engine.apply_cookies_to_session = fake

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_auto_mode_does_not_touch_browser_data(self):
        self._install_cookie_source()
        with FaultServer(size=512 * 1024) as srv:
            task = DownloadTask(1, srv.url('/file'), self.path('plain.bin'),
                                num_threads=2, cookie_mode='auto')
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(self.calls, [], '普通下载不应读取浏览器 Cookie')
            self.assertFalse(task._cookies_loaded)

    def test_auto_mode_loads_cookies_on_401_and_retries(self):
        self._install_cookie_source(header=('Authorization', BASIC))
        with FaultServer(size=512 * 1024) as srv:
            task = DownloadTask(1, srv.url('/auth'), self.path('authed.bin'),
                                num_threads=2, cookie_mode='auto', retry_count=2)
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(len(self.calls), 1, '应恰好读取一次浏览器 Cookie')
            with open(task.save_path, 'rb') as f:
                self.assertEqual(f.read(), srv.expected_bytes())

    def test_auto_mode_handles_403_then_forbidden_retry(self):
        self._install_cookie_source(header=('X-Auth', 'yes'))
        with FaultServer(size=512 * 1024) as srv:
            task = DownloadTask(1, srv.url('/forbidden'), self.path('fb.bin'),
                                num_threads=2, cookie_mode='auto', retry_count=2)
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertGreaterEqual(len(self.calls), 1)
            with open(task.save_path, 'rb') as f:
                self.assertEqual(f.read(), srv.expected_bytes())

    def test_off_mode_never_reads_and_fails_cleanly(self):
        self._install_cookie_source(header=('Authorization', BASIC))
        with FaultServer(size=512 * 1024) as srv:
            task = DownloadTask(1, srv.url('/auth'), self.path('off.bin'),
                                num_threads=2, cookie_mode='off', retry_count=1)
            task.start()
            self.assertEqual(wait(task), 'error')
            self.assertEqual(self.calls, [], 'off 模式不应读取浏览器 Cookie')

    def test_always_mode_reads_up_front(self):
        self._install_cookie_source()
        with FaultServer(size=512 * 1024) as srv:
            task = DownloadTask(1, srv.url('/file'), self.path('always.bin'),
                                num_threads=2, cookie_mode='always')
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(len(self.calls), 1, 'always 模式应在开始时读取一次')

    def test_cookie_read_happens_at_most_once(self):
        self._install_cookie_source()          # 注入不生效 -> 一直 401
        with FaultServer(size=256 * 1024) as srv:
            task = DownloadTask(1, srv.url('/auth'), self.path('loop.bin'),
                                num_threads=1, cookie_mode='auto', retry_count=3,
                                retry_backoff=0.05)
            task.start()
            self.assertEqual(wait(task), 'error')
            self.assertEqual(len(self.calls), 1, '浏览器数据最多读一次')

    def test_invalid_mode_falls_back_to_auto(self):
        task = DownloadTask(1, 'http://127.0.0.1:9/x.bin', self.path('x.bin'),
                            cookie_mode='bogus')
        self.assertEqual(task.cookie_mode, 'auto')


if __name__ == '__main__':
    unittest.main(verbosity=2)
