# cli.py - 命令行下载器
#
# 用法：
#     python cli.py <url> [<url> ...] [选项]
#     python cli.py --input urls.txt -o D:\\Downloads
#
# 打包版（Nuitka onefile 编译为无控制台窗口的程序）会在检测到命令行参数时
# 自动挂接到调用者的控制台，所以也能这样用：
#     FastDownloader.exe --cli https://example.com/big.zip -o D:\\Downloads
#
# 退出码：0 全部成功 / 1 有失败 / 2 参数错误 / 130 用户中断
import argparse
import json
import os
import sys
import threading
import time
from collections import OrderedDict

from engine import DownloadTask, file_sha256, looks_like_sha256
from settings import Settings
from utils import format_size, format_time, _extract_filename
from version import __version__

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def _rebind_stream(name, fd_index):
    """把 sys.<name> 接到一个真正可用的输出上。

    为什么需要这么绕：Nuitka 用 --windows-console-mode=disable 编译出来的程序
    属于 GUI 子系统，启动时 sys.stdout 可能是 None —— 此时 print() 会**静默**
    丢弃，表现为"命令跑完了、退出码 0、但一个字都没有输出"。

    顺序很重要：
      1. 原来的流还能写 -> 保持不动（cmd 的 > 重定向要靠它）
      2. 原始文件描述符 -> 覆盖输出被重定向到文件/管道的情况
      3. 打开 CONOUT$   -> 直接连到调用者的控制台
    """
    current = getattr(sys, name, None)
    if current is not None and not getattr(current, "closed", True):
        try:
            current.write("")
            current.flush()
            return True
        except Exception:
            pass
    try:
        stream = os.fdopen(fd_index, "w", encoding="utf-8", errors="replace",
                           buffering=1, closefd=False)
        stream.write("")
        stream.flush()
        setattr(sys, name, stream)
        return True
    except Exception:
        pass
    try:
        stream = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        setattr(sys, name, stream)
        return True
    except OSError:
        return False


def attach_console():
    """让没有控制台的打包版程序把输出交给调用它的 shell。

    从资源管理器双击（没有父控制台）时挂接会失败，那种情况本来就该走 GUI。
    """
    if os.name != "nt":
        return False
    attached = False
    try:
        import ctypes
        ATTACH_PARENT_PROCESS = -1
        attached = bool(ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS))
    except Exception:
        attached = False
    ok_out = _rebind_stream("stdout", 1)
    ok_err = _rebind_stream("stderr", 2)
    try:
        if not getattr(sys, "stdin", None):
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except OSError:
        pass
    return attached or ok_out or ok_err

def build_parser():
    p = argparse.ArgumentParser(
        prog='fastdownloader',
        description='极速下载器 Pro 命令行版',
        epilog='示例：fastdownloader https://example.com/a.zip -o D:\\dl --sha256 <哈希>',
    )
    p.add_argument('urls', nargs='*', help='下载链接（可给多个）')
    p.add_argument('-i', '--input', metavar='FILE',
                   help='从文本文件读取链接（每行一个，# 开头为注释）')
    p.add_argument('-o', '--output', metavar='PATH',
                   help='保存路径：单个链接可以是文件或目录；多个链接应是目录')
    p.add_argument('-t', '--threads', type=int, metavar='N', help='单任务线程数（默认取设置）')
    p.add_argument('-j', '--jobs', type=int, metavar='N', default=3, help='同时下载的任务数（默认 3）')
    p.add_argument('-s', '--speed-limit', type=int, metavar='KB', help='全局限速 KB/s（0=不限）')
    p.add_argument('--sha256', metavar='HASH', help='期望的 SHA256（仅单个链接时可用）')
    p.add_argument('--hash', action='store_true',
                   help='下载完成后输出文件 SHA256（需要时自动计算）')
    p.add_argument('--no-verify-hash', action='store_true',
                   help='不自动探测服务器上的 .sha256 校验文件')
    p.add_argument('--proxy', metavar='URL', help='代理，如 http://127.0.0.1:7890 或 socks5://...')
    p.add_argument('-H', '--header', action='append', default=[], metavar='"Name: Value"',
                   help='附加请求头，可重复')
    p.add_argument('--retries', type=int, metavar='N', help='分片重试次数（默认取设置）')
    p.add_argument('--conflict', choices=('rename', 'overwrite', 'skip'), default='overwrite',
                   help='目标已存在时：rename 改名 / overwrite 覆盖（默认）/ skip 跳过')
    p.add_argument('--no-progress', action='store_true', help='不输出进度')
    p.add_argument('--json', action='store_true', help='以 JSON 输出结果（便于脚本处理）')
    p.add_argument('-v', '--verbose', action='store_true', help='输出详细日志')
    p.add_argument('--version', action='version', version=f'FastDownloader Pro {__version__}')
    return p


def parse_headers(items):
    out = {}
    for item in items or []:
        if ':' not in item:
            continue
        name, value = item.split(':', 1)
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


def collect_urls(args, parser):
    urls = list(args.urls)
    if args.input:
        try:
            with open(args.input, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        urls.append(line)
        except OSError as e:
            parser.error(f'无法读取 {args.input}: {e}')
    # 去重但保持顺序
    seen = OrderedDict()
    for u in urls:
        if u not in seen:
            seen[u] = True
    return list(seen)


def guess_name(url):
    return _extract_filename(url)


def resolve_target(url, output, multiple):
    """决定这条链接保存到哪个文件"""
    if not output:
        return guess_name(url)
    output = os.path.normpath(output)
    if multiple or os.path.isdir(output) or output.endswith(('\\', '/')):
        return os.path.join(output, guess_name(url))
    return output


class ProgressReporter:
    """简单的终端进度显示：单任务画进度条，多任务画汇总行"""

    def __init__(self, total, enabled=True):
        self.total = total
        self.enabled = enabled and sys.stdout.isatty()
        self.lock = threading.Lock()
        self.rows = {}
        self.finished = 0
        self._last_render = 0.0

    def update(self, key, name, percent, speed, done, size):
        if not self.enabled:
            return
        with self.lock:
            self.rows[key] = (name, percent, speed, done, size)
            now = time.time()
            if now - self._last_render < 0.2:
                return
            self._last_render = now
            self._render_locked()

    def task_finished(self, key):
        with self.lock:
            self.rows.pop(key, None)
            self.finished += 1
            if self.enabled:
                self._clear_locked()
        if not self.enabled:
            return

    def _clear_locked(self):
        if self.rows:
            sys.stdout.write('\r' + ' ' * 100 + '\r')
            sys.stdout.flush()

    def _render_locked(self):
        if len(self.rows) == 1:
            name, percent, speed, done, size = next(iter(self.rows.values()))
            bar_len = 24
            filled = int(bar_len * min(percent, 100) / 100)
            bar = '=' * filled + '-' * (bar_len - filled)
            line = (f'\r[{bar}] {percent:5.1f}%  {format_size(done)}/{format_size(size)}'
                    f'  {format_size(speed)}/s   ')
            sys.stdout.write(line[:160])
        else:
            done_n = self.finished
            sys.stdout.write(f'\r已完成 {done_n}/{self.total}，进行中 {len(self.rows)} 个任务…        ')
        sys.stdout.flush()

    def finish(self):
        if self.enabled:
            sys.stdout.write('\r' + ' ' * 110 + '\r')
            sys.stdout.flush()


def run_one(task, key, reporter, quiet):
    """跑一个任务并返回结果字典"""
    started = time.time()
    result = {'url': task.url, 'status': 'pending', 'path': task.save_path,
              'bytes': 0, 'sha256': '', 'elapsed': 0.0, 'error': ''}
    done = threading.Event()

    def on_event(tid, event, data=None):
        if event in ('completed', 'error', 'stopped', 'skipped'):
            if event == 'error':
                result['status'] = 'error'
                result['error'] = str(data or '')
            elif event == 'skipped':
                result['status'] = 'skipped'
            else:
                result['status'] = event
            done.set()

    task.set_callback(on_event)
    task.start()
    while not done.wait(0.25):
        info = task.get_info()
        reporter.update(key, os.path.basename(task.save_path), info['percent'],
                        info['speed'], info['downloaded'], info['total'])
        if task.status in ('error',) and not done.is_set():
            # 早退（例如磁盘不足）时 start() 直接返回错误
            result['status'] = 'error'
            result['error'] = task._error_msg
            done.set()
    reporter.task_finished(key)
    result['elapsed'] = time.time() - started
    result['status'] = task.status
    result['error'] = task._error_msg or result['error']
    result['path'] = task._final_path or task.save_path
    result['bytes'] = task.downloaded
    result['sha256'] = task.file_hash
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    urls = collect_urls(args, parser)
    if not urls:
        parser.print_help()
        return EXIT_USAGE
    if args.sha256 and len(urls) > 1:
        parser.error('--sha256 只在下载单个链接时可用')

    settings = Settings()
    threads = args.threads or settings.thread_count
    speed_limit = args.speed_limit if args.speed_limit is not None else settings.speed_limit
    retries = args.retries if args.retries is not None else settings.retry_count
    proxy = args.proxy if args.proxy is not None else settings.proxy
    headers = dict(settings.custom_headers)
    headers.update(parse_headers(args.header))

    multiple = len(urls) > 1
    quiet = args.no_progress or args.json
    reporter = ProgressReporter(len(urls), enabled=not quiet)

    if not quiet:
        print(f'极速下载器 Pro {__version__} —— {len(urls)} 个任务，'
              f'线程 {threads}，并发 {max(1, args.jobs)}')
        if speed_limit:
            print(f'限速 {speed_limit} KB/s')

    results = []
    lock = threading.Lock()
    pending = list(enumerate(urls, 1))
    jobs = max(1, args.jobs)

    def worker():
        while True:
            with lock:
                if not pending:
                    return
                index, url = pending.pop(0)
            target = resolve_target(url, args.output, multiple)
            expect = args.sha256 if (args.sha256 and len(urls) == 1) else ''
            task = DownloadTask(
                index, url, target,
                num_threads=threads,
                speed_limit=speed_limit,
                proxy=proxy,
                headers=headers,
                retry_count=retries,
                connect_timeout=settings.connect_timeout,
                read_timeout=settings.read_timeout,
                verify_ssl=settings.verify_ssl,
                conflict_policy=args.conflict,
                check_disk_space=settings.check_disk_space,
                min_free_mb=settings.min_free_mb,
                cookie_mode=settings.cookie_mode,
                expected_sha256=expect,
                sha256_auto_probe=not args.no_verify_hash,
            )
            try:
                result = run_one(task, index, reporter, quiet)
            except Exception as e:                      # noqa: BLE001
                result = {'url': url, 'status': 'error', 'path': target, 'bytes': 0,
                          'sha256': '', 'elapsed': 0.0, 'error': f'{type(e).__name__}: {e}'}
            if args.hash and not result['sha256'] and result['status'] in ('completed', 'skipped'):
                try:
                    result['sha256'] = file_sha256(result['path'])
                except OSError:
                    pass
            with lock:
                results.append(result)

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(min(jobs, len(urls)))]
    for w in workers:
        w.start()
    try:
        for w in workers:
            while w.is_alive():
                w.join(0.3)
    except KeyboardInterrupt:
        reporter.finish()
        print('\n已中断：正在停止任务（.part 已保留，可用相同命令续传）…', file=sys.stderr)
        return EXIT_INTERRUPTED
    reporter.finish()

    results.sort(key=lambda r: urls.index(r['url']))

    if args.json:
        payload = {
            'version': __version__,
            'ok': all(r['status'] in ('completed', 'skipped') for r in results),
            'results': results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        ok = 0
        for r in results:
            if r['status'] in ('completed', 'skipped'):
                ok += 1
                line = (f'✔ {os.path.basename(r["path"])}  {format_size(r["bytes"])}'
                        f'  用时 {format_time(r["elapsed"])}')
                if r['sha256']:
                    line += f'\n    SHA256 {r["sha256"]}'
                if r['status'] == 'skipped':
                    line = f'⏭ 已跳过（文件已存在）：{r["path"]}'
                print(line)
            else:
                print(f'✘ {r["url"]}\n    {r["error"] or r["status"]}', file=sys.stderr)
        print(f'\n完成 {ok}/{len(results)}')
        if args.verbose:
            for r in results:
                print(f'  [{r["status"]}] {r["url"]} -> {r["path"]}')

    return EXIT_OK if all(r['status'] in ('completed', 'skipped') for r in results) else EXIT_FAILED


if __name__ == '__main__':
    if os.name == 'nt':
        attach_console()
    sys.exit(main())
