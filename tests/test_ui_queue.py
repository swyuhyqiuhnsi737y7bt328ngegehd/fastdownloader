# tests/test_ui_queue.py - 队列调度与任务持久化测试（需要 PyQt5）
#
# 运行：  python -m unittest discover -s tests -v
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtWidgets import QApplication          # noqa: E402

import task_store                                  # noqa: E402
from fault_server import FaultServer               # noqa: E402
import ui as ui_mod                                # noqa: E402
from ui_helpers import FakeMessageBox as _FakeMessageBox  # noqa: E402

MB = 1024 * 1024
_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


class _FakeMessageBox:
    """替身：测试期间不弹任何对话框（不能靠 _closing，它会顺带停掉调度器）"""
    Yes, No = 1, 0

    @staticmethod
    def critical(*a, **k):
        return 0

    @staticmethod
    def information(*a, **k):
        return 0

    @staticmethod
    def warning(*a, **k):
        return 0

    @staticmethod
    def question(*a, **k):
        return 0

    @staticmethod
    def about(*a, **k):
        return 0


class QueueAndPersistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fd_ui_')
        self._orig_store = task_store.STORE_FILE
        task_store.STORE_FILE = os.path.join(self.tmp, 'tasks.json')
        self.win = ui_mod.MainWindow()
        self.win.settings.max_concurrent_tasks = 2
        self.win.settings.thread_count = 2
        self.win.settings.save = lambda: None      # 不写真实配置文件
        ui_mod.QMessageBox = _FakeMessageBox       # 不弹任何对话框

    def tearDown(self):
        self.win._closing = True   # 关闭期间别再调度/弹窗
        for task in list(self.win.tasks.values()):
            try:
                task.stop()
            except Exception:
                pass
        self.win.close()
        task_store.STORE_FILE = self._orig_store
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, url, name, priority=0):
        return self.win._add_task(url, os.path.join(self.tmp, name), priority=priority)

    def test_queue_respects_concurrency_limit(self):
        with FaultServer(size=2 * MB, rate=0.3) as srv:
            tasks = [self._add(srv.url('/file'), f'q{i}.bin') for i in range(5)]
            time.sleep(0.4)
            busy = {id(t) for t in tasks
                    if t.status in ('running', 'ready') or getattr(t, '_dispatching', False)}
            queued = [t for t in tasks if t.status == 'queued']
            self.assertLessEqual(len(busy), 2, f'并发超限: {len(busy)} 个任务在跑')
            self.assertGreaterEqual(len(queued), 3, f'应有任务排队, got {len(queued)}')
            # 全部完成后队列应被排空（测试里手动驱动调度器，因为没有事件循环）
            deadline = time.time() + 120
            while time.time() < deadline:
                self.win._schedule()
                if all(t.status == 'completed' for t in tasks):
                    break
                time.sleep(0.2)
            self.assertTrue(all(t.status == 'completed' for t in tasks),
                            f'队列未排空: {[t.status for t in tasks]}')

    def test_priority_orders_the_queue(self):
        """并发已满时高优先级任务应在下一个空位插队执行"""
        with FaultServer(size=1 * MB, rate=0.5) as srv:
            self.win.settings.max_concurrent_tasks = 1
            first = self._add(srv.url('/file'), 'p1.bin')
            second = self._add(srv.url('/file'), 'p2.bin')
            third = self._add(srv.url('/file'), 'p3.bin')
            time.sleep(0.3)
            self.assertIn(first.status, ('running', 'ready', 'completed'))
            self.assertEqual(second.status, 'queued')
            self.assertEqual(third.status, 'queued')

            third.priority = 1                     # 让第三个插队
            deadline = time.time() + 60
            while time.time() < deadline:
                self.win._schedule()
                if third.status in ('running', 'ready', 'completed'):
                    break
                time.sleep(0.1)
            self.assertNotEqual(third.status, 'queued', '高优先级任务应插队执行')
            self.assertEqual(second.status, 'queued', '普通优先级任务应继续排队')

    def test_persist_and_restore_round_trip(self):
        with FaultServer(size=1 * MB) as srv:
            t = self._add(srv.url('/file'), 'persist.bin')
            deadline = time.time() + 60
            while time.time() < deadline and t.status != 'completed':
                time.sleep(0.05)
            self.assertEqual(t.status, 'completed')
            self.win._persist_tasks(force=True)

            records = task_store.load_tasks()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['url'], srv.url('/file'))
            self.assertEqual(records[0]['status'], 'completed')
            # 已完成且文件仍在 -> 恢复后应显示"已完成"
            self.assertEqual(task_store.restored_status(records[0]), 'completed')

    def test_restore_marks_unfinished_as_paused(self):
        """有 .part 的未完成任务恢复后应为 paused（可继续续传）"""
        save = os.path.join(self.tmp, 'half.bin')
        with open(save + '.part', 'wb') as f:
            f.write(b'x' * 1024)
        task_store.save_tasks([{
            'url': 'http://127.0.0.1:1/x.bin', 'save_path': save, 'status': 'running',
            'downloaded': 1024, 'total': 4096, 'priority': 0,
        }])
        records = task_store.load_tasks()
        self.assertEqual(task_store.restored_status(records[0]), 'paused')

    def test_corrupt_store_is_ignored(self):
        with open(task_store.STORE_FILE, 'w', encoding='utf-8') as f:
            f.write('{ this is not json')
        self.assertEqual(task_store.load_tasks(), [])

    def test_settings_sanitises_bad_values(self):
        from settings import Settings, DEFAULTS, _clean
        self.assertEqual(_clean('thread_count', 999), DEFAULTS['thread_count'])
        self.assertEqual(_clean('thread_count', 'x'), DEFAULTS['thread_count'])
        self.assertEqual(_clean('proxy', 123), '')
        self.assertEqual(_clean('custom_headers', ['a']), {})
        self.assertEqual(_clean('retry_count', 3), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
