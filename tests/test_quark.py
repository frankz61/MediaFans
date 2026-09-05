"""夸克驱动全流程测试：MockTransport 模拟夸克接口，不访问真实网络."""

import json

import httpx
import pytest

from mediafans.drive.quark import QuarkDrive
from mediafans.errors import ConfigError, DriveError


def make_drive(handler):
    return QuarkDrive({"cookie": "__puus=test"}, transport=httpx.MockTransport(handler))


def test_cookie_required():
    with pytest.raises(ConfigError):
        QuarkDrive({}, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))


def test_account_name():
    def handler(request):
        assert "pan.quark.cn/account/info" in str(request.url)
        return httpx.Response(200, json={"status": 200, "data": {"nickname": "tester"}})

    assert make_drive(handler).account_name() == "tester"


def test_account_name_success_bool_semantics():
    """扫码登录后的 account/info 用 success 布尔语义，且可能带非 200 的 status."""
    def handler(request):
        return httpx.Response(200, json={
            "success": True, "status": 401, "code": 401,
            "data": {"nickname": "夸父0387"},
        })

    assert make_drive(handler).account_name() == "夸父0387"


def test_success_false_raises():
    def handler(request):
        return httpx.Response(200, json={"success": False, "message": "not logged in"})

    with pytest.raises(DriveError, match="not logged in"):
        make_drive(handler).account_name()


def test_share_and_save_full_flow():
    task_polls = {"n": 0}
    seen = {}

    def handler(request):
        path = request.url.path
        if path.endswith("/share/sharepage/token"):
            body = json.loads(request.content)
            assert body == {"pwd_id": "abc123", "passcode": "8xk2"}
            return httpx.Response(200, json={"status": 200, "code": 0, "data": {"stoken": "ST"}})
        if path.endswith("/share/sharepage/detail"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "list": [
                        {"fid": "F1", "file_name": "沙丘2.4K.mkv", "dir": False, "size": 1024 ** 4,
                         "share_fid_token": "TK1"},
                        {"fid": "F2", "file_name": "合集", "dir": True, "share_fid_token": "TK2"},
                    ],
                    "metadata": {"_total": 2},
                },
            })
        if path.endswith("/file/info/path_list"):
            body = json.loads(request.content)
            assert body["file_path"] == ["/MediaFans"]
            return httpx.Response(200, json={"code": 0, "data": [{"fid": "DIR1", "file_path": "/MediaFans"}]})
        if path.endswith("/share/sharepage/save"):
            body = json.loads(request.content)
            seen["save"] = body
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "TASK9"}})
        if path.endswith("/task"):
            task_polls["n"] += 1
            assert "task_id=TASK9" in str(request.url)
            if task_polls["n"] == 1:
                return httpx.Response(200, json={"code": 0, "data": {"status": 1}})
            return httpx.Response(200, json={
                "code": 0,
                "data": {"status": 2, "save_as": {"save_as_top_fids": ["NEW1"]}},
            })
        raise AssertionError(f"unexpected request: {path}")

    drive = make_drive(handler)
    ctx = drive.open_share("https://pan.quark.cn/s/abc123?pwd=8xk2")
    assert ctx.pwd_id == "abc123" and ctx.stoken == "ST"

    files = drive.list_share_files(ctx)
    assert [f.name for f in files] == ["沙丘2.4K.mkv", "合集"]
    video = [f for f in files if not f.is_dir][0]
    assert video.share_fid_token == "TK1"

    fids = drive.save_share_files(ctx, [video], "/MediaFans")
    assert fids == ["NEW1"]
    # 转存请求体的关键字段
    assert seen["save"]["fid_list"] == ["F1"]
    assert seen["save"]["fid_token_list"] == ["TK1"]
    assert seen["save"]["to_pdir_fid"] == "DIR1"
    assert seen["save"]["pwd_id"] == "abc123"
    assert seen["save"]["stoken"] == "ST"
    assert seen["save"]["scene"] == "link"
    assert task_polls["n"] == 2


def test_share_needs_passcode_error():
    def handler(request):
        return httpx.Response(200, json={"status": 500, "code": 41008, "message": "需要提取码"})

    with pytest.raises(DriveError, match="需要提取码"):
        make_drive(handler).open_share("https://pan.quark.cn/s/abc123")


def test_not_a_quark_link():
    with pytest.raises(DriveError, match="解析夸克分享 ID"):
        make_drive(lambda r: httpx.Response(200, json={})).open_share("https://example.com/s/x")


def test_list_files_paged():
    calls = []

    def handler(request):
        calls.append(request.url.params.get("_page"))
        pdir = request.url.params.get("pdir_fid")
        page = int(request.url.params.get("_page"))
        if pdir == "0":
            if page == 1:
                return httpx.Response(200, json={"code": 0, "data": {
                    "list": [{"fid": f"f{i}", "file_name": f"a{i}.mkv", "dir": False} for i in range(100)],
                    "metadata": {"_total": 101}}})
            return httpx.Response(200, json={"code": 0, "data": {
                "list": [{"fid": "f100", "file_name": "a100.mkv", "dir": False}],
                "metadata": {"_total": 101}}})
        raise AssertionError(f"unexpected pdir {pdir}")

    files = make_drive(handler).list_files("0")
    assert len(files) == 101
    assert calls == ["1", "2"]


def test_resolve_path_missing_returns_none():
    def handler(request):
        return httpx.Response(200, json={"code": 0, "data": []})

    assert make_drive(handler).resolve_path("/not/exist") is None


def test_resolve_path_file_falls_back_to_parent_listing():
    """path_list 只认目录；文件应通过 resolve_entry 的父目录精确匹配解析."""
    def handler(request):
        path = request.url.path
        if path.endswith("/file/info/path_list"):
            body = json.loads(request.content)
            if body["file_path"][0].endswith(".mkv"):
                return httpx.Response(200, json={"code": 0, "data": []})  # 文件查不到
            return httpx.Response(200, json={
                "code": 0, "data": [{"fid": "DIRFID", "file_path": "/MediaFans/合集"}]})
        if path.endswith("/file/sort"):
            return httpx.Response(200, json={"code": 0, "data": {
                "list": [{"fid": "F_MKV", "file_name": "奥本海默.mkv", "dir": False}],
                "metadata": {"_total": 1}}})
        raise AssertionError(f"unexpected {path}")

    drive = make_drive(handler)
    assert drive.resolve_path("/MediaFans/合集/奥本海默.mkv") is None  # path_list 查不到文件
    assert drive.resolve_entry("/MediaFans/合集/奥本海默.mkv") == "F_MKV"


def test_ensure_dir_creates_when_missing():
    def handler(request):
        path = request.url.path
        if path.endswith("/file/info/path_list"):
            # 首次查询不存在（返回空）
            return httpx.Response(200, json={"code": 0, "data": []})
        if path.endswith("/file") and request.method == "POST":
            body = json.loads(request.content)
            assert body["dir_path"] == "/MediaFans/新剧"
            return httpx.Response(200, json={"code": 0, "data": {"fid": "NEWDIR"}})
        raise AssertionError(f"unexpected {path}")

    assert make_drive(handler).ensure_dir("/MediaFans/新剧") == "NEWDIR"


def _video_entry(resolution, height, width, url, **over):
    """/file/v2/play 的 video_list 条目（字段名与夸克真实返回一致）."""
    entry = {
        "resolution": resolution,
        "trans_status": "success",
        "accessable": True,
        "video_info": {"url": url, "height": height, "width": width,
                       "size": height * 1000, "bitrate": height * 2.0, "format": "mp4"},
    }
    entry.update(over)
    return entry


def _play_handler(video_list, download_extra=None, dl_cookie="__puus=dl",
                  play_cookie="__puus=play"):
    """转码档来自 /file/v2/play，原画来自 /file/download —— 两个接口各下发一次 cookie."""
    def handler(request):
        if request.url.path.endswith("/file/download"):
            item = {
                "file_name": "x.mkv",
                "download_url": "https://dl.example.com/raw",
                "obj_category": "video",
                "video_width": 3840, "video_height": 2160, "size": 88 * 1024 ** 3,
            }
            item.update(download_extra or {})
            return httpx.Response(
                200, headers=httpx.Headers([("set-cookie", f"{dl_cookie}; Path=/")]),
                json={"code": 0, "data": [item]})
        if request.url.path.endswith("/file/v2/play"):
            assert json.loads(request.content)["fid"] == "F1"
            return httpx.Response(
                200, headers=httpx.Headers([("set-cookie", f"{play_cookie}; Path=/")]),
                json={"code": 0, "data": {"default_resolution": "super",
                                          "video_list": video_list}})
        raise AssertionError(request.url.path)

    return handler


def test_get_play_target_lists_transcodes_and_origin():
    handler = _play_handler([
        _video_entry("super", 810, 1440, "https://dl.example.com/super"),
        _video_entry("4k", 2160, 3840, "https://dl.example.com/4k"),
        _video_entry("low", 270, 480, "https://dl.example.com/low"),
    ])
    pt = make_drive(handler).get_play_target("F1")
    # 转码档按分辨率从高到低，原画垫底
    assert [(v.key, v.label) for v in pt.variants] == [
        ("4k", "4K"), ("super", "超清"), ("low", "流畅"), ("origin", "原画")]
    assert pt.variants[0].display() == "4K 2160P"
    # 默认播网盘建议的那一档，而不是原画
    assert pt.default_key == "super"
    assert pt.url == "https://dl.example.com/super"
    assert pt.download_url == "https://dl.example.com/raw"
    # 两个接口的 cookie 合并（同名后者覆盖）
    assert pt.cookie == "__puus=play"
    assert pt.headers()["Cookie"] == "__puus=play"
    assert pt.ua  # 带默认 UA


def test_get_play_target_skips_unavailable_transcodes():
    """没会员权限、或还在转码的档不该出现在清晰度列表里."""
    handler = _play_handler([
        _video_entry("4k", 2160, 3840, "https://dl.example.com/4k", accessable=False),
        _video_entry("super", 810, 1440, "https://dl.example.com/s", trans_status="running"),
        _video_entry("high", 540, 960, ""),
        _video_entry("low", 270, 480, "https://dl.example.com/low"),
    ])
    pt = make_drive(handler).get_play_target("F1")
    assert [v.key for v in pt.variants] == ["low", "origin"]
    # 建议档 super 已被过滤，退到最高可用档
    assert pt.default_key == "low"


def test_get_play_target_no_transcode_only_original():
    """REMUX 原盘常见：转码列表为空，只剩原画."""
    pt = make_drive(_play_handler([])).get_play_target("F1")
    assert [v.key for v in pt.variants] == ["origin"]
    assert pt.url == "https://dl.example.com/raw"
    assert pt.default_key == "origin"
    assert pt.variants[0].origin is True


def test_get_play_target_transcode_api_failure_is_not_fatal():
    """转码接口报错（风控/接口变动）只该丢清晰度，不该让播放整个失败."""
    def handler(request):
        if request.url.path.endswith("/file/download"):
            return httpx.Response(200, json={"code": 0, "data": [{
                "file_name": "x.mkv", "download_url": "https://dl.example.com/raw",
                "obj_category": "video"}]})
        return httpx.Response(200, json={"status": 400, "code": 1, "message": "风控"})

    pt = make_drive(handler).get_play_target("F1")
    assert pt.url == "https://dl.example.com/raw"
    assert [v.key for v in pt.variants] == ["origin"]


def test_get_play_target_skips_transcode_lookup_for_non_video():
    """非视频文件不该白白多打一次转码接口."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": [{
            "file_name": "x.zip", "download_url": "https://dl.example.com/zip",
            "obj_category": "doc"}]})

    pt = make_drive(handler).get_play_target("F1")
    assert [v.key for v in pt.variants] == ["origin"]
    assert not any(p.endswith("/file/v2/play") for p in calls)


def test_get_play_target_without_any_link_raises():
    def handler(request):
        return httpx.Response(200, json={"code": 0, "data": [{"file_name": "x.mkv"}]})

    with pytest.raises(DriveError, match="未拿到直链"):
        make_drive(handler).get_play_target("F1")


def test_api_error_message_surfaced():
    def handler(request):
        return httpx.Response(200, json={"status": 400, "code": 1, "message": "登录失效"})

    with pytest.raises(DriveError, match="登录失效"):
        make_drive(handler).list_files("0")
