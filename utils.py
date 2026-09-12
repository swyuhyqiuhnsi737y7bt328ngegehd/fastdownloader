import os
import urllib.parse


def _default_save_path(base_dir, url):
    """按默认下载目录 + 链接推断保存路径。

    链接能识别出文件名时返回 目录/文件名，否则返回目录本身
    （引擎在保存路径是目录时会自动补文件名）。
    """
    base = os.path.normpath(base_dir or os.path.expanduser('~'))
    url = (url or '').strip()
    if url.lower().startswith(('http://', 'https://', 'ftp://', 'ftps://')):
        name = _extract_filename(url)
        if name:
            return os.path.normpath(os.path.join(base, name))
    return base


def _sanitize_filename(name):
    """过滤 Windows 文件名非法字符（<>:"/\\|?* 和控制字符），防止路径注入"""
    name = name.strip().strip('.')
    invalid = '<>:"/\\|?*'
    name = ''.join(c for c in name if c not in invalid and ord(c) >= 32)
    return name or 'download'


def _extract_filename(url):
    """从 URL 推断文件名（优先响应头参数，其次路径末段）"""
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


def format_size(size):
    if size < 0:
        return '未知'
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def format_time(sec):
    if sec < 0:
        return '计算中'
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"
