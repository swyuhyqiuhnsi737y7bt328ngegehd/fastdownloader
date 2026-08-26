import os
import json
import ftplib
import time
import urllib.parse
from curl_cffi import requests as curl_requests

from paths import data_file

SERVERS_FILE = data_file('servers.json')

def load_servers():
    try:
        if os.path.exists(SERVERS_FILE):
            with open(SERVERS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return []

def save_servers(servers):
    with open(SERVERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(servers, f, ensure_ascii=False, indent=2)


class RemoteServer:
    def __init__(self, host, port, username, password, protocol='ftp', name=''):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.protocol = protocol.lower()
        self.name = name or host
        self._conn = None

    def connect(self):
        if self.protocol == 'ftp':
            self._conn = ftplib.FTP()
            port = self.port or 21
            try:
                self._conn.connect(self.host, port, timeout=10)
                self._conn.login(self.username, self.password)
                return True
            except Exception as e:
                self._conn = None
                raise e
        elif self.protocol == 'ftps':
            self._conn = ftplib.FTP_TLS()
            port = self.port or 990
            try:
                self._conn.connect(self.host, port, timeout=10)
                self._conn.login(self.username, self.password)
                self._conn.prot_p()
                return True
            except Exception as e:
                self._conn = None
                raise e
        elif self.protocol in ('webdav', 'http', 'https'):
            port = self.port or (443 if self.protocol == 'https' else 80)
            base = f"{self.protocol}://{self.host}:{port}"
            self._conn = {'base': base, 'auth': (self.username, self.password)}
            return True
        return False

    def disconnect(self):
        if self._conn and hasattr(self._conn, 'quit'):
            try:
                self._conn.quit()
            except:
                pass
        self._conn = None

    def list_dir(self, path='/'):
        if self.protocol in ('ftp', 'ftps'):
            items = []
            self._conn.dir(path, lambda line: items.append(self._parse_ftp_line(line)))
            return [i for i in items if i is not None]
        elif self.protocol in ('webdav', 'http', 'https'):
            return self._list_webdav(path)
        return []

    def _parse_ftp_line(self, line):
        # Unix 风格: drwxr-xr-x  2 owner group 4096 Jan 1 00:00 name with spaces
        if line.startswith(('total ', 'd ')):
            return None  # total 统计行 / MS-DOS 风格不解析
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            return None  # 无法识别的一律跳过，避免 UI 出现伪条目
        is_dir = parts[0].startswith('d')
        size = int(parts[4]) if parts[4].isdigit() else 0
        name = parts[8]
        if name in ('.', '..') or not name:
            return None
        return {'name': name, 'type': 'dir' if is_dir else 'file', 'size': size}

    def _list_webdav(self, path):
        base = self._conn['base']
        auth = self._conn['auth']
        url = base.rstrip('/') + '/' + urllib.parse.quote(path.lstrip('/'), safe='/')
        try:
            r = curl_requests.request('PROPFIND', url, auth=auth, verify=False, timeout=10,
                                      headers={'Depth': '1'})
            if r.status_code not in (200, 207):
                return []
            items = []
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.text)
            ns = {'d': 'DAV:'}
            req_path = '/' + path.strip('/')
            for resp in root.findall('d:response', ns):
                href = resp.find('d:href', ns)
                if href is None:
                    continue
                href_path = urllib.parse.urlsplit(href.text).path.rstrip('/') or '/'
                if href_path == req_path or href_path == '/':
                    continue  # 排除请求目录本身和根
                name = urllib.parse.unquote(href_path.split('/')[-1])
                is_dir = False
                size = 0
                # 遍历所有 propstat（404 的 propstat 可能排在前面，不能只看第一个）
                for propstat in resp.findall('d:propstat', ns):
                    props = propstat.find('d:prop', ns)
                    if props is None:
                        continue
                    res_type = props.find('d:resourcetype', ns)
                    if res_type is not None and res_type.find('d:collection', ns) is not None:
                        is_dir = True
                    cl = props.find('d:getcontentlength', ns)
                    if cl is not None and cl.text and cl.text.isdigit():
                        size = int(cl.text)
                    if is_dir:
                        break
                if name:
                    items.append({'name': name, 'type': 'dir' if is_dir else 'file', 'size': size})
            return items
        except Exception:
            return []

    def _url_with_auth(self, url):
        """把用户名/密码以标准 URL userinfo 形式嵌入（HTTP Basic Auth / FTP），
        替代之前拼进查询串的 ?auth=user:pass（凭据会泄露且服务器不识别）。"""
        user = urllib.parse.quote(self.username or '', safe='')
        pwd = urllib.parse.quote(self.password or '', safe='')
        parsed = urllib.parse.urlsplit(url)
        # IPv6 主机必须加方括号，否则冒号会被误解析（urlsplit 也无法取端口）
        host = self.host
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        port = ''
        try:
            if parsed.port is not None:
                port = ':' + str(parsed.port)
        except ValueError:
            # 未加括号的 IPv6 netloc 无法解析端口：退回 self.port
            if self.port:
                port = ':' + str(self.port)
        netloc = host + port
        if user or pwd:
            # 只填密码（无用户名）也要带上，否则凭据会丢
            netloc = f"{user}:{pwd}@{netloc}" if pwd else f"{user}@{netloc}"
        return urllib.parse.urlunsplit(
            (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    def download_url(self, filepath):
        # 路径必须先百分号编码：# / ? 空格等会破坏 URL 结构
        enc_path = urllib.parse.quote(filepath.lstrip('/'), safe='/')
        if self.protocol in ('ftp', 'ftps'):
            scheme = 'ftps' if self.protocol == 'ftps' else 'ftp'
            port = self.port or (21 if self.protocol == 'ftp' else 990)
            url = f"{scheme}://{self.host}:{port}/{enc_path}"
            return self._url_with_auth(url)
        elif self.protocol in ('webdav', 'http', 'https'):
            base = self._conn['base']
            url = base.rstrip('/') + '/' + enc_path
            return self._url_with_auth(url)
        return ''

    def to_dict(self):
        return {
            'name': self.name, 'host': self.host, 'port': self.port,
            'username': self.username, 'password': self.password,
            'protocol': self.protocol,
        }

    @staticmethod
    def from_dict(d):
        return RemoteServer(
            d.get('host', ''), d.get('port', 0), d.get('username', ''),
            d.get('password', ''), d.get('protocol', 'ftp'), d.get('name', ''),
        )
