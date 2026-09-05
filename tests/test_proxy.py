"""本地流式代理测试：两个本地 HTTP 服务做真实回环验证（不访问外网）."""

import hashlib
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from mediafans.models import PlayTarget
from mediafans.proxy import StreamProxy, should_use_proxy

UPSTREAM_DATA = bytes(range(256)) * 8  # 2048 字节
seen_headers = {}


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _serve(self, with_body=True):
        seen_headers.clear()
        seen_headers.update(dict(self.headers))
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(UPSTREAM_DATA) - 1
            end = min(end, len(UPSTREAM_DATA) - 1)
            body = UPSTREAM_DATA[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(UPSTREAM_DATA)}")
        else:
            body = UPSTREAM_DATA
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._serve(with_body=True)

    def do_HEAD(self):  # noqa: N802
        self._serve(with_body=False)


@pytest.fixture(scope="module")
def upstream_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/file.mkv"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def proxy_url(upstream_url):
    target = PlayTarget(
        file_name="x.mkv", url=upstream_url,
        cookie="__puus=secret", ua="QUARK-UA",
    )
    proxy = StreamProxy(target)
    url = proxy.start()
    yield url
    proxy.stop()


def test_proxy_forwards_full_get_with_injected_headers(proxy_url):
    with httpx.Client(timeout=10) as c:
        r = c.get(proxy_url)  # 播放器视角：不带任何头
    assert r.status_code == 200
    assert r.content == UPSTREAM_DATA
    assert r.headers["content-type"] == "video/x-matroska"
    assert r.headers["accept-ranges"] == "bytes"
    # 代理向上游补了 Cookie 和 UA
    assert seen_headers.get("Cookie") == "__puus=secret"
    assert seen_headers.get("User-Agent") == "QUARK-UA"


def test_proxy_passes_through_range(proxy_url):
    with httpx.Client(timeout=10) as c:
        r = c.get(proxy_url, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.content == UPSTREAM_DATA[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(UPSTREAM_DATA)}"
    assert seen_headers.get("Range") == "bytes=100-199"


def test_proxy_head(proxy_url):
    with httpx.Client(timeout=10) as c:
        r = c.head(proxy_url)
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(UPSTREAM_DATA))
    assert r.content == b""


def test_should_use_proxy():
    with_cookie = PlayTarget(file_name="x", url="https://dl/x", cookie="a=1", ua="UA")
    no_cookie = PlayTarget(file_name="x", url="https://dl/x")
    assert should_use_proxy(with_cookie, "vlc") is True
    assert should_use_proxy(with_cookie, "potplayer") is True
    assert should_use_proxy(with_cookie, "system") is True
    assert should_use_proxy(with_cookie, None) is True
    assert should_use_proxy(with_cookie, "mpv") is False      # mpv 直连可带头
    assert should_use_proxy(with_cookie, "custom") is False   # 自定义命令可带头
    assert should_use_proxy(no_cookie, "vlc") is False       # 无需 Cookie 时代理没意义


# ---------------------------------------------------------------- 并发分片拉取
# 单条 TLS 连接喂不饱高码率原盘，大区间会被拆成多片并发拉再按序拼回。
# 拼错顺序不会报错、只会静默损坏视频，所以这里逐字节验。

BIG_DATA = random.Random(20260902).randbytes(200_000)  # 伪随机：错序无法蒙混过关
range_log = []
_log_lock = threading.Lock()


class _BigUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        with _log_lock:
            range_log.append(rng)
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else len(BIG_DATA) - 1, len(BIG_DATA) - 1)
            body = BIG_DATA[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BIG_DATA)}")
        else:
            body = BIG_DATA
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def big_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BigUpstreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/big.mkv"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def big_proxy(big_upstream, monkeypatch):
    """把分片调小，让 200KB 的测试数据也能压到几十个分片的拼接路径."""
    from mediafans import proxy as P

    monkeypatch.setattr(P, "PART_SIZE", 8 * 1024)
    monkeypatch.setattr(P, "PARALLEL", 4)
    monkeypatch.setattr(P, "PARALLEL_MIN_SPAN", 16 * 1024)
    range_log.clear()
    proxy = StreamProxy(PlayTarget(file_name="big.mkv", url=big_upstream,
                                   cookie="__puus=secret", ua="QUARK-UA"))
    url = proxy.start()
    yield url
    proxy.stop()


def test_parallel_assembly_is_byte_exact(big_proxy):
    """整file 拉取要被拆成多片并发取，拼回来必须和原始数据逐字节一致."""
    r = httpx.get(big_proxy, timeout=30)
    assert r.status_code == 200
    assert len(r.content) == len(BIG_DATA)
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(BIG_DATA).hexdigest()
    # 确实走了并发路径：首片直写 + 后续每片各一次 Range 请求
    assert len(range_log) > 10
    assert sum(1 for x in range_log if x) >= 10


def test_parallel_preserves_range_semantics(big_proxy):
    """206 的 Content-Range/Content-Length 必须是整个请求区间，而不是某个分片的."""
    start, length = 12_345, 100_000          # 跨十几个分片，且首尾都不对齐
    r = httpx.get(big_proxy, headers={"Range": f"bytes={start}-{start + length - 1}"},
                  timeout=30)
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {start}-{start + length - 1}/{len(BIG_DATA)}"
    assert r.headers["content-length"] == str(length)
    assert r.content == BIG_DATA[start:start + length]


def test_small_span_stays_sequential(big_proxy):
    """小区间（元数据探测、moov 拉取）并发只会徒增延迟，应该只打一次上游."""
    r = httpx.get(big_proxy, headers={"Range": "bytes=0-999"}, timeout=30)
    assert r.status_code == 206
    assert r.content == BIG_DATA[:1000]
    assert len(range_log) == 1


def test_client_abort_midstream_then_next_request_ok(big_proxy):
    """播放器 seek 会在半路掐断连接；并发窗口必须能收干净，不能卡死后续请求."""
    with httpx.Client(timeout=30) as c:
        with c.stream("GET", big_proxy) as r:
            for _ in r.iter_raw(4096):
                break                        # 只读一点就走人
    before = len([t for t in threading.enumerate() if t.name.startswith("mediafans-range")])
    r2 = httpx.get(big_proxy, headers={"Range": f"bytes=0-{99_999}"}, timeout=30)
    assert r2.status_code == 206
    assert r2.content == BIG_DATA[:100_000]  # 断过一次之后仍然正确
    assert before < 64                        # 没有把线程堆起来


def test_buffer_sizes_are_env_tunable(monkeypatch):
    """小内存机器必须能把缓冲调小 —— 默认每路 16MB，128MB 的 VPS 扛不住几路并发."""
    import importlib

    from mediafans import proxy as P

    monkeypatch.setenv("MEDIAFANS_PART_SIZE", str(1024 * 1024))
    monkeypatch.setenv("MEDIAFANS_PARALLEL", "2")
    monkeypatch.setenv("MEDIAFANS_READAHEAD_CHUNKS", "8")
    importlib.reload(P)
    try:
        assert P.PART_SIZE == 1024 * 1024
        assert P.PARALLEL == 2
        assert P.READAHEAD_CHUNKS == 8
        assert P.PART_SIZE * P.PARALLEL == 2 * 1024 * 1024   # 每路上限 2MB
    finally:
        monkeypatch.undo()
        importlib.reload(P)
    assert P.PART_SIZE == 4 * 1024 * 1024 and P.PARALLEL == 4   # 恢复默认


def test_bad_env_values_fall_back_to_defaults(monkeypatch):
    """写错了别把服务搞崩，退回默认就好."""
    import importlib

    from mediafans import proxy as P

    monkeypatch.setenv("MEDIAFANS_PARALLEL", "abc")
    monkeypatch.setenv("MEDIAFANS_PART_SIZE", "-5")
    importlib.reload(P)
    try:
        assert P.PARALLEL == 4
        assert P.PART_SIZE == 4 * 1024 * 1024
    finally:
        monkeypatch.undo()
        importlib.reload(P)
