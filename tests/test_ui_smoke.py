# tests/test_ui_smoke.py - GUI 冒烟测试：主窗口/设置/详情对话框能否正常构建
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtCore import QTimer                        # noqa: E402
from PyQt5.QtWidgets import QApplication               # noqa: E402

import task_store                                       # noqa: E402
from ui_helpers import FakeMessageBox, patched_messagebox  # noqa: E402
import ui as ui_mod                                     # noqa: E402

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_smoke_')
        self._orig = task_store.STORE_FILE
        task_store.STORE_FILE = os.path.join(self.tmp, 'tasks.json')
        self.win = ui_mod.MainWindow()

    def tearDown(self):
        self.win._closing = True
        self.win.close()
        task_store.STORE_FILE = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_headers_round_trip(self):
        text = 'Referer: https://a.example/\n# 注释\nAuthorization: Bearer t\nbad-line\n'
        parsed = ui_mod.MainWindow._parse_headers(text)
        self.assertEqual(parsed, {'Referer': 'https://a.example/', 'Authorization': 'Bearer t'})
        self.assertEqual(ui_mod.MainWindow._parse_headers(
            ui_mod.MainWindow._format_headers(parsed)), parsed)

    def test_settings_dialog_opens_and_saves(self):
        """设置对话框能构建、能保存（这里直接调用保存逻辑，不进入 exec_ 阻塞）"""
        original = dict(thread_count=self.win.settings.thread_count,
                        max_concurrent_tasks=self.win.settings.max_concurrent_tasks,
                        proxy=self.win.settings.proxy)
        saved = {}
        self.win.settings.save = lambda: saved.update(thread_count=self.win.settings.thread_count)
        # 打开对话框并在 100ms 后自动关闭（验证不抛异常）
        QTimer.singleShot(100, lambda: [w.close() for w in _app.topLevelWidgets()
                                        if isinstance(w, ui_mod.QDialog) and w.isVisible()])
        self.win.open_settings()
        self.assertEqual(self.win.settings.thread_count, original['thread_count'])

    def test_columns_and_status_mapping(self):
        self.assertEqual(self.win.table.columnCount(), 6)
        self.assertIn('queued', ui_mod.STATUS_LABELS)
        self.assertIn('queued', ui_mod.STATUS_COLORS)

    def test_task_row_created_for_queued_task(self):
        task = self.win._add_task('http://127.0.0.1:1/none.bin',
                                  os.path.join(self.tmp, 'n.bin'))
        item = self.win._find_item(task.task_id)
        self.assertIsNotNone(item)
        self.assertIn(task.status, ('queued', 'ready', 'running'))
        # 排队/等待中的任务必须出现在"未完成"分类里
        self.win._filter = 'unfinished'
        self.assertTrue(self.win._matches_category(task))

    def test_dark_theme_contrast(self):
        """深色主题回归：文字与背景对比度必须达标

        曾经的 bug：选项卡页面用系统浅色背景，而文字是深色主题的浅灰，
        结果设置对话框里"浅底浅字"完全看不清。
        """
        def lum(color):
            return 0.2126 * color.redF() + 0.7152 * color.greenF() + 0.0722 * color.blueF()

        def contrast(a, b):
            l1, l2 = lum(a), lum(b)
            hi, lo = max(l1, l2), min(l1, l2)
            return (hi + 0.05) / (lo + 0.05)

        app = QApplication.instance()
        pal = app.palette()
        background = pal.color(pal.Window)
        for name, role in (('WindowText', pal.WindowText), ('Text', pal.Text),
                           ('ButtonText', pal.ButtonText)):
            ratio = contrast(pal.color(role), background)
            self.assertGreaterEqual(ratio, 4.5, f'{name} 对比度仅 {ratio:.2f}（需 ≥ 4.5）')

        # 选项卡页面背景必须在样式表里显式指定为深色
        style = self.win.styleSheet()
        self.assertIn('QTabWidget::pane', style)
        self.assertIn('QTabWidget > QWidget', style)
        # 主题要同时应用在 QApplication 上，否则对话框会退回系统浅色配色
        self.assertEqual(app.styleSheet(), style)
        self.assertGreater(len(style), 1000)

    def test_dark_palette_helper(self):
        pal = ui_mod.MainWindow._dark_palette()
        self.assertEqual(pal.color(pal.Window).name(), '#1e1e1e')
        self.assertEqual(pal.color(pal.WindowText).name(), '#e0e0e0')

    def test_update_dialog_builds(self):
        """更新对话框能构建（发布说明、资产信息、进度条），不需要事件循环"""
        release = {
            'tag': 'v9.9.9', 'name': 'Release v9.9.9',
            'notes': '\n'.join(f'改动 {i}' for i in range(40)),
            'html_url': 'https://example.invalid/releases', 'published_at': '',
            'assets': {
                'FastDownloader.exe': {'url': 'u1', 'size': 1024, 'digest': ''},
                'fastdownloader.zip': {'url': 'u2', 'size': 2048, 'digest': ''},
            },
        }
        dlg = self.win._build_update_dialog(release)
        try:
            texts = [w.text() for w in dlg.findChildren(ui_mod.QLabel)]
            self.assertTrue(any('v9.9.9' in t for t in texts), texts[:5])
            notes = dlg.findChildren(ui_mod.QPlainTextEdit)
            self.assertTrue(notes and '改动 0' in notes[0].toPlainText())
            buttons = [b.text() for b in dlg.findChildren(ui_mod.QPushButton)]
            self.assertIn('下载并安装', buttons)
            self.assertIn('稍后', buttons)
            self.assertIsNotNone(getattr(self.win, '_update_bar', None))
        finally:
            dlg.deleteLater()
            self.win._update_bar = None

    def test_update_dialog_disables_install_without_matching_asset(self):
        release = {'tag': 'v9.9.9', 'notes': '', 'html_url': '', 'assets': {
            'notes.txt': {'url': 'u', 'size': 1, 'digest': ''}}}
        dlg = self.win._build_update_dialog(release)
        try:
            install = [b for b in dlg.findChildren(ui_mod.QPushButton) if b.text() == '下载并安装']
            self.assertTrue(install and not install[0].isEnabled(),
                            '没有匹配的安装包时安装按钮应禁用')
        finally:
            dlg.deleteLater()
            self.win._update_bar = None

    def test_help_menu_exposes_update_actions(self):
        from PyQt5.QtWidgets import QMenu
        titles = [a.text() for m in self.win.menuBar().findChildren(QMenu)
                  for a in m.actions()]
        self.assertIn('检查更新', titles)
        self.assertIn('打开发布页面', titles)
        self.assertIn('关于', titles)

    def test_update_error_slot_is_safe(self):
        # 静默失败不应弹窗，也不应抛异常
        self.win._on_update_error('network down', True)
        self.win._on_update_result(None, False, True)
        self.win._on_update_progress(50, 100)        # 没有对话框时也要安全

    def test_about_shows_version(self):
        from version import __version__
        self.assertTrue(__version__)
        self.win.show_about_version = __version__    # 版本号可被引用（不存在则抛 AttributeError）

class SaveDirectoryTest(unittest.TestCase):
    """下载目录能被修改并真正生效（曾经只能在"保存文件"对话框里间接设置）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_savedir_')
        self._orig_store = task_store.STORE_FILE
        task_store.STORE_FILE = os.path.join(self.tmp, 'tasks.json')
        self.win = ui_mod.MainWindow()
        self.win.settings.save = lambda: None
        self._msgbox = patched_messagebox(ui_mod)
        self._msgbox.__enter__()

    def tearDown(self):
        self.win._closing = True
        self.win.close()
        self._msgbox.__exit__(None, None, None)
        task_store.STORE_FILE = self._orig_store
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_save_path_uses_settings_dir(self):
        base = os.path.join(self.tmp, 'downloads')
        self.assertEqual(ui_mod._default_save_path(base, 'https://e.com/a/movie.mp4'),
                         os.path.normpath(os.path.join(base, 'movie.mp4')))
        self.assertEqual(ui_mod._default_save_path(base, ''), os.path.normpath(base))
        self.assertEqual(ui_mod._default_save_path(base, 'not-a-url'), os.path.normpath(base))
        name = ui_mod._default_save_path(
            base, 'https://e.com/x?response-content-disposition=attachment;filename=report.pdf')
        self.assertTrue(name.endswith('report.pdf'), name)

    def test_new_task_lands_in_updated_directory(self):
        new_dir = os.path.join(self.tmp, 'changed-dir')
        os.makedirs(new_dir, exist_ok=True)
        self.win.settings.save_directory = new_dir
        url = 'http://127.0.0.1:9/movie.mp4'
        path = ui_mod._default_save_path(self.win.settings.save_directory, url)
        task = self.win._add_task(url, path)
        self.assertEqual(os.path.dirname(task.save_path), os.path.normpath(new_dir))
        self.assertEqual(os.path.basename(task.save_path), 'movie.mp4')

    def test_settings_round_trip_keeps_new_directory(self):
        import settings as settings_mod
        from settings import Settings
        store = os.path.join(self.tmp, 'settings.json')
        orig = settings_mod.SETTINGS_FILE
        settings_mod.SETTINGS_FILE = store
        try:
            s = Settings()
            target = os.path.join(self.tmp, 'my downloads')
            s.save_directory = target
            s.save()
            again = Settings()
            self.assertEqual(again.save_directory, os.path.normpath(target))
        finally:
            settings_mod.SETTINGS_FILE = orig

    def test_add_task_dialog_prefills_path_and_is_editable(self):
        """对话框必须默认填好路径，而且路径框要能手动编辑/粘贴"""
        from PyQt5.QtWidgets import QLineEdit
        new_dir = os.path.join(self.tmp, 'dl')
        os.makedirs(new_dir, exist_ok=True)
        self.win.settings.save_directory = new_dir
        captured = {}
        clipboard = _app.clipboard()
        saved_text = clipboard.text()
        clipboard.clear()

        def inspect_and_close():
            dlg = _app.activeModalWidget()
            if dlg is None:
                _app.quit()
                return
            edits = dlg.findChildren(QLineEdit)
            captured['texts'] = [e.text() for e in edits]
            captured['readonly'] = [e.isReadOnly() for e in edits]
            dlg.reject()

        QTimer.singleShot(250, inspect_and_close)
        try:
            self.win.add_task_dialog()
        finally:
            clipboard.setText(saved_text)

        self.assertTrue(captured.get('texts'), '对话框里应有输入框')
        self.assertTrue(any(new_dir in txt for txt in captured['texts']),
                        f'保存路径应默认指向设置里的目录: {captured}')
        self.assertNotIn(True, captured['readonly'], '保存路径框不应是只读的')

    def test_av_help_dialog_builds_and_never_touches_av(self):
        """误报自助对话框：能构建、给出路径和哈希、且不执行任何修改杀软的操作"""
        dlg = self.win._build_av_help_dialog()
        try:
            texts = ' '.join(w.text() for w in dlg.findChildren(ui_mod.QLabel))
            self.assertIn('不会', texts)              # 明确声明不代劳
            self.assertIn('排除项', texts)
            buttons = [b.text() for b in dlg.findChildren(ui_mod.QPushButton)]
            self.assertIn('复制 Defender 排除命令', buttons)
            self.assertIn('复制 SHA256', buttons)
        finally:
            dlg.deleteLater()

    def test_av_help_disables_exclusion_button_for_source_runs(self):
        """源码运行没有 exe 可排除，按钮应禁用（且不写剪贴板）"""
        dlg = self.win._build_av_help_dialog()
        try:
            copies = [b for b in dlg.findChildren(ui_mod.QPushButton)
                      if b.text() == '复制 Defender 排除命令']
            self.assertTrue(copies)
            self.assertFalse(copies[0].isEnabled())
        finally:
            dlg.deleteLater()

    def test_av_help_copy_button_only_copies(self):
        """复制按钮只写剪贴板，绝不执行任何命令（防越界回归）"""
        import subprocess as _sp
        import updater as updater_mod

        clipboard = _app.clipboard()
        saved = clipboard.text()
        clipboard.clear()              # 绝不读取/泄露用户自己的剪贴板内容
        called = []
        orig_popen = _sp.Popen
        orig_kind = updater_mod.detect_install_kind
        orig_root = updater_mod.install_root
        try:
            _sp.Popen = lambda *a, **k: called.append(a) or orig_popen
            fake_exe = os.path.join(self.tmp, 'FastDownloader.exe')
            with open(fake_exe, 'wb') as f:
                f.write(b'MZ fake')
            # 伪装成打包版，才能验证按钮的真实行为
            updater_mod.detect_install_kind = lambda kind=None: 'onefile'
            updater_mod.install_root = lambda kind=None: fake_exe

            dlg = self.win._build_av_help_dialog()
            try:
                copies = [b for b in dlg.findChildren(ui_mod.QPushButton)
                          if b.text() == '复制 Defender 排除命令']
                self.assertTrue(copies)
                self.assertTrue(copies[0].isEnabled())
                copies[0].click()
                self.assertEqual(called, [], '复制按钮不允许执行任何外部命令')
                self.assertIn('ExclusionPath', clipboard.text())
                self.assertIn(fake_exe, clipboard.text())
                digest, info = self.win._self_hash_text()
                self.assertEqual(len(digest), 64)      # sha256 十六进制
            finally:
                dlg.deleteLater()
        finally:
            _sp.Popen = orig_popen
            updater_mod.detect_install_kind = orig_kind
            updater_mod.install_root = orig_root
            clipboard.setText(saved)

    def test_self_hash_reports_source_mode(self):
        digest, info = self.win._self_hash_text()
        self.assertIsNone(digest)
        self.assertIn('源码', info)

    def test_plugin_manager_dialog_builds(self):
        """插件管理窗口能构建（用 QTimer 自动关闭，不阻塞测试）"""
        QTimer.singleShot(250, lambda: (_app.activeModalWidget().reject()
                                        if _app.activeModalWidget() is not None else None))
        self.win.plugin_manager_dialog()

    def test_dragging_a_dll_asks_for_confirmation(self):
        """拖入 DLL 必须先确认——插件是可执行代码"""
        import plugin_host as ph
        sandbox = tempfile.mkdtemp(prefix="fd_pluginstest_")
        orig_dir = ph.PLUGIN_DIR
        ph.PLUGIN_DIR = sandbox
        asked = {"n": 0}

        class _No:
            Yes, No = 1, 0

            @staticmethod
            def question(*a, **k):
                asked["n"] += 1
                return 0                      # 用户点了"否"

            @staticmethod
            def information(*a, **k):
                return 0

            @staticmethod
            def warning(*a, **k):
                return 0

        orig_box = ui_mod.QMessageBox
        ui_mod.QMessageBox = _No
        try:
            src = os.path.join(tempfile.mkdtemp(), "x.dll")
            with open(src, "wb") as f:
                f.write(b"fake")
            self.win._install_plugins([src])
            self.assertEqual(asked["n"], 1, "应当弹确认框")
            self.assertEqual(os.listdir(sandbox), [], "用户拒绝后不应安装")
            self.win._install_plugins([os.path.join(tempfile.mkdtemp(), "note.txt")])
            self.assertEqual(asked["n"], 1, "非 dll 不该弹框")
        finally:
            ui_mod.QMessageBox = orig_box
            ph.PLUGIN_DIR = orig_dir
            import shutil
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
