# paths.py - 数据文件位置解析
#
# Nuitka --onefile 会把程序解压到 %TEMP%\\onefile_XXXX 后运行，退出时自动删除该目录。
# 若数据文件（设置/日志/缓存/服务器列表）仍按 __file__ 目录存放，写入的内容会在
# 每次退出后丢失。这里统一解析出持久化目录：
#   - 源码运行（开发）        -> 项目根目录（与 downloader_settings.json 同目录）
#   - 打包运行（onefile/standalone）-> %LOCALAPPDATA%\\FastDownloader（回退 %APPDATA%）
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def get_data_dir():
    if getattr(sys, 'frozen', False):
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
        if base:
            d = os.path.join(base, 'FastDownloader')
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                pass
        # 极端情况（无用户目录）：退回源码目录，至少不会崩溃
    return _HERE


def data_file(name):
    return os.path.join(get_data_dir(), name)

