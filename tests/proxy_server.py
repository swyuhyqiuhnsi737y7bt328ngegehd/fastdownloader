# tests/proxy_server.py - 极简 HTTP 代理（测试"代理设置是否真正生效"）
#
# 只用于本地测试：把请求转发给目标服务器并把响应（含状态码/Range 相关头）原样回传。
import http.server
import socketserver
import threading
import urllib.error
import urllib.request

_HOP_BY_HOP = {'connection', 'proxy-connection', 'keep-alive', 'transfer-encoding',
               'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'upgrade'}


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _proxy(self):
        state = self.server.state
        with state['lock']:
            state['requests'] = state.get('requests', 0) + 1
            state.setdefault('paths', []).append(self.path)

        req = urllib.request.Request(self.path, method=self.command)
        for key, value in self.headers.items():
            if key.lower() in _HOP_BY_HOP or key.lower() in ('host', 'content-length'):
                continue
            req.add_header(key, value)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:      # 401/404/503 等仍要透传状态码
            resp = e
        except Exception:
            self.send_error(502, 'proxy upstream failure')
            return

        try:
            self.send_response(resp.status)
            if self.command == 'HEAD':
                # HEAD 必须原样保留上游的 Content-Length（改写会让客户端误判文件大小）
                for key, value in resp.headers.items():
                    if key.lower() in _HOP_BY_HOP:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                return
            body = resp.read()
            for key, value in resp.headers.items():
                if key.lower() in _HOP_BY_HOP or key.lower() == 'content-length':
                    continue
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    do_GET = _proxy
    do_HEAD = _proxy
    do_POST = _proxy


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ProxyServer:
    """上下文管理器：启动本地 HTTP 代理"""

    def __init__(self):
        self._httpd = None
        self._thread = None

    def __enter__(self):
        self._httpd = _Server(('127.0.0.1', 0), _ProxyHandler)
        self._httpd.state = {'lock': threading.Lock()}
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        return False

    @property
    def url(self):
        return f'http://127.0.0.1:{self._httpd.server_address[1]}'

    @property
    def request_count(self):
        return self._httpd.state.get('requests', 0)
