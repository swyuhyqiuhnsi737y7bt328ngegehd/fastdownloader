# tests/test_cli.py - 命令行下载器测试
import contextlib
import hashlib
import io
import json
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

import cli                                    # noqa: E402
import settings as settings_mod               # noqa: E402
from fault_server import FaultServer          # noqa: E402


class CliHelpersTest(unittest.TestCase):
    def test_parse_headers(self):
        got = cli.parse_headers(['Referer: https://a/', 'X-Test:1', 'broken'])
        self.assertEqual(got, {'Referer': 'https://a/', 'X-Test': '1'})

    def test_resolve_target(self):
        self.assertEqual(cli.resolve_target('https://e.com/a/b.zip', '', False), 'b.zip')
        out_dir = tempfile.mkdtemp()
        try:
            self.assertEqual(cli.resolve_target('https://e.com/a/b.zip', out_dir, False),
                             os.path.join(out_dir, 'b.zip'))
            self.assertEqual(cli.resolve_target('https://e.com/a/b.zip', out_dir, True),
                             os.path.join(out_dir, 'b.zip'))
            explicit = os.path.join(out_dir, 'renamed.bin')
            self.assertEqual(cli.resolve_target('https://e.com/a/b.zip', explicit, False), explicit)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_collect_urls_dedupes_and_reads_file(self):
        tmp = tempfile.mkdtemp()
        try:
            listfile = os.path.join(tmp, 'urls.txt')
            with open(listfile, 'w', encoding='utf-8') as f:
                f.write('# comment\nhttps://a/1\n\nhttps://a/2\n')
            args = cli.build_parser().parse_args(['https://a/1', '-i', listfile])
            urls = cli.collect_urls(args, cli.build_parser())
            self.assertEqual(urls, ['https://a/1', 'https://a/2'])   # 去重且保序
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cli_does_not_import_pyqt(self):
        """命令行模式不该把 GUI 框架拖进来（否则打包版启动很慢）。

        必须在独立解释器里验证：完整测试套件里其它测试早就把 PyQt5 导进来了。"""
        import subprocess
        code = (
            "import sys; sys.path.insert(0, r'%s'); import cli; "
            "print(len([m for m in sys.modules if m.startswith('PyQt5')]))" % ROOT
        )
        proc = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        self.assertEqual(proc.stdout.strip(), '0',
                         'CLI 模块不应把 PyQt5 拉进来')


class CliRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_cli_')
        self._orig_settings = settings_mod.SETTINGS_FILE
        settings_mod.SETTINGS_FILE = os.path.join(self.tmp, 'settings.json')

    def tearDown(self):
        settings_mod.SETTINGS_FILE = self._orig_settings
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_download_success(self):
        with FaultServer(size=512 * 1024) as srv:
            target = os.path.join(self.tmp, 'ok.bin')
            code, out, err = self._run([srv.url('/file'), '-o', target, '-t', '2', '--no-progress'])
            self.assertEqual(code, cli.EXIT_OK)
            self.assertTrue(os.path.exists(target))
            self.assertEqual(os.path.getsize(target), 512 * 1024)

    def test_json_output(self):
        with FaultServer(size=256 * 1024) as srv:
            target = os.path.join(self.tmp, 'j.bin')
            code, out, _ = self._run([srv.url('/file'), '-o', target, '--json', '--hash'])
            self.assertEqual(code, cli.EXIT_OK)
            payload = json.loads(out)
            self.assertTrue(payload['ok'])
            self.assertEqual(len(payload['results']), 1)
            entry = payload['results'][0]
            self.assertEqual(entry['status'], 'completed')
            self.assertEqual(entry['bytes'], 256 * 1024)
            self.assertEqual(len(entry['sha256']), 64)
            self.assertEqual(entry['sha256'], hashlib.sha256(srv.expected_bytes()).hexdigest())

    def test_wrong_sha256_fails_with_exit_code_1(self):
        with FaultServer(size=256 * 1024) as srv:
            target = os.path.join(self.tmp, 'bad.bin')
            code, _, err = self._run([srv.url('/file'), '-o', target,
                                      '--sha256', '0' * 64, '--no-progress'])
            self.assertEqual(code, cli.EXIT_FAILED)
            self.assertIn('SHA256', err)
            self.assertFalse(os.path.exists(target))
            self.assertTrue(os.path.exists(target + '.part'))

    def test_correct_sha256_passes_and_is_reported(self):
        with FaultServer(size=256 * 1024) as srv:
            expect = hashlib.sha256(srv.expected_bytes()).hexdigest()
            target = os.path.join(self.tmp, 'good.bin')
            code, out, _ = self._run([srv.url('/file'), '-o', target,
                                      '--sha256', expect, '--json'])
            self.assertEqual(code, cli.EXIT_OK)
            self.assertEqual(json.loads(out)['results'][0]['sha256'], expect)

    def test_multiple_urls_into_directory(self):
        with FaultServer(size=256 * 1024) as srv:
            code, out, _ = self._run([srv.url('/file'), srv.url('/norange'),
                                      '-o', self.tmp, '--json', '-j', '2'])
            self.assertEqual(code, cli.EXIT_OK)
            results = json.loads(out)['results']
            self.assertEqual(len(results), 2)
            self.assertTrue(all(r['status'] == 'completed' for r in results))
            self.assertTrue(os.path.exists(os.path.join(self.tmp, 'file.bin')))
            self.assertTrue(os.path.exists(os.path.join(self.tmp, 'norange.bin')))

    def test_no_urls_returns_usage(self):
        code, out, _ = self._run([])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn('usage', out.lower())

    def test_sha256_with_multiple_urls_is_rejected(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(['http://a/1', 'http://a/2', '--sha256', '0' * 64])

    def test_input_file(self):
        with FaultServer(size=256 * 1024) as srv:
            listfile = os.path.join(self.tmp, 'list.txt')
            with open(listfile, 'w', encoding='utf-8') as f:
                f.write(f'# 注释行\n{srv.url("/file")}\n')
            code, out, _ = self._run(['-i', listfile, '-o', self.tmp, '--json'])
            self.assertEqual(code, cli.EXIT_OK)
            self.assertEqual(len(json.loads(out)['results']), 1)

    def test_conflict_policy_rename(self):
        """CLI 默认覆盖，但 --conflict rename 时不能动已有文件"""
        with FaultServer(size=256 * 1024) as srv:
            target = os.path.join(self.tmp, 'exists.bin')
            with open(target, 'wb') as f:
                f.write(b'KEEP')
            code, _, _ = self._run([srv.url('/file'), '-o', target,
                                    '--conflict', 'rename', '--no-progress'])
            self.assertEqual(code, cli.EXIT_OK)
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'KEEP')

    def test_failed_download_reports_error(self):
        with FaultServer(size=64 * 1024) as srv:
            code, _, err = self._run([srv.url('/404'), '-o', os.path.join(self.tmp, 'x.bin'),
                                      '--no-progress', '--retries', '0'])
            self.assertEqual(code, cli.EXIT_FAILED)
            self.assertTrue(err.strip())


if __name__ == '__main__':
    unittest.main(verbosity=2)
