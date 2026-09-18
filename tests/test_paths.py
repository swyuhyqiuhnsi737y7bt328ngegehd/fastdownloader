# tests/test_paths.py - 数据目录解析（打包/源码）的回归测试
#
# 背景：paths.get_data_dir() 原来只认 sys.frozen，而 **Nuitka 不设置 sys.frozen**。
# 结果打包版永远回退到 _HERE，也就是 onefile 的临时解压目录 —— 设置改了不保存、
# 拖进去的插件退出即消失。这些测试锁住正确的判定方式。
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_KEYS = ('LOCALAPPDATA', 'APPDATA', 'NUITKA_ONEFILE_BINARY', 'NUITKA_ONEFILE_PARENT')


class SourceModeTest(unittest.TestCase):
    """源码运行：数据就放在项目目录（开发和测试都依赖这一点）"""

    def test_not_detected_as_packaged(self):
        self.assertFalse(paths.is_packaged())

    def test_data_dir_is_project_root(self):
        self.assertEqual(os.path.normcase(paths.get_data_dir()),
                         os.path.normcase(PROJECT_ROOT))

    def test_data_file_joins_the_name(self):
        self.assertEqual(paths.data_file('a.json'),
                         os.path.join(paths.get_data_dir(), 'a.json'))

    def test_source_run_has_none_of_the_packaging_markers(self):
        """文档化前提：源码运行时三个标记都不存在"""
        self.assertIsNone(globals().get('__compiled__'))
        self.assertFalse(getattr(sys, 'frozen', False))
        self.assertFalse(getattr(sys, '_MEIPASS', None))


class PackagedModeTest(unittest.TestCase):
    """打包运行：必须落到用户目录，绝不能是会被删掉的解压目录"""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        self._argv0 = list(sys.argv)
        self._tmp = tempfile.mkdtemp(prefix='fd_paths_')
        patcher = mock.patch.object(paths, 'is_packaged', lambda: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.argv[:] = self._argv0
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _clear_user_env(self):
        os.environ.pop('LOCALAPPDATA', None)
        os.environ.pop('APPDATA', None)

    def test_uses_localappdata_when_present(self):
        os.environ['LOCALAPPDATA'] = self._tmp
        os.environ.pop('APPDATA', None)
        expected = os.path.join(self._tmp, paths.APP_DIR_NAME)
        self.assertEqual(paths.get_data_dir(), expected)
        self.assertTrue(os.path.isdir(expected), '数据目录应当被创建出来')

    def test_falls_back_to_appdata(self):
        self._clear_user_env()
        os.environ['APPDATA'] = self._tmp
        self.assertEqual(paths.get_data_dir(),
                         os.path.join(self._tmp, paths.APP_DIR_NAME))

    def test_asks_windows_when_both_env_vars_are_missing(self):
        """环境变量缺失时不能直接放弃（实测某些 shell 里就是空的）"""
        self._clear_user_env()
        with mock.patch.object(paths, '_user_data_base', lambda: self._tmp):
            self.assertEqual(paths.get_data_dir(),
                             os.path.join(self._tmp, paths.APP_DIR_NAME))

    def test_never_falls_back_to_the_onefile_temp_dir(self):
        """核心回归：拿不到用户目录时，宁可落在 exe 旁边，也不能用 _HERE。

        onefile 的 _HERE 是 %TEMP%\\onefile_XXXX，退出即删。
        """
        self._clear_user_env()
        fake_exe = os.path.join(self._tmp, 'FastDownloader.exe')
        sys.argv[:] = [fake_exe]
        with mock.patch.object(paths, '_user_data_base', lambda: None):
            result = paths.get_data_dir()
        self.assertEqual(os.path.normcase(result), os.path.normcase(self._tmp))
        self.assertNotEqual(os.path.normcase(result), os.path.normcase(paths._HERE))

    def test_data_file_lands_in_the_user_dir(self):
        os.environ['LOCALAPPDATA'] = self._tmp
        self.assertTrue(paths.data_file('plugins').startswith(self._tmp))


class PackagingMarkerTest(unittest.TestCase):
    """判定依据本身：Nuitka 注入的 __compiled__ 必须被认出来"""

    def test_nuitka_marker_is_recognised(self):
        self.assertFalse(paths.is_packaged())
        paths.__compiled__ = object()          # 模拟 Nuitka 编译产物
        try:
            self.assertTrue(paths.is_packaged())
        finally:
            del paths.__compiled__

    def test_frozen_flag_is_recognised(self):
        with mock.patch.object(sys, 'frozen', True, create=True):
            self.assertTrue(paths.is_packaged())

    def test_meipass_is_recognised(self):
        with mock.patch.object(sys, '_MEIPASS', self._tmp_dir(), create=True):
            self.assertTrue(paths.is_packaged())

    def test_nuitka_onefile_env_marker_is_recognised(self):
        os.environ['NUITKA_ONEFILE_PARENT'] = '1234'
        try:
            self.assertTrue(paths.is_packaged())
        finally:
            os.environ.pop('NUITKA_ONEFILE_PARENT', None)

    @staticmethod
    def _tmp_dir():
        d = tempfile.mkdtemp(prefix='fd_meipass_')
        unittest.TestCase().addCleanup(shutil.rmtree, d, True)
        return d


class Win32FallbackTest(unittest.TestCase):
    """不依赖环境变量也能问到用户目录（Windows 上）"""

    @unittest.skipUnless(os.name == 'nt', '仅 Windows')
    def test_shell_api_returns_a_real_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LOCALAPPDATA', None)
            os.environ.pop('APPDATA', None)
            base = paths._user_data_base()
        self.assertTrue(base, '应当通过 SHGetFolderPathW 拿到用户目录')
        self.assertTrue(os.path.isdir(base))


if __name__ == '__main__':
    unittest.main()
