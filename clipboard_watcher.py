from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

class ClipboardWatcher:
    def __init__(self, parent, callback):
        self.parent = parent
        self.callback = callback
        self.last_text = ''
        self._timer = QTimer(parent)
        self._timer.timeout.connect(self._poll)
        self._timer.start(1000)

    def _poll(self):
        try:
            text = QApplication.clipboard().text()
            if text != self.last_text and text.startswith('http'):
                self.last_text = text
                self.callback(text)
        except Exception:
            pass

    def stop(self):
        self._timer.stop()
