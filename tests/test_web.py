"""网页播放器服务测试：MockTransport 假网盘 + 本地假上游，全回环验证."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from mediafans.drive.quark import QuarkDrive
from mediafans.web import WebApp

UPSTREAM_DATA = bytes(range(256)) * 8  # 2048 字节


UPSTREAM_CONNECTIONS = []


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        UPSTREAM_CONNECTIONS.append(self.client_address)
        super().setup()

    def log_message(self, *args):
        pass

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else len(UPSTREAM_DATA) - 1,
                      len(UPSTREAM_DATA) - 1)
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
        if self.command != "HEAD":
            self.wfile.write(body)


@pytest.fixture(scope="module")
def upstream_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/file.mkv"
    server.shutdown()
    server.server_close()


def make_webapp(upstream_url, search_fn=None):
    state = {}

    def handler(request):
        path = request.url.path
        if path.endswith("/file/info/path_list"):
            body = json.loads(request.content)
            fp = body["file_path"][0]
            if fp == "/MediaFans":
                return httpx.Response(200, json={
                    "code": 0, "data": [{"fid": "DIRFID", "file_path": "/MediaFans"}]})
            return httpx.Response(200, json={"code": 0, "data": []})
        if path.endswith("/file/sort"):
            return httpx.Response(200, json={"code": 0, "data": {
                "list": [
                    {"fid": "D1", "file_name": "剧集A", "dir": True},
                    {"fid": "F1", "file_name": "movie.mkv", "dir": False,
                     "size": 2048, "updated_at": 1783000000000},
                ], "metadata": {"_total": 2}}})
        if path.endswith("/share/sharepage/token"):
            body = json.loads(request.content)
            assert body["pwd_id"] == "q1"
            if body.get("passcode") != "88ab":
                return httpx.Response(200, json={
                    "status": 500, "code": 41008, "message": "需要提取码"})
            return httpx.Response(200, json={"status": 200, "code": 0, "data": {"stoken": "ST"}})
        if path.endswith("/share/sharepage/detail"):
            return httpx.Response(200, json={"code": 0, "data": {
                "list": [
                    {"fid": "SF1", "file_name": "movie.2160p.mkv", "dir": False,
                     "size": 1024 ** 3, "share_fid_token": "TK1"},
                    {"fid": "SF2", "file_name": "note.txt", "dir": False,
                     "size": 12, "share_fid_token": "TK2"},
                ], "metadata": {"_total": 2}}})
        if path.endswith("/clouddrive/file") and request.method == "POST":
            body = json.loads(request.content)
            state["created_dir"] = body["dir_path"]
            return httpx.Response(200, json={"code": 0, "data": {"fid": "NEWDIR"}})
        if path.endswith("/share/sharepage/save"):
            state["save"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "TASK1"}})
        if path.endswith("/task"):
            return httpx.Response(200, json={"code": 0, "data": {
                "status": 2, "save_as": {"save_as_top_fids": ["N1"]}}})
        if path.endswith("/file/download"):
            return httpx.Response(
                200,
                headers=httpx.Headers([("set-cookie", "__puus=web-test; Path=/")]),
                json={"code": 0, "data": [{
                    "file_name": "movie.mkv",
                    "download_url": upstream_url,
                    "obj_category": "video",
                    "video_width": 3840, "video_height": 2160, "size": 2048,
                }]},
            )
        if path.endswith("/file/v2/play"):
            def v(res, h, w, suffix):
                return {"resolution": res, "trans_status": "success", "accessable": True,
                        "video_info": {"url": upstream_url + suffix, "height": h, "width": w,
                                       "size": h * 1000, "bitrate": h * 2.0, "format": "mp4"}}
            return httpx.Response(
                200,
                headers=httpx.Headers([("set-cookie", "__puus=web-play; Path=/")]),
                json={"code": 0, "data": {
                    "default_resolution": "high",
                    "video_list": [v("4k", 2160, 3840, "?lv=4k"),
                                   v("high", 540, 960, "?lv=high")],
                }},
            )
        raise AssertionError(f"unexpected {path}")

    drive = QuarkDrive({"cookie": "__puus=x", "save_dir": "/MediaFans"},
                       transport=httpx.MockTransport(handler))
    app = WebApp(lambda nd="quark": drive, search_fn=search_fn)
    app.test_state = state
    app.start()
    return app


def fake_search(kw, netdisk=None):
    from mediafans.models import ShareLink

    links = [
        ShareLink(url="https://pan.quark.cn/s/q1", netdisk="quark", passcode="88ab",
                  note="沙丘 4K", datetime="2026-08-01T00:00:00Z", source="tg:x"),
        ShareLink(url="https://www.alipan.com/s/a1", netdisk="aliyun", note="n"),
    ]
    if netdisk:
        links = [l for l in links if l.netdisk == netdisk]
    return links, [("yiso", RuntimeError("down"))]


@pytest.fixture()
def webapp(upstream_url):
    app = make_webapp(upstream_url, search_fn=fake_search)
    base = f"http://127.0.0.1:{app.port}"
    yield base, app
    app.stop()


def test_page_served(webapp):
    base, _ = webapp
    r = httpx.get(base + "/", timeout=10)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<video" in r.text and "MediaFans" in r.text


def test_api_list(webapp):
    base, _ = webapp
    r = httpx.get(base + "/api/list", params={"path": "/MediaFans"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["path"] == "/MediaFans"
    names = [f["name"] for f in data["files"]]
    assert "movie.mkv" in names and "剧集A" in names
    # 目录排前面
    assert data["files"][0]["is_dir"] is True
    movie = next(f for f in data["files"] if f["name"] == "movie.mkv")
    assert movie["size_h"] == "2.00KB"
    assert movie["path"] == "/MediaFans/movie.mkv"


def test_api_play_then_stream(webapp):
    base, app = webapp
    r = httpx.get(base + "/api/play", params={"path": "/MediaFans/movie.mkv"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["file_name"] == "movie.mkv"
    assert data["needs_cookie"] is True
    assert data["stream_url"].startswith("/stream?t=")

    # 浏览器视角：不带任何头，Range 请求经 /stream 转发到上游
    with httpx.Client(timeout=10) as c:
        full = c.get(base + data["stream_url"])
        assert full.status_code == 200
        assert full.content == UPSTREAM_DATA

        ranged = c.get(base + data["stream_url"], headers={"Range": "bytes=100-199"})
        assert ranged.status_code == 206
        assert ranged.content == UPSTREAM_DATA[100:200]
        assert ranged.headers["content-range"] == f"bytes 100-199/{len(UPSTREAM_DATA)}"


def test_api_play_quality_streams(webapp):
    """转码各档（高->低）+ 原画都要出现在清晰度列表里，默认播网盘建议的那一档."""
    base, _ = webapp
    data = httpx.get(base + "/api/play",
                     params={"path": "/MediaFans/movie.mkv"}, timeout=10).json()
    assert [s["key"] for s in data["streams"]] == ["4k", "high", "origin"]
    assert [s["label"] for s in data["streams"]] == ["4K 2160P", "高清 540P", "原画 2160P"]
    assert data["has_transcode"] is True
    assert data["default_key"] == "high"
    # 默认播放地址 = 建议档，而不是原画
    high = next(s for s in data["streams"] if s["key"] == "high")
    assert data["stream_url"] == high["url"]
    assert [s["origin"] for s in data["streams"]] == [False, False, True]
    assert all(s["url"].startswith("/stream?t=") for s in data["streams"])

    # 每一档都能真实出流
    with httpx.Client(timeout=10) as c:
        for s in data["streams"]:
            r = c.get(base + s["url"], headers={"Range": "bytes=0-99"})
            assert r.status_code == 206
            assert r.content == UPSTREAM_DATA[:100]


def test_stream_supports_head(webapp):
    """播放内核常先 HEAD 探测 Range 支持，再决定要不要边下边播."""
    base, _ = webapp
    data = httpx.get(base + "/api/play",
                     params={"path": "/MediaFans/movie.mkv"}, timeout=10).json()
    r = httpx.head(base + data["stream_url"], timeout=10)
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == str(len(UPSTREAM_DATA))
    assert r.content == b""


def test_stream_reuses_upstream_connections(webapp):
    """连接复用是流畅度的关键：多次 Range 不该每次都重新握手上游."""
    base, _ = webapp
    data = httpx.get(base + "/api/play",
                     params={"path": "/MediaFans/movie.mkv"}, timeout=10).json()
    before = len(UPSTREAM_CONNECTIONS)
    with httpx.Client(timeout=10) as c:
        for i in range(6):
            r = c.get(base + data["stream_url"],
                      headers={"Range": f"bytes={i * 100}-{i * 100 + 99}"})
            assert r.status_code == 206
    # 6 次 Range 请求远少于 6 条新上游连接
    assert len(UPSTREAM_CONNECTIONS) - before < 6


def test_bad_token_and_errors(webapp):
    base, _ = webapp
    r = httpx.get(base + "/stream?t=nonexistent", timeout=10)
    assert r.status_code == 404
    assert "令牌" in r.json()["error"]

    r = httpx.get(base + "/api/play", params={"path": "/不存在.mkv"}, timeout=10)
    assert r.status_code == 400
    assert "不存在" in r.json()["error"]

    r = httpx.get(base + "/nope", timeout=10)
    assert r.status_code == 404


def test_api_search(webapp):
    base, _ = webapp
    r = httpx.get(base + "/api/search", params={"kw": "沙丘", "netdisk": "quark"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert [x["netdisk"] for x in data["results"]] == ["quark"]  # netdisk 过滤生效
    item = data["results"][0]
    assert item["title"] == "沙丘 4K"
    assert item["passcode"] == "88ab"
    assert data["errors"] == ["yiso: down"]

    # 不过滤时返回夸克+阿里
    data_all = httpx.get(base + "/api/search", params={"kw": "沙丘"}, timeout=10).json()
    assert [x["netdisk"] for x in data_all["results"]] == ["quark", "aliyun"]

    # 缺关键词
    r = httpx.get(base + "/api/search", timeout=10)
    assert r.status_code == 400


def test_api_share_and_save_selected(webapp):
    base, app = webapp
    share = "https://pan.quark.cn/s/q1"

    # 缺提取码 -> 报错且信息友好
    r = httpx.get(base + "/api/share", params={"url": share}, timeout=10)
    assert r.status_code == 400
    assert "提取码" in r.json()["error"]

    # 带提取码 -> 文件列表
    r = httpx.get(base + "/api/share", params={"url": share, "code": "88ab"}, timeout=10)
    assert r.status_code == 200
    files = r.json()["files"]
    assert [f["name"] for f in files] == ["movie.2160p.mkv", "note.txt"]
    assert files[0]["size_h"] == "1.00GB"
    assert files[0]["fid"] == "SF1"

    # 勾选了 SF1 -> 只转存它，目标目录默认 save_dir
    r = httpx.post(base + "/api/save", timeout=15,
                   json={"url": share, "code": "88ab", "fids": ["SF1"]})
    assert r.status_code == 200
    assert r.json() == {"saved": 1, "dir": "/MediaFans", "netdisk": "quark"}
    save_body = app.test_state["save"]
    assert save_body["fid_list"] == ["SF1"]
    assert save_body["fid_token_list"] == ["TK1"]
    assert save_body["to_pdir_fid"] == "DIRFID"
    assert "created_dir" not in app.test_state  # 已存在，无需建目录

    # 指定新目录 -> 自动创建
    r = httpx.post(base + "/api/save", timeout=15,
                   json={"url": share, "code": "88ab", "fids": [],
                         "to": "/MediaFans/新剧"})
    assert r.status_code == 200
    assert r.json()["dir"] == "/MediaFans/新剧"
    assert app.test_state["created_dir"] == "/MediaFans/新剧"
    assert app.test_state["save"]["fid_list"] == ["SF1", "SF2"]  # 不勾选 = 全部

    # 非网盘链接
    r = httpx.get(base + "/api/share", params={"url": "https://example.com/x"}, timeout=10)
    assert r.status_code == 400
    assert "分享链接" in r.json()["error"]


# ---------------------------------------------------------------- 扫码登录
def test_qr_svg_data_uri_renders_without_pil():
    """网页版给的是 URL，得本地渲染成二维码；SvgPathImage 是纯 Python，不依赖 PIL."""
    from mediafans.web import qr_svg_data_uri

    uri = qr_svg_data_uri("https://su.quark.cn/4_eMHBJ?token=abc")
    assert uri.startswith("data:image/svg+xml;base64,")
    import base64
    assert b"<svg" in base64.b64decode(uri.split(",", 1)[1])


def test_login_start_rejects_unknown_kind(webapp):
    base, _ = webapp
    r = httpx.post(base + "/api/login/start", json={"kind": "aliyun"}, timeout=10)
    assert r.status_code == 400
    assert "未知登录方式" in r.json()["error"]


def test_login_start_without_configured_paths(webapp):
    """没配存放位置时要明确报错，而不是扫完码才发现存不下去."""
    base, app = webapp
    assert app.cookie_path is None and app.tv_token_path is None
    for kind in ("quark", "tv"):
        r = httpx.post(base + "/api/login/start", json={"kind": kind}, timeout=10)
        assert r.status_code == 400
        assert "存放位置" in r.json()["error"]


def test_login_poll_with_dead_session(webapp):
    base, _ = webapp
    r = httpx.get(base + "/api/login/poll", params={"sid": "nope"}, timeout=10)
    assert r.status_code == 400
    assert "会话已失效" in r.json()["error"]


def test_login_saves_credentials(tmp_path, upstream_url, monkeypatch):
    """扫码成功后必须真的落盘，并让后续请求用上新凭据."""
    from mediafans import web as W

    cookie_path = tmp_path / "quark.cookie"
    tv_path = tmp_path / "quark_tv.json"
    app = make_webapp(upstream_url)
    app.cookie_path, app.tv_token_path = cookie_path, tv_path
    base = f"http://127.0.0.1:{app.port}"

    class _FakeFlow:
        def __init__(self, kind):
            self.kind = kind

        def get_qr_code(self):
            return ("HANDLE", "data:image/png;base64,AAA")

        def poll_once(self, handle):
            assert handle == "HANDLE"
            return "success", "CODE"

        def exchange_ticket(self, code):
            return "__puus=from-scan"

        def exchange_code(self, code):
            return {"access_token": "AT", "refresh_token": "RT", "device_id": "DEV"}

    monkeypatch.setattr(W, "qr_svg_data_uri", lambda url: "data:image/svg+xml;base64,AAA")
    monkeypatch.setattr("mediafans.auth.QuarkQRLogin", lambda *a, **k: _FakeFlow("quark"))
    monkeypatch.setattr("mediafans.auth.QuarkTVLogin", lambda *a, **k: _FakeFlow("tv"))
    try:
        for kind, path in (("quark", cookie_path), ("tv", tv_path)):
            started = httpx.post(base + "/api/login/start", json={"kind": kind}, timeout=10).json()
            assert started["kind"] == kind and started["qr"].startswith("data:image/")
            done = httpx.get(base + "/api/login/poll",
                             params={"sid": started["sid"]}, timeout=10).json()
            assert done["state"] == "success", done
            assert path.exists()
        assert cookie_path.read_text(encoding="utf-8") == "__puus=from-scan"
        saved = json.loads(tv_path.read_text(encoding="utf-8"))
        assert saved["access_token"] == "AT" and saved["refresh_token"] == "RT"
    finally:
        app.stop()


# ------------------------------------------------------- 分享浏览：钻空壳 / 分类
def _nested_share_app(upstream_url):
    """分享结构: 根 -> 「沙丘2 4K合集」 -> 「4K原盘」 -> 2 个视频 + 说明.txt + 字幕."""
    layout = {
        "0": [{"fid": "L1", "file_name": "沙丘2 4K合集", "dir": True}],
        "L1": [{"fid": "L2", "file_name": "4K原盘", "dir": True}],
        "L2": [
            {"fid": "V1", "file_name": "Dune2.2160p.mkv", "dir": False,
             "size": 30 * 1024 ** 3, "share_fid_token": "TV1"},
            {"fid": "V2", "file_name": "Dune2.1080p.mp4", "dir": False,
             "size": 8 * 1024 ** 3, "share_fid_token": "TV2"},
            {"fid": "T1", "file_name": "更多资源请关注.txt", "dir": False,
             "size": 120, "share_fid_token": "TT1"},
            {"fid": "S1", "file_name": "Dune2.chs.srt", "dir": False,
             "size": 40, "share_fid_token": "TS1"},
        ],
    }
    state = {}

    def handler(request):
        path = request.url.path
        if path.endswith("/share/sharepage/token"):
            return httpx.Response(200, json={"status": 200, "code": 0, "data": {"stoken": "ST"}})
        if path.endswith("/share/sharepage/detail"):
            fid = request.url.params.get("pdir_fid", "0")
            lst = layout.get(fid, [])
            return httpx.Response(200, json={"code": 0, "data": {
                "list": lst, "metadata": {"_total": len(lst)}}})
        if path.endswith("/file/info/path_list"):
            return httpx.Response(200, json={
                "code": 0, "data": [{"fid": "DIRFID", "file_path": "/MediaFans"}]})
        if path.endswith("/share/sharepage/save"):
            state["save"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "T"}})
        if path.endswith("/task"):
            return httpx.Response(200, json={"code": 0, "data": {
                "status": 2, "save_as": {"save_as_top_fids": ["N1"]}}})
        raise AssertionError(f"unexpected {path}")

    drive = QuarkDrive({"cookie": "__puus=x", "save_dir": "/MediaFans"},
                       transport=httpx.MockTransport(handler))
    app = WebApp(lambda nd="quark": drive)
    app.test_state = state
    app.start()
    return app


@pytest.fixture()
def nested_share(upstream_url):
    app = _nested_share_app(upstream_url)
    yield f"http://127.0.0.1:{app.port}", app
    app.stop()


def test_share_auto_descends_through_empty_shells(nested_share):
    """套了两层只有一个子目录的空壳，应该直接落到真正有文件的那层."""
    base, _ = nested_share
    data = httpx.get(base + "/api/share",
                     params={"url": "https://pan.quark.cn/s/q1"}, timeout=10).json()
    assert data["dir_fid"] == "L2"
    assert data["crumb"] == "沙丘2 4K合集/4K原盘"
    assert [f["name"] for f in data["files"]] == [
        "Dune2.1080p.mp4", "Dune2.2160p.mkv", "Dune2.chs.srt", "更多资源请关注.txt"]


def test_share_classifies_playable_and_junk(nested_share):
    base, _ = nested_share
    data = httpx.get(base + "/api/share",
                     params={"url": "https://pan.quark.cn/s/q1"}, timeout=10).json()
    kinds = {f["name"]: (f["kind"], f["playable"]) for f in data["files"]}
    assert kinds["Dune2.2160p.mkv"] == ("video", True)
    assert kinds["Dune2.chs.srt"] == ("subtitle", False)
    assert kinds["更多资源请关注.txt"] == ("other", False)
    assert data["counts"]["playable"] == 2
    assert data["counts"]["junk"] == 2      # 字幕 + 广告 txt
    assert data["counts"]["size_h"] == "38.00GB"


def test_share_explicit_dir_navigation(nested_share):
    """手动点进某一层时，也要能正常列出来."""
    base, _ = nested_share
    data = httpx.get(base + "/api/share",
                     params={"url": "https://pan.quark.cn/s/q1", "dir_fid": "L1",
                             "crumb": "沙丘2 4K合集"}, timeout=10).json()
    assert data["dir_fid"] == "L2"          # L1 里只有一个空壳，继续钻
    assert data["crumb"] == "沙丘2 4K合集/4K原盘"


def test_save_from_subdirectory_uses_that_level(nested_share):
    """从子目录勾选转存时，必须按那一层去取 share_fid_token，否则会失败."""
    base, app = nested_share
    r = httpx.post(base + "/api/save", timeout=15, json={
        "url": "https://pan.quark.cn/s/q1", "dir_fid": "L2",
        "fids": ["V1", "V2"], "to": "/MediaFans"})
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 1
    body = app.test_state["save"]
    assert body["fid_list"] == ["V1", "V2"]
    assert body["fid_token_list"] == ["TV1", "TV2"]   # 拿到了子目录层的 token


def test_api_list_marks_playable(webapp):
    base, _ = webapp
    files = httpx.get(base + "/api/list", params={"path": "/MediaFans"},
                      timeout=10).json()["files"]
    by = {f["name"]: f for f in files}
    assert by["movie.mkv"]["kind"] == "video" and by["movie.mkv"]["playable"] is True
    assert by["剧集A"]["kind"] == "dir" and by["剧集A"]["playable"] is False


# ------------------------------------------------------- 局域网开放 + 访问令牌
def test_no_token_when_loopback_only(webapp):
    """默认只听本机，不设令牌，一切照旧."""
    base, app = webapp
    assert app.token == ""
    assert httpx.get(base + "/", timeout=10).status_code == 200
    assert httpx.get(base + "/api/list", params={"path": "/MediaFans"},
                     timeout=10).status_code == 200


def test_page_url_carries_token_and_host(upstream_url):
    app = make_webapp(upstream_url)
    app.token = "SECRET"
    try:
        url = app.page_url("/MediaFans", host="192.168.1.9")
        assert url.startswith("http://192.168.1.9:")
        assert "token=SECRET" in url and "path=/MediaFans" in url
        assert "token=SECRET" in app.page_url()          # 没有 path 也要带令牌
    finally:
        app.stop()


def test_loopback_client_skips_token(upstream_url):
    """设了令牌，本机浏览器也不用带 —— 测试就是从 127.0.0.1 连的."""
    app = make_webapp(upstream_url)
    app.token = "SECRET"
    base = f"http://127.0.0.1:{app.port}"
    try:
        assert httpx.get(base + "/", timeout=10).status_code == 200
        assert httpx.get(base + "/api/list", params={"path": "/MediaFans"},
                         timeout=10).status_code == 200
    finally:
        app.stop()


def test_token_check_logic(upstream_url):
    """非本机来源必须带对令牌：直接验鉴权函数，绕开测试环境只能从回环连的限制."""
    app = make_webapp(upstream_url)
    app.token = "SECRET"
    try:
        handler = app._make_handler()
        checker = handler._authorized

        class Fake:
            def __init__(self, addr, cookie=""):
                self.client_address = (addr, 1234)
                self.headers = {"Cookie": cookie} if cookie else {}
                self.headers = httpx.Headers(self.headers)

        assert checker(Fake("192.168.1.9"), {}) is False              # 外部裸访问
        assert checker(Fake("192.168.1.9"), {"token": "WRONG"}) is False
        assert checker(Fake("192.168.1.9"), {"token": "SECRET"}) is True
        assert checker(Fake("192.168.1.9", "mf_token=SECRET"), {}) is True
        assert checker(Fake("192.168.1.9", "a=1; mf_token=SECRET"), {}) is True
        assert checker(Fake("192.168.1.9", "mf_token=nope"), {}) is False
        assert checker(Fake("127.0.0.1"), {}) is True                 # 本机免验
    finally:
        app.stop()


def test_page_sets_token_cookie(upstream_url):
    """首页带对令牌就种 cookie，之后 <video src> 这类子请求不用再挂令牌."""
    app = make_webapp(upstream_url)
    app.token = "SECRET"
    base = f"http://127.0.0.1:{app.port}"
    try:
        r = httpx.get(base + "/", params={"token": "SECRET"}, timeout=10)
        assert r.status_code == 200
        assert "mf_token=SECRET" in r.headers.get("set-cookie", "")
        # 令牌不对就不该种 cookie
        r2 = httpx.get(base + "/", params={"token": "WRONG"}, timeout=10)
        assert "mf_token" not in r2.headers.get("set-cookie", "")
    finally:
        app.stop()


# ------------------------------------------------------- TV 直链播放
def _tv_webapp(tmp_path, upstream_url, tv_ok=True):
    """PC 驱动照旧（列目录/转存要用），播放走 TV 客户端拿免凭据直链."""
    app = make_webapp(upstream_url)
    token = tmp_path / "quark_tv.json"
    token.write_text(json.dumps(
        {"access_token": "AT", "refresh_token": "RT", "device_id": "DEV"}), encoding="utf-8")
    app.tv_token_path = token

    class FakeTV:
        def get_play_target(self, fid, name=""):
            if not tv_ok:
                raise RuntimeError("设备数超限")
            from mediafans.models import PlayTarget, StreamVariant
            return PlayTarget(
                file_name=name, url="https://cdn.quark.cn/4k",
                download_url="https://dl.quark.cn/raw",
                cookie="", ua="",          # TV 直链不需要任何凭据
                default_key="4k",
                variants=[
                    StreamVariant(key="4k", label="4K", url="https://cdn.quark.cn/4k",
                                  height=2160, width=3840, size=1, bitrate=3500, fmt="mp4"),
                    StreamVariant(key="origin", label="原画",
                                  url="https://dl.quark.cn/raw",
                                  fmt="video/x-matroska", origin=True),
                ])

    app._tv = FakeTV()
    return app


def test_play_returns_direct_cdn_links(tmp_path, upstream_url):
    """有 TV token 时，播放地址应是网盘 CDN 的绝对地址，浏览器直连，不经本机."""
    app = _tv_webapp(tmp_path, upstream_url)
    base = f"http://127.0.0.1:{app.port}"
    try:
        d = httpx.get(base + "/api/play",
                      params={"path": "/MediaFans/movie.mkv"}, timeout=10).json()
        assert d["direct"] is True
        assert d["fallback_reason"] == ""
        assert d["stream_url"] == "https://cdn.quark.cn/4k"     # 默认档是绝对直链
        for s in d["streams"]:
            assert s["direct"] is True
            assert s["url"].startswith("https://")
            # 同时给出代理地址，直链被拒时前端能就地回退
            assert s["proxy_url"].startswith("/stream?t=")
        assert d["needs_cookie"] is False                        # TV 直链免凭据
    finally:
        app.stop()


def test_falls_back_to_proxy_when_tv_unavailable(tmp_path, upstream_url):
    """TV 挂了（设备数超限/token 过期）要无缝退回 PC + 本机转发."""
    app = _tv_webapp(tmp_path, upstream_url, tv_ok=False)
    base = f"http://127.0.0.1:{app.port}"
    try:
        d = httpx.get(base + "/api/play",
                      params={"path": "/MediaFans/movie.mkv"}, timeout=10).json()
        assert d["direct"] is False
        assert "设备数超限" in d["fallback_reason"]
        assert d["stream_url"].startswith("/stream?t=")
        assert all(s["direct"] is False for s in d["streams"])
        assert d["needs_cookie"] is True                         # 走 PC 直链就要 cookie
        # 退回来的流仍然真的能出数据
        r = httpx.get(base + d["stream_url"], headers={"Range": "bytes=0-99"}, timeout=10)
        assert r.status_code == 206
    finally:
        app.stop()


def test_no_tv_switch_forces_proxy(tmp_path, upstream_url):
    """--no-tv 时即使有 token 也全走转发."""
    app = _tv_webapp(tmp_path, upstream_url)
    app.use_tv = False
    base = f"http://127.0.0.1:{app.port}"
    try:
        d = httpx.get(base + "/api/play",
                      params={"path": "/MediaFans/movie.mkv"}, timeout=10).json()
        assert d["direct"] is False
        assert d["stream_url"].startswith("/stream?t=")
    finally:
        app.stop()


def test_page_disables_referrer():
    """夸克 CDN 对直链做了防盗链：带 Referer 一律 403，而 <video> 没有元素级开关，
    只能整页关掉。这一行掉了直链播放就全废，所以专门盯住。"""
    from mediafans.web import PAGE_HTML

    assert '<meta name="referrer" content="no-referrer">' in PAGE_HTML


def test_proxied_requests_do_not_get_the_loopback_exemption(upstream_url):
    """放在 nginx 后面时请求也来自 127.0.0.1，若照样免令牌就等于认证被绕过."""
    app = make_webapp(upstream_url)
    app.token = "SECRET"
    try:
        checker = app._make_handler()._authorized

        class Fake:
            def __init__(self, headers):
                self.client_address = ("127.0.0.1", 1234)
                self.headers = httpx.Headers(headers)

        assert checker(Fake({}), {}) is True                                # 真本机浏览器
        assert checker(Fake({"X-Forwarded-For": "1.2.3.4"}), {}) is False   # 反代来的
        assert checker(Fake({"X-Forwarded-For": "1.2.3.4"}),
                       {"token": "SECRET"}) is True                          # 带对令牌才行
        assert checker(Fake({"X-Forwarded-For": "1.2.3.4",
                             "Cookie": "mf_token=SECRET"}), {}) is True
    finally:
        app.stop()


# ------------------------------------------------------- 按作品搜（TMDB 优先）
def _tmdb_webapp(upstream_url, items=None, boom=False):
    from mediafans.models import MediaItem

    app = make_webapp(upstream_url)

    class FakeTmdb:
        from mediafans.metadata.tmdb import TmdbClient as _T

        is_chinese = staticmethod(_T.is_chinese)
        is_animation = staticmethod(_T.is_animation)

        def search(self, q, kind="multi", prefer_chinese=True):
            if boom:
                raise RuntimeError("TMDB 挂了")
            return items if items is not None else [
                MediaItem(title="末日地堡", original_title="Silo", media_type="tv",
                          year="2023", rating=8.1, overview="地堡里的故事",
                          tmdb_id=125988, poster="https://img/p.jpg"),
                MediaItem(title="沙丘", original_title="Dune", media_type="movie",
                          year="2021", rating=7.8, overview="", tmdb_id=438631,
                          poster=""),
                MediaItem(title="没有id的", media_type="tv", tmdb_id=0),
            ]

    app._tmdb = FakeTmdb()
    app.tmdb_key = "x"
    return app


def test_search_media_returns_works_not_shares(upstream_url):
    """顶栏搜的是作品（剧/电影），不是分享链接——先定位作品才有季/集结构可用."""
    app = _tmdb_webapp(upstream_url)
    base = f"http://127.0.0.1:{app.port}"
    try:
        d = httpx.get(base + "/api/search/media", params={"kw": "末日地堡"},
                      timeout=10).json()
        assert d["query"] == "末日地堡"
        titles = [i["title"] for i in d["items"]]
        assert titles == ["末日地堡", "沙丘"]        # tmdb_id 为 0 的丢掉
        tv = d["items"][0]
        assert tv["media_type"] == "tv" and tv["tmdb_id"] == 125988
        assert tv["poster"] and tv["year"] == "2023" and tv["rating"] == 8.1
        assert d["items"][1]["media_type"] == "movie"   # 电影走另一条路
    finally:
        app.stop()


def test_search_media_requires_keyword_and_tmdb(upstream_url):
    app = _tmdb_webapp(upstream_url)
    base = f"http://127.0.0.1:{app.port}"
    try:
        r = httpx.get(base + "/api/search/media", timeout=10)
        assert r.status_code == 400 and "关键词" in r.json()["error"]
    finally:
        app.stop()

    plain = make_webapp(upstream_url)          # 没配 tmdb_key
    try:
        r = httpx.get(f"http://127.0.0.1:{plain.port}/api/search/media",
                      params={"kw": "x"}, timeout=10)
        assert r.status_code == 400 and "tmdb" in r.json()["error"].lower()
    finally:
        plain.stop()


def test_search_media_surfaces_upstream_failure(upstream_url):
    app = _tmdb_webapp(upstream_url, boom=True)
    try:
        r = httpx.get(f"http://127.0.0.1:{app.port}/api/search/media",
                      params={"kw": "x"}, timeout=10)
        assert r.status_code == 400 and "搜索失败" in r.json()["error"]
    finally:
        app.stop()


def test_page_search_box_targets_media_not_shares():
    """顶栏输入框要走按作品搜；原始网盘搜索挪到了「搜资源」页自己的框."""
    from mediafans.web import PAGE_HTML

    assert "searchMedia()" in PAGE_HTML
    assert 'id="diskKw"' in PAGE_HTML          # 搜资源页仍能直接搜分享
    assert 'id="segFound"' in PAGE_HTML        # 搜索结果作为追剧页的一个分段


def test_page_discards_stale_poster_wall_responses():
    """页面刚打开就搜索时，先发出的榜单请求会后回来盖掉搜索结果——用序号丢弃过期响应."""
    from mediafans.web import PAGE_HTML as html

    assert "let showsSeq = 0;" in html
    # 搜索和榜单都要取号，并在两个分支上都校验
    assert html.count("const seq = ++showsSeq;") == 2
    assert html.count("if (seq !== showsSeq) return;") == 4


# ---------------------------------------------------------------- 进度 / 最近观看
def _watch_app(tmp_path):
    from mediafans.web import WebApp

    return WebApp(lambda nd="quark": None, watch_path=tmp_path / "watch.json")


def test_watch_roundtrip(tmp_path):
    app = _watch_app(tmp_path)
    app.api_watch_save({"path": "/a/E01.mkv", "position": 600, "duration": 2700,
                        "tmdb_id": 125988, "title": "末日地堡", "season": 2, "episode": 1})
    assert app.api_watch_get("/a/E01.mkv")["mark"]["resume_at"] == 600
    rows = app.api_watch_recent(10)["items"]
    assert [(r["title"], r["season"], r["episode"]) for r in rows] == [("末日地堡", 2, 1)]


def test_watch_save_requires_path(tmp_path):
    from mediafans.errors import MediaFansError

    with pytest.raises(MediaFansError):
        _watch_app(tmp_path).api_watch_save({"position": 10})


def test_watch_get_unknown_path_is_null(tmp_path):
    assert _watch_app(tmp_path).api_watch_get("/没看过.mkv")["mark"] is None


def test_watch_forget(tmp_path):
    app = _watch_app(tmp_path)
    app.api_watch_save({"path": "/a/E01.mkv", "position": 600, "duration": 2700})
    assert app.api_watch_forget({"path": "/a/E01.mkv"})["ok"] is True
    assert app.api_watch_recent(10)["items"] == []


def test_watch_tolerates_junk_ids(tmp_path):
    """前端偶尔会把 tmdb_id 传成空串，不该 500."""
    app = _watch_app(tmp_path)
    app.api_watch_save({"path": "/a/x.mkv", "position": 600, "duration": 2700,
                        "tmdb_id": "", "season": "abc", "episode": None})
    assert app.api_watch_get("/a/x.mkv")["mark"]["tmdb_id"] is None


def test_series_rows_carry_watch_state(tmp_path):
    """剧集矩阵要能显示每一集看到哪了，不然得点进去播一下才知道."""
    app = _watch_app(tmp_path)
    app.api_watch_save({"path": "/MediaFans/剧/E01.mkv", "position": 600, "duration": 2700})
    data = app._attach_watch({"episodes": [
        {"episode": 1, "local": {"path": "/MediaFans/剧/E01.mkv"}},
        {"episode": 2, "local": {"path": "/MediaFans/剧/E02.mkv"}},
        {"episode": 3, "local": None},
    ]})
    assert data["episodes"][0]["watched"]["percent"] == 22
    assert "watched" not in data["episodes"][1]
    assert "watched" not in data["episodes"][2]


def test_page_has_series_and_watch_tabs_with_drive_last():
    """剧集和最近观看是独立标签页；我的网盘排最后."""
    from mediafans.web import PAGE_HTML

    order = [PAGE_HTML.index(f'id="tabbtn-{t}"')
             for t in ("shows", "series", "watch", "search", "mine")]
    assert order == sorted(order), "标签顺序应为 追剧/剧集/最近观看/搜资源/我的网盘"
    assert 'id="tab-series"' in PAGE_HTML and 'id="tab-watch"' in PAGE_HTML
    # 剧集不再是盖住播放器的弹层
    assert "seriesPanel" not in PAGE_HTML


def test_page_progress_is_server_side():
    """进度存服务端才能跨设备接着看；localStorage 那套要彻底拿掉."""
    from mediafans.web import PAGE_HTML

    assert "mf_pos" not in PAGE_HTML and "mf_recent" not in PAGE_HTML
    assert "/api/watch" in PAGE_HTML


def test_page_auto_next_prefers_next_episode():
    """在剧集页播完要接下一集，而不是按目录顺序取下一个文件."""
    from mediafans.web import PAGE_HTML

    assert "function episodeNav()" in PAGE_HTML
    # playAdjacent 在剧集页里走的是集，不是目录里的下一个文件
    assert "if (target) playEpisode(series.data, target, 0);" in PAGE_HTML


def test_pages_and_apis_are_not_cached(upstream_url):
    """页面的 JS 是内联的，每次部署都变。

    不禁缓存的话浏览器会按启发式缓存一直跑旧代码，看起来就像「部署了但没生效」。
    """
    app = make_webapp(upstream_url)
    base = f"http://127.0.0.1:{app.port}"
    try:
        for path in ("/", "/api/list?path=/"):
            r = httpx.get(base + path, timeout=10)
            assert "no-store" in r.headers.get("cache-control", ""), path
    finally:
        app.stop()


def test_boot_does_not_clobber_an_immediate_search():
    """开屏那步是异步的：用户一打开页面就输关键词，榜单后回来会把结果盖掉。

    序号只挡得住「旧请求晚回来」，挡不住「开屏比用户晚出发」，所以开屏
    自己也要看一眼用户是不是已经动手了。
    """
    from mediafans.web import PAGE_HTML

    assert "const before = showsSeq;" in PAGE_HTML
    assert "if (showsSeq !== before) return;" in PAGE_HTML
    # 搜过之后切回追剧页不该再自动拉榜单
    assert "showsLoaded = true;\n  switchTab('shows');" in PAGE_HTML


def test_season_fetch_needs_a_scanned_season(tmp_path):
    """没扫过就没有来源信息，得说清楚是让人去扫，而不是报个 500."""
    from mediafans.errors import MediaFansError
    from mediafans.web import WebApp

    app = WebApp(lambda nd="quark": None, watch_path=tmp_path / "w.json")
    with pytest.raises(MediaFansError) as e:
        app.api_season_fetch({"tmdb_id": 1, "season": 1})
    assert "重新扫描" in str(e.value)


def test_season_fetch_refuses_when_there_is_nothing_to_get(tmp_path):
    from mediafans.agent import EpisodeRow, LocalFile, SeriesView
    from mediafans.errors import MediaFansError
    from mediafans.web import WebApp

    app = WebApp(lambda nd="quark": None, watch_path=tmp_path / "w.json")
    row = EpisodeRow(episode=1, title="第1集")
    row.local = LocalFile(path="/d/E01.mkv", name="E01.mkv", size=1)
    app.series_cache.put(("quark", 1, 1), SeriesView(tmdb_id=1, title="剧", season=1,
                                            seasons=[], rows=[row]))
    with pytest.raises(MediaFansError) as e:
        app.api_season_fetch({"tmdb_id": 1, "season": 1})
    assert "没有可以补的集" in str(e.value)


def test_page_has_a_one_click_grab_button():
    """27 集逐个点太折磨人."""
    from mediafans.web import PAGE_HTML

    assert 'id="grabBtn"' in PAGE_HTML and "function fetchSeason()" in PAGE_HTML
    # 没扫出来源时按了也没用，所以按可补集数决定显不显示；
    # 电影只有一行，逐个版本挑才是重点，批量按钮反而碍事
    assert "grab.style.display = (!isMovie && c.available) ? '' : 'none';" in PAGE_HTML


# ---------------------------------------------------------------- 百度网盘全链路
def test_baidu_share_save_play_stream(upstream_url, tmp_path):
    from urllib.parse import parse_qs

    from mediafans.drive.baidu import BaiduDrive

    state = {}

    def handler(request):
        p = request.url.path
        if p == "/share/list":
            return httpx.Response(200, json={"errno": 0, "list": [{"fs_id": 1}]})
        if p == "/share/wxlist":
            state["wxlist"] = {k: v[0] for k, v in
                               parse_qs(request.content.decode()).items()}
            return httpx.Response(200, json={"errno": 0, "data": {
                "shareid": 111, "uk": 222, "seckey": "SEK",
                "list": [
                    {"fs_id": 333, "server_filename": "movie.mkv", "isdir": 0,
                     "size": 2048, "path": "/s/movie.mkv"},
                ]}})
        if p == "/api/gettemplatevariable":
            return httpx.Response(200, json={"errno": 0, "result": {"bdstoken": "B"}})
        if p == "/share/transfer":
            state["transfer"] = {k: v[0] for k, v in
                                 parse_qs(request.content.decode()).items()}
            state["transfer_cookie"] = request.headers.get("cookie", "")
            return httpx.Response(200, json={"errno": 0})
        if p == "/api/list":
            d = request.url.params.get("dir", "/")
            if d == "/":
                return httpx.Response(200, json={"errno": 0, "list": [
                    {"fs_id": 9, "path": "/MediaFans",
                     "server_filename": "MediaFans", "isdir": 1}]})
            if d == "/MediaFans":
                return httpx.Response(200, json={"errno": 0, "list": [
                    {"fs_id": 333, "path": "/MediaFans/movie.mkv",
                     "server_filename": "movie.mkv", "isdir": 0, "size": 2048}]})
            return httpx.Response(200, json={"errno": 0, "list": []})
        if p == "/api/create":
            return httpx.Response(200, json={"errno": 0, "path": "/MediaFans"})
        if p == "/api/filemetas":
            return httpx.Response(200, json={"errno": 0, "info": [
                {"fs_id": 333, "server_filename": "movie.mkv", "size": 2048,
                 "dlink": "https://pan.baidu.com/dl/x?fid=333"}]})
        if p == "/dl/x":
            state["dlink_head"] = request.headers.get("user-agent")
            return httpx.Response(302, headers={"Location": upstream_url})
        raise AssertionError(f"unexpected {p}")

    baidu = BaiduDrive({"cookie": "BDUSS=b; STOKEN=t",
                        "save_dir": "/MediaFans"},
                       transport=httpx.MockTransport(handler))
    app = WebApp(lambda nd="quark": baidu if nd == "baidu" else None,
                 watch_path=tmp_path / "watch.json")
    app.start()
    base = f"http://127.0.0.1:{app.port}"
    try:
        share = "https://pan.baidu.com/s/1BduLink?pwd=ab12"
        # 打开分享：网盘自动识别 + 文件列表
        r = httpx.get(base + "/api/share", params={"url": share, "code": "ab12"}, timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["netdisk"] == "baidu"
        assert [f["fid"] for f in body["files"]] == ["333"]
        # 转存：sekey 以 BDCLND cookie 传给 transfer
        r = httpx.post(base + "/api/save", json={"url": share, "code": "ab12",
                                                 "fids": ["333"]}, timeout=10)
        assert r.status_code == 200
        assert r.json()["netdisk"] == "baidu"
        assert state["transfer"]["fsidlist"] == "[333]"
        assert "BDCLND=SEK" in state["transfer_cookie"]
        # 列百度盘目录
        r = httpx.get(base + "/api/list", params={"path": "/MediaFans", "nd": "baidu"}, timeout=10)
        assert r.status_code == 200
        assert [f["name"] for f in r.json()["files"]] == ["movie.mkv"]
        # 播放：直链带 UA 要求，走本地 /stream 中继
        r = httpx.get(base + "/api/play",
                      params={"path": "/MediaFans/movie.mkv", "nd": "baidu"}, timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["netdisk"] == "baidu"
        assert not data["direct"]
        assert data["stream_url"].startswith("/stream?t=")
        rr = httpx.get(base + data["stream_url"],
                       headers={"Range": "bytes=0-99"}, timeout=10)
        assert rr.status_code == 206
        assert rr.content == UPSTREAM_DATA[:100]
        assert state["dlink_head"] == "pan.baidu.com"
        # 进度：百度记录带网盘前缀，最近观看解析出 netdisk
        r = httpx.post(base + "/api/watch", json={
            "path": "/MediaFans/movie.mkv", "netdisk": "baidu",
            "position": 600, "duration": 2700}, timeout=10)
        assert r.status_code == 200
        r = httpx.get(base + "/api/watch/recent", timeout=10)
        item = r.json()["items"][0]
        assert item["path"] == "baidu:/MediaFans/movie.mkv"
        assert item["netdisk"] == "baidu"
        assert item["play_path"] == "/MediaFans/movie.mkv"
    finally:
        app.stop()


def test_expired_credentials_get_a_clickable_relogin_bar():
    """凭据过期跟别的错不一样：它不会自己好，用户必须去重登一次。

    百度的网页登录态尤其短（实测二十来分钟），只在角落里丢一句错误，
    用户只会觉得「怎么又不好使了」。
    """
    from mediafans.web import PAGE_HTML

    assert 'id="authBar"' in PAGE_HTML and "function reloginFromBar()" in PAGE_HTML
    # 提示条要能带着「是哪个盘」去开对应的扫码
    assert "startLogin(authBarNd)" in PAGE_HTML
    # 关键的几条出错路径都要接上
    assert PAGE_HTML.count("reportError(") >= 7
    # 批量转存里单个来源失败不会让任务整体 error，凭据过期藏在逐条错误里，
    # 不检查的话用户只看到「10 集失败」，正是最该提示的场景
    assert "for (const e of (b.errors || [])) {" in PAGE_HTML


def test_baidu_paste_login_normalises_the_cookie(tmp_path):
    """粘贴来的 cookie 也要去掉 *_BFESS 重复键，跟扫码存的形态保持一致."""
    from mediafans.web import WebApp

    app = WebApp(lambda nd=None: None,
                 cookie_paths={"baidu": tmp_path / "baidu.cookie"},
                 watch_path=tmp_path / "w.json")
    app.api_login_baidu({"cookie": "BDUSS=a; BDUSS_BFESS=a2; STOKEN=s; STOKEN_BFESS=s2"})
    saved = (tmp_path / "baidu.cookie").read_text(encoding="utf-8")
    assert saved == "BDUSS=a; STOKEN=s"


def test_baidu_paste_login_warns_about_missing_stoken(tmp_path):
    from mediafans.web import WebApp

    app = WebApp(lambda nd=None: None,
                 cookie_paths={"baidu": tmp_path / "baidu.cookie"},
                 watch_path=tmp_path / "w.json")
    d = app.api_login_baidu({"cookie": "BDUSS=a"})
    assert "转存会失败" in d["message"]


def test_auth_bar_strips_the_source_prefix():
    """逐条错误带「哪个来源失败了」的前缀，提示条只要原因那半句."""
    from mediafans.web import PAGE_HTML

    assert "const cut = why.lastIndexOf('：');" in PAGE_HTML


def test_tv_failure_is_not_permanent(tmp_path, monkeypatch):
    """TV 挂过一次就永久走代理，是「有时候卡、重启就好」的根源。

    实测两条路差得很远：TV 直链浏览器直连 CDN 5-7 MB/s，退回代理经服务器中转
    只有 0.85 MB/s 还带秒级停顿。一次网络抖动把之后所有播放永久钉在慢路上，
    这种粘性降级最难查，所以失败要有冷却期。
    """
    import time as _t

    from mediafans.web import WebApp

    tok = tmp_path / "quark_tv.json"
    tok.write_text("{}", encoding="utf-8")
    app = WebApp(lambda nd=None: None, tv_token_path=tok,
                 watch_path=tmp_path / "w.json")

    built = []

    class FakeTV:
        def __init__(self, path):
            built.append(1)

    monkeypatch.setattr("mediafans.drive.quark_tv.QuarkTVClient", FakeTV)
    assert app.tv is not None and len(built) == 1

    # 模拟一次瞬时失败
    app._tv_failed = "网络抖了一下"
    app._tv_failed_at = _t.time()
    assert app.tv is None, "冷却期内应该走代理"

    # 冷却期过了要自己再试，不该等重启
    app._tv_failed_at = _t.time() - app.TV_RETRY_AFTER - 1
    assert app.tv is not None, "冷却结束后应该重新尝试 TV 直链"
    assert len(built) == 2


# ---------------------------------------------------------------- 播放器 UI
def test_controls_float_over_the_video():
    """控制条原来是流式布局的一员，会把画面往上挤出一条黑边——全屏时等于永久遮挡."""
    from mediafans.web import PAGE_HTML

    assert "#ctrl { position:absolute; left:0; right:0; bottom:0;" in PAGE_HTML
    # 悬浮之后进度条要能压住画面，不能再用不透明底色
    assert "background:rgba(255,255,255,.3)" in PAGE_HTML


def test_controls_auto_hide_only_while_playing():
    """暂停着还自动消失会让人以为卡死了."""
    from mediafans.web import PAGE_HTML

    assert "#stage.idle #ctrl { opacity:0;" in PAGE_HTML
    assert "if (video.paused || !video.src) return;" in PAGE_HTML
    # 鼠标停在控制条上时不隐，不然想点的东西会跑掉
    assert "if (!ctrlHot && !video.paused)" in PAGE_HTML


def test_tap_toggles_the_controls():
    from mediafans.web import PAGE_HTML

    assert "if (stage.classList.contains('idle')) showCtrl();" in PAGE_HTML


def test_vertical_swipe_adjusts_brightness_and_volume():
    """左半屏调亮度、右半屏调音量，是手机播放器的通用手势。

    亮度改的是 video 的 CSS filter——浏览器没有调系统背光的 API。
    """
    from mediafans.web import PAGE_HTML

    assert "e.clientX - r.left) < r.width / 2 ? 'bright' : 'vol'" in PAGE_HTML
    assert "brightness(${bright.toFixed(2)})" in PAGE_HTML
    # 竖滑要用来调节，不能被页面滚动抢走
    assert "touch-action:none; }" in PAGE_HTML
    # 小位移算点击，不算滑动，否则点一下就把音量改了
    assert "GESTURE_SLOP" in PAGE_HTML


# ---------------------------------------------------------------- 电影
def _movie_webapp(upstream_url):
    """一个只认电影的 app：TMDB 侧只实现 movie_detail + 榜单。"""
    from mediafans.models import MediaItem

    app = make_webapp(upstream_url)

    class FakeTmdb:
        from mediafans.metadata.tmdb import TmdbClient as _T

        is_chinese = staticmethod(_T.is_chinese)
        is_animation = staticmethod(_T.is_animation)

        def movie_detail(self, tmdb_id):
            return {"tmdb_id": tmdb_id, "title": "流浪地球",
                    "original_title": "The Wandering Earth", "year": "2019",
                    "release_date": "2019-02-05", "animation": False,
                    "overview": "太阳要完了。", "poster": "https://img/p.jpg",
                    "runtime": 125, "total_seasons": 0, "total_episodes": 0,
                    "seasons": []}

        def chinese_movie(self, kind="now"):
            return [MediaItem(title="流浪地球", original_title="The Wandering Earth",
                              original_language="zh", media_type="movie", year="2019",
                              rating=7.9, tmdb_id=300, poster="https://img/p.jpg")]

        def now_playing(self, page=1):
            return [MediaItem(title="Dune", original_language="en",
                              media_type="movie", year="2021", tmdb_id=438631)]

    app._tmdb = FakeTmdb()
    app.tmdb_key = "x"
    return app


def test_series_endpoint_serves_movies_too(upstream_url):
    """电影走同一个端点：它就是只有一行的「季」，下游不用分两套。"""
    app = _movie_webapp(upstream_url)
    base = f"http://127.0.0.1:{app.port}"
    try:
        d = httpx.get(base + "/api/series",
                      params={"tmdb_id": 300, "season": 1, "media": "movie"},
                      timeout=10).json()
        assert d["media_type"] == "movie"
        assert d["season"] == 0            # season 只是缓存键，电影恒为 0
        assert d["year"] == "2019" and d["runtime"] == 125
        assert d["counts"]["total"] == 1
        assert d["episodes"][0]["episode"] == 1
        assert d["seasons"] == []
    finally:
        app.stop()


def test_movie_and_tv_do_not_share_a_cache_slot(upstream_url):
    """同一个 tmdb_id 在电影和剧集里是两部作品，缓存不能串。"""
    app = _movie_webapp(upstream_url)
    app.api_series(300, 1, False, "quark", "movie")
    assert app.series_cache.get(("quark", 300, 0)) is not None
    assert app.series_cache.get(("quark", 300, 1)) is None


def test_discover_has_movie_lists(upstream_url):
    """「发现」页要能切到电影榜，而不是只有剧集."""
    app = _movie_webapp(upstream_url)
    base = f"http://127.0.0.1:{app.port}"
    try:
        d = httpx.get(base + "/api/discover", params={"kind": "now"}, timeout=10).json()
        assert d["media_type"] == "movie"
        titles = [i["title"] for i in d["items"]]
        assert titles == ["流浪地球", "Dune"]        # 华语在前
        assert all(i["media_type"] == "movie" for i in d["items"])
    finally:
        app.stop()


def test_unknown_discover_kind_falls_back_to_tv(upstream_url):
    """乱传 kind 不该 500——回到默认那一档就行."""
    app = _movie_webapp(upstream_url)
    assert app.DISCOVER["airing"][2] == "tv"
    assert app.DISCOVER.get("乱来") is None


def test_watch_mark_remembers_it_was_a_movie(tmp_path, upstream_url):
    """电影没有季/集，但散片同样两者皆空——得记下类型，「继续观看」才知道去哪。"""
    app = make_webapp(upstream_url)
    app.watch = __import__("mediafans.watch", fromlist=["WatchStore"]).WatchStore(
        tmp_path / "w.json")
    app.api_watch_save({"path": "/MediaFans/流浪地球/a.mkv", "position": 600,
                        "duration": 7500, "tmdb_id": 300, "title": "流浪地球",
                        "year": "2019", "media_type": "movie"})
    got = app.api_watch_recent(5)["items"][0]
    assert got["media_type"] == "movie"
    assert got["season"] is None and got["episode"] is None
    app.stop()


def test_page_can_switch_between_tv_and_movie_lists():
    from mediafans.web import PAGE_HTML

    assert "setDiscoverMedia" in PAGE_HTML and 'data-media="movie"' in PAGE_HTML
    assert "正在上映" in PAGE_HTML and "即将上映" in PAGE_HTML
    # 电影卡片也进详情页，不再只有「自动找片」一条路
    assert "card.onclick = () => openSeries(it);" in PAGE_HTML
    # 详情页对电影换措辞。文案只有 scanLabel() 一个出处——
    # 以前 renderEpisodes 和出错分支各写各的，电影页一报错按钮就改了名
    assert "'找资源' : '找缺失的集'" in _js_fn("scanLabel")


def test_inline_javascript_parses(tmp_path):
    """整个界面是一段内联 <script>：语法错一个字符就整页白屏。

    而所有 Python 测试都照样绿——它们只检查 HTML 里有没有某段文本，
    根本不会去解析 JS。所以这里借 node 真解析一遍。
    """
    import re
    import shutil
    import subprocess

    from mediafans.web import PAGE_HTML

    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过 JS 语法检查")
    js = re.search(r"<script>(.*)</script>", PAGE_HTML, re.S).group(1)
    f = tmp_path / "page.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:600]


# ---------------------------------------------------------------- 多副本
def test_watch_progress_follows_the_copy_you_actually_played(tmp_path, upstream_url):
    """一集存了多份时，进度记在具体文件上——只看主副本会显示成「没看过」。"""
    from mediafans.agent import EpisodeRow, LocalFile, SeriesView
    from mediafans.watch import WatchStore

    app = make_webapp(upstream_url)
    app.watch = WatchStore(tmp_path / "w.json")
    row = EpisodeRow(episode=1, title="第1集")
    row.copies = [LocalFile(path="/d/E01.2160p.mkv", name="a", size=9, height=2160),
                  LocalFile(path="/d/E01.1080p.mp4", name="b", size=3, height=1080)]
    view = SeriesView(tmdb_id=1, title="剧", season=1, seasons=[], rows=[row])

    # 用户看的是第二份（主副本是 4K 那个，浏览器解不了才换过来的）
    app.api_watch_save({"path": "/d/E01.1080p.mp4", "position": 900, "duration": 3000})
    got = app._attach_watch(view.as_dict())
    assert got["episodes"][0]["watched"]["path"] == "/d/E01.1080p.mp4"
    assert got["episodes"][0]["watched"]["percent"] == 30
    app.stop()


def test_series_json_carries_every_copy(upstream_url):
    from mediafans.agent import EpisodeRow, LocalFile, SeriesView

    row = EpisodeRow(episode=1)
    row.copies = [LocalFile(path="/d/a.mkv", name="a.mkv", size=9, height=2160),
                  LocalFile(path="/d/b.mp4", name="b.mp4", size=3, height=1080)]
    d = SeriesView(tmdb_id=1, title="剧", season=1, seasons=[], rows=[row]).as_dict()
    ep = d["episodes"][0]
    assert ep["status"] == "saved"
    assert ep["local"]["name"] == "a.mkv"                 # 主副本还是最好的那个
    assert [c["name"] for c in ep["copies"]] == ["a.mkv", "b.mp4"]


def test_fetch_all_needs_sources_first(upstream_url):
    from mediafans.agent import EpisodeRow, SeriesView
    from mediafans.errors import MediaFansError

    app = make_webapp(upstream_url)
    view = SeriesView(tmdb_id=7, title="剧", season=1, seasons=[],
                      rows=[EpisodeRow(episode=1)])
    app.series_cache.put(("quark", 7, 1), view)
    with pytest.raises(MediaFansError, match="还没有找到来源"):
        app.api_episode_fetch_all({"tmdb_id": 7, "season": 1, "episode": 1})
    app.stop()


def _js_fn(name: str) -> str:
    """从页面 JS 里抠出一个函数体（到下一个顶格 `function` 为止）。

    直接在整页 HTML 上断言某行存在太弱：`const at = video.currentTime` 在
    fallbackToProxy 里本来就有，写在 switchSource 里没有都照样绿。
    """
    import re

    from mediafans.web import PAGE_HTML

    m = re.search(r"^function " + name + r"\(.*?(?=^function )", PAGE_HTML,
                  re.S | re.M)
    assert m, f"页面里没有 {name}()"
    return m.group(0)


def test_page_can_switch_sources_while_playing():
    from mediafans.web import PAGE_HTML

    assert 'id="sources"' in PAGE_HTML and "function switchSource" in PAGE_HTML
    # 换来源要接着当前进度播，而不是从头开始
    body = _js_fn("switchSource")
    assert "video.currentTime" in body and "startAt: at" in body
    assert "keepCtx: true" in body          # 别把剧集上下文丢了，进度会记到散片上
    # 解不了这个文件时自动换一份，别让用户先看懂 HEVC/DTS-HD 再自己点
    assert "const alt = nextBestCopy();" in PAGE_HTML
    assert "已自动换到" in PAGE_HTML


def test_every_series_call_from_the_page_sends_media():
    """电影页点「找资源」曾经直接 400：scanSources 少发了 media。

    漏掉的原因是我的线上验证脚本直接带 media 调的接口，绕过了前端，
    所以接口是好的、按钮是坏的。这里把四个调用点一起钉住。
    """
    for fn, ep in (("scanSources", "/api/series/scan"),
                   ("fetchSeason", "/api/season/fetch"),
                   ("fetchAllSources", "/api/episode/fetch/all")):
        body = _js_fn(fn)
        assert ep in body, f"{fn} 不再调用 {ep}？"
        assert "media" in body, f"{fn} 没有把 media 发出去"


def test_poster_grid_pins_row_height_to_content():
    """海报墙的行高必须写死成 max-content，别退回 auto。

    海报是 `width:100% + aspect-ratio:2/3`，这种图**在算网格行高时贡献 0**
    （百分比宽度对着不定容器解不出来）。auto 行于是只按标题那 41px 算，
    再被 align-content:stretch 平摊，海报被 card 的 overflow:hidden 裁成一条。

    手机上尤其惨：实测 375px 宽、13 行摊 629px → 每行 39px，海报只剩一条色带，
    标题也一起被裁掉。改成 max-content 后行高 210px（168 图 + 41 标题）。
    """
    from mediafans.web import PAGE_HTML

    grid = PAGE_HTML.split("#shows {")[1].split("}")[0]
    assert "grid-auto-rows:max-content" in grid.replace(" ", "")
    assert "aspect-ratio:2/3" in PAGE_HTML.replace(" ", "")
