# paths.py - 数据文件位置解析
#
# Nuitka --onefile 会把程序解压到 %TEMP%\\onefile_XXXX 后运行，退出时自动删除该目录。
# 若数据文件（设置/日志/缓存/服务器列表/插件）仍按 __file__ 目录存放，写入的内容
# 会在每次退出后丢失——表现就是"改过的设置没保存""拖进去的插件下次启动不见了"。
#
# 这里统一解析出持久化目录：
#   - 源码运行（开发）              -> 项目根目录（与 downloader_settings.json 同目录）
#   - 打包运行（onefile/standalone）-> %LOCALAPPDATA%\\FastDownloader（回退 %APPDATA%）
#
# 判定"是否打包运行"不能只看 sys.frozen —— 那是 PyInstaller 的约定，**Nuitka 不设置它**。
# 实测 Nuitka 4.1.3 的 onefile 产物（tools 探针）：
#     sys.frozen      == 缺失
#     sys._MEIPASS    == 缺失
#     sys.executable  == %TEMP%\\onefile_XXXX\\python.exe   （是解释器名，不能拿它判断）
#     sys.argv[0]     == 真正的 exe 路径
#     __compiled__    == __nuitka_version__(standalone=True, onefile=True, ...)
# 所以主判据是 __compiled__（updater.detect_install_kind() 用的也是它）。
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

APP_DIR_NAME = 'FastDownloader'


def is_packaged():
    """是否运行在打包产物里（Nuitka onefile/standalone、PyInstaller 等）"""
    if globals().get('__compiled__') is not None:
        return True                      # Nuitka：每个被编译的模块都会注入
    if getattr(sys, 'frozen', False):
        return True                      # PyInstaller / cx_Freeze
    if getattr(sys, '_MEIPASS', None):
        return True                      # PyInstaller onefile
    if os.environ.get('NUITKA_ONEFILE_BINARY') or os.environ.get('NUITKA_ONEFILE_PARENT'):
        return True                      # Nuitka onefile 引导进程设置的标记
    return False


def _user_data_base():
    """用户数据根目录：环境变量优先，取不到就直接问 Windows。

    受限环境里 LOCALAPPDATA/APPDATA 可能为空（实测某些 shell、任务计划程序里就是），
    这时不能就此放弃——退回临时解压目录等于每次退出都丢数据。
    """
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if base:
        return base
    if os.name == 'nt':
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            shell32 = ctypes.windll.shell32
            # CSIDL_LOCAL_APPDATA = 0x1c，CSIDL_APPDATA = 0x1a
            for csidl in (0x001c, 0x001a):
                if shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
                    return buf.value
        except Exception:
            pass
    return None


def get_data_dir():
    if is_packaged():
        base = _user_data_base()
        if base:
            d = os.path.join(base, APP_DIR_NAME)
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                pass
        # 极端受限环境（连用户目录都取不到）：宁可落在 exe 旁边，
        # 也绝不能用 __file__ 目录 —— 那是 onefile 的临时解压目录，退出就被删。
        argv0 = sys.argv[0] if sys.argv else ''
        exe_dir = os.path.dirname(os.path.abspath(argv0)) if argv0 else ''
        if exe_dir and os.path.isdir(exe_dir):
            return exe_dir
    return _HERE


def data_file(name):
    return os.path.join(get_data_dir(), name)
