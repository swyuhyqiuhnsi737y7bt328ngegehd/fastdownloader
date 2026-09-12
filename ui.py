import os, json, time, threading, urllib.parse, socket, re, tempfile
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtCore import pyqtSignal

from engine import DownloadTask
from utils import (format_size, format_time, _sanitize_filename,
                   _extract_filename, _default_save_path)
from settings import Settings
from clipboard_watcher import ClipboardWatcher
from remote_browser import RemoteServer, load_servers, save_servers
from task_store import save_tasks, load_tasks, restored_status
import updater
from version import __version__

STATUS_LABELS = {
    'running':   '正在下载', 'completed': '已完成',
    'error':     '失败',     'paused':    '已暂停',
    'stopped':   '已停止',   'ready':     '等待中',
    'queued':    '排队中', 'skipped':   '已存在',
}
STATUS_COLORS = {
    'running':   '#5dade2', 'completed': '#58d68d',
    'error':     '#ec7063', 'paused':    '#f5b041',
    'stopped':   '#bdc3c7', 'ready':     '#aeb6bf',
    'queued':    '#8e9aaf', 'skipped':   '#7fb3d5',
}

CATEGORY_RULES = [
    ('all', '全部任务'),
    ('video', '视频'), ('music', '音乐'), ('docs', '文档'),
    ('programs', '程序'), ('compressed', '压缩文件'),
    ('unfinished', '未完成'), ('completed', '已完成'),
]

CATEGORY_EXTS = {
    'video': ['.mp4','.avi','.mkv','.mov','.wmv','.flv','.webm','.ts','.m2ts'],
    'music': ['.mp3','.flac','.wav','.aac','.ogg','.wma','.m4a','.opus'],
    'docs': ['.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.txt','.md','.epub'],
    'programs': ['.exe','.msi','.dmg','.appimage','.deb','.rpm','.apk'],
    'compressed': ['.zip','.rar','.7z','.tar','.gz','.bz2','.xz','.zst','.iso'],
}

class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() != 3:
            super().paint(painter, option, index)
            return
        data = index.data(Qt.UserRole)
        if data is None:
            return
        pct, status = data
        rect = option.rect.adjusted(4, 4, -4, -4)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        col = QColor(STATUS_COLORS.get(status, '#3498db'))
        painter.setBrush(QColor('#3a3a3a'))
        painter.drawRoundedRect(rect, 3, 3)
        if pct > 0:
            w = int(rect.width() * pct / 100.0)
            if w > 4:
                bar = QRect(rect.x(), rect.y(), w, rect.height())
                grad = QLinearGradient(bar.topLeft(), bar.topRight())
                grad.setColorAt(0, col.lighter(130))
                grad.setColorAt(1, col)
                painter.setBrush(grad)
                painter.drawRoundedRect(bar, 3, 3)
        painter.setPen(QColor('#e0e0e0'))
        painter.drawText(rect, Qt.AlignCenter, f"{pct:.1f}%")
        painter.restore()


class MainWindow(QMainWindow):
    # 工作线程通过信号把事件投递到 GUI 线程，避免跨线程操作 Qt 控件
    _task_event = pyqtSignal(int, str, object)
    _update_result = pyqtSignal(object, bool, bool)   # release, has_update, silent
    _update_error = pyqtSignal(str, bool)             # message, silent
    _update_progress = pyqtSignal(int, int)           # downloaded, total
    _update_done = pyqtSignal()                        # 替换脚本已就绪

    def __init__(self):
        super().__init__()
        self.tasks = {}
        self._task_event.connect(self._on_event_gui)
        self._update_result.connect(self._on_update_result)
        self._update_error.connect(self._on_update_error)
        self._update_progress.connect(self._on_update_progress)
        self._update_done.connect(self._on_update_done)
        self._update_checking = False
        self._update_dlg = None
        self._closing = False
        self._last_persist = 0.0
        self.next_id = 0
        self._filter = 'all'
        self._hidden = set()
        self.settings = Settings()
        self.setWindowTitle("极速下载器 Pro")
        self.resize(1150, 720)
        self.setMinimumSize(900, 500)
        self._setup_ui()
        self._setup_menu()
        self._timer = QTimer()
        self._timer.timeout.connect(self._update_ui)
        self._timer.start(500)
        self.clip_watcher = ClipboardWatcher(self, self.on_clipboard_url)
        if getattr(self.settings, 'check_update_on_start', True):
            # 延迟几秒，别和启动时的界面绘制抢时间
            QTimer.singleShot(4000, lambda: self.check_updates(silent=True))
        try:
            n = self._restore_tasks()
            if n:
                self.status_label.setText(f'已恢复 {n} 个历史任务（点击"继续"即可断点续传）')
        except Exception:
            pass

    # ---- UI setup ----

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        hdr = QWidget()
        hdr.setFixedHeight(48)
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(15, 0, 15, 0)
        icon = QLabel("⚡")
        icon.setStyleSheet("font-size: 22px;")
        hl.addWidget(icon)
        title = QLabel("极速下载器 Pro")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        hl.addWidget(title)
        ver = QLabel("")
        ver.setStyleSheet("color: #9aa6b2;")
        hl.addWidget(ver)
        hl.addStretch()
        layout.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #3a3a3a;")
        layout.addWidget(sep)

        # Toolbar
        tb = QWidget()
        tbb = QHBoxLayout(tb)
        tbb.setContentsMargins(10, 4, 10, 4)
        self._add_btn(tbb, '➕ 添加', self.add_task_dialog, '#2ecc71')
        self._add_btn(tbb, '⏸ 暂停', self.pause_selected, '#f39c12')
        self._add_btn(tbb, '▶ 继续', self.resume_selected, '#3498db')
        self._add_btn(tbb, '⏹ 停止', self.stop_selected, '#e74c3c')
        self._add_btn(tbb, '🗑 删除', self.delete_selected, '#e74c3c')
        s = QFrame(); s.setFrameShape(QFrame.VLine); s.setStyleSheet("color: #3a3a3a;")
        tbb.addWidget(s)
        self._add_btn(tbb, '全部开始', self.start_all, '#2ecc71', outline=True)
        self._add_btn(tbb, '全部暂停', self.pause_all, '#f39c12', outline=True)
        tbb.addStretch()
        self._speed_btn = self._add_btn(tbb, '限速: 关', self.toggle_speed, '#95a5a6', outline=True)
        self._add_btn(tbb, '远程', self.remote_browser_dialog, '#9b59b6', outline=True)
        self._add_btn(tbb, '详情', self.show_detail, '#3498db', outline=True)
        self._add_btn(tbb, '设置', self.open_settings, '#95a5a6', outline=True)
        layout.addWidget(tb)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color: #3a3a3a;")
        layout.addWidget(sep2)

        # Search bar
        search_bar = QWidget()
        search_bar.setFixedHeight(32)
        search_layout = QHBoxLayout(search_bar)
        search_layout.setContentsMargins(10, 2, 10, 2)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索任务...")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setFixedWidth(250)
        self._search_edit.textChanged.connect(self._apply_filter)
        search_layout.addWidget(self._search_edit)
        search_layout.addStretch()
        layout.addWidget(search_bar)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        side = QWidget()
        side.setFixedWidth(130)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(10, 8, 5, 8)
        cat_title = QLabel("分类")
        cat_title.setStyleSheet("font-weight: bold; font-size: 11px; padding-bottom: 4px;")
        side_layout.addWidget(cat_title)
        self._cat_btns = {}
        for cid, clabel in CATEGORY_RULES:
            btn = QPushButton(clabel)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _, c=cid: self._set_category(c))
            side_layout.addWidget(btn)
            self._cat_btns[cid] = btn
        side_layout.addStretch()
        self._cat_btns['all'].setChecked(True)
        body_layout.addWidget(side)

        # Main area
        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(5, 5, 10, 5)

        # Table
        self.table = QTreeWidget()
        self.table.setHeaderLabels(['状态', '文件名', '大小', '进度', '速度', '剩余时间'])
        self.table.setColumnCount(6)
        self.table.setRootIsDecorated(False)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setIndentation(0)
        self.table.setItemDelegateForColumn(3, ProgressDelegate(self.table))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemDoubleClicked.connect(self._on_double_click)
        h = self.table.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.resizeSection(0, 70)
        h.resizeSection(2, 150)
        h.resizeSection(3, 140)
        h.resizeSection(4, 110)
        h.resizeSection(5, 90)

        self.table.setStyleSheet("""
            QTreeWidget { border: none; font-size: 9pt; }
            QTreeWidget::item { padding: 4px 2px; }
            QHeaderView::section { padding: 4px; font-weight: bold; border: none;
                border-bottom: 1px solid #3a3a3a; }
        """)

        main_layout.addWidget(self.table)

        body_layout.addWidget(main, 1)
        layout.addWidget(body, 1)

        # Status bar
        status = QWidget()
        status.setFixedHeight(26)
        sl = QHBoxLayout(status)
        sl.setContentsMargins(12, 0, 12, 0)
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("font-size: 9pt;")
        sl.addWidget(self.status_label)
        sl.addStretch()
        self.speed_label = QLabel("")
        self.speed_label.setStyleSheet("color: #aaaaaa; font-size: 9pt;")
        sl.addWidget(self.speed_label)
        layout.addWidget(status)

        self._apply_theme()

    def _add_btn(self, layout, text, slot, color, outline=False):
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        style = self._btn_style(color, outline)
        btn.setStyleSheet(style)
        layout.addWidget(btn)
        return btn

    def _btn_style(self, color, outline):
        if outline:
            return (f"QPushButton {{ color: {color}; border: 1px solid {color}; "
                    f"border-radius: 3px; padding: 4px 10px; font-size: 9pt; }}"
                    f"QPushButton:hover {{ background: {color}22; }}")
        return (f"QPushButton {{ color: white; background: {color}; "
                f"border: none; border-radius: 3px; padding: 4px 10px; font-size: 9pt; }}"
                f"QPushButton:hover {{ background: {self._lighten(color)}; }}")

    def _lighten(self, color):
        c = QColor(color)
        return c.lighter(130).name()

    @staticmethod
    def _dark_palette():
        """深色 QPalette：让风格绘制的图元（箭头/滚动条/禁用文字）也适配深色主题"""
        pal = QPalette()
        window = QColor('#1e1e1e')
        base = QColor('#252525')
        alt = QColor('#2a2a2a')
        text = QColor('#e0e0e0')
        disabled = QColor('#7f8a95')
        highlight = QColor('#264f78')
        pal.setColor(QPalette.Window, window)
        pal.setColor(QPalette.WindowText, text)
        pal.setColor(QPalette.Base, base)
        pal.setColor(QPalette.AlternateBase, alt)
        pal.setColor(QPalette.Text, text)
        pal.setColor(QPalette.Button, alt)
        pal.setColor(QPalette.ButtonText, text)
        pal.setColor(QPalette.BrightText, QColor('#ff6b6b'))
        pal.setColor(QPalette.ToolTipBase, alt)
        pal.setColor(QPalette.ToolTipText, text)
        pal.setColor(QPalette.Highlight, highlight)
        pal.setColor(QPalette.HighlightedText, QColor('#ffffff'))
        pal.setColor(QPalette.PlaceholderText, QColor('#8b98a5'))
        pal.setColor(QPalette.Link, QColor('#5dade2'))
        for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
            pal.setColor(QPalette.Disabled, role, disabled)
        return pal
    def _apply_theme(self):
        style = """
            QMainWindow { background: #1e1e1e; }
            QWidget { color: #e0e0e0; font-family: 'Segoe UI'; font-size: 9pt; }
            QLabel { color: #e0e0e0; }
            QTreeWidget { background: #252525; color: #e0e0e0;
                alternate-background-color: #2a2a2a; }
            QTreeWidget::item:selected { background: #264f78; }
            QTreeWidget::item { color: #e0e0e0; }
            QHeaderView::section { background: #1e1e1e; color: #e0e0e0;
                border-bottom: 1px solid #3a3a3a; padding: 5px; }
            QHeaderView::section:hover { background: #2a2a2a; }
            QPushButton { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a;
                border-radius: 3px; padding: 5px 12px; }
            QPushButton:hover { background: #333333; border-color: #555555; }
            QPushButton:checked { background: #264f78; border-color: #3498db;
                color: #ffffff; font-weight: bold; }
            QPushButton:pressed { background: #1a3a5a; }
            QComboBox { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a;
                padding: 3px; border-radius: 2px; }
            QComboBox QAbstractItemView { color: #e0e0e0; background: #252525; selection-background-color: #264f78; }
            QLineEdit { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a;
                padding: 3px 6px; border-radius: 2px; }
            QSpinBox { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a;
                padding: 2px; border-radius: 2px; }
            QSlider::groove:horizontal { background: #3a3a3a; height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #3498db; width: 14px; margin: -4px 0; border-radius: 7px; }
            QMenuBar { background: #1e1e1e; color: #e0e0e0; }
            QMenuBar::item:selected { background: #264f78; }
            QMenu { background: #252525; color: #e0e0e0; border: 1px solid #3a3a3a; }
            QMenu::item:selected { background: #264f78; }
            QScrollBar:vertical { background: #1e1e1e; width: 10px; }
            QScrollBar::handle:vertical { background: #3a3a3a; border-radius: 4px; min-height: 30px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QDialog { background: #1e1e1e; }
            QDialog QLabel, QDialog QCheckBox, QDialog QRadioButton,
            QDialog QGroupBox, QDialog QTabWidget { color: #e0e0e0; }
            QDialog QLineEdit, QDialog QComboBox, QDialog QSpinBox,
            QDialog QDoubleSpinBox, QDialog QPlainTextEdit { color: #e0e0e0; background: #2a2a2a; }
            QDialog QComboBox QAbstractItemView { color: #e0e0e0; background: #252525;
                selection-background-color: #264f78; }
            QToolTip { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a; }
            QPlainTextEdit { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a; }
            QDoubleSpinBox { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a;
                padding: 2px; border-radius: 2px; }
            QCheckBox { color: #e0e0e0; spacing: 6px; }
            QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #5a5a5a;
                background: #2a2a2a; border-radius: 2px; }
            QCheckBox::indicator:checked { background: #3498db; border-color: #3498db; }
            /* 选项卡：默认页面背景是系统浅色，必须显式设为深色，否则浅色文字看不见 */
            QTabWidget::pane { border: 1px solid #3a3a3a; background: #1e1e1e; top: -1px; }
            QTabWidget > QWidget { background: #1e1e1e; }
            QTabBar { background: #1e1e1e; }
            QTabBar::tab { background: #2a2a2a; color: #d6dde4; border: 1px solid #3a3a3a;
                border-bottom: none; padding: 7px 16px; margin-right: 2px; font-size: 9pt; }
            QTabBar::tab:selected { background: #264f78; color: #ffffff; border-color: #3498db; }
            QTabBar::tab:hover:!selected { background: #333333; color: #ffffff; }
            QScrollArea { background: #1e1e1e; border: 1px solid #3a3a3a; }
            QScrollArea > QWidget > QWidget { background: #1e1e1e; }
            QScrollArea QLabel { color: #e0e0e0; background: transparent; }
            QGroupBox { color: #e0e0e0; border: 1px solid #3a3a3a; border-radius: 3px; margin-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
        """
        self.setStyleSheet(style)
        # 同时设置到 QApplication：对话框/消息框若只继承 MainWindow 样式，
        # 内部控件会退回系统浅色配色，出现"浅底浅字"看不清的问题。
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style)
            # 调色板：样式表管不到风格绘制的元素（下拉箭头、滚动条、禁用文字），
            # 不设的话它们在深色背景上会是系统浅色配色的深色图元，看起来发黑。
            app.setPalette(self._dark_palette())

    def _setup_menu(self):
        bar = self.menuBar()
        fm = bar.addMenu('文件')
        fm.addAction('新建任务', self.add_task_dialog, QKeySequence.New)
        fm.addAction('批量下载', self.batch_download, QKeySequence('Ctrl+B'))
        fm.addSeparator()
        fm.addAction('导入任务', self.import_tasks)
        fm.addAction('导出任务', self.export_tasks)
        fm.addSeparator()
        fm.addAction('退出', self.close)

        dm = bar.addMenu('下载')
        dm.addAction('继续', self.resume_selected)
        dm.addAction('暂停', self.pause_selected)
        dm.addAction('停止', self.stop_selected)
        dm.addSeparator()
        dm.addAction('全部开始', self.start_all)
        dm.addAction('全部暂停', self.pause_all)
        dm.addAction('全部停止', self.stop_all)

        vm = bar.addMenu('查看')
        vm.addAction('搜索任务', self.focus_search, QKeySequence.Find)
        hm = bar.addMenu('帮助')
        hm.addAction('检查更新', lambda: self.check_updates(silent=False))
        hm.addAction('打开发布页面', self.open_releases_page)
        hm.addAction('被杀软误报了？', self.show_av_help)
        hm.addSeparator()
        hm.addAction('关于', self.show_about)

    # ---- Category ----

    def _set_category(self, cid):
        self._filter = cid
        for cid2, btn in self._cat_btns.items():
            btn.setChecked(cid2 == cid)
        self._apply_filter()

    def _matches_category(self, task):
        f = self._filter
        if f == 'all':
            return True
        if f == 'unfinished':
            return task.status in ('ready', 'running', 'paused', 'stopped', 'error', 'queued')
        if f == 'completed':
            return task.status in ('completed', 'skipped')
        exts = CATEGORY_EXTS.get(f, [])
        if not exts:
            return True
        _, ext = os.path.splitext(task.save_path)
        return ext.lower() in exts

    def _apply_filter(self):
        search = self._search_edit.text().strip().lower() if hasattr(self, '_search_edit') else ''
        self._hidden.clear()
        for tid, task in self.tasks.items():
            item = self._find_item(tid)
            if item is None:
                continue
            name = os.path.basename(task.save_path).lower()
            cat_ok = self._matches_category(task)
            search_ok = not search or search in name
            if cat_ok and search_ok:
                item.setHidden(False)
            else:
                item.setHidden(True)
                self._hidden.add(tid)

    # ---- Updates ----

    def _update_ui(self):
        active = running = completed = error_count = 0
        total_speed = 0.0
        for tid, task in list(self.tasks.items()):
            info = task.get_info()
            st = info['status']
            if st == 'running':
                active += 1
                total_speed += info['speed']
            elif st == 'completed':
                completed += 1
            elif st == 'error':
                error_count += 1

            item = self._find_item(tid)
            if item is None:
                continue
            if tid in self._hidden:
                item.setHidden(True)
                continue

            fname = os.path.basename(info.get('final_path') or task.save_path)
            sz = f"{format_size(info['downloaded'])} / {format_size(info['total'])}" if info['total'] > 0 else format_size(info['downloaded'])
            pct = info['percent']
            spd = f"{format_size(info['speed'])}/s" if st == 'running' else "---"
            eta = format_time((info['total'] - info['downloaded']) / info['speed']) if info['speed'] > 0 and info['total'] > 0 else "---"
            label = STATUS_LABELS.get(st, st)
            color = STATUS_COLORS.get(st, '#95a5a6')

            item.setText(0, label)
            item.setText(1, fname)
            item.setText(2, sz)
            item.setData(3, Qt.UserRole, (pct, st))
            item.setText(4, spd)
            item.setText(5, eta)
            item.setForeground(0, QColor(color))

            if st == 'completed' and not getattr(task, '_notified', False):
                task._notified = True
                fp = info.get('final_path') or task.save_path
                QTimer.singleShot(0, lambda fp=fp: QMessageBox.information(self, '下载完成', f'文件已保存到:\n{fp}'))

        self._schedule()          # 有空闲额度就启动排队任务
        self._persist_tasks()     # 周期性保存任务列表（节流）

        total = len(self.tasks) - len(self._hidden)
        queued = sum(1 for t in self.tasks.values() if t.status == 'queued')
        parts = [f"📦 {total} 个任务"]
        if queued:
            parts.append(f"⏳ {queued} 个排队")
        if active:
            parts.append(f"⬇ {active} 个下载中")
        if completed:
            parts.append(f"✅ {completed} 个已完成")
        if error_count:
            parts.append(f"❌ {error_count} 个错误")
        self.status_label.setText("  ·  ".join(parts))
        self.speed_label.setText(format_size(total_speed) + "/s" if total_speed > 0 else "")

    # ---- Task management ----

    def _start_task_async(self, task):
        """后台线程启动任务：start() 里有 Cookie 读取/HEAD/Range 探测，
        可能耗时数秒到数十秒，不能阻塞 GUI 线程。

        _dispatching 标记保证调度器不会在 start() 真正把状态改成 running 之前
        重复占用并发额度（超发任务）。"""
        task._error_notified = False
        task._dispatching = True

        def _run():
            try:
                task.start()
            finally:
                task._dispatching = False

        threading.Thread(target=_run, daemon=True).start()

    def _task_config(self):
        """把当前设置转换成 DownloadTask 的网络/重试配置"""
        s = self.settings
        return dict(
            num_threads=s.thread_count,
            speed_limit=s.speed_limit,
            proxy=s.proxy,
            headers=s.custom_headers,
            retry_count=s.retry_count,
            retry_backoff=s.retry_backoff,
            connect_timeout=s.connect_timeout,
            read_timeout=s.read_timeout,
            verify_ssl=s.verify_ssl,
            conflict_policy=s.conflict_policy,
            check_disk_space=s.check_disk_space,
            min_free_mb=s.min_free_mb,
            cookie_mode=s.cookie_mode,
            sha256_auto_probe=getattr(s, 'sha256_auto_probe', True),
        )

    def _add_task(self, url, save_path, priority=0, start_now=True, expected_sha256=''):
        tid = self.next_id
        self.next_id += 1
        save_path = os.path.normpath(save_path)
        task = DownloadTask(tid, url, save_path,
                            overwrite=True, expected_sha256=expected_sha256,
                            **self._task_config())
        task.set_callback(self._on_task_event)
        task.priority = priority
        task.status = 'queued'
        self.tasks[tid] = task
        self._create_item(task)
        if start_now:
            self._schedule()
        self._persist_tasks(force=True)
        self._apply_filter()
        return task

    def _create_item(self, task):
        """为任务创建表格行（新增与恢复共用）"""
        item = QTreeWidgetItem(self.table)
        item.setData(0, Qt.UserRole, task.task_id)
        item.setText(0, STATUS_LABELS.get(task.status, task.status))
        item.setText(1, os.path.basename(task.save_path))
        item.setText(2, '---')
        item.setData(3, Qt.UserRole, (0.0, task.status))
        item.setText(4, '---')
        item.setText(5, '---')
        item.setForeground(0, QColor(STATUS_COLORS.get(task.status, '#95a5a6')))
        self.table.addTopLevelItem(item)
        return item

    # ---- 队列调度 ----

    def _active_slots(self):
        """当前占用的并发额度（运行中 + 正在启动）"""
        return sum(1 for t in self.tasks.values()
                   if t.status == 'running' or getattr(t, '_dispatching', False))

    def _schedule(self):
        """在最大并发任务数内按优先级启动排队任务"""
        if getattr(self, '_closing', False):
            return
        limit = max(1, int(self.settings.max_concurrent_tasks))
        free = limit - self._active_slots()
        if free <= 0:
            return
        pending = [t for t in self.tasks.values() if t.status == 'queued']
        if not pending:
            return
        # 优先级高的先跑；同级按加入顺序（task_id 递增）
        pending.sort(key=lambda t: (-getattr(t, 'priority', 0), t.task_id))
        for task in pending[:free]:
            task.status = 'ready'
            self._start_task_async(task)

    def _enqueue(self, task):
        """把任务放回队列等待调度"""
        if task.status in ('running',):
            return
        task.status = 'queued'
        self._schedule()
        self._persist_tasks(force=True)

    # ---- 持久化 ----

    def _persist_tasks(self, force=False):
        now = time.time()
        if not force and now - self._last_persist < 3:
            return
        self._last_persist = now
        records = []
        for task in self.tasks.values():
            info = task.get_info()
            records.append({
                'url': task.url,
                'save_path': task.save_path,
                'status': info['status'],
                'downloaded': info['downloaded'],
                'total': info['total'],
                'priority': getattr(task, 'priority', 0),
            })
        save_tasks(records)

    def _restore_tasks(self):
        """启动时恢复上次的任务列表（不自动开始下载）"""
        records = load_tasks()
        if not records:
            return 0
        for rec in records:
            tid = self.next_id
            self.next_id += 1
            task = DownloadTask(tid, rec['url'], os.path.normpath(rec['save_path']),
                                overwrite=True, **self._task_config())
            task.set_callback(self._on_task_event)
            task.priority = rec.get('priority', 0)
            status = restored_status(rec)
            task.status = status
            if status == 'completed':
                task.downloaded = rec.get('total') or rec.get('downloaded') or 0
                task.total_size = rec.get('total') or 0
                task._final_path = rec['save_path']
                task._notified = True     # 不重复弹"下载完成"
            self.tasks[tid] = task
            self._create_item(task)
        self._apply_filter()
        return len(records)

    def _on_task_event(self, tid, event, data=None):
        # 由工作线程（下载线程/monitor）调用：只做线程安全的信号投递，
        # 绝不直接操作 Qt 控件（QTimer.singleShot 在非 GUI 线程中回调永远不会执行）
        self._task_event.emit(tid, event, data)

    def _on_event_gui(self, tid, event, data=None):
        """GUI 线程中处理任务事件（信号自动队列到主线程）"""
        if getattr(self, '_closing', False):
            return
        if event == 'skipped':
            QMessageBox.information(self, '已跳过', f'任务 {tid} 的目标文件已存在，按设置跳过下载:\n{data}')
            return
        if event == 'warning':
            QMessageBox.warning(self, '提示', str(data))
            return
        if event == 'error':
            task = self.tasks.get(tid)
            if task is None:
                return  # 任务已被删除，不再弹窗
            if getattr(task, '_error_notified', False):
                return  # 同一任务只提示一次
            task._error_notified = True
            QMessageBox.critical(self, '下载错误', f'任务 {tid} 失败:\n{data}')

    def _tid_of(self, item):
        return item.data(0, Qt.UserRole) if item else None

    def _find_item(self, tid):
        for i in range(self.table.topLevelItemCount()):
            it = self.table.topLevelItem(i)
            if self._tid_of(it) == tid:
                return it
        return None

    def _get_selected_tid(self):
        return self._tid_of(self.table.currentItem())

    def _context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
        self.table.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction('开始下载', self.resume_selected)
        menu.addAction('暂停', self.pause_selected)
        menu.addAction('停止', self.stop_selected)
        menu.addAction('重新下载', self.restart_selected)
        menu.addSeparator()
        menu.addAction('⏫ 高优先级', lambda: self.set_priority(1))
        menu.addAction('⏬ 低优先级', lambda: self.set_priority(-1))
        menu.addAction('➖ 普通优先级', lambda: self.set_priority(-getattr(self.tasks.get(self._get_selected_tid()), 'priority', 0) if self._get_selected_tid() in self.tasks else 0))
        menu.addSeparator()
        menu.addAction('查看详情', self.show_detail)
        menu.addAction('删除任务', self.delete_selected)
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def _on_double_click(self, item, col):
        tid = self._get_selected_tid()
        if tid is None:
            return
        task = self.tasks.get(tid)
        if not task:
            return
        if task.status == 'running':
            task.pause()
        elif task.status in ('paused', 'ready', 'error'):
            self._start_task_async(task)

    # ---- Task operations ----

    def pause_selected(self):
        tid = self._get_selected_tid()
        if tid is not None and tid in self.tasks:
            t = self.tasks[tid]
            if t.status == 'queued':
                t.status = 'paused'        # 排队中的任务：取消排队（用户可随时继续）
                self._persist_tasks(force=True)
            else:
                t.pause()

    def resume_selected(self):
        tid = self._get_selected_tid()
        if tid is not None and tid in self.tasks:
            t = self.tasks[tid]
            if t.status in ('paused', 'ready', 'error'):
                self._enqueue(t)           # 有额度立即跑，否则排队

    def stop_selected(self):
        tid = self._get_selected_tid()
        if tid is not None and tid in self.tasks:
            t = self.tasks[tid]
            if t.status == 'queued':
                t.status = 'stopped'
                self._persist_tasks(force=True)
            else:
                t.stop()

    def restart_selected(self):
        tid = self._get_selected_tid()
        if tid is not None and tid in self.tasks:
            t = self.tasks[tid]
            if t.status == 'queued':
                t.status = 'stopped'
            else:
                t.stop()
            self._enqueue(t)

    def set_priority(self, delta):
        """调整选中任务的优先级（右键菜单）"""
        tid = self._get_selected_tid()
        if tid is None or tid not in self.tasks:
            return
        t = self.tasks[tid]
        prio = max(-1, min(1, getattr(t, 'priority', 0) + delta))
        if prio != getattr(t, 'priority', 0):
            t.priority = prio
            self._persist_tasks(force=True)
            self._schedule()

    def delete_selected(self):
        tid = self._get_selected_tid()
        if tid is None:
            return
        task = self.tasks.pop(tid, None)
        if task:
            if task.status == 'queued':
                task.status = 'stopped'    # 还没开始，直接移除，别触发下载
            else:
                task.stop()
        item = self._find_item(tid)
        if item is not None:
            self.table.invisibleRootItem().removeChild(item)
        self._hidden.discard(tid)
        self._persist_tasks(force=True)

    def start_all(self):
        for t in self.tasks.values():
            if t.status in ('ready', 'paused', 'error'):
                self._enqueue(t)

    def pause_all(self):
        for t in self.tasks.values():
            if t.status == 'running':
                t.pause()
            elif t.status == 'queued':
                t.status = 'paused'

    def stop_all(self):
        for t in self.tasks.values():
            if t.status in ('running', 'paused'):
                t.stop()
            elif t.status == 'queued':
                t.status = 'stopped'

    # ---- Dialogs ----

    def add_task_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('添加下载任务')
        dlg.resize(620, 260)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 18)

        layout.addWidget(QLabel('下载链接'))
        url_edit = QLineEdit()
        layout.addWidget(url_edit)

        layout.addSpacing(8)
        layout.addWidget(QLabel('保存到'))
        path_layout = QHBoxLayout()
        path_edit = QLineEdit()
        path_edit.setPlaceholderText('可直接粘贴完整路径，或点右侧按钮选择文件夹')
        path_layout.addWidget(path_edit)
        browse_btn = QPushButton('选择文件夹')
        path_layout.addWidget(browse_btn)
        layout.addLayout(path_layout)
        hint = QLabel('留空则使用设置里的默认下载目录；只填目录时会按链接自动生成文件名。')
        hint.setStyleSheet('color: #b8c4d0;')
        layout.addWidget(hint)

        layout.addSpacing(6)
        layout.addWidget(QLabel('校验 SHA256（可选）'))
        sha_edit = QLineEdit()
        sha_edit.setPlaceholderText('粘贴 64 位哈希；留空则自动尝试服务器上的 .sha256 文件')
        layout.addWidget(sha_edit)

        # 用户手动改过路径后，就不再被链接变化覆盖
        manual = {'edited': False}

        def _default_path():
            return _default_save_path(self.settings.save_directory, url_edit.text())

        def apply_default(*_args):
            if manual['edited']:
                return
            path_edit.setText(_default_path())
            path_edit.setCursorPosition(0)

        def on_browse():
            chosen = self._pick_directory(path_edit.text(), '选择下载文件夹')
            if not chosen:
                return
            # 顺手记住为默认下载目录，下次添加任务直接用这里
            self.settings.save_directory = chosen
            self.settings.save()
            manual['edited'] = False
            apply_default()

        url_edit.textChanged.connect(apply_default)
        path_edit.textEdited.connect(lambda _t: manual.__setitem__('edited', True))
        browse_btn.clicked.connect(on_browse)

        # 打开对话框：剪贴板里有链接就填上；无论如何都给出一个默认路径
        clip = ''
        try:
            clip = (QApplication.clipboard().text() or '').strip()
        except Exception:
            pass
        if clip.lower().startswith(('http://', 'https://', 'ftp://', 'ftps://')):
            url_edit.setText(clip)      # 触发 textChanged -> 填充默认路径
        elif re.fullmatch(r'[0-9a-fA-F]{64}', clip):
            sha_edit.setText(clip.lower())   # 剪贴板里是个哈希，直接填进校验框
        if not path_edit.text().strip():
            apply_default()

        layout.addSpacing(12)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel = QPushButton('取消')
        cancel.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel)
        ok = QPushButton('开始下载')
        ok.setStyleSheet("QPushButton { background: #2ecc71; color: white; padding: 6px 20px; border-radius: 3px; }")

        def submit():
            url = url_edit.text().strip()
            # 路径留空时回落到默认目录（引擎会按链接补文件名）
            path = path_edit.text().strip() or _default_path()
            digest = sha_edit.text().strip().lower()
            if digest and not re.fullmatch(r'[0-9a-f]{64}', digest):
                QMessageBox.warning(dlg, '提示', 'SHA256 应该是 64 位十六进制字符，请检查后重试')
                return
            self._submit_task(dlg, url, path, expected_sha256=digest)

        ok.clicked.connect(submit)
        btn_layout.addWidget(ok)
        layout.addLayout(btn_layout)

        dlg.exec_()

    def _submit_task(self, dlg, url, path, expected_sha256=''):
        if url and path:
            self._add_task(url, path, expected_sha256=expected_sha256)
            dlg.accept()

    def _pick_directory(self, start=None, title='选择文件夹'):
        """统一的目录选择框：起点是文件路径时自动取其所在目录"""
        start = (start or '').strip() or self.settings.save_directory or os.path.expanduser('~')
        if not os.path.isdir(start):
            start = os.path.dirname(start) or os.path.expanduser('~')
        chosen = QFileDialog.getExistingDirectory(self, title, start)
        return os.path.normpath(chosen) if chosen else ''

    def batch_download(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('批量下载')
        dlg.resize(560, 380)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(QLabel('下载链接（每行一个URL）'))
        text_box = QPlainTextEdit()
        text_box.setFont(QFont('Consolas', 10))
        layout.addWidget(text_box)

        try:
            clip = QApplication.clipboard().text()
            lines = [l.strip() for l in clip.split('\n') if l.strip().startswith('http')]
            if lines:
                text_box.setPlainText('\n'.join(lines))
        except:
            pass

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel = QPushButton('取消')
        cancel.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel)
        ok = QPushButton('开始批量下载')
        ok.setStyleSheet("QPushButton { background: #2ecc71; color: white; padding: 6px 20px; border-radius: 3px; }")
        ok.clicked.connect(lambda: self._submit_batch(dlg, text_box.toPlainText()))
        btn_layout.addWidget(ok)
        layout.addLayout(btn_layout)
        dlg.exec_()

    def _submit_batch(self, dlg, raw):
        urls = [u.strip() for u in raw.split('\n') if u.strip().startswith('http')]
        if not urls:
            QMessageBox.warning(self, '提示', '未发现有效的下载链接')
            return
        save_dir = QFileDialog.getExistingDirectory(self, '选择保存目录', self.settings.save_directory)
        if not save_dir:
            return
        for u in urls:
            fn = _extract_filename(u)
            self._add_task(u, os.path.normpath(os.path.join(save_dir, fn)))
        dlg.accept()
        QMessageBox.information(self, '批量添加', f'已添加 {len(urls)} 个下载任务')

    def show_detail(self):
        tid = self._get_selected_tid()
        if tid is None or tid not in self.tasks:
            QMessageBox.warning(self, '提示', '请先选择一个任务')
            return
        task = self.tasks[tid]
        info = task.get_info()
        stats = task.get_thread_stats()

        dlg = QDialog(self)
        dlg.setWindowTitle(f'任务详情 #{tid}')
        dlg.resize(600, 420)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(15, 15, 15, 15)

        info_text = (f"URL:     {task.url}\n"
                     f"保存到:  {task.save_path}\n"
                     f"状态:    {info['status']}\n"
                     f"大小:    {format_size(info['total'])} ({info['downloaded']}/{info['total']})\n"
                     f"速度:    {format_size(info['speed'])}/s\n"
                     f"线程数:  {len(stats) if stats else task.num_threads}")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(140)
        scroll.setFrameShape(QFrame.NoFrame)
        info_label = QLabel(info_text)
        info_label.setFont(QFont('Consolas', 9))
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #e0e0e0;")
        scroll.setWidget(info_label)
        layout.addWidget(scroll)

        layout.addSpacing(8)
        layout.addWidget(QLabel('线程详情'))
        table = QTreeWidget()
        table.setHeaderLabels(['#', '字节范围', '已下载', '速度', '状态'])
        table.setRootIsDecorated(False)
        table.setColumnCount(5)
        h = table.header()
        h.resizeSection(0, 40)
        h.resizeSection(1, 200)
        h.resizeSection(2, 100)
        h.resizeSection(3, 100)
        h.resizeSection(4, 80)

        if stats:
            for idx, s in sorted(stats.items()):
                rg = f"[{s['start']}, {s['end']}]" if s['end'] >= 0 else f"[{s['start']}, ∞)"
                QTreeWidgetItem(table, [str(idx), rg, format_size(s['downloaded']),
                                        f"{format_size(s['speed'])}/s", s['status']])
        else:
            QTreeWidgetItem(table, ['—', '等待线程启动...', '—', '—', '—'])
        layout.addWidget(table)

        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec_()

    def remote_browser_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('远程服务器')
        dlg.resize(680, 520)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)

        # Server selector
        top = QHBoxLayout()
        self._server_combo = QComboBox()
        self._server_combo.setMinimumWidth(200)
        for sv in load_servers():
            self._server_combo.addItem(f"{sv['name']} ({sv['host']})", sv)
        top.addWidget(QLabel('服务器:'))
        top.addWidget(self._server_combo, 1)
        add_srv = QPushButton('+ 添加')
        add_srv.clicked.connect(lambda: self._edit_server(dlg))
        top.addWidget(add_srv)
        edit_srv = QPushButton('编辑')
        edit_srv.clicked.connect(lambda: self._edit_server(dlg, self._server_combo.currentData()))
        top.addWidget(edit_srv)
        del_srv = QPushButton('删除')
        del_srv.clicked.connect(self._delete_server)
        top.addWidget(del_srv)
        layout.addLayout(top)

        # Browser
        self._remote_tree = QTreeWidget()
        self._remote_tree.setHeaderLabels(['名称', '大小', '类型'])
        self._remote_tree.setRootIsDecorated(True)
        self._remote_tree.setColumnCount(3)
        self._remote_tree.setAlternatingRowColors(True)
        h = self._remote_tree.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.resizeSection(1, 100)
        h.resizeSection(2, 60)
        self._remote_tree.itemDoubleClicked.connect(self._remote_navigate)
        self._remote_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._remote_tree.customContextMenuRequested.connect(self._remote_context)
        layout.addWidget(self._remote_tree, 1)

        # Buttons
        btn_layout = QHBoxLayout()
        self._remote_status = QLabel('未连接')
        btn_layout.addWidget(self._remote_status)
        btn_layout.addStretch()
        connect_btn = QPushButton('连接')
        connect_btn.setStyleSheet("QPushButton { background: #3498db; color: white; padding: 5px 16px; border-radius: 3px; }")
        connect_btn.clicked.connect(lambda: self._remote_connect(dlg))
        btn_layout.addWidget(connect_btn)
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        self._current_remote = None
        self._remote_path = '/'
        self._remote_conn = None
        dlg.exec_()

    def _edit_server(self, parent, server_data=None):
        dlg = QDialog(parent)
        dlg.setWindowTitle('编辑服务器' if server_data else '添加服务器')
        dlg.resize(400, 280)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(15, 15, 15, 15)

        grid = QGridLayout()
        grid.addWidget(QLabel('名称:'), 0, 0)
        name_edit = QLineEdit(server_data.get('name', '') if server_data else '')
        grid.addWidget(name_edit, 0, 1)

        grid.addWidget(QLabel('协议:'), 1, 0)
        proto_combo = QComboBox()
        proto_combo.addItems(['ftp', 'ftps', 'webdav', 'https'])
        if server_data:
            proto_combo.setCurrentText(server_data.get('protocol', 'ftp'))
        grid.addWidget(proto_combo, 1, 1)

        grid.addWidget(QLabel('主机:'), 2, 0)
        host_edit = QLineEdit(server_data.get('host', '') if server_data else '')
        grid.addWidget(host_edit, 2, 1)

        grid.addWidget(QLabel('端口:'), 3, 0)
        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(server_data.get('port', 21) if server_data else 21)
        grid.addWidget(port_spin, 3, 1)

        grid.addWidget(QLabel('用户名:'), 4, 0)
        user_edit = QLineEdit(server_data.get('username', '') if server_data else '')
        grid.addWidget(user_edit, 4, 1)

        grid.addWidget(QLabel('密码:'), 5, 0)
        pass_edit = QLineEdit(server_data.get('password', '') if server_data else '')
        pass_edit.setEchoMode(QLineEdit.Password)
        grid.addWidget(pass_edit, 5, 1)

        layout.addLayout(grid)
        layout.addSpacing(10)

        btn_lay = QHBoxLayout()
        btn_lay.addStretch()
        cancel = QPushButton('取消')
        cancel.clicked.connect(dlg.reject)
        btn_lay.addWidget(cancel)
        ok = QPushButton('保存')
        ok.clicked.connect(lambda: self._save_server(dlg, name_edit.text(), host_edit.text(),
                            port_spin.value(), user_edit.text(), pass_edit.text(), proto_combo.currentText(), server_data))
        btn_lay.addWidget(ok)
        layout.addLayout(btn_lay)
        dlg.exec_()

    def _save_server(self, dlg, name, host, port, username, password, protocol, old_data=None):
        if not name or not host:
            QMessageBox.warning(dlg, '提示', '名称和主机不能为空')
            return
        servers = load_servers()
        if old_data:
            for s in servers:
                if s.get('name') == old_data.get('name') and s.get('host') == old_data.get('host'):
                    s.update({'name': name, 'host': host, 'port': port,
                              'username': username, 'password': password, 'protocol': protocol})
                    break
        else:
            servers.append({'name': name, 'host': host, 'port': port,
                            'username': username, 'password': password, 'protocol': protocol})
        save_servers(servers)
        self._server_combo.clear()
        for sv in servers:
            self._server_combo.addItem(f"{sv['name']} ({sv['host']})", sv)
        dlg.accept()

    def _delete_server(self):
        idx = self._server_combo.currentIndex()
        if idx < 0:
            return
        servers = load_servers()
        if idx < len(servers):
            servers.pop(idx)
            save_servers(servers)
            self._server_combo.removeItem(idx)

    def _remote_connect(self, parent):
        data = self._server_combo.currentData()
        if not data:
            QMessageBox.warning(parent, '提示', '请先添加服务器')
            return
        sv = RemoteServer.from_dict(data)
        try:
            sv.connect()
            self._current_remote = sv
            self._remote_path = '/'
            self._remote_status.setText(f'已连接 {sv.name} ({sv.host})')
            self._remote_status.setStyleSheet('color: #2ecc71;')
            self._remote_browse('/')
        except Exception as e:
            QMessageBox.critical(parent, '连接失败', str(e))
            self._remote_status.setText('连接失败')
            self._remote_status.setStyleSheet('color: #e74c3c;')

    def _remote_browse(self, path):
        self._remote_tree.clear()
        if not self._current_remote:
            return
        self._remote_path = path
        try:
            items = self._current_remote.list_dir(path)
            # Parent dir（data(1) 必须带 dir 类型，_remote_navigate 才能识别）
            if path != '/':
                parent = QTreeWidgetItem(self._remote_tree, ['..', '', '📁'])
                parent.setData(0, Qt.UserRole, os.path.dirname(path.rstrip('/')) or '/')
                parent.setData(1, Qt.UserRole, {'type': 'dir'})
            for item in items:
                name = item['name']
                if name in ('.', '..'):
                    continue
                sz = format_size(item['size']) if item['type'] == 'file' else ''
                tp = '📁' if item['type'] == 'dir' else '📄'
                wi = QTreeWidgetItem(self._remote_tree, [name, sz, tp])
                wi.setData(0, Qt.UserRole, os.path.join(path.rstrip('/'), name).replace('\\', '/'))
                wi.setData(1, Qt.UserRole, item)
        except Exception as e:
            QMessageBox.warning(self, '错误', f'读取目录失败: {e}')

    def _remote_navigate(self, item, col):
        data = item.data(0, Qt.UserRole)
        info = item.data(1, Qt.UserRole)
        if info and info.get('type') == 'dir':
            self._remote_browse(data)
        elif info and info.get('type') == 'file':
            self._remote_download_file(data)

    def _remote_context(self, pos):
        item = self._remote_tree.itemAt(pos)
        if not item:
            return
        info = item.data(1, Qt.UserRole)
        if not info or info.get('type') != 'file':
            return
        self._remote_tree.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction('下载文件', lambda: self._remote_download_file(item.data(0, Qt.UserRole)))
        menu.exec_(self._remote_tree.viewport().mapToGlobal(pos))

    def _remote_download_file(self, remote_path):
        if not self._current_remote:
            return
        filename = os.path.basename(remote_path)
        save_path, _ = QFileDialog.getSaveFileName(self, '保存文件', filename)
        if not save_path:
            return
        # Download via engine
        url = self._current_remote.download_url(remote_path)
        if url:
            self._add_task(url, save_path)

    def _apply_speed_to_tasks(self):
        for task in self.tasks.values():
            task.set_speed_limit(self.settings.speed_limit)  # 运行中立即生效

    def toggle_speed(self):
        cur = self.settings.speed_limit
        if cur > 0:
            self.settings.speed_limit = 0
            self._speed_btn.setText('限速: 关')
        else:
            val, ok = QInputDialog.getInt(self, '限速设置', '输入限速值 (KB/s):', value=1024, min=1, max=99999)
            if ok:
                self.settings.speed_limit = val
                self._speed_btn.setText(f'限速: {val}')
        self.settings.save()
        self._apply_speed_to_tasks()

    def focus_search(self):
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    def export_tasks(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出任务', 'download_tasks.json', 'JSON (*.json)')
        if not path:
            return
        data = []
        for tid, task in self.tasks.items():
            info = task.get_info()
            data.append({
                'url': task.url, 'save_path': task.save_path,
                'num_threads': task.num_threads,
                'speed_limit': self.settings.speed_limit,
                'status': info['status'], 'downloaded': info['downloaded'], 'total': info['total'],
            })
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        QMessageBox.information(self, '导出成功', f'已导出 {len(data)} 个任务')

    def import_tasks(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入任务', '', 'JSON (*.json)')
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except:
            QMessageBox.critical(self, '导入失败', '文件格式错误')
            return
        if not isinstance(data, list):
            QMessageBox.critical(self, '导入失败', '文件格式错误：根节点应为任务列表')
            return
        ok_count = 0
        for item in data:
            if not isinstance(item, dict) or not item.get('url') or not item.get('save_path'):
                continue  # 跳过缺字段的条目，不中断整个导入
            self._add_task(item['url'], item['save_path'])
            ok_count += 1
        QMessageBox.information(self, '导入成功', f'已导入 {ok_count} 个任务（共 {len(data)} 条记录）')

    @staticmethod
    def _parse_headers(text):
        """把\"Name: Value\"多行文本解析成字典（忽略空行和 # 注释）"""
        out = {}
        for line in (text or '').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or ':' not in line:
                continue
            key, value = line.split(':', 1)
            key = key.strip()
            if key:
                out[key] = value.strip()
        return out

    @staticmethod
    def _format_headers(headers):
        return '\n'.join(f'{k}: {v}' for k, v in (headers or {}).items())

    def open_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('设置')
        dlg.resize(520, 560)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(15, 15, 15, 15)

        tabs = QTabWidget()

        def _dark_page():
            """新建选项卡页并强制深色背景（样式表对 QStackedWidget 页面不一定生效）"""
            page = QWidget()
            page.setAutoFillBackground(True)
            pal = page.palette()
            pal.setColor(page.backgroundRole(), QColor('#1e1e1e'))
            page.setPalette(pal)
            return page

        # ---- 常规 ----
        page1 = _dark_page()
        f1 = QFormLayout(page1)
        dir_edit = QLineEdit(os.path.normpath(self.settings.save_directory or ''))
        dir_edit.setPlaceholderText('例如 D:\\Downloads')
        dir_btn = QPushButton('浏览')
        dir_row = QHBoxLayout()
        dir_row.setContentsMargins(0, 0, 0, 0)
        dir_row.addWidget(dir_edit, 1)
        dir_row.addWidget(dir_btn)
        f1.addRow('默认下载目录', dir_row)
        dir_btn.clicked.connect(
            lambda: dir_edit.setText(self._pick_directory(dir_edit.text(), '选择默认下载目录')
                                     or dir_edit.text()))
        thread_spin = QSpinBox(); thread_spin.setRange(1, 16)
        thread_spin.setValue(self.settings.thread_count)
        f1.addRow('单任务线程数', thread_spin)
        conc_spin = QSpinBox(); conc_spin.setRange(1, 64)
        conc_spin.setValue(self.settings.max_concurrent_tasks)
        f1.addRow('同时下载任务数', conc_spin)
        speed_spin = QSpinBox(); speed_spin.setRange(0, 999999); speed_spin.setSuffix(' KB/s')
        speed_spin.setValue(self.settings.speed_limit)
        f1.addRow('全局限速（0=不限）', speed_spin)
        policy_combo = QComboBox()
        policy_combo.addItem('自动重命名 name (1).ext', 'rename')
        policy_combo.addItem('覆盖已有文件', 'overwrite')
        policy_combo.addItem('跳过已有文件', 'skip')
        _pidx = policy_combo.findData(self.settings.conflict_policy)
        policy_combo.setCurrentIndex(_pidx if _pidx >= 0 else 0)
        f1.addRow('目标文件已存在', policy_combo)
        disk_chk = QCheckBox('下载前检查磁盘剩余空间')
        disk_chk.setChecked(bool(self.settings.check_disk_space))
        f1.addRow(disk_chk)
        sha_chk = QCheckBox('自动使用服务器上的 .sha256 校验下载结果')
        sha_chk.setChecked(bool(getattr(self.settings, 'sha256_auto_probe', True)))
        f1.addRow(sha_chk)
        update_chk = QCheckBox('启动时检查更新（GitHub Releases）')
        update_chk.setChecked(bool(getattr(self.settings, 'check_update_on_start', True)))
        f1.addRow(update_chk)
        free_spin = QSpinBox(); free_spin.setRange(0, 1024000); free_spin.setSuffix(' MB')
        free_spin.setValue(self.settings.min_free_mb)
        f1.addRow('磁盘保留余量', free_spin)
        hint1 = QLabel('线程数作用于单个任务；同时下载任务数决定队列并发上限，\n'
                       '超出部分自动排队（可用右键菜单调整优先级）。\n'
                       '磁盘保留余量：剩余空间低于该值时自动暂停下载，避免写满系统盘\n'
                       '（断点续传的 .part 会自动保留，清理磁盘后点"继续"即可）。')
        hint1.setStyleSheet('color: #b8c4d0;')
        f1.addRow(hint1)
        tabs.addTab(page1, '常规')

        # ---- 网络 ----
        page2 = _dark_page()
        f2 = QFormLayout(page2)
        proxy_edit = QLineEdit(self.settings.proxy)
        proxy_edit.setPlaceholderText('http://127.0.0.1:7890 或 socks5://127.0.0.1:1080（留空=直连）')
        f2.addRow('代理', proxy_edit)
        connect_spin = QSpinBox(); connect_spin.setRange(1, 600); connect_spin.setSuffix(' 秒')
        connect_spin.setValue(self.settings.connect_timeout)
        f2.addRow('连接超时', connect_spin)
        read_spin = QSpinBox(); read_spin.setRange(1, 3600); read_spin.setSuffix(' 秒')
        read_spin.setValue(self.settings.read_timeout)
        f2.addRow('停滞超时', read_spin)
        verify_chk = QCheckBox('校验服务器 TLS 证书（自签名证书站点需关闭）')
        verify_chk.setChecked(bool(self.settings.verify_ssl))
        f2.addRow(verify_chk)
        cookie_combo = QComboBox()
        cookie_combo.addItem('自动：服务器要求认证时才读取', 'auto')
        cookie_combo.addItem('始终读取浏览器 Cookie', 'always')
        cookie_combo.addItem('不使用浏览器 Cookie', 'off')
        _cidx = cookie_combo.findData(getattr(self.settings, 'cookie_mode', 'auto'))
        cookie_combo.setCurrentIndex(_cidx if _cidx >= 0 else 0)
        f2.addRow('浏览器 Cookie', cookie_combo)
        note2 = QLabel('停滞超时：连接后长时间收不到数据即判定失败并重试。\n'
                       'Cookie 选"自动"时，普通下载不会读取浏览器数据，\n'
                       '只有服务器返回 401/403 需要登录态时才去读取。')
        note2.setStyleSheet('color: #b8c4d0;')
        f2.addRow(note2)
        tabs.addTab(page2, '网络')

        # ---- 重试 ----
        page3 = _dark_page()
        f3 = QFormLayout(page3)
        retry_spin = QSpinBox(); retry_spin.setRange(0, 20)
        retry_spin.setValue(self.settings.retry_count)
        f3.addRow('分片重试次数', retry_spin)
        backoff_spin = QDoubleSpinBox(); backoff_spin.setRange(0.0, 60.0)
        backoff_spin.setSingleStep(0.5); backoff_spin.setSuffix(' 秒')
        backoff_spin.setValue(float(self.settings.retry_backoff))
        f3.addRow('初始退避', backoff_spin)
        note3 = QLabel('失败后按 退避×2ⁿ 重试（含抖动，上限 30 秒），\n并从已写入的位置断点继续，不会重复下载。\n4xx（认证/权限/不存在）不重试。')
        note3.setStyleSheet('color: #b8c4d0;')
        f3.addRow(note3)
        tabs.addTab(page3, '重试')

        # ---- 自定义请求头 ----
        page4 = _dark_page()
        f4 = QVBoxLayout(page4)
        f4.addWidget(QLabel('每行一个，格式 Name: Value（会覆盖同名默认头）'))
        headers_edit = QPlainTextEdit(self._format_headers(self.settings.custom_headers))
        headers_edit.setPlaceholderText('Referer: https://example.com/\nAuthorization: Bearer xxx')
        f4.addWidget(headers_edit)
        tabs.addTab(page4, '自定义请求头')

        layout.addWidget(tabs)
        layout.addSpacing(10)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton('保存并关闭')

        def _apply_settings():
            new_dir = os.path.normpath(dir_edit.text().strip()) if dir_edit.text().strip() else ''
            if new_dir:
                self.settings.save_directory = new_dir
            self.settings.thread_count = thread_spin.value()
            self.settings.max_concurrent_tasks = conc_spin.value()
            self.settings.speed_limit = speed_spin.value()
            self.settings.proxy = proxy_edit.text().strip()
            self.settings.connect_timeout = connect_spin.value()
            self.settings.read_timeout = read_spin.value()
            self.settings.verify_ssl = verify_chk.isChecked()
            self.settings.retry_count = retry_spin.value()
            self.settings.retry_backoff = backoff_spin.value()
            self.settings.custom_headers = self._parse_headers(headers_edit.toPlainText())
            self.settings.conflict_policy = policy_combo.currentData()
            self.settings.check_disk_space = disk_chk.isChecked()
            self.settings.min_free_mb = free_spin.value()
            self.settings.cookie_mode = cookie_combo.currentData()
            self.settings.check_update_on_start = update_chk.isChecked()
            self.settings.sha256_auto_probe = sha_chk.isChecked()
            self.settings.save()
            self._apply_speed_to_tasks()   # 限速对运行中任务立即生效
            self._schedule()               # 并发上限调大时立即启动排队任务
            dlg.accept()

        close_btn.clicked.connect(_apply_settings)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        dlg.exec_()
    # ---- 更新对话框 ----

    _KIND_LABELS = {'onefile': '单文件版', 'standalone': '目录版', 'source': '源码运行'}

    def _show_update_dialog(self, release, allow_auto=True):
        if getattr(self, '_update_dlg', None) is not None:
            return
        dlg = self._build_update_dialog(release, allow_auto=allow_auto)
        self._update_dlg = dlg
        try:
            dlg.exec_()
        finally:
            self._update_dlg = None
            self._update_bar = None

    def _build_update_dialog(self, release, allow_auto=True):
        """构建更新对话框（不执行 exec_，便于测试）

        allow_auto=False 时禁用"下载并安装"：上次自动替换失败过，再点只会重复失败，
        应该引导用户手动下载。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle('发现新版本')
        dlg.resize(580, 470)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 18)

        head = QLabel(f'<b style="font-size:13pt">新版本 {release.get("tag", "")}</b>')
        layout.addWidget(head)
        layout.addWidget(QLabel(f'当前版本：v{__version__}'))

        notes = QPlainTextEdit(release.get('notes') or '（本次发布没有填写说明）')
        notes.setReadOnly(True)
        layout.addWidget(notes, 1)

        kind = updater.detect_install_kind()
        asset_name, _info = updater.pick_asset(release, kind)
        kind_text = self._KIND_LABELS.get(kind, kind)
        info_text = f'安装方式：{kind_text}'
        if asset_name:
            info_text += f'　将下载：{asset_name}'
        else:
            info_text += '　（该发布没有适用于当前安装方式的文件）'
        layout.addWidget(QLabel(info_text))

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        layout.addWidget(bar)
        self._update_bar = bar

        btn_row = QHBoxLayout()
        page_btn = QPushButton('打开发布页面')
        page_btn.clicked.connect(self.open_releases_page)
        btn_row.addWidget(page_btn)
        btn_row.addStretch()
        later_btn = QPushButton('稍后')
        later_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(later_btn)
        install_btn = QPushButton('下载并安装' if allow_auto else '需手动下载（上次未成功）')
        install_btn.setStyleSheet('QPushButton { background: #2ecc71; color: white; padding: 6px 18px; border-radius: 3px; }')
        install_btn.clicked.connect(lambda: self._start_self_update(dlg, release))
        btn_row.addWidget(install_btn)
        if not asset_name or not allow_auto:
            install_btn.setEnabled(False)
        layout.addLayout(btn_row)
        return dlg

    def _start_self_update(self, dlg, release):
        kind = updater.detect_install_kind()
        ok, reason = updater.can_self_update(kind)
        if not ok:
            QMessageBox.warning(self, '无法自动更新', f'{reason}\n\n可手动从发布页面下载。')
            self.open_releases_page()
            return
        target = updater.install_root(kind)
        if QMessageBox.question(
                self, '确认更新',
                f'将下载新版本，并在程序退出后替换：\n{target}\n随后自动重新启动。\n\n继续吗？',
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        bar = getattr(self, '_update_bar', None)
        if bar is not None:
            bar.setRange(0, 0)      # 未知总量：忙碌指示

        def worker():
            try:
                updater.self_update(release, kind,
                                    progress=lambda d, t: self._update_progress.emit(d, t))
            except Exception as e:
                self._update_error.emit(f'更新失败：{e}', False)
                return
            self._update_done.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_progress(self, done, total):
        bar = getattr(self, '_update_bar', None)
        if bar is None:
            return
        if total > 0:
            bar.setRange(0, 100)
            bar.setValue(int(done * 100 / total))
            bar.setFormat(f'%p%  ({format_size(done)} / {format_size(total)})')
        else:
            bar.setRange(0, 0)

    def _on_update_done(self):
        self._closing = True
        dlg = getattr(self, '_update_dlg', None)
        if dlg is not None:
            dlg.accept()
        QMessageBox.information(
            self, '更新已就绪',
            '新版本已下载完成。\n\n程序将立即退出，随后自动替换文件并重新启动。')
        self.close()      # 走 closeEvent：保存设置/任务列表并停止下载

    # ---- 杀软误报自助 ----

    def _self_hash_text(self):
        """本程序文件的 SHA256（打包版才有意义）"""
        import hashlib
        target = updater.install_root()
        if updater.detect_install_kind() == 'source':
            return None, '源码运行模式，无需校验'
        if not os.path.isfile(target):
            return None, '当前是目录版，请对 main.exe 计算哈希'
        try:
            h = hashlib.sha256()
            with open(target, 'rb') as f:
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    h.update(chunk)
            return h.hexdigest(), target
        except OSError as e:
            return None, f'读取失败: {e}'

    def show_av_help(self):
        dlg = self._build_av_help_dialog()
        dlg.exec_()

    def _build_av_help_dialog(self):
        """误报自助说明（只读文本 + 复制按钮，不执行任何提权操作）。

        程序绝不会去修改杀软设置：自动关闭/规避杀软属于恶意软件行为，
        也会被 Defender 判为篡改。加不加白名单由用户自己决定并手动执行。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle('被杀毒软件误报了？')
        dlg.resize(640, 520)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 18)

        target = updater.install_root()
        kind = updater.detect_install_kind()
        kind_text = {'onefile': '单文件版', 'standalone': '目录版', 'source': '源码运行'}.get(kind, kind)
        digest, info = self._self_hash_text()

        lines = [
            '<b>本程序不会也不能替你关闭杀毒软件</b>',
            '自动规避杀软属于恶意软件行为，也会被杀软判为"篡改防护"。',
            '是否加白名单，请你自己判断后手动操作。',
            '',
            '<b>加白名单之前，先确认文件没被篡改：</b>',
        ]
        if digest:
            lines.append(f'当前文件：{info}')
            lines.append(f'SHA256：{digest}')
            lines.append('和 Release 说明里公布的哈希比对，一致才继续。')
        else:
            lines.append(info)
        lines += [
            '',
            '<b>Windows 安全中心（Defender）加入排除项：</b>',
            '设置 → 隐私和安全性 → Windows 安全中心 → 病毒和威胁防护',
            '→ "病毒和威胁防护"设置 → 管理设置 → 排除项 → 添加或删除排除项',
            f'→ 添加排除项 → 选择"文件"或"文件夹" → 选中下面这个路径：',
            f'　{target}',
            '',
            '也可以自己以【管理员】身份打开 PowerShell，粘贴程序里复制的命令执行。',
            '',
            '<b>使用第三方杀软（360 / 火绒 / 腾讯电脑管家 / 卡巴斯基等）：</b>',
            '在其设置里找到"信任区 / 排除项 / 白名单"，把上面的路径加进去，',
            '然后把被隔离的文件从"隔离区"还原。',
            '',
            '<b>不想加白名单？</b>可以直接用源码运行（python main.py），或下载 zip 目录版，',
            '目录版的误报率明显低于单文件版。',
        ]
        label = QLabel('<br>'.join(lines))
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(label)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        cmd_btn = QPushButton('复制 Defender 排除命令')

        def copy_cmd():
            cmd = f"Add-MpPreference -ExclusionPath '{target}'"
            QApplication.clipboard().setText(cmd)
            QMessageBox.information(
                dlg, '已复制',
                '命令已复制。\n\n请自行以【管理员】身份打开 PowerShell，粘贴执行：\n'
                '（程序不会替你执行任何提权或修改杀软设置的操作）')

        cmd_btn.clicked.connect(copy_cmd)
        cmd_btn.setEnabled(kind != 'source')
        btn_row.addWidget(cmd_btn)
        hash_btn = QPushButton('复制 SHA256')
        hash_btn.setEnabled(bool(digest))
        hash_btn.clicked.connect(lambda: (QApplication.clipboard().setText(digest or ''),
                                          QMessageBox.information(dlg, '已复制', 'SHA256 已复制到剪贴板')))
        btn_row.addWidget(hash_btn)
        page_btn = QPushButton('打开发布页面')
        page_btn.clicked.connect(self.open_releases_page)
        btn_row.addWidget(page_btn)
        btn_row.addStretch()
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        return dlg

    def open_releases_page(self):
        QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE))

    def check_updates(self, silent=False):
        """检查更新；silent=True 表示后台静默检查（无更新时不打扰用户）"""
        if getattr(self, '_update_checking', False):
            return
        self._update_checking = True
        if not silent:
            self.status_label.setText('正在检查更新...')

        def worker():
            try:
                release, has_update = updater.check_for_update()
                self._update_result.emit(release, has_update, silent)
            except Exception as e:
                self._update_error.emit(str(e), silent)
            finally:
                self._update_checking = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_error(self, message, silent):
        if not silent and not getattr(self, '_closing', False):
            QMessageBox.warning(self, '检查更新失败',
                                f'无法获取版本信息：\n{message}\n\n可手动访问发布页面查看。')
        self.status_label.setText('检查更新失败')

    def _on_update_result(self, release, has_update, silent):
        if getattr(self, '_closing', False):
            return
        if release is None:
            return
        if not has_update:
            # 已经是最新版本：若之前记录过"更新尝试"，说明那次成功了，清理掉
            updater.clear_update_attempt()
            if not silent:
                QMessageBox.information(self, '检查更新',
                                        f'当前已是最新版本 v{__version__}')
            self.status_label.setText(f'已是最新版本 v{__version__}')
            return

        # 上次尝试更新到这个版本却没成功（程序仍是旧版本）：
        # 不能再让用户点"下载并安装"——替换失败时那就是"反复弹窗、反复失败"的死循环。
        failed = updater.failed_attempt_tag()
        blocked = bool(failed) and updater.parse_version(failed) == release.get('version')
        self.status_label.setText(f'发现新版本 {release["tag"]}')
        if blocked and not silent:
            log_path = os.path.join(tempfile.gettempdir(), 'fd_update.log')
            QMessageBox.warning(
                self, '上次自动更新未成功',
                f'上次尝试更新到 {failed} 没有成功，当前仍是 v{__version__}。'
                '\n\n为避免反复失败，本次不再自动替换。'
                '\n请从发布页面手动下载覆盖，或查看更新日志：\n' + log_path)
        self._show_update_dialog(release, allow_auto=not blocked)
    def show_about(self):
        text = ("⚡ 极速下载器 Pro - Fast Downloader Pro  v" + __version__ + "\n\n"
                "▸ 多线程并发下载\n▸ 浏览器 Cookie 导入\n"
                "▸ Playwright 浏览器降级\n▸ curl_cffi TLS 指纹模拟\n"
                "▸ 断点续传 / 暂停 / 恢复\n▸ 批量下载 / 导出导入")
        QMessageBox.about(self, '关于 极速下载器 Pro', text)

    def on_clipboard_url(self, url):
        result = QMessageBox.question(self, '发现下载链接', f'是否下载:\n{url}',
                                      QMessageBox.Yes | QMessageBox.No)
        if result == QMessageBox.Yes:
            self.add_task_dialog()

    def closeEvent(self, event):
        self._closing = True  # 关闭期间不再弹任何错误对话框
        self.settings.save()
        self._persist_tasks(force=True)   # 保存任务列表，下次启动可恢复
        self.clip_watcher.stop()
        for task in list(self.tasks.values()):
            if task.status == 'running':
                task.stop()
        event.accept()
