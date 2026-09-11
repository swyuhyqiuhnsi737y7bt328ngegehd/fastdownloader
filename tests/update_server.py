# tests/update_server.py - 模拟 GitHub Releases API 的本地服务器（更新检查测试用）
import hashlib
import http.server
import io
import json
import socketserver
import threading
import zipfile


def make_zip(files):
    """files: {name: bytes} -> zip 字节流"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, body, status=200, content_type='application/json', headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        state = self.server.state
        with state['lock']:
            state['requests'].append(self.path)
        path = self.path.split('?')[0]

        if path.endswith('/releases/latest'):
            if state.get('fail_api'):
                self._send(b'{"message":"boom"}', status=500)
                return
            self._send(json.dumps(self.server.release).encode())
            return

        if path.startswith('/assets/'):
            name = path[len('/assets/'):]
            data = self.server.asset_data.get(name)
            if data is None:
                self._send(b'not found', status=404, content_type='text/plain')
                return
            self._send(data, content_type='application/octet-stream')
            return

        self._send(b'{}', status=404)

    do_HEAD = do_GET


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


class UpdateServer:
    """上下文管理器：本地假 GitHub Releases API + 资产下载"""

    def __init__(self, tag='v9.9.9', exe_bytes=b'MZ-new-single-file-exe',
                 zip_files=None, with_digest=True, asset_names=None,
                 notes='本版本修复了很多问题。'):
        self.tag = tag
        names = asset_names or {}
        self.exe_name = names.get('onefile', 'FastDownloader.exe')
        self.zip_name = names.get('standalone', 'fastdownloader.zip')
        self.exe_bytes = exe_bytes
        self.zip_files = zip_files if zip_files is not None else {
            'main.exe': b'MZ-new-standalone-exe',
            'python312.dll': b'dll',
            'lib/extra.pyd': b'pyd',
        }
        self.zip_bytes = make_zip(self.zip_files)
        self.with_digest = with_digest
        self.notes = notes
        self._httpd = None

    def __enter__(self):
        self._httpd = _Server(('127.0.0.1', 0), _Handler)
        port = self._httpd.server_address[1]
        base = f'http://127.0.0.1:{port}'
        self.asset_data = {self.exe_name: self.exe_bytes, self.zip_name: self.zip_bytes}
        self.release = {
            'tag_name': self.tag,
            'name': f'Release {self.tag}',
            'body': self.notes,
            'html_url': f'{base}/releases/tag/{self.tag}',
            'published_at': '2026-01-01T00:00:00Z',
            'prerelease': False,
            'assets': [],
        }
        for name, data in self.asset_data.items():
            asset = {
                'name': name,
                'browser_download_url': f'{base}/assets/{name}',
                'size': len(data),
            }
            if self.with_digest:
                asset['digest'] = 'sha256:' + hashlib.sha256(data).hexdigest()
            self.release['assets'].append(asset)
        self._httpd.release = self.release
        self._httpd.asset_data = self.asset_data
        self._httpd.state = {'lock': threading.Lock(), 'requests': []}
        self.base = base
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        return False

    @property
    def api_url(self):
        return f'{self.base}/repos/test/test/releases/latest'

    @property
    def requests(self):
        return list(self._httpd.state['requests'])

    def corrupt_digest(self):
        """把 exe 资产的 digest 改错，用于校验失败测试"""
        for asset in self.release['assets']:
            if asset['name'] == self.exe_name:
                asset['digest'] = 'sha256:' + '0' * 64
