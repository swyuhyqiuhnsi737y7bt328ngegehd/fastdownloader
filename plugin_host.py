# plugin_host.py - 插件宿主：发现、加载并调用 .dll 插件
#
# 插件是一个导出固定 C 函数的 DLL（接口见 plugins/plugin_api.h）。
# 这里用 ctypes 直接调用，不依赖 Python ABI，也不做任何注入：
# 插件能做什么完全由它导出的那几个函数决定，出错只会让对应钩子返回失败，
# 不会影响下载器本身。
import ctypes
import hashlib
import os
import sys
import threading

from paths import data_file

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
    """插件目录（不存在则创建）"""
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    return PLUGIN_DIR


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
