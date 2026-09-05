from __future__ import annotations

import os
import queue
import re
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

import httpx

from .models import PlayTarget


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """不把浏览器/播放器断开连接打成 traceback（Chromium 播放内核频繁开断连接属常态）."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

# 流式转发超时：连接要快，读要耐心（大文件 + 播放器缓冲）
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=30.0)

# 透传给播放器/浏览器的上游响应头（其余头与本地回环场景无关）
RELAY_HEADERS = (
    "Content-Type", "Content-Length", "Content-Range",
    "Accept-Ranges", "Content-Disposition", "Last-Modified", "ETag",
)

def _env_int(name: str, default: int) -> int:
    """允许用环境变量调缓冲大小 —— 小内存机器（如 128MB 的 VPS）必须能调小。"""
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


CHUNK = 512 * 1024      # 单次从上游读取的块大小
# 预读缓冲上限（32 * 512KB = 16MB）
READAHEAD_CHUNKS = _env_int("MEDIAFANS_READAHEAD_CHUNKS", 32)

# 单条 TLS 连接的吞吐是有上限的（实测夸克 ~6.4 MB/s），而 REMUX 原盘要 8+ MB/s 才不卡。
# 大区间拆成多片、多连接并发拉、按序写出，才能喂饱高码率原画。
# 并发度不宜大：夸克按账号限流，开太多反而更慢（见 README 的实测数据）。
#
# 内存代价 = PART_SIZE * PARALLEL * 同时在播的路数。默认 4MB*4=16MB/路，
# 内存紧张时用 MEDIAFANS_PART_SIZE / MEDIAFANS_PARALLEL 调小，代价是原画码率跟不上。
PART_SIZE = _env_int("MEDIAFANS_PART_SIZE", 4 * 1024 * 1024)
PARALLEL = _env_int("MEDIAFANS_PARALLEL", 4)
PARALLEL_MIN_SPAN = 8 * 1024 * 1024   # 区间太小不值得并发（元数据探测、moov 拉取都很小）

# 全局连接池：浏览器 <video> 每次拖动/续播都会重开 Range 请求，
# 每次新建 client 意味着一次完整 TLS 握手（实测 TTFB 0.9s vs 复用 0.03s）。
_client_lock = threading.Lock()
_client: Optional[httpx.Client] = None


def upstream_client() -> httpx.Client:
    """共享的上游 httpx.Client（keep-alive 连接池），首次调用时建。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=STREAM_TIMEOUT,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16,
                                    keepalive_expiry=120.0),
            )
        return _client


def _pump(handler: BaseHTTPRequestHandler, response: httpx.Response) -> int:
    """上游 -> 浏览器，中间垫一层预读缓冲，返回写出的字节数。

    没有缓冲时「读上游」和「写播放器」是串行的：TLS 解密一块、写一块，两边互相等，
    而且播放器缓冲满了不再读时上游连接会一起停住。用一个后台线程持续把上游读进队列，
    播放器再慢也先攒着，上游抖动就被这 16MB 吃掉了。
    """
    q: "queue.Queue" = queue.Queue(maxsize=READAHEAD_CHUNKS)
    stop = threading.Event()
    done = object()

    def produce():
        try:
            for chunk in response.iter_raw(CHUNK):
                while not stop.is_set():
                    try:
                        q.put(chunk, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if stop.is_set():
                    return
        except Exception:
            pass  # 上游断了就当读完，已写出的部分交给播放器自己重试 Range
        finally:
            try:
                q.put(done, timeout=1)
            except queue.Full:
                pass

    reader = threading.Thread(target=produce, daemon=True)
    reader.start()
    written = 0
    try:
        while True:
            chunk = q.get()
            if chunk is done:
                break
            handler.wfile.write(chunk)
            written += len(chunk)
        return written
    finally:
        stop.set()
        while True:  # 让生产者从 put 阻塞里脱身
            try:
                q.get_nowait()
            except queue.Empty:
                break
        reader.join(timeout=2)


def content_span(r: httpx.Response) -> Optional[Tuple[int, int]]:
    """从上游响应推断本次要送出的字节区间 (起始偏移, 长度)，推断不出返回 None."""
    raw = r.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        length = int(raw)
    except ValueError:
        return None
    if r.status_code == 206:
        m = re.match(r"bytes\s+(\d+)-(\d+)/", r.headers.get("Content-Range", ""))
        return (int(m.group(1)), length) if m else None
    if r.status_code == 200:
        return (0, length)
    return None


def _can_parallelize(r: httpx.Response, span: Optional[Tuple[int, int]]) -> bool:
    """只有上游支持 Range、区间够大时并发才划算."""
    if PARALLEL <= 1 or span is None or span[1] < PARALLEL_MIN_SPAN:
        return False
    if r.status_code == 206:
        return True  # 上游已经按 Range 回了，自然支持
    return r.headers.get("Accept-Ranges", "").lower() == "bytes"


def _write_span(handler: BaseHTTPRequestHandler, response: httpx.Response, limit: int) -> int:
    """把已经在流的响应写出至多 limit 字节，返回实际写出数。

    首片走这条路而不是先攒进内存，首字节延迟才不会因为并发改造而变差。
    """
    written = 0
    for chunk in response.iter_raw(CHUNK):
        if written + len(chunk) > limit:
            chunk = chunk[:limit - written]
        if chunk:
            handler.wfile.write(chunk)
            written += len(chunk)
        if written >= limit:
            break
    return written


def _pump_parallel(handler: BaseHTTPRequestHandler, response: httpx.Response,
                   target: PlayTarget, start: int, total: int) -> int:
    """首片直写 + 后续分片多连接并发预取，严格按序写出，返回写出的字节数。

    首片复用 relay_stream already 建好的那条响应（省一次握手），之后每片各走一条连接。
    滑动窗口保证最多 PARALLEL 片在飞，内存有上限；播放器不读时 write 阻塞，
    窗口自然停下来，不会失控预取。
    """
    written = _write_span(handler, response, min(PART_SIZE, total))
    response.close()
    if written >= total:
        return written

    parts = []
    pos, end = start + written, start + total
    while pos < end:
        parts.append((pos, min(PART_SIZE, end - pos)))
        pos += PART_SIZE

    stop = threading.Event()
    headers = dict(target.headers())

    def fetch(offset: int, size: int) -> bytes:
        if stop.is_set():
            return b""
        h = dict(headers)
        h["Range"] = f"bytes={offset}-{offset + size - 1}"
        buf = bytearray()
        try:
            # 边收边看 stop：播放器一断开（seek/切清晰度是常态），在飞的几片要立刻放弃，
            # 否则要等它们各自下完 4MB 才罢休，白占连接和带宽，拖慢紧接着的新请求
            with upstream_client().stream("GET", target.url, headers=h) as r:
                if r.status_code not in (200, 206):
                    return b""
                for chunk in r.iter_raw(CHUNK):
                    if stop.is_set():
                        return b""
                    buf += chunk
        except (httpx.HTTPError, OSError):
            return b""
        return bytes(buf)

    pool = ThreadPoolExecutor(max_workers=PARALLEL,
                              thread_name_prefix="mediafans-range")
    inflight: deque = deque()
    nxt = 0
    try:
        while nxt < len(parts) or inflight:
            while len(inflight) < PARALLEL and nxt < len(parts):
                inflight.append(pool.submit(fetch, *parts[nxt]))
                nxt += 1
            data = inflight.popleft().result()
            if not data:
                break  # 这一片没取到，停在这里，播放器会自己重发 Range
            handler.wfile.write(data)
            written += len(data)
    finally:
        stop.set()
        for f in inflight:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return written


def should_use_proxy(target: PlayTarget, player_kind: Optional[str]) -> bool:
    """直链需要 Cookie 而播放器带不了时（vlc/potplayer/系统默认/网页），走本地转发。

    mpv 和自定义命令能带请求头，直连即可。
    """
    if not target.cookie:
        return False
    return player_kind not in ("mpv", "custom", "none")


def relay_stream(handler: BaseHTTPRequestHandler, target: PlayTarget,
                 send_body: bool = True) -> None:
    """把 handler 收到的请求（含 Range）补上 Cookie/UA 后转发到 target.url，流式回写。

    供 StreamProxy 和网页播放器的 /stream 路由共用。
    """
    headers = dict(target.headers())
    if handler.headers.get("Range"):
        headers["Range"] = handler.headers["Range"]
    client = upstream_client()
    method = "GET" if send_body else "HEAD"
    request = client.build_request(method, target.url, headers=headers)
    try:
        r = client.send(request, stream=True)
    except (httpx.HTTPError, OSError):
        try:
            handler.send_error(502, "upstream error")
        except Exception:
            pass
        return
    try:
        length = r.headers.get("Content-Length")
        handler.send_response(r.status_code)
        for h in RELAY_HEADERS:
            if r.headers.get(h):
                handler.send_header(h, r.headers[h])
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        if length is None:
            handler.close_connection = True  # 长度未知，只能靠关连接标记结尾
        if send_body:
            span = content_span(r)
            if _can_parallelize(r, span):
                written = _pump_parallel(handler, r, target, span[0], span[1])
            else:
                written = _pump(handler, r)
            if length is not None and written != int(length):
                # 少写/多写都会让 keep-alive 的下一个响应错位，断开最稳
                handler.close_connection = True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        handler.close_connection = True  # 播放器 seek/断开/关闭是常态
    except (httpx.HTTPError, OSError, ValueError):
        handler.close_connection = True
    finally:
        r.close()


class StreamProxy:
    """本地直链代理（外部播放器场景）。

    播放器访问 http://127.0.0.1:<port>/ 不需要任何请求头；
    代理在转发时补上直链要求的 Cookie/UA，并原样透传 Range（拖进度条）。
    仅绑定回环地址，cookie 不会出本机。
    """

    def __init__(self, target: PlayTarget, host: str = "127.0.0.1"):
        self.target = target
        self.host = host
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> str:
        target = self.target

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True  # 视频块要立刻上路，别等凑包

            def log_message(self, *args):  # 静音默认访问日志
                pass

            def do_GET(self):  # noqa: N802
                relay_stream(self, target, send_body=True)

            def do_HEAD(self):  # noqa: N802
                relay_stream(self, target, send_body=False)

            @property
            def server_version(self):  # 不暴露实现细节
                return "MediaFansProxy/1.0"

        self._server = QuietThreadingHTTPServer((self.host, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{self.host}:{self._server.server_address[1]}/stream"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
