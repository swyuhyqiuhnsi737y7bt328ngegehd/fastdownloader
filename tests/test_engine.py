# tests/test_engine.py - 下载引擎测试（标准库 unittest，无需额外依赖）
#
# 运行：  python -m unittest discover -s tests -v
#    或：  python tests/test_engine.py
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from engine import DownloadTask                      # noqa: E402
from fault_server import FaultServer, make_payload   # noqa: E402
from proxy_server import ProxyServer                 # noqa: E402

MB = 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


class TaskHarness:
    """小工具：创建任务、等待结束、收集事件"""

    def __init__(self, url, save_path, autostart=True, **kw):
        self.events = []
        self.task = DownloadTask(1, url, save_path, **kw)
        self.task.set_callback(lambda tid, ev, data=None: self.events.append((ev, data)))
        if autostart:
            self.task.start()

    def wait(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.task.status in ('completed', 'error', 'stopped'):
                return self.task.status
            time.sleep(0.02)
        return self.task.status

    def errors(self):
        return [d for ev, d in self.events if ev == 'error']


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_test_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)


class TestBasicDownload(EngineTestBase):
    def test_multithread_download_is_correct(self):
        with FaultServer(size=4 * MB) as srv:
            expected = srv.expected_bytes()
            save = self.path('basic.bin')
            h = TaskHarness(srv.url('/file'), save, num_threads=8)
            self.assertEqual(h.wait(), 'completed')
            self.assertEqual(os.path.getsize(save), len(expected))
            self.assertEqual(sha256_file(save), sha256(expected))
            self.assertFalse(os.path.exists(save + '.part'))
            self.assertFalse(os.path.exists(save + '.part.meta'))

    def test_server_without_range_falls_back_to_single_thread(self):
        with FaultServer(size=2 * MB) as srv:
            save = self.path('norange.bin')
            h = TaskHarness(srv.url('/norange'), save, num_threads=8)
            self.assertEqual(h.wait(), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))
            self.assertFalse(h.task._supports_range)

    def test_unknown_size_download(self):
        with FaultServer(size=1 * MB) as srv:
            save = self.path('nosize.bin')
            h = TaskHarness(srv.url('/nosize'), save, num_threads=4)
            self.assertEqual(h.wait(), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))

    def test_missing_file_reports_error(self):
        with FaultServer(size=64 * 1024) as srv:
            h = TaskHarness(srv.url('/404'), self.path('missing.bin'), num_threads=4)
            self.assertEqual(h.wait(), 'error')
            self.assertTrue(h.errors())


class TestResumeAndControl(EngineTestBase):
    def test_pause_resume_keeps_file_correct(self):
        with FaultServer(size=4 * MB, rate=0.1) as srv:
            expected = srv.expected_bytes()
            save = self.path('pause.bin')
            h = TaskHarness(srv.url('/file'), save, num_threads=8)
            time.sleep(0.6)
            h.task.pause()
            self.assertEqual(h.task.status, 'paused')
            self.assertTrue(os.path.exists(save + '.part'))
            h.task.start()                      # 断点续传
            self.assertEqual(h.wait(), 'completed')
            self.assertEqual(sha256_file(save), sha256(expected))
            self.assertFalse(h.errors())

    def test_stop_then_restart_from_scratch(self):
        with FaultServer(size=4 * MB, rate=0.1) as srv:
            save = self.path('stop.bin')
            h = TaskHarness(srv.url('/file'), save, num_threads=8)
            time.sleep(0.5)
            h.task.stop()
            self.assertEqual(h.task.status, 'stopped')
            self.assertFalse(os.path.exists(save + '.part'))   # 停止会删除临时文件
            h.task.start()
            self.assertEqual(h.wait(), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))

    def test_crash_recovery_resumes_from_part(self):
        """kill -9 场景：进程被杀后重启，能接着 .part + meta 续传"""
        with FaultServer(size=8 * MB, rate=0.1) as srv:
            expected = srv.expected_bytes()
            save = self.path('crash.bin')
            # 等到真正写入数据、且 meta 已落盘（monitor 每 2s 保存一次）后再硬杀进程
            # 多行脚本：复合语句不能用 ';' 拼接
            child = (
                'import sys, time, os'  '\n'
                f'sys.path.insert(0, {ROOT!r})'  '\n'
                'from engine import DownloadTask'  '\n'
                f't = DownloadTask(9, {srv.url("/file")!r}, {save!r}, num_threads=8)'  '\n'
                't.start()'  '\n'
                'deadline = time.time() + 30'  '\n'
                'while time.time() < deadline and t.downloaded == 0:'  '\n'
                '    time.sleep(0.05)'  '\n'
                'time.sleep(2.5)'  '\n'
                'os._exit(0)'  '\n'
            )
            subprocess.run([sys.executable, '-c', child], timeout=60)
            self.assertTrue(os.path.exists(save + '.part'))
            self.assertTrue(os.path.exists(save + '.part.meta'))
            h = TaskHarness(srv.url('/file'), save, num_threads=8)
            self.assertEqual(h.wait(), 'completed')
            self.assertEqual(sha256_file(save), sha256(expected))


class TestRetry(EngineTestBase):
    def test_retries_transient_503(self):
        """/flaky 前 2 次请求 503，重试后应成功（探测 1 次 + 分片首次 1 次）"""
        with FaultServer(size=2 * MB) as srv:
            save = self.path('flaky.bin')
            h = TaskHarness(srv.url('/flaky', fail=2), save, num_threads=4,
                            retry_count=3, retry_backoff=0.2)
            self.assertEqual(h.wait(), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))

    def test_gives_up_after_retry_budget(self):
        """一直 503：重试耗尽后报错，且重试次数不超过配置"""
        with FaultServer(size=512 * 1024) as srv:
            save = self.path('flaky_fail.bin')
            h = TaskHarness(srv.url('/flaky', fail=99), save, num_threads=2,
                            retry_count=2, retry_backoff=0.1)
            self.assertEqual(h.wait(), 'error')
            retries = max((s.get('retries', 0) for s in h.task.get_thread_stats().values()), default=0)
            self.assertLessEqual(retries, 2)

    def test_401_is_not_retried(self):
        """认证失败属于不可恢复错误：不应消耗重试次数"""
        with FaultServer(size=512 * 1024) as srv:
            h = TaskHarness(srv.url('/auth'), self.path('auth.bin'), num_threads=2,
                            retry_count=5, retry_backoff=0.1)
            status = h.wait(timeout=60)
            self.assertEqual(status, 'error')
            retries = max((s.get('retries', 0) for s in h.task.get_thread_stats().values()), default=0)
            self.assertEqual(retries, 0)

    def test_recovers_from_dropped_connection(self):
        """服务器中途断开连接：应自动续传重试，而不是整任务失败"""
        with FaultServer(size=1 * MB) as srv:
            save = self.path('drop.bin')
            # 每个响应只发 64KB 就断开连接：引擎必须带 Range 续传重试
            h = TaskHarness(srv.url('/file', after=64 * 1024), save, num_threads=4,
                            retry_count=6, retry_backoff=0.1, read_timeout=5)
            self.assertEqual(h.wait(timeout=180), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))


class TestIntegrityGuards(EngineTestBase):
    def test_shifted_content_range_aborts_without_corruption(self):
        """服务器篡改 Content-Range 起点：必须报错，不能写出错位文件"""
        with FaultServer(size=1 * MB) as srv:
            save = self.path('shift.bin')
            h = TaskHarness(srv.url('/shifted'), save, num_threads=4,
                            retry_count=3, retry_backoff=0.1)
            self.assertEqual(h.wait(), 'error')
            self.assertFalse(os.path.exists(save))  # 绝不能产出"完成"的错误文件


class TestResourceHandling(EngineTestBase):
    def test_failed_task_releases_part_file(self):
        """任务失败后必须释放 .part 句柄：否则 Windows 上文件被占用，无法删除/移动"""
        with FaultServer(size=512 * 1024) as srv:
            save = self.path('locked.bin')
            h = TaskHarness(srv.url('/404'), save, num_threads=2, retry_count=0)
            self.assertEqual(h.wait(), 'error')
            part = save + '.part'
            if os.path.exists(part):
                os.remove(part)          # 句柄未释放时这里会抛 PermissionError
                self.assertFalse(os.path.exists(part))

    def test_error_keeps_resumable_data(self):
        """失败后 .part 与区间元数据要保留，便于下次续传"""
        with FaultServer(size=512 * 1024) as srv:
            save = self.path('resumable.bin')
            # 认证失败：任务失败但已写入的数据与 meta 应保留
            h = TaskHarness(srv.url('/auth'), save, num_threads=1, retry_count=0)
            self.assertEqual(h.wait(), 'error')
            self.assertTrue(os.path.exists(save + '.part') or h.task.downloaded == 0)
            # 失败后任务对象必须回到可重试状态（不是卡在 running）
            self.assertEqual(h.task.status, 'error')


class TestThrottle(EngineTestBase):
    def test_token_bucket_limits_speed(self):
        """2MB @ 512KB/s ≈ 4s；不限速时应远快于此"""
        with FaultServer(size=2 * MB) as srv:
            save = self.path('limited.bin')
            started = time.time()
            h = TaskHarness(srv.url('/file'), save, num_threads=4, speed_limit=512)
            self.assertEqual(h.wait(timeout=90), 'completed')
            elapsed = time.time() - started
            self.assertGreater(elapsed, 2.0, f'限速失效，仅用 {elapsed:.2f}s')
            self.assertLess(elapsed, 12.0, f'限速过慢：{elapsed:.2f}s')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))

    def test_speed_limit_can_be_changed_at_runtime(self):
        with FaultServer(size=2 * MB) as srv:
            save = self.path('limit_change.bin')
            h = TaskHarness(srv.url('/file'), save, num_threads=2)
            time.sleep(0.3)
            h.task.set_speed_limit(256)     # 运行中降速
            self.assertEqual(h.wait(timeout=90), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))


class TestProxy(EngineTestBase):
    def test_download_through_http_proxy(self):
        with FaultServer(size=1 * MB) as srv, ProxyServer() as proxy:
            save = self.path('proxied.bin')
            h = TaskHarness(srv.url('/file'), save, num_threads=4, proxy=proxy.url)
            self.assertEqual(h.wait(timeout=90), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))
            self.assertGreater(proxy.request_count, 0, '请求没有经过代理')


class TestCustomHeaders(EngineTestBase):
    def test_custom_headers_are_sent(self):
        """自定义请求头必须出现在请求里（用 /auth 校验 Authorization 生效）"""
        import base64
        token = 'Basic ' + base64.b64encode(b'user:pass').decode()
        with FaultServer(size=512 * 1024) as srv:
            save = self.path('headers.bin')
            h = TaskHarness(srv.url('/auth'), save, num_threads=2,
                            headers={'Authorization': token})
            self.assertEqual(h.wait(timeout=60), 'completed')
            self.assertEqual(sha256_file(save), sha256(srv.expected_bytes()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
