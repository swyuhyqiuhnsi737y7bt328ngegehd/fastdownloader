# tests/test_build_guard.py - 构建前的产物占用自检
#
# 背景：构建脚本以前用 shutil.rmtree(ignore_errors=True) 清理 dist，删不掉也不报错。
# 程序还开着的时候 exe 被锁住，旧产物就留在 dist 里；等二十多分钟编译结束，
# 最后一步替换 exe 才抛 PermissionError —— 用户看到的是"跑了半天没有打包结果"。
# check_output_available() 把这件事提前到构建开始之前。
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_env  # noqa: E402


def _lock_file(handle):
    """给一个已打开的二进制文件加独占锁（仅 Windows）"""
    import msvcrt
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_file(handle):
    import msvcrt
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


class LockedReasonTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_guard_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = os.path.join(self.tmp, 'FastDownloader.exe')
        with open(self.target, 'wb') as f:
            f.write(b'fake exe payload')

    def test_missing_path_is_not_a_problem(self):
        self.assertIsNone(build_env.locked_reason(os.path.join(self.tmp, 'nope.exe')))

    def test_free_file_is_replaceable(self):
        self.assertIsNone(build_env.locked_reason(self.target))

    def test_directory_can_be_checked_too(self):
        sub = os.path.join(self.tmp, 'main.dist')
        os.makedirs(sub)
        self.assertIsNone(build_env.locked_reason(sub))

    @unittest.skipUnless(os.name == 'nt', '文件独占锁是 Windows 行为')
    def test_detects_an_exclusively_locked_file(self):
        with open(self.target, 'r+b') as handle:
            _lock_file(handle)
            try:
                reason = build_env.locked_reason(self.target)
                self.assertIsNotNone(reason, '被锁住的产物必须被发现')
                self.assertIn('FastDownloader.exe', reason)
            finally:
                _unlock_file(handle)

    @unittest.skipUnless(os.name == 'nt', '文件独占锁是 Windows 行为')
    def test_check_does_not_destroy_the_file(self):
        """检查用的是"改名再改回来"，失败时也必须还原，不能把产物弄丢"""
        with open(self.target, 'r+b') as handle:
            _lock_file(handle)
            try:
                build_env.locked_reason(self.target)
            finally:
                _unlock_file(handle)
        self.assertTrue(os.path.exists(self.target))
        self.assertEqual(os.listdir(self.tmp), ['FastDownloader.exe'])

    @unittest.skipUnless(os.name == 'nt', '文件独占锁是 Windows 行为')
    def test_available_again_after_unlock(self):
        with open(self.target, 'r+b') as handle:
            _lock_file(handle)
            locked = build_env.locked_reason(self.target)
            _unlock_file(handle)
        self.assertIsNotNone(locked)
        self.assertIsNone(build_env.locked_reason(self.target))


class RunningProcessesTest(unittest.TestCase):

    @unittest.skipUnless(os.name == 'nt', 'tasklist 只在 Windows 上有')
    def test_finds_the_current_interpreter(self):
        found = build_env.running_processes(['python.exe'])
        self.assertTrue(found, '至少应该看到正在跑测试的这个 python 进程')
        self.assertTrue(all('PID' in item for item in found), found)

    def test_unknown_name_finds_nothing(self):
        self.assertEqual(build_env.running_processes(['definitely-not-running-xyz.exe']), [])


class CheckOutputAvailableTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_out_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.target = os.path.join(self.tmp, 'FastDownloader.exe')
        with open(self.target, 'wb') as f:
            f.write(b'x')

    def _run_quietly(self, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = build_env.check_output_available(*args, **kwargs)
        return result, buf.getvalue()

    def test_free_output_passes(self):
        ok, _ = self._run_quietly([self.target], ['definitely-not-running-xyz.exe'])
        self.assertTrue(ok)

    def test_reports_a_running_program(self):
        with mock.patch.object(build_env, 'running_processes',
                               lambda names: ['FastDownloader.exe (PID 4242)']):
            ok, printed = self._run_quietly([self.target], ['FastDownloader.exe'])
        self.assertFalse(ok, '程序还开着时不应该开始构建')
        self.assertIn('PID 4242', printed)
        self.assertIn('先关掉', printed)

    @unittest.skipUnless(os.name == 'nt', '文件独占锁是 Windows 行为')
    def test_reports_a_locked_output(self):
        with open(self.target, 'r+b') as handle:
            _lock_file(handle)
            try:
                ok, printed = self._run_quietly([self.target],
                                                ['definitely-not-running-xyz.exe'])
            finally:
                _unlock_file(handle)
        self.assertFalse(ok)
        self.assertIn('无法替换', printed)


if __name__ == '__main__':
    unittest.main()
