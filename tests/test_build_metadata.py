# tests/test_build_metadata.py - 打包参数自检
#
# 事故背景：给构建脚本加版本资源时把选项名写成了 --windows-company-name /
# --windows-copyright，而 Nuitka 4.x 实际是 --company-name / --copyright，
# 结果打包在参数解析阶段就失败（no such option）。这个测试用 Nuitka 自己的
# --help-all 输出做白名单，把这类拼写错误挡在提交之前。
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_ALLOWED = None


def _nuitka_help_options():
    """返回 Nuitka 认可的所有长选项；Nuitka 不可用时返回 None"""
    global _ALLOWED
    if _ALLOWED is not None:
        return _ALLOWED or None
    try:
        proc = subprocess.run([sys.executable, '-m', 'nuitka', '--help-all'],
                              capture_output=True, text=True, timeout=180,
                              encoding='utf-8', errors='replace')
    except Exception:
        _ALLOWED = set()
        return None
    text = (proc.stdout or '') + (proc.stderr or '')
    if '--mode=' not in text and '--standalone' not in text:
        _ALLOWED = set()
        return None
    _ALLOWED = set(re.findall(r'--[a-zA-Z0-9][a-zA-Z0-9-]*', text))
    return _ALLOWED


def _load_module(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _flags():
    """收集两个构建脚本会传给 Nuitka 的选项"""
    out = []
    for fname in ('build_onefile.py', 'build.py'):
        path = os.path.join(ROOT, fname)
        if not os.path.exists(path):
            continue
        mod = _load_module(path, fname.replace('.py', '_probe'))
        if hasattr(mod, 'metadata_flags'):
            out.append((fname, mod.metadata_flags()))
    return out


class BuildFlagTest(unittest.TestCase):
    def setUp(self):
        self.allowed = _nuitka_help_options()
        if self.allowed is None:
            self.skipTest('Nuitka 不可用，跳过构建参数校验')

    def test_metadata_flags_are_real_nuitka_options(self):
        flagsets = _flags()
        self.assertTrue(flagsets, '没有找到任何构建脚本')
        for fname, flags in flagsets:
            for opt in re.findall(r'--[a-zA-Z0-9][a-zA-Z0-9-]*', flags):
                with self.subTest(script=fname, option=opt):
                    self.assertIn(opt, self.allowed,
                                  f'{fname} 使用了 Nuitka 不认识的选项 {opt}')

    def test_metadata_contains_expected_fields(self):
        for fname, flags in _flags():
            with self.subTest(script=fname):
                for opt in ('--product-name', '--file-version', '--product-version'):
                    self.assertIn(opt, flags, f'{fname} 缺少 {opt}')

    def test_flags_are_ascii_safe(self):
        """命令行参数保持 ASCII：Windows 代码页下中文参数会被拆错"""
        for fname, flags in _flags():
            with self.subTest(script=fname):
                self.assertTrue(all(ord(ch) < 128 for ch in flags),
                                f'{fname} 的选项里含非 ASCII 字符，可能导致命令行解析失败')

    def test_version_comes_from_version_module(self):
        from version import __version__
        expect = '.'.join(((__version__.split('.') + ['0', '0', '0', '0'])[:4]))
        for fname, flags in _flags():
            with self.subTest(script=fname):
                self.assertIn(f'--file-version={expect}', flags)


if __name__ == '__main__':
    unittest.main(verbosity=2)
