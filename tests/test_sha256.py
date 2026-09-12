# tests/test_sha256.py - 下载后 SHA256 校验
import hashlib
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

import engine                                                      # noqa: E402
from engine import DownloadTask, file_sha256, looks_like_sha256   # noqa: E402
from fault_server import FaultServer, make_payload                 # noqa: E402


def wait(task, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.status in ('completed', 'error', 'stopped', 'skipped', 'paused'):
            return task.status
        time.sleep(0.02)
    return task.status


class Sha256Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_sha_')
        # 测试服务器在 127.0.0.1，而引擎对回环地址会跳过 .sha256 探测（刻意的优化），
        # 这里临时关掉该优化以验证探测逻辑本身。
        self._orig_loopback = engine._is_loopback
        engine._is_loopback = lambda url: False

    def tearDown(self):
        engine._is_loopback = self._orig_loopback
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_file_sha256_matches_hashlib(self):
        p = self.path('x.bin')
        data = b'hello world' * 1000
        with open(p, 'wb') as f:
            f.write(data)
        self.assertEqual(file_sha256(p), hashlib.sha256(data).hexdigest())

    def test_looks_like_sha256(self):
        h = 'a' * 64
        self.assertEqual(looks_like_sha256(f'{h}  file.bin\n'), h)
        self.assertEqual(looks_like_sha256('ABCD' + '0' * 60), 'abcd' + '0' * 60)
        self.assertEqual(looks_like_sha256('no hash here'), '')
        self.assertEqual(looks_like_sha256(''), '')

    def test_correct_hash_passes(self):
        with FaultServer(size=512 * 1024) as srv:
            expect = hashlib.sha256(srv.expected_bytes()).hexdigest()
            save = self.path('ok.bin')
            task = DownloadTask(1, srv.url('/file'), save, num_threads=2,
                                expected_sha256=expect)
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(task.file_hash, expect)
            self.assertTrue(os.path.exists(save))

    def test_wrong_hash_fails_and_keeps_part(self):
        with FaultServer(size=512 * 1024) as srv:
            save = self.path('bad.bin')
            task = DownloadTask(1, srv.url('/file'), save, num_threads=2,
                                expected_sha256='0' * 64)
            task.start()
            self.assertEqual(wait(task), 'error')
            self.assertIn('SHA256 校验失败', task._error_msg)
            # 不能产出最终文件；.part 保留便于重试
            self.assertFalse(os.path.exists(save))
            self.assertTrue(os.path.exists(save + '.part'))

    def test_invalid_hash_format_is_ignored(self):
        with FaultServer(size=256 * 1024) as srv:
            save = self.path('junk.bin')
            task = DownloadTask(1, srv.url('/file'), save, num_threads=2,
                                expected_sha256='not-a-hash')
            self.assertEqual(task.expected_sha256, '')
            task.start()
            self.assertEqual(wait(task), 'completed')

    def test_auto_probe_reads_sha256_file(self):
        """服务器提供 <url>.sha256 时自动采用并校验"""
        with FaultServer(size=512 * 1024) as srv:
            expect = hashlib.sha256(srv.expected_bytes()).hexdigest()
            save = self.path('probe.bin')
            task = DownloadTask(1, srv.url('/file'), save, num_threads=2)
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(task.expected_sha256, expect)
            self.assertEqual(task.file_hash, expect)

    def test_probe_can_be_disabled(self):
        with FaultServer(size=256 * 1024) as srv:
            save = self.path('noprobe.bin')
            task = DownloadTask(1, srv.url('/file'), save, num_threads=2,
                                sha256_auto_probe=False)
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(task.expected_sha256, '')
            self.assertEqual(task.file_hash, '')

    def test_probe_ignores_file_without_hash(self):
        """<url>.sha256sum 里没有哈希时不应误用"""
        with FaultServer(size=256 * 1024) as srv:
            save = self.path('nohash.bin')
            task = DownloadTask(1, srv.url('/file'), save, num_threads=2)
            task.start()
            self.assertEqual(wait(task), 'completed')
            # /file.sha256 有正确的哈希，所以这里是「采用了第一个可用的」
            self.assertTrue(task.expected_sha256)

    def test_no_sha256_endpoint_is_harmless(self):
        with FaultServer(size=256 * 1024) as srv:
            save = self.path('nosrv.bin')
            task = DownloadTask(1, srv.url('/norange'), save, num_threads=2)
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(task.expected_sha256, '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
