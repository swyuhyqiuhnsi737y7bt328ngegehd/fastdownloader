import json
import os

from paths import data_file

SETTINGS_FILE = data_file('downloader_settings.json')

# 所有可配置项的默认值；load() 对缺失/类型非法的字段一律回退到这里
DEFAULTS = {
    'thread_count': 8,           # 单任务分片线程数（1-16）
    'speed_limit': 0,            # 全局限速 KB/s，0 = 不限速
    'save_directory': os.path.normpath(os.path.expanduser('~/Downloads')),
    'max_concurrent_tasks': 3,   # 同时下载的任务数上限（队列调度）
    'retry_count': 3,            # 单个分片的最大重试次数
    'retry_backoff': 1.0,        # 重试初始退避秒数（指数增长 + 抖动）
    'connect_timeout': 15,       # 连接超时（秒）
    'read_timeout': 30,          # 读取超时（秒）
    'proxy': '',                 # 代理：http://host:port / socks5://host:port
    'custom_headers': {},        # 附加到每个请求的请求头 {name: value}
    'verify_ssl': False,         # 是否校验服务器 TLS 证书
}

# 数值型字段的合法范围（越界一律回退默认值）
_RANGES = {
    'thread_count': (1, 16),
    'speed_limit': (0, 999999),
    'max_concurrent_tasks': (1, 64),
    'retry_count': (0, 20),
    'retry_backoff': (0.0, 60.0),
    'connect_timeout': (1, 600),
    'read_timeout': (1, 3600),
}


def _clean(key, value):
    """校验单个配置值，非法则返回默认值"""
    default = DEFAULTS[key]
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return dict(default)
        # 只保留字符串键值对，避免 JSON 里塞进奇怪结构
        return {str(k): str(v) for k, v in value.items() if k}
    if key in _RANGES:
        lo, hi = _RANGES[key]
        if isinstance(default, float):
            try:
                num = float(value)
            except (TypeError, ValueError):
                return default
        else:
            try:
                num = int(value)
            except (TypeError, ValueError):
                return default
        return num if lo <= num <= hi else default
    if key == 'verify_ssl':
        return bool(value)
    if key == 'save_directory':
        if not isinstance(value, str) or not value.strip():
            return default
        return os.path.normpath(value)
    if key == 'proxy':
        return value.strip() if isinstance(value, str) else default
    return value if isinstance(value, type(default)) else default


class Settings:
    def __init__(self):
        for key, default in DEFAULTS.items():
            setattr(self, key, dict(default) if isinstance(default, dict) else default)
        self.load()

    def load(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        for key in DEFAULTS:
            if key in data:
                setattr(self, key, _clean(key, data[key]))

    def save(self):
        data = {key: getattr(self, key) for key in DEFAULTS}
        try:
            # 原子写：避免写一半崩溃导致配置文件损坏
            tmp = SETTINGS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
        except OSError:
            pass
