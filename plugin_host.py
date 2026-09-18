# plugin_host.py - 插件宿主：发现、加载并调用 .dll 插件
#
# 插件是一个导出固定 C 函数的 DLL（接口见 plugins/plugin_api.h）。
# 这里用 ctypes 直接调用，不依赖 Python ABI，也不做任何注入：
# 插件能做什么完全由它导出的那几个函数决定，出错只会让对应钩子返回失败，
# 不会影响下载器本身。
import ctypes
import hashlib
import os
import shutil
import sys
import threading

from paths import data_file

PLUGIN_HELP_TEXT = """Fast Downloader Pro —— 插件说明
=====================================

【安装插件】
  把 .dll 文件直接拖到主窗口上，确认后会自动复制到本目录，下次启动时加载。
  也可以菜单「插件 → 打开插件目录」，手动把 DLL 放进来，再点「重新加载插件」。

  安装前的确认框会显示文件名、大小和 SHA256 —— 插件是能执行代码的 DLL，
  请只安装来源可信的。

【本目录】
  %LOCALAPPDATA%\\FastDownloader\\plugins\\        （打包版）
  <项目目录>\\plugins\\                            （源码运行）

【管理插件】
  菜单「插件 → 插件管理」：查看已装插件、启用/停用、删除、重新加载、打开目录。
  加载失败的插件会留在列表里并显示原因（例如接口版本不匹配）。

【可以挂的钩子】
  接口定义见同目录的 plugin_api.h。只有 fd_plugin_api_version() 必须实现，
  其余按需实现即可；没实现的钩子会被跳过。

  fd_plugin_api_version()          必须。返回 FD_PLUGIN_API_VERSION(1)，不匹配则拒绝加载
  fd_plugin_name / _version /      可选。插件名、版本、说明、作者，显示在插件管理里
      _description / _author
  fd_plugin_on_load()              加载后调用；返回非 0 表示插件不可用
  fd_plugin_on_unload()            卸载/退出时调用，释放资源
  fd_plugin_on_url(char* url,      任务创建前改写下载链接（镜像加速、清洗参数）。
      int size)                    返回 1 表示已改写，0 未改，负数表示拒绝该链接
  fd_plugin_on_headers(const char* 为链接追加请求头，按 "Name: Value\\n" 每行一条，
      url, char* out, int size)    返回写入的字节数（0 表示不加）
  fd_plugin_on_task_start(url,     任务开始下载
      save_path)
  fd_plugin_on_task_done(path,     任务成功；sha256 可能为空字符串
      size, sha256)
  fd_plugin_on_error(url, msg)     任务失败
  fd_plugin_on_progress(url, pct)  进度更新（调用频繁，请自行节流）

【自己写一个】
  1. 新建 .c 文件，包含 plugin_api.h；
  2. 至少实现：
         #include "plugin_api.h"
         int fd_plugin_api_version(void) { return FD_PLUGIN_API_VERSION; }
  3. 按需实现上表中的钩子；
  4. 编译成 DLL：
         gcc -shared -O2 -I. -o myplugin.dll myplugin.c        （MinGW）
         cl /LD /O2 myplugin.c                                  （MSVC）
  5. 把 DLL 拖进主窗口安装。

  注意：
    - 接口是纯 C ABI，不依赖 Python 版本，MSVC / MinGW 编的都能用；
    - 字符串一律 UTF-8；插件返回的字符串需在插件内保持有效（静态或全局）；
    - 写缓冲区时务必检查 size，越界会破坏主程序内存；
    - 插件运行在主进程内，耗时操作会拖慢界面，建议自己开线程或快速返回；
    - 插件抛异常/崩溃主程序无法兜底，请在插件内部做好错误处理。

【示例插件】
  仓库 plugins/examples/ 里有三个示例（源码 + 预编译 DLL + 一键编译脚本）：
    strip_tracking.dll   下载前去掉 utm_* / fbclid / gclid 等统计参数
    github_mirror.dll    GitHub 下载直链走镜像（环境变量 FD_GITHUB_MIRROR 可改/清空）
    task_logger.dll      每次下载成功追加一行到 download_history.csv
  安装版里可能没带示例，可从项目仓库获取。

【取消插件】
  在「插件管理」里停用或删除即可；停用只对本次运行有效，删除是永久移除文件。
"""
PLUGIN_HELP_NAME = "插件说明.txt"

PLUGIN_API_SUMMARY = """/* plugin_api.h - Fast Downloader Pro 插件接口（摘要版）
 *
 * 完整版见项目仓库 plugins/plugin_api.h。
 * 只有 fd_plugin_api_version() 是必须实现的，其余按需实现。
 */
#ifndef FD_PLUGIN_API_H
#define FD_PLUGIN_API_H

#define FD_PLUGIN_API_VERSION 1

#ifdef _WIN32
#  define FD_EXPORT __declspec(dllexport)
#else
#  define FD_EXPORT __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

FD_EXPORT int fd_plugin_api_version(void);                      /* 必须 */

FD_EXPORT const char* fd_plugin_name(void);                     /* 可选：元信息 */
FD_EXPORT const char* fd_plugin_version(void);
FD_EXPORT const char* fd_plugin_description(void);
FD_EXPORT const char* fd_plugin_author(void);

FD_EXPORT int  fd_plugin_on_load(void);                         /* 加载后；非 0 表示不可用 */
FD_EXPORT void fd_plugin_on_unload(void);

FD_EXPORT int  fd_plugin_on_url(char* url, int size);           /* 1=已改写 0=未改 <0=拒绝 */
FD_EXPORT int  fd_plugin_on_headers(const char* url, char* out, int size);

FD_EXPORT void fd_plugin_on_task_start(const char* url, const char* save_path);
FD_EXPORT void fd_plugin_on_task_done(const char* path, long long size, const char* sha256);
FD_EXPORT void fd_plugin_on_error(const char* url, const char* message);
FD_EXPORT void fd_plugin_on_progress(const char* url, double percent);

#ifdef __cplusplus
}
#endif

#endif /* FD_PLUGIN_API_H */
"""

PLUGIN_DIR = data_file('plugins')
API_VERSION = 1

# 钩子签名：名称 -> (参数类型列表, 返回类型)
_HOOKS = {
    'on_load': ([], ctypes.c_int),
    'on_unload': ([], None),
    'on_url': ([ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
    'on_headers': ([ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
    'on_task_start': ([ctypes.c_char_p, ctypes.c_char_p], None),
    'on_task_done': ([ctypes.c_char_p, ctypes.c_longlong, ctypes.c_char_p], None),
    'on_error': ([ctypes.c_char_p, ctypes.c_char_p], None),
    'on_progress': ([ctypes.c_char_p, ctypes.c_double], None),
}

_META = {
    'name': ctypes.c_char_p,
    'version': ctypes.c_char_p,
    'description': ctypes.c_char_p,
    'author': ctypes.c_char_p,
}


def plugin_dir():
    """插件目录（不存在则创建，并保证里面有使用说明与接口头文件）"""
    try:
        os.makedirs(PLUGIN_DIR, exist_ok=True)
    except OSError:
        return PLUGIN_DIR
    if not os.path.exists(os.path.join(PLUGIN_DIR, PLUGIN_HELP_NAME)):
        write_plugin_docs()
    return PLUGIN_DIR


def write_plugin_docs(force=False):
    """把插件说明和接口头文件放进插件目录。

    用户打开插件目录就能看到怎么用、怎么自己写，不用去翻仓库。
    返回 (说明文件路径, 头文件路径)。
    """
    directory = PLUGIN_DIR      # 不要调用 plugin_dir()：它会回头调用本函数
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return None, None
    readme = os.path.join(directory, PLUGIN_HELP_NAME)
    if force or not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(PLUGIN_HELP_TEXT)
        except OSError:
            readme = None
    header = os.path.join(directory, "plugin_api.h")
    if not os.path.exists(header):
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "plugins", "plugin_api.h")
        try:
            if os.path.exists(source):
                shutil.copy2(source, header)
            else:
                with open(header, "w", encoding="utf-8") as f:
                    f.write(PLUGIN_API_SUMMARY)
        except OSError:
            header = None
    return readme, header

def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


class Plugin:
    """一个已加载的插件"""

    def __init__(self, path):
        self.path = path
        self.filename = os.path.basename(path)
        self.name = self.filename
        self.version = ''
        self.description = ''
        self.author = ''
        self.enabled = True
        self.error = ''
        self._lib = None
        self._hooks = {}

    # ---- 加载 ----

    def load(self):
        try:
            self._lib = ctypes.CDLL(self.path)
        except OSError as e:
            self.error = f'无法加载 DLL: {e}'
            self._release()
            return False
        try:
            version_fn = self._lib.fd_plugin_api_version
            version_fn.restype = ctypes.c_int
            if version_fn() != API_VERSION:
                self.error = f'接口版本不匹配（插件 {version_fn()}，宿主 {API_VERSION}）'
                self._release()
                return False
        except AttributeError:
            self.error = '不是有效的插件（缺少 fd_plugin_api_version）'
            self._release()
            return False

        for key, rtype in _META.items():
            try:
                fn = getattr(self._lib, f'fd_plugin_{key}')
                fn.restype = rtype
                value = fn()
                if value:
                    setattr(self, key, value.decode('utf-8', 'replace'))
            except (AttributeError, UnicodeDecodeError):
                pass

        for hook, (argtypes, restype) in _HOOKS.items():
            try:
                fn = getattr(self._lib, f'fd_plugin_{hook}')
            except AttributeError:
                continue
            fn.argtypes = argtypes
            fn.restype = restype
            self._hooks[hook] = fn

        if 'on_load' in self._hooks:
            try:
                if self._hooks['on_load']() != 0:
                    self.error = '插件自检未通过（on_load 返回非 0）'
                    self._release()
                    return False
            except Exception as e:                          # noqa: BLE001
                self.error = f'on_load 异常: {e}'
                self._release()
                return False
        return True

    def _release(self):
        """丢弃已加载的库句柄。校验失败或卸载时都要调用，
        否则句柄泄漏，而且界面会把无效插件显示成"已启用"。"""
        self._hooks.clear()
        self._lib = None

    def unload(self):
        if 'on_unload' in self._hooks:
            try:
                self._hooks['on_unload']()
            except Exception:                               # noqa: BLE001
                pass
        self._release()

    # ---- 钩子 ----

    def has(self, hook):
        return hook in self._hooks and self.enabled

    def on_url(self, url):
        """返回 (url, 结果)；结果 0=未改 1=已改 负数=拒绝"""
        fn = self._hooks.get('on_url')
        if fn is None or not self.enabled:
            return url, 0
        buf = ctypes.create_string_buffer(url.encode('utf-8'), max(len(url.encode('utf-8')) + 1, 4096))
        try:
            result = fn(buf, len(buf))
        except Exception as e:                              # noqa: BLE001
            self.error = f'on_url 异常: {e}'
            return url, 0
        if result and result > 0:
            try:
                return buf.value.decode('utf-8', 'replace'), result
            except Exception:                               # noqa: BLE001
                return url, 0
        return url, result

    def on_headers(self, url):
        """返回该插件追加的请求头字典"""
        fn = self._hooks.get('on_headers')
        if fn is None or not self.enabled:
            return {}
        buf = ctypes.create_string_buffer(8192)
        try:
            written = fn(url.encode('utf-8'), buf, len(buf))
        except Exception as e:                              # noqa: BLE001
            self.error = f'on_headers 异常: {e}'
            return {}
        if not written:
            return {}
        text = buf.value.decode('utf-8', 'replace')
        headers = {}
        for line in text.splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                k = k.strip()
                if k:
                    headers[k] = v.strip()
        return headers

    def call(self, hook, *args):
        fn = self._hooks.get(hook)
        if fn is None or not self.enabled:
            return
        try:
            prepared = [a.encode('utf-8') if isinstance(a, str) else a for a in args]
            fn(*prepared)
        except Exception as e:                              # noqa: BLE001
            self.error = f'{hook} 异常: {e}'


class PluginHost:
    """管理插件目录里的所有插件"""

    def __init__(self):
        self.plugins = []
        self._lock = threading.Lock()
        self._errors = []

    def discover(self):
        directory = plugin_dir()
        found = []
        try:
            for name in sorted(os.listdir(directory)):
                if name.lower().endswith('.dll'):
                    found.append(os.path.join(directory, name))
        except OSError as e:
            self._errors.append(f'读取插件目录失败: {e}')
        return found

    def load_all(self):
        """加载目录下所有插件；单个插件出错不影响其它插件"""
        self.unload_all()
        self._errors = []
        for path in self.discover():
            plugin = Plugin(path)
            if plugin.load():
                self.plugins.append(plugin)
            else:
                self._errors.append(f'{plugin.filename}: {plugin.error}')
                self.plugins.append(plugin)     # 保留在列表里，界面上能看到失败原因
        return self.plugins

    def unload_all(self):
        for plugin in self.plugins:
            plugin.unload()
        self.plugins = []

    # ---- 可供下载流程调用的钩子 ----

    def transform_url(self, url):
        """依次让插件改写链接；返回 (最终URL, 拒绝原因)"""
        current = url
        for plugin in self.plugins:
            if not plugin.has('on_url'):
                continue
            new_url, result = plugin.on_url(current)
            if result < 0:
                return current, f'{plugin.name} 拒绝了该链接'
            if result > 0 and new_url:
                current = new_url
        return current, ''

    def collect_headers(self, url):
        headers = {}
        for plugin in self.plugins:
            if plugin.has('on_headers'):
                headers.update(plugin.on_headers(url))
        return headers

    def notify(self, hook, *args):
        with self._lock:
            for plugin in list(self.plugins):
                plugin.call(hook, *args)

    def active(self):
        return [p for p in self.plugins if p._lib is not None and not p.error]


_host = None


def host():
    """全局插件宿主（首次调用时加载）"""
    global _host
    if _host is None:
        _host = PluginHost()
        _host.load_all()
    return _host


def install_dll(src_path, overwrite=False):
    """把 DLL 复制进插件目录。返回 (目标路径, 错误信息)"""
    if not src_path.lower().endswith('.dll'):
        return None, '只支持 .dll 插件'
    if not os.path.isfile(src_path):
        return None, '文件不存在'
    dest = os.path.join(plugin_dir(), os.path.basename(src_path))
    if os.path.exists(dest) and not overwrite:
        return None, '同名插件已存在'
    try:
        import shutil
        shutil.copy2(src_path, dest)
        return dest, ''
    except OSError as e:
        return None, f'复制失败: {e}'
