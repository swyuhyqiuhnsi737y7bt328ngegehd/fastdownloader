# tests/ui_helpers.py - GUI 测试共用工具
"""测试期间替换掉 QMessageBox，避免弹出模态对话框阻塞用例。"""


class FakeMessageBox:
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


class patched_messagebox:
    """上下文管理器：临时把 ui 模块的 QMessageBox 换掉，退出时还原"""

    def __init__(self, ui_module):
        self.ui = ui_module
        self.original = None

    def __enter__(self):
        self.original = self.ui.QMessageBox
        self.ui.QMessageBox = FakeMessageBox
        return self

    def __exit__(self, *exc):
        self.ui.QMessageBox = self.original
        return False
