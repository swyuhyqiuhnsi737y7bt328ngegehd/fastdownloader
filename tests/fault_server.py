# tests/fault_server.py - 进程内故障注入 HTTP 服务器（测试专用）
#
# 用于验证下载引擎在恶劣网络/异常服务器下的行为：
#   /file                     正常：支持 Range(206)、可选限速
#   /file?rate=0.25           限速 0.25 MB/s
#   /norange                  忽略 Range，始终返回 200 全量
#   /shifted                  返回 206 但 Content-Range 起点被篡改（错位写入陷阱）
#   /flaky?fail=2&then=ok     前 N 次请求返回 503，之后正常（验证重试退避）
#   /drop?after=65536         发送 N 字节后强制断开（验证中途断开重试）
#   /auth                     需要 Basic 认证（user/pass）
#   /empty                    声明 Content-Length 但不发送任何数据
#   /404                      始终 404
#   /nosize                   chunked/无 Content-Length（未知大小）
import http.server
import os
import socket
import socketserver
import threading
import time
from urllib.parse import urlparse, parse_qs

CHUNK = 8192
DEFAULT_SIZE = 4 * 1024 * 1024  # 4 MB


def make_payload(size=DEFAULT_SIZE):
    """生成确定性测试数据（按字节位置可预测，便于校验错位）"""
    block = bytes(range(256)) * (CHUNK // 256)  # 8 KB
    return (block * (size // len(block) + 1))[:size]


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    payload = b''

    def log_message(self, *args):
        pass  # 静默

    # ---- 工具 ----
    def _state(self):
        return self.server.state

    def _send_full(self, body, status=200, extra=None, rate=0.0, drop_after=None):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Accept-Ranges', 'bytes')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self._write_body(body, rate=rate, drop_after=drop_after)

    def _write_body(self, body, rate=0.0, drop_after=None):
        sent = 0
        for i in range(0, len(body), CHUNK):
            chunk = body[i:i + CHUNK]
            if drop_after is not None and sent >= drop_after:
                # 模拟连接中断：真正关闭套接字（客户端应立即收到 FIN 而不是干等超时）
                self.close_connection = True
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.connection.close()
                except OSError:
                    pass
                return
            if rate > 0:
                time.sleep(len(chunk) / rate)
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            sent += len(chunk)

    def _range_of(self, size):
        h = self.headers.get('Range')
        if not h or not h.startswith('bytes='):
            return None
        spec = h[6:].split(',')[0].strip()
        if '-' not in spec:
            return None
        a, b = spec.split('-', 1)
        try:
            a = int(a) if a else 0
            b = int(b) if b else size - 1
        except ValueError:
            return None
        return a, min(b, size - 1)

    # ---- 路由 ----
    def do_HEAD(self):
        path = urlparse(self.path).path
        if path == '/forbidden' and not (self.headers.get('Authorization') or self.headers.get('X-Auth')):
            self.send_response(403)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path == '/404':
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path == '/nosize':
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(self.payload)))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # 校验文件端点：/file.sha256 -> 返回 payload 的哈希（模拟项目的校验文件约定）
        if path.endswith('.sha256'):
            # 只有 /file 附带校验文件；其他路径返回 404，模拟“服务器没有校验文件”
            if path != '/file.sha256':
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            import hashlib as _hl
            body = (_hl.sha256(self.payload).hexdigest() + '  payload.bin\n').encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.endswith('.sha256sum'):
            body = b'not a hash here\n'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        qs = parse_qs(parsed.query)
        state = self._state()

        if path == '/404':
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        if path == '/forbidden':
            # 没有认证信息一律 403（测试"先 403，注入 Cookie 后放行"）
            if not (self.headers.get('Authorization') or self.headers.get('X-Auth')):
                body = b'forbidden'
                self.send_response(403)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if path == '/auth':
            expect = 'Basic ' + __import__('base64').b64encode(b'user:pass').decode()
            if self.headers.get('Authorization') != expect:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="test"')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return

        if path == '/empty':
            self.send_response(200)
            self.send_header('Content-Length', str(len(self.payload)))
            self.end_headers()
            return

        if path == '/nosize':
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            remaining = self.payload
            step = 65536
            for i in range(0, len(remaining), step):
                chunk = remaining[i:i + step]
                self.wfile.write(b'%X\r\n' % len(chunk) + chunk + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
            return

        # /flaky：前 N 次请求失败（全局计数，用于重试测试）
        if path == '/flaky':
            fail = int(qs.get('fail', ['2'])[0])
            with state['lock']:
                state['hits'] = state.get('hits', 0) + 1
                hit = state['hits']
            if hit <= fail:
                body = b'server busy'
                self.send_response(503)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        rate = float(qs.get('rate', [str(self.server.rate)])[0]) * 1024 * 1024
        drop_after = qs.get('after', [None])[0]
        drop_after = int(drop_after) if drop_after else None
        size = len(self.payload)

        if path == '/norange':
            self._send_full(self.payload, rate=rate)
            return

        rng = self._range_of(size)
        if rng is None:
            self._send_full(self.payload, rate=rate, drop_after=drop_after)
            return

        a, b = rng
        if a >= size or b < a:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        length = b - a + 1
        if path == '/shifted':
            # 篡改起点：模拟中间代理/服务器错误返回错位区间
            a_send = min(a + 100000, size - length)
        else:
            a_send = a
        self.send_response(206)
        self.send_header('Content-Length', str(length))
        self.send_header('Content-Range', f'bytes {a_send}-{a_send + length - 1}/{size}')
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        self._write_body(self.payload[a_send:a_send + length], rate=rate, drop_after=drop_after)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # 客户端被强杀/主动断连属于测试预期行为，不打印堆栈
        pass


class FaultServer:
    """上下文管理器：启动一个进程内测试服务器

    用法：
        with FaultServer() as srv:
            url = srv.url('/file')
    """

    def __init__(self, size=DEFAULT_SIZE, rate=0.0):
        self.size = size
        self.rate = rate
        self._httpd = None
        self._thread = None

    def __enter__(self):
        handler = type('Handler', (_Handler,), {'payload': make_payload(self.size)})
        self._httpd = _Server(('127.0.0.1', 0), handler)
        self._httpd.state = {'lock': threading.Lock()}
        self._httpd.rate = self.rate
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        return False

    @property
    def port(self):
        return self._httpd.server_address[1]

    def url(self, path='/file', **params):
        query = ''
        if params:
            query = '?' + '&'.join(f'{k}={v}' for k, v in params.items())
        return f'http://127.0.0.1:{self.port}{path}{query}'

    def expected_bytes(self):
        return make_payload(self.size)
