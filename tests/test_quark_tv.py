"""夸克 TV 版驱动测试（MockTransport 假接口，不访问真实网络）.

TV 版和 PC 版是两套接口，最容易踩的坑是字段名长得像但结构不同：
PC 版转码档在 data.video_list，每档再套一层 video_info；
TV 版在 data.video_info，而且是扁平的。解析错了会静默丢掉所有清晰度。
"""

import json

import httpx
import pytest

from mediafans.drive.quark_tv import QuarkTVClient
from mediafans.errors import ConfigError, DriveError


def write_token(tmp_path, **over):
    data = {"access_token": "AT", "refresh_token": "RT", "device_id": "DEV"}
    data.update(over)
    path = tmp_path / "quark_tv.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_client(tmp_path, handler):
    return QuarkTVClient(write_token(tmp_path), transport=httpx.MockTransport(handler))


def _video(resolution, w, h, url, **over):
    """TV 版的扁平结构 —— 注意没有嵌套的 video_info."""
    e = {"resolution": resolution, "width": w, "height": h, "url": url,
         "size": h * 1000, "bitrate": h * 2.0, "format": "mp4",
         "accessable": True, "trans_status": "success"}
    e.update(over)
    return e


def test_missing_token_file_is_actionable(tmp_path):
    with pytest.raises(ConfigError, match="login --tv"):
        QuarkTVClient(tmp_path / "nope.json")


def test_token_file_without_access_token(tmp_path):
    path = tmp_path / "quark_tv.json"
    path.write_text('{"refresh_token": "RT"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="access_token"):
        QuarkTVClient(path)


def test_play_target_parses_flat_video_info(tmp_path):
    """转码档按分辨率从高到低，原画垫底；TV 版不下发 cookie."""
    def handler(request):
        if request.url.params.get("method") == "download":
            return httpx.Response(200, json={"status": 0, "data": {
                "fid": "F1", "file_name": "x.mkv", "size": 88,
                "download_url": "https://dl-c-zb-u.example/raw"}})
        return httpx.Response(200, json={"status": 0, "data": {
            "default_resolution": "super",
            "video_info": [
                _video("super", 1440, 810, "https://v.example/super"),
                _video("4k", 3840, 2160, "https://v.example/4k"),
                _video("low", 480, 270, "https://v.example/low"),
            ]}})

    pt = make_client(tmp_path, handler).get_play_target("F1", name="x.mkv")
    assert [(v.key, v.label) for v in pt.variants] == [
        ("4k", "4K"), ("super", "超清"), ("low", "流畅"), ("origin", "原画")]
    assert pt.default_key == "super"
    assert pt.url == "https://v.example/super"
    # 关键区别：TV 版直链不需要凭据，所以 PlayTarget 不带 cookie/ua
    assert pt.cookie == "" and pt.ua == ""
    assert pt.headers() == {}


def test_play_target_skips_unavailable(tmp_path):
    """accessable 可能是布尔也可能是 0/1，两种都要认."""
    def handler(request):
        if request.url.params.get("method") == "download":
            return httpx.Response(200, json={"status": 0, "data": {
                "download_url": "https://dl.example/raw"}})
        return httpx.Response(200, json={"status": 0, "data": {
            "default_resolution": "4k",
            "video_info": [
                _video("4k", 3840, 2160, "https://v/4k", accessable=False),
                _video("super", 1440, 810, "https://v/s", accessable=0),
                _video("high", 960, 540, "https://v/h", trans_status="running"),
                _video("low", 480, 270, ""),
                _video("normal", 640, 360, "https://v/n"),
            ]}})

    pt = make_client(tmp_path, handler).get_play_target("F1")
    assert [v.key for v in pt.variants] == ["normal", "origin"]
    assert pt.default_key == "normal"  # 建议档不可用时退到最高可用档


def test_streaming_failure_still_gives_origin(tmp_path):
    """转码接口挂了不该让原画也拿不到."""
    def handler(request):
        if request.url.params.get("method") == "download":
            return httpx.Response(200, json={"status": 0, "data": {
                "download_url": "https://dl.example/raw"}})
        return httpx.Response(200, json={"status": -1, "errno": 31001, "error_info": "boom"})

    pt = make_client(tmp_path, handler).get_play_target("F1")
    assert [v.key for v in pt.variants] == ["origin"]
    assert pt.url == "https://dl.example/raw"


def test_device_limit_error_explains_the_fix(tmp_path):
    """errno 32009 光看数字没法自救，必须告诉用户去哪解绑."""
    def handler(request):
        return httpx.Response(200, json={
            "status": -1, "errno": 32009, "error_info": "设备数超限"})

    with pytest.raises(DriveError, match="解绑"):
        make_client(tmp_path, handler).account_name()


def test_signed_request_carries_token_and_signature(tmp_path):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        seen["_headers"] = dict(request.headers)
        return httpx.Response(200, json={"status": 0, "data": {"nickname": "tv-user"}})

    assert make_client(tmp_path, handler).account_name() == "tv-user"
    assert seen["access_token"] == "AT"
    assert seen["device_id"] == "DEV"
    assert seen["platform"] == "tv"
    assert len(seen["req_id"]) == 32
    h = seen["_headers"]
    assert len(h["x-pan-token"]) == 64          # SHA256 十六进制
    assert h["x-pan-tm"].isdigit() and len(h["x-pan-tm"]) == 13
    assert h["x-pan-client-id"]
