import os, json, time, threading, urllib.parse, socket
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtCore import pyqtSignal

from engine import DownloadTask
from utils import format_size, format_time
from settings import Settings
from clipboard_watcher import ClipboardWatcher
from remote_browser import RemoteServer, load_servers, save_servers

STATUS_LABELS = {
    'running':   '正在下载', 'completed': '已完成',
    'error':     '失败',     'paused':    '已暂停',
    'stopped':   '已停止',   'ready':     '等待中',
}
STATUS_COLORS = {
    'running':   '#5dade2', 'completed': '#58d68d',
    'error':     '#ec7063', 'paused':    '#f5b041',
    'stopped':   '#bdc3c7', 'ready':     '#aeb6bf',
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

def _sanitize_filename(name):
    """过滤 Windows 文件名非法字符（<>:"/ 竖线 ?* 和控制字符），防止路径注入"""
    name = name.strip().strip('.')
    invalid = '<>:"/\\|?*'
    name = ''.join(c for c in name if c not in invalid and ord(c) >= 32)
    return name or 'download'


def _extract_filename(url):
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    for key in ('response-content-disposition', 'rscd', 'filename', 'download_fname'):
        if key in params:
            val = urllib.parse.unquote(params[key][0])
            if 'filename=' in val:
                val = val.split('filename=')[-1].split(';')[0].strip('"\' ')
            val = _sanitize_filename(val)
            if val:
                return val
    filename = urllib.parse.unquote(url.rstrip('/').split('/')[-1].split('?')[0]) or 'download'
    filename = _sanitize_filename(filename)
    if '.' not in filename:
        filename += '.bin'
    return filename


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

    def __init__(self):
        super().__init__()
        self.tasks = {}
        self._task_event.connect(self._on_event_gui)
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
        ver.setStyleSheet("color: #999999;")
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

    def _apply_theme(self):
        self.setStyleSheet("""
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
            QPlainTextEdit { color: #e0e0e0; background: #2a2a2a; border: 1px solid #3a3a3a; }
        """)

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
        vm.addSeparator()
        vm.addAction('关于', self.show_about)

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
            return task.status in ('ready', 'running', 'paused', 'stopped', 'error')
        if f == 'completed':
            return task.status == 'completed'
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

        total = len(self.tasks) - len(self._hidden)
        parts = [f"📦 {total} 个任务"]
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
        可能耗时数秒到数十秒，不能阻塞 GUI 线程。"""
        task._error_notified = False
        threading.Thread(target=task.start, daemon=True).start()

    def _add_task(self, url, save_path):
        tid = self.next_id
        self.next_id += 1
        save_path = os.path.normpath(save_path)
        task = DownloadTask(tid, url, save_path,
                            num_threads=self.settings.thread_count,
                            speed_limit=self.settings.speed_limit,
                            overwrite=True)
        task.set_callback(self._on_task_event)
        self.tasks[tid] = task
        item = QTreeWidgetItem(self.table)
        item.setData(0, Qt.UserRole, tid)
        item.setText(0, '等待中')
        item.setText(1, os.path.basename(save_path))
        item.setText(2, '---')
        item.setData(3, Qt.UserRole, (0.0, 'ready'))
        item.setText(4, '---')
        item.setText(5, '---')
        item.setForeground(0, QColor(STATUS_COLORS['ready']))
        self.table.addTopLevelItem(item)
        self._start_task_async(task)
        self._apply_filter()

    def _on_task_event(self, tid, event, data=None):
        # 由工作线程（下载线程/monitor）调用：只做线程安全的信号投递，
        # 绝不直接操作 Qt 控件（QTimer.singleShot 在非 GUI 线程中回调永远不会执行）
        self._task_event.emit(tid, event, data)

    def _on_event_gui(self, tid, event, data=None):
        """GUI 线程中处理任务事件（信号自动队列到主线程）"""
        if event == 'error' and not getattr(self, '_closing', False):
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
            self.tasks[tid].pause()

    def resume_selected(self):
        tid = self._get_selected_tid()
        if tid is not None and tid in self.tasks:
            t = self.tasks[tid]
            if t.status in ('paused', 'ready', 'error'):
                self._start_task_async(t)

    def stop_selected(self):
        tid = self._get_selected_tid()
        if tid is not None and tid in self.tasks:
            self.tasks[tid].stop()

    def restart_selected(self):
        tid = self._get_selected_tid()
        if tid is not None and tid in self.tasks:
            t = self.tasks[tid]
            t.stop()
            self._start_task_async(t)

    def delete_selected(self):
        tid = self._get_selected_tid()
        if tid is None:
            return
        task = self.tasks.pop(tid, None)
        if task:
            task.stop()
        item = self._find_item(tid)
        if item is not None:
            self.table.invisibleRootItem().removeChild(item)
        self._hidden.discard(tid)

    def start_all(self):
        for t in self.tasks.values():
            if t.status in ('ready', 'paused', 'error'):
                self._start_task_async(t)

    def pause_all(self):
        for t in self.tasks.values():
            if t.status == 'running':
                t.pause()

    def stop_all(self):
        for t in self.tasks.values():
            if t.status in ('running', 'paused'):
                t.stop()

    # ---- Dialogs ----

    def add_task_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('添加下载任务')
        dlg.resize(560, 240)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 18)

        layout.addWidget(QLabel('下载链接'))
        url_edit = QLineEdit()
        layout.addWidget(url_edit)

        layout.addSpacing(8)
        layout.addWidget(QLabel('保存路径'))
        path_layout = QHBoxLayout()
        path_edit = QLineEdit()
        path_edit.setReadOnly(True)
        path_layout.addWidget(path_edit)
        browse_btn = QPushButton('浏览')
        browse_btn.clicked.connect(lambda: self._browse_save(path_edit))
        path_layout.addWidget(browse_btn)
        layout.addLayout(path_layout)

        # Fill from clipboard
        try:
            clip = QApplication.clipboard().text()
            if clip.startswith('http'):
                url_edit.setText(clip)
                fn = _extract_filename(clip)
                base = os.path.normpath(self.settings.save_directory)
                path_edit.setText(os.path.normpath(os.path.join(base, fn)))
        except:
            pass

        layout.addSpacing(12)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel = QPushButton('取消')
        cancel.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel)
        ok = QPushButton('开始下载')
        ok.setStyleSheet("QPushButton { background: #2ecc71; color: white; padding: 6px 20px; border-radius: 3px; }")
        ok.clicked.connect(lambda: self._submit_task(dlg, url_edit.text().strip(), path_edit.text().strip()))
        btn_layout.addWidget(ok)
        layout.addLayout(btn_layout)

        dlg.exec_()

    def _submit_task(self, dlg, url, path):
        if url and path:
            self._add_task(url, path)
            dlg.accept()

    def _browse_save(self, edit):
        path, _ = QFileDialog.getSaveFileName(self, '保存文件', self.settings.save_directory)
        if path:
            path = os.path.normpath(path)
            self.settings.save_directory = os.path.dirname(path)
            self.settings.save()
            edit.setText(path)

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
        limit = self.settings.speed_limit * 1024  # KB/s -> B/s
        for task in self.tasks.values():
            task.speed_limit = limit
            task._thread_speed_limit = limit / task.num_threads if task.num_threads else 0

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

    def open_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('设置')
        dlg.resize(380, 200)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 18, 18, 18)

        layout.addWidget(QLabel('最大线程数'))
        thread_slider = QSlider(Qt.Horizontal)
        thread_slider.setRange(1, 16)
        thread_slider.setValue(self.settings.thread_count)
        thread_slider.valueChanged.connect(lambda v: setattr(self.settings, 'thread_count', v) or self.settings.save())
        layout.addWidget(thread_slider)

        layout.addSpacing(10)
        layout.addWidget(QLabel('下载限速 (KB/s, 0=不限速)'))
        speed_spin = QSpinBox()
        speed_spin.setRange(0, 99999)
        speed_spin.setValue(self.settings.speed_limit)
        def _on_speed_change(v):
            self.settings.speed_limit = v
            self.settings.save()
            self._apply_speed_to_tasks()
        speed_spin.valueChanged.connect(_on_speed_change)
        layout.addWidget(speed_spin)

        layout.addSpacing(15)
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec_()

    def show_about(self):
        text = ("⚡ 极速下载器 Pro - Fast Downloader Pro\n\n"
                "\n\n"
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
        self.clip_watcher.stop()
        for task in list(self.tasks.values()):
            if task.status == 'running':
                task.stop()
        event.accept()
