# downloader_api.py - Python 实现的 C 兼容 API
# 函数签名与 downloader_api.h 完全一致
# 未来替换为 C++ DLL 时，UI 层代码不需要改动（同一套 ctypes 绑定）

import os
import json
import threading
import time as _time
from engine import DownloadTask

_lock = threading.Lock()
_tasks = {}
_next_id = 1
_callback = None
_userdata = None
_running = False

# ---- 状态常量（与 .h 一致） ----
DL_OK = 0
DL_ERR_GENERAL = -1
DL_ERR_INIT = -2
DL_ERR_NOT_FOUND = -3
DL_ERR_BUSY = -4
DL_ERR_PARAM = -5

DL_STATUS_READY = 0
DL_STATUS_RUNNING = 1
DL_STATUS_PAUSED = 2
DL_STATUS_COMPLETED = 3
DL_STATUS_ERROR = 4
DL_STATUS_STOPPED = 5

DL_EVENT_ERROR = 0
DL_EVENT_COMPLETED = 1
DL_EVENT_PAUSED = 2
DL_EVENT_STOPPED = 3

_STATUS_MAP = {
    'ready': DL_STATUS_READY,
    'running': DL_STATUS_RUNNING,
    'paused': DL_STATUS_PAUSED,
    'completed': DL_STATUS_COMPLETED,
    'error': DL_STATUS_ERROR,
    'stopped': DL_STATUS_STOPPED,
}


def _fire_event(task_id, event_type, data):
    cb = _callback
    ud = _userdata
    if cb:
        try:
            cb(task_id, event_type, data, ud)
        except Exception:
            pass


def _on_task_event(task_id, event, data=None):
    event_map = {
        'error': DL_EVENT_ERROR,
        'completed': DL_EVENT_COMPLETED,
        'paused': DL_EVENT_PAUSED,
        'stopped': DL_EVENT_STOPPED,
    }
    et = event_map.get(event, DL_EVENT_ERROR)
    _fire_event(task_id, et, data)


# ====================================================================
# API 函数（签名与 downloader_api.h 一致）
# ====================================================================

def dl_init(config_path=None, callback=None, userdata=None):
    global _callback, _userdata, _running
    with _lock:
        if _running:
            return DL_ERR_INIT
        _callback = callback
        _userdata = userdata
        _running = True
    if config_path:
        from settings import SETTINGS_FILE
        global _config_path
        _config_path = config_path
    return DL_OK


def dl_create_task(url, save_path, num_threads=8, speed_limit_kb=0):
    global _next_id
    if not url or not save_path:
        return DL_ERR_PARAM
    if int(num_threads) < 1:
        return DL_ERR_PARAM
    with _lock:
        tid = _next_id
        _next_id += 1
        task = DownloadTask(tid, url, save_path, num_threads, speed_limit_kb, overwrite=True)
        task.set_callback(_on_task_event)
        _tasks[tid] = task
    return tid


def dl_start_task(task_id):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return DL_ERR_NOT_FOUND
    try:
        task.start()
        if task.status == 'error':
            return DL_ERR_GENERAL
        return DL_OK
    except Exception:
        return DL_ERR_GENERAL


def dl_pause_task(task_id):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return DL_ERR_NOT_FOUND
    try:
        task.pause()
        return DL_OK
    except Exception:
        return DL_ERR_GENERAL


def dl_cancel_task(task_id):
    with _lock:
        task = _tasks.pop(task_id, None)
    if not task:
        return DL_ERR_NOT_FOUND
    try:
        task.stop()
        return DL_OK
    except Exception:
        return DL_ERR_GENERAL


def dl_get_status(task_id):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return DL_ERR_NOT_FOUND
    return _STATUS_MAP.get(task.status, DL_STATUS_READY)


def dl_get_progress(task_id):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return -1.0
    info = task.get_info()
    if info['total'] > 0:
        return (info['downloaded'] / info['total']) * 100.0
    return 0.0


def dl_get_info_json(task_id):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return _alloc_string('{}')
    info = task.get_info()
    info['task_id'] = task_id
    info['url'] = task.url
    info['save_path'] = task.save_path
    info['num_threads'] = task.num_threads
    return _alloc_string(json.dumps(info, ensure_ascii=False))


def dl_free_string(str_ptr):
    pass  # Python GC handles this; in C++ DLL this frees the allocated buffer


def dl_get_error(task_id):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return _alloc_string('task not found')
    return _alloc_string(task._error_msg or '')


def dl_shutdown():
    global _running
    with _lock:
        tasks = list(_tasks.values())
        _tasks.clear()
        _running = False
    for t in tasks:
        try:
            t.stop()
        except Exception:
            pass


# ---- 内存管理（C++ DLL 兼容） ----
_allocated_strings = set()

def _alloc_string(s):
    # Python 版直接返回 str，不做特殊分配
    # C++ DLL 版需要 malloc + 返回指针
    return s


def get_api_functions():
    """返回所有 API 函数的 dict，用于 ctypes 绑定生成"""
    return {
        'dl_init': dl_init,
        'dl_create_task': dl_create_task,
        'dl_start_task': dl_start_task,
        'dl_pause_task': dl_pause_task,
        'dl_cancel_task': dl_cancel_task,
        'dl_get_status': dl_get_status,
        'dl_get_progress': dl_get_progress,
        'dl_get_info_json': dl_get_info_json,
        'dl_free_string': dl_free_string,
        'dl_get_error': dl_get_error,
        'dl_shutdown': dl_shutdown,
    }
