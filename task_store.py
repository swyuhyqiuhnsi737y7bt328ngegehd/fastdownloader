# task_store.py - 任务列表持久化（JSON），用于重启/崩溃后恢复任务
#
# 只保存"任务是什么"，不保存运行态对象：恢复时按状态重建 DownloadTask，
# 未完成的任务依赖 .part + .part.meta 断点续传，不会重新下载已完成的部分。
import json
import os
import time

from paths import data_file

STORE_FILE = data_file('tasks.json')

# 这些状态在恢复时视为"未完成"，重启后不自动开始（避免意外流量）
_RESUMABLE = ('running', 'queued', 'ready', 'paused', 'error', 'stopped')


def save_tasks(records):
    """records: [{'url':..., 'save_path':..., 'status':..., 'downloaded':..., 'total':..., 'priority':...}]"""
    try:
        data = {'version': 1, 'saved_at': time.time(), 'tasks': list(records)}
        tmp = STORE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE_FILE)   # 原子替换：写一半崩溃也不会破坏旧文件
        return True
    except OSError:
        return False


def load_tasks():
    """返回任务记录列表；文件缺失/损坏时返回空列表"""
    if not os.path.exists(STORE_FILE):
        return []
    try:
        with open(STORE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    tasks = data.get('tasks')
    if not isinstance(tasks, list):
        return []
    out = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        url = item.get('url')
        save_path = item.get('save_path')
        if not url or not save_path:
            continue
        out.append({
            'url': str(url),
            'save_path': str(save_path),
            'status': str(item.get('status') or 'ready'),
            'downloaded': int(item.get('downloaded') or 0),
            'total': int(item.get('total') or 0),
            'priority': int(item.get('priority') or 0),
        })
    return out


def restored_status(record):
    """把持久化状态映射为恢复后的初始状态"""
    path = record.get('save_path', '')
    if record.get('status') == 'completed' and os.path.exists(path):
        return 'completed'
    if record.get('status') == 'completed':
        return 'ready'          # 文件已不在，需要重新下载
    if record.get('status') in _RESUMABLE:
        part = path + '.part'
        return 'paused' if os.path.exists(part) else 'ready'
    return 'ready'
