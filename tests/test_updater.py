# tests/test_updater.py - 更新检查 / 下载校验 / 解压 / 替换脚本 测试
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

import updater                                    # noqa: E402
from update_server import UpdateServer            # noqa: E402
from version import __version__                   # noqa: E402


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version('v1.2.3'), (1, 2, 3))
        self.assertEqual(updater.parse_version('1.10.0'), (1, 10, 0))
        self.assertEqual(updater.parse_version('v2.0.0-beta.1'), (2, 0, 0))
        self.assertEqual(updater.parse_version(''), ())
        self.assertEqual(updater.parse_version('garbage'), ())

    def test_is_newer(self):
        self.assertTrue(updater.is_newer('v1.2.0', '1.1.0'))
        self.assertFalse(updater.is_newer('v1.1.0', '1.1.0'))
        self.assertFalse(updater.is_newer('v1.0.9', '1.1.0'))
        # 数字段比较而不是字符串比较：1.10 > 1.9
        self.assertTrue(updater.is_newer('v1.10.0', '1.9.0'))
        self.assertFalse(updater.is_newer('v1.9.0', '1.10.0'))
        # 段数不同按 0 补齐
        self.assertTrue(updater.is_newer('v1.2', '1.1.9'))
        self.assertFalse(updater.is_newer('v1.2.0', '1.2'))
        # 无法解析时不算更新
        self.assertFalse(updater.is_newer('', '1.0.0'))
        self.assertFalse(updater.is_newer('not-a-version', '1.0.0'))


class InstallKindTest(unittest.TestCase):
    def test_source_run_is_not_self_updatable(self):
        self.assertEqual(updater.detect_install_kind(), 'source')
        ok, reason = updater.can_self_update('source')
        self.assertFalse(ok)
        self.assertIn('源码', reason)

    def test_onefile_and_standalone_detection(self):
        fake = type('Compiled', (), {'onefile': True, 'standalone': False})()
        orig = updater.compiled_info
        try:
            updater.compiled_info = lambda: fake
            self.assertEqual(updater.detect_install_kind(), 'onefile')
            fake2 = type('Compiled', (), {'onefile': False, 'standalone': True})()
            updater.compiled_info = lambda: fake2
            self.assertEqual(updater.detect_install_kind(), 'standalone')
        finally:
            updater.compiled_info = orig

    def test_pick_asset_matches_install_kind(self):
        release = {'assets': {
            'FastDownloader.exe': {'url': 'a', 'size': 1, 'digest': ''},
            'fastdownloader.zip': {'url': 'b', 'size': 2, 'digest': ''},
        }}
        self.assertEqual(updater.pick_asset(release, 'onefile')[0], 'FastDownloader.exe')
        self.assertEqual(updater.pick_asset(release, 'standalone')[0], 'fastdownloader.zip')
        self.assertEqual(updater.pick_asset(release, 'source'), (None, None))

    def test_pick_asset_falls_back_to_extension(self):
        release = {'assets': {'release-1.2.3-win.zip': {'url': 'x', 'size': 1, 'digest': ''}}}
        self.assertEqual(updater.pick_asset(release, 'standalone')[0], 'release-1.2.3-win.zip')

    def test_pick_asset_missing(self):
        release = {'assets': {'notes.txt': {'url': 'x', 'size': 1, 'digest': ''}}}
        self.assertEqual(updater.pick_asset(release, 'onefile'), (None, None))


class UpdateFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_update_test_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_finds_new_version(self):
        with UpdateServer(tag='v9.9.9') as srv:
            release, has_update = updater.check_for_update('1.0.0', api_url=srv.api_url)
            self.assertTrue(has_update)
            self.assertEqual(release['tag'], 'v9.9.9')
            self.assertIn('FastDownloader.exe', release['assets'])
            self.assertIn('fastdownloader.zip', release['assets'])
            self.assertTrue(release['notes'])

    def test_check_reports_up_to_date(self):
        with UpdateServer(tag='v1.0.0') as srv:
            release, has_update = updater.check_for_update('1.1.0', api_url=srv.api_url)
            self.assertFalse(has_update)

    def test_api_failure_raises(self):
        with UpdateServer() as srv:
            srv._httpd.state['fail_api'] = True
            with self.assertRaises(Exception):
                updater.fetch_latest(api_url=srv.api_url)

    def test_prepare_onefile_downloads_and_verifies(self):
        with UpdateServer() as srv:
            release, _ = updater.check_for_update('1.0.0', api_url=srv.api_url)
            prepared = updater.prepare_update(release, kind='onefile',
                                              work_dir=os.path.join(self.tmp, 'work'))
            self.assertEqual(prepared['asset'], 'FastDownloader.exe')
            payload_file = os.path.join(prepared['payload'], 'FastDownloader.exe')
            self.assertTrue(os.path.exists(payload_file))
            with open(payload_file, 'rb') as f:
                self.assertEqual(f.read(), srv.exe_bytes)

    def test_prepare_standalone_extracts_zip(self):
        with UpdateServer() as srv:
            release, _ = updater.check_for_update('1.0.0', api_url=srv.api_url)
            prepared = updater.prepare_update(release, kind='standalone',
                                              work_dir=os.path.join(self.tmp, 'work'))
            payload = prepared['payload']
            self.assertTrue(os.path.exists(os.path.join(payload, 'main.exe')))
            self.assertTrue(os.path.exists(os.path.join(payload, 'python312.dll')))
            self.assertTrue(os.path.exists(os.path.join(payload, 'lib', 'extra.pyd')))
            # 下载的 zip 用完应删掉，只留解压结果
            self.assertFalse(any(f.endswith('.zip') for f in os.listdir(prepared['work_dir'])))

    def test_bad_digest_is_rejected(self):
        with UpdateServer() as srv:
            release, _ = updater.check_for_update('1.0.0', api_url=srv.api_url)
            srv.corrupt_digest()
            release, _ = updater.check_for_update('1.0.0', api_url=srv.api_url)
            with self.assertRaises(ValueError) as ctx:
                updater.prepare_update(release, kind='onefile',
                                       work_dir=os.path.join(self.tmp, 'work'))
            self.assertIn('校验失败', str(ctx.exception))
            # 校验失败的文件必须被丢弃
            self.assertFalse(os.path.exists(
                os.path.join(self.tmp, 'work', 'payload', 'FastDownloader.exe')))

    def test_progress_is_reported(self):
        with UpdateServer() as srv:
            release, _ = updater.check_for_update('1.0.0', api_url=srv.api_url)
            seen = []
            updater.prepare_update(release, kind='onefile',
                                   work_dir=os.path.join(self.tmp, 'work'),
                                   progress=lambda done, total: seen.append((done, total)))
            self.assertTrue(seen)
            self.assertEqual(seen[-1][0], seen[-1][1])   # 最后一次是下完的总量

    def test_zip_slip_is_rejected(self):
        files = {'main.exe': b'MZ', '../../evil.txt': b'pwned'}
        with UpdateServer(zip_files=files) as srv:
            release, _ = updater.check_for_update('1.0.0', api_url=srv.api_url)
            with self.assertRaises(ValueError) as ctx:
                updater.prepare_update(release, kind='standalone',
                                       work_dir=os.path.join(self.tmp, 'work'))
            self.assertIn('非法路径', str(ctx.exception))


class ApplyScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_apply_test_')
        self.payload = os.path.join(self.tmp, 'payload')
        os.makedirs(self.payload)
        with open(os.path.join(self.payload, 'main.exe'), 'wb') as f:
            f.write(b'MZ')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, path):
        with open(path, 'r', encoding='mbcs', errors='replace') as f:
            return f.read()

    def test_onefile_script_replaces_and_restarts(self):
        exe = os.path.join(self.tmp, 'FastDownloader.exe')
        script = updater.build_update_script(self.payload, kind='onefile', target=exe)
        self.assertTrue(os.path.exists(script))
        text = self._read(script)
        self.assertIn('PAYLOAD=', text)
        self.assertIn(exe, text)
        self.assertIn('copy /y', text)
        self.assertIn('rmdir /s /q', text)          # 清理临时文件
        self.assertIn('start ""', text)             # 重启新版本
        self.assertIn(':waitloop', text)            # 等本进程退出
        self.assertNotIn('xcopy', text)

    def test_standalone_script_copies_tree(self):
        target = os.path.join(self.tmp, 'install')
        os.makedirs(target)
        script = updater.build_update_script(self.payload, kind='standalone', target=target)
        text = self._read(script)
        self.assertIn('xcopy /y /e /i /q', text)
        self.assertIn(os.path.join(target, 'main.exe'), text)   # 重启的是 main.exe
        self.assertIn('rmdir /s /q', text)

    def test_script_can_skip_restart(self):
        exe = os.path.join(self.tmp, 'FastDownloader.exe')
        script = updater.build_update_script(self.payload, kind='onefile',
                                             target=exe, restart=False)
        text = self._read(script)
        self.assertNotIn('start "" "%RESTART%"', text)
        self.assertIn('no restart', text)

    def test_script_is_written_with_crlf(self):
        exe = os.path.join(self.tmp, 'FastDownloader.exe')
        script = updater.build_update_script(self.payload, kind='onefile', target=exe)
        with open(script, 'rb') as f:
            data = f.read()
        self.assertIn(b'\r\n', data)
        self.assertNotIn(b'\n\n\n', data.replace(b'\r\n', b'\n').replace(b'\n\n', b'\n'))

    def test_apply_payload_copies_over_install_dir(self):
        """模拟脚本里的复制步骤：安装目录被新文件覆盖，旧的多余文件仍在（xcopy 不删）"""
        install = os.path.join(self.tmp, 'install')
        os.makedirs(os.path.join(install, 'lib'))
        with open(os.path.join(install, 'main.exe'), 'wb') as f:
            f.write(b'OLD')
        with open(os.path.join(install, 'python312.dll'), 'wb') as f:
            f.write(b'old-dll')
        with open(os.path.join(install, 'lib', 'extra.pyd'), 'wb') as f:
            f.write(b'old-pyd')
        # 模拟 xcopy /y /e /i /q payload install
        shutil.copytree(self.payload, install, dirs_exist_ok=True)
        with open(os.path.join(install, 'python312.dll'), 'rb') as f:
            self.assertEqual(f.read(), b'old-dll')   # 未被覆盖的保持不变


if __name__ == '__main__':
    unittest.main(verbosity=2)
