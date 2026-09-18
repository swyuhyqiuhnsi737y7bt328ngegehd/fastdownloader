# tests/test_plugins.py - 插件系统测试
#
# 用 plugins/examples/ 里编译好的示例 DLL 做真实的加载与钩子调用；
# 没有编译产物时自动跳过（例如在没跑过 build_examples.bat 的机器上）。
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import plugin_host                                      # noqa: E402

EXAMPLES = os.path.join(ROOT, 'plugins', 'examples', 'dist')
HAVE_EXAMPLES = all(os.path.exists(os.path.join(EXAMPLES, n))
                    for n in ('strip_tracking.dll', 'github_mirror.dll', 'task_logger.dll'))


class HostTestBase(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix='fd_plugins_')
        self._orig_dir = plugin_host.PLUGIN_DIR
        plugin_host.PLUGIN_DIR = self.sandbox

    def tearDown(self):
        plugin_host.PLUGIN_DIR = self._orig_dir
        shutil.rmtree(self.sandbox, ignore_errors=True)

    def copy_example(self, name):
        dest = os.path.join(self.sandbox, name)
        shutil.copy2(os.path.join(EXAMPLES, name), dest)
        return dest

    def make_host(self, *names):
        for n in names:
            self.copy_example(n)
        host = plugin_host.PluginHost()
        host.load_all()
        return host


class PluginHostBasics(HostTestBase):
    def test_empty_directory_loads_nothing(self):
        host = plugin_host.PluginHost()
        self.assertEqual(host.load_all(), [])
        self.assertEqual(host.transform_url('https://x/y'), ('https://x/y', ''))
        self.assertEqual(host.collect_headers('https://x/y'), {})
        host.notify('on_task_done', 'p', 1, '')            # 没有插件也不能报错

    def test_non_plugin_dll_is_rejected_but_harmless(self):
        """随便一个 DLL 不是插件：要给出原因，且不影响其它插件"""
        system_dll = r'C:\Windows\System32\version.dll'
        if not os.path.exists(system_dll):
            self.skipTest('没有可用于测试的系统 DLL')
        shutil.copy2(system_dll, os.path.join(self.sandbox, 'notaplugin.dll'))
        if HAVE_EXAMPLES:
            self.copy_example('strip_tracking.dll')
        host = plugin_host.PluginHost()
        plugins = host.load_all()
        bad = [p for p in plugins if p.filename == 'notaplugin.dll'][0]
        self.assertIsNone(bad._lib)
        self.assertTrue(bad.error)
        if HAVE_EXAMPLES:
            good = [p for p in plugins if p.filename == 'strip_tracking.dll'][0]
            self.assertIsNotNone(good._lib, '一个坏插件不该影响好插件')

    def test_install_copies_and_rejects_non_dll(self):
        src = os.path.join(ROOT, 'README.md')
        dest, err = plugin_host.install_dll(src)
        self.assertIsNone(dest)
        self.assertIn('.dll', err)
        if HAVE_EXAMPLES:
            src_dll = os.path.join(EXAMPLES, 'strip_tracking.dll')
            dest, err = plugin_host.install_dll(src_dll)
            self.assertTrue(dest and os.path.exists(dest), err)
            # 第二次不覆盖时应当拒绝
            again, err2 = plugin_host.install_dll(src_dll)
            self.assertIsNone(again)
            self.assertIn('已存在', err2)
            over, err3 = plugin_host.install_dll(src_dll, overwrite=True)
            self.assertTrue(over, err3)

    def test_plugin_dir_is_created(self):
        nested = os.path.join(self.sandbox, 'a', 'b')
        plugin_host.PLUGIN_DIR = nested
        self.assertTrue(os.path.isdir(plugin_host.plugin_dir()))


@unittest.skipUnless(HAVE_EXAMPLES, '示例插件未编译（运行 plugins/examples/build_examples.bat）')
class ExamplePluginTest(HostTestBase):
    def test_metadata_is_read(self):
        host = self.make_host('strip_tracking.dll', 'github_mirror.dll', 'task_logger.dll')
        names = sorted(p.name for p in host.plugins)
        self.assertEqual(names, ['Download History CSV', 'GitHub Mirror Rewrite',
                                 'Tracking Param Cleaner'])
        for p in host.plugins:
            self.assertTrue(p.version)
            self.assertTrue(p.description)
            self.assertIsNotNone(p._lib)
            self.assertEqual(p.error, '')

    def test_strip_tracking_rewrites_url(self):
        host = self.make_host('strip_tracking.dll')
        cases = [
            ('https://x.com/f.zip?utm_source=tg&id=7&fbclid=abc', 'https://x.com/f.zip?id=7'),
            ('https://x.com/f.zip?fbclid=abc', 'https://x.com/f.zip'),
            ('https://x.com/f.zip?utm_medium=a&utm_campaign=b', 'https://x.com/f.zip'),
            ('https://x.com/f.zip?id=7&page=2', 'https://x.com/f.zip?id=7&page=2'),
            ('https://x.com/f.zip', 'https://x.com/f.zip'),
        ]
        for src, expect in cases:
            got, reject = host.transform_url(src)
            self.assertEqual(got, expect, src)
            self.assertEqual(reject, '')

    def test_github_mirror_rewrites_download_links(self):
        host = self.make_host('github_mirror.dll')
        url = 'https://github.com/a/b/releases/download/v1/f.zip'
        got, _ = host.transform_url(url)
        self.assertNotEqual(got, url)
        self.assertIn('github.com/a/b/releases/download/v1/f.zip', got)
        # 普通站点不动
        plain = 'https://example.com/f.zip'
        self.assertEqual(host.transform_url(plain)[0], plain)
        # 幂等：已经是镜像地址就不再套一层
        self.assertEqual(host.transform_url(got)[0], got)

    def test_task_logger_writes_csv(self):
        host = self.make_host('task_logger.dll')
        host.notify('on_task_done', r'C:\tmp\a.zip', 2048, 'b' * 64)
        host.notify('on_task_done', r'C:\tmp\b.bin', 1, '')
        csv_path = os.path.join(self.sandbox, 'download_history.csv')
        self.assertTrue(os.path.exists(csv_path))
        with open(csv_path, encoding='utf-8', errors='replace') as f:
            rows = [l for l in f.read().splitlines() if l.strip()]
        self.assertEqual(len(rows), 2)
        self.assertIn('2048', rows[0])
        self.assertIn('b' * 64, rows[0])
        self.assertIn('C:\\tmp\\a.zip', rows[0])
        self.assertIn('-,C:\\tmp\\b.bin', rows[1])       # 没有哈希时用 -

    def test_disable_and_reload(self):
        host = self.make_host('strip_tracking.dll')
        url = 'https://x.com/f.zip?utm_source=tg'
        self.assertNotEqual(host.transform_url(url)[0], url)
        host.plugins[0].enabled = False
        self.assertEqual(host.transform_url(url)[0], url, '停用后不应再生效')
        host.load_all()
        self.assertNotEqual(host.transform_url(url)[0], url, '重新加载后应恢复')

    def test_unload_releases_library(self):
        host = self.make_host('strip_tracking.dll')
        host.unload_all()
        self.assertEqual(host.plugins, [])
        self.assertEqual(host.transform_url('https://x/f?utm_a=1')[0], 'https://x/f?utm_a=1')

    def test_broken_plugin_does_not_break_others(self):
        """把一个无效文件伪装成插件放进目录"""
        with open(os.path.join(self.sandbox, 'broken.dll'), 'wb') as f:
            f.write(b'not a real dll at all')
        self.copy_example('strip_tracking.dll')
        host = plugin_host.PluginHost()
        host.load_all()
        broken = [p for p in host.plugins if p.filename == 'broken.dll'][0]
        self.assertIsNone(broken._lib)
        self.assertTrue(broken.error)
        good = host.transform_url('https://x/f.zip?utm_source=tg')[0]
        self.assertEqual(good, 'https://x/f.zip')


if __name__ == '__main__':
    unittest.main(verbosity=2)
