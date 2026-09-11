# tests/test_conflict_disk.py - 文件名冲突策略与磁盘空间保护测试
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

from engine import DownloadTask, DiskFullError, _unique_path   # noqa: E402
from fault_server import FaultServer, make_payload             # noqa: E402

MB = 1024 * 1024
PAYLOAD_SIZE = 512 * 1024


def wait(task, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.status in ('completed', 'error', 'stopped', 'skipped', 'paused'):
            return task.status
        time.sleep(0.02)
    return task.status


class ConflictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_conflict_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_unique_path_sequence(self):
        target = self.path('movie.mp4')
        self.assertEqual(_unique_path(target), target)      # 不存在 -> 原名
        open(target, 'wb').close()
        second = _unique_path(target)
        self.assertTrue(second.endswith('movie (1).mp4'), second)
        open(second, 'wb').close()
        third = _unique_path(target)
        self.assertTrue(third.endswith('movie (2).mp4'), third)
        # .part 也算占用，避免与未完成的下载撞名
        part_only = self.path('other.bin')
        open(part_only + '.part', 'wb').close()
        self.assertTrue(_unique_path(part_only).endswith('other (1).bin'))

    def test_rename_policy_keeps_existing_file(self):
        with FaultServer(size=PAYLOAD_SIZE) as srv:
            target = self.path('dup.bin')
            with open(target, 'wb') as f:
                f.write(b'ORIGINAL')
            task = DownloadTask(1, srv.url('/file'), target,
                                num_threads=2, conflict_policy='rename')
            task.start()
            self.assertEqual(wait(task), 'completed')
            # 原文件必须保留，内容不动
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'ORIGINAL')
            # 新文件是 name (1).bin 且内容正确
            new_path = task.save_path
            self.assertNotEqual(new_path, target)
            self.assertTrue(os.path.exists(new_path), new_path)
            with open(new_path, 'rb') as f:
                self.assertEqual(f.read(), srv.expected_bytes())

    def test_overwrite_policy_replaces_file(self):
        with FaultServer(size=PAYLOAD_SIZE) as srv:
            target = self.path('over.bin')
            with open(target, 'wb') as f:
                f.write(b'OLD')
            task = DownloadTask(1, srv.url('/file'), target,
                                num_threads=2, conflict_policy='overwrite')
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(task.save_path, target)
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), srv.expected_bytes())

    def test_skip_policy_marks_skipped(self):
        with FaultServer(size=PAYLOAD_SIZE) as srv:
            target = self.path('skip.bin')
            with open(target, 'wb') as f:
                f.write(b'KEEP')
            events = []
            task = DownloadTask(1, srv.url('/file'), target,
                                num_threads=2, conflict_policy='skip')
            task.set_callback(lambda tid, ev, d=None: events.append((ev, d)))
            task.start()
            self.assertEqual(wait(task), 'skipped')
            self.assertFalse(os.path.exists(target + '.part'))
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'KEEP')
            self.assertIn('skipped', [e for e, _ in events])

    def test_existing_part_keeps_resume_path(self):
        """有 .part 说明是续传，不能因为目标文件存在就改名（否则丢掉进度）"""
        with FaultServer(size=PAYLOAD_SIZE) as srv:
            target = self.path('resume.bin')
            with open(target, 'wb') as f:
                f.write(b'PARTIAL-TARGET')
            with open(target + '.part', 'wb') as f:
                f.write(b'x' * 128)          # 假装有未完成的下载
            task = DownloadTask(1, srv.url('/file'), target,
                                num_threads=2, conflict_policy='rename')
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertEqual(task.save_path, target, '续传不应改路径')

    def test_legacy_overwrite_flag_does_not_override_policy(self):
        """旧接口的 overwrite 默认 True，不能悄悄把用户选的策略改成覆盖"""
        with FaultServer(size=PAYLOAD_SIZE) as srv:
            target = self.path('legacy.bin')
            with open(target, 'wb') as f:
                f.write(b'OLD')
            task = DownloadTask(1, srv.url('/file'), target, num_threads=2,
                                overwrite=True, conflict_policy='rename')
            task.start()
            self.assertEqual(wait(task), 'completed')
            self.assertNotEqual(task.save_path, target, 'overwrite 不应覆盖策略选择')
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'OLD')


class DiskSpaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_disk_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_precheck_blocks_when_space_is_short(self):
        with FaultServer(size=1 * MB) as srv:
            task = DownloadTask(1, srv.url('/file'), self.path('nospace.bin'), num_threads=2)
            task._free_space = lambda: 4 * 1024        # 只剩 4KB
            task.start()
            self.assertEqual(task.status, 'error')
            self.assertIn('磁盘空间不足', task._error_msg)
            self.assertFalse(os.path.exists(task.save_path + '.part'))

    def test_precheck_allows_when_space_is_plenty(self):
        with FaultServer(size=1 * MB) as srv:
            task = DownloadTask(1, srv.url('/file'), self.path('ok.bin'), num_threads=2)
            task._free_space = lambda: 10 * 1024 * MB
            task.start()
            self.assertEqual(wait(task), 'completed')

    def test_precheck_can_be_disabled(self):
        with FaultServer(size=1 * MB) as srv:
            task = DownloadTask(1, srv.url('/file'), self.path('noc.bin'),
                                num_threads=2, check_disk_space=False)
            task._free_space = lambda: 1
            task.start()
            self.assertEqual(wait(task), 'completed')

    def test_resume_only_requires_remaining_bytes(self):
        """续传时按剩余字节判断，不能按整文件大小误报"""
        with FaultServer(size=2 * MB) as srv:
            target = self.path('resume_space.bin')
            task = DownloadTask(1, srv.url('/file'), target, num_threads=1,
                                check_disk_space=True, min_free_mb=0)
            # 可用空间略大于整文件，续传只需要补空洞
            task._free_space = lambda: 2 * MB + 64 * 1024
            task.start()
            self.assertEqual(wait(task, timeout=90), 'completed')

    def test_disk_full_error_is_not_retried(self):
        task = DownloadTask(1, 'http://127.0.0.1:1/x.bin', self.path('x.bin'), retry_count=5)
        self.assertFalse(task._should_retry(DiskFullError('disk full')))
        self.assertTrue(task._should_retry(ConnectionResetError('reset')))

    def test_runtime_low_space_pauses_download(self):
        """下载过程中空间掉到安全线以下：自动暂停并保留 .part"""
        with FaultServer(size=2 * MB, rate=0.05) as srv:
            target = self.path('lowspace.bin')
            task = DownloadTask(1, srv.url('/file'), target, num_threads=4,
                                min_free_mb=1000, check_disk_space=False)
            seq = {'n': 0}

            def fake_free():
                seq['n'] += 1
                return 100 * 1024 * MB if seq['n'] == 1 else 1024   # 之后只剩 1KB

            task._free_space = fake_free
            task.space_check_interval = 0.3      # 测试里加快检查
            task.start()
            deadline = time.time() + 30
            while time.time() < deadline and task.status == 'running':
                time.sleep(0.1)
            self.assertEqual(task.status, 'paused', f'应自动暂停，实际 {task.status}')
            self.assertIn('磁盘剩余空间不足', task._error_msg)
            self.assertTrue(os.path.exists(target + '.part'), '.part 必须保留以便续传')


if __name__ == '__main__':
    unittest.main(verbosity=2)
