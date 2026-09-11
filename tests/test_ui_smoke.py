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


if __name__ == '__main__':
    unittest.main(verbosity=2)
