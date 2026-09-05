"""搜索聚合源 + token 中转站 + 播放器命令构建的离线测试."""

import httpx

from mediafans.models import PlayTarget, ShareLink
from mediafans.player import build_command
from mediafans.search import PanSouProvider, YisoProvider, aggregate_search
from mediafans.tokens import TokenRelay


class FakeProvider:
    def __init__(self, name, results=None, error=None):
        self.name = name
        self._results = results or []
        self._error = error

    def search(self, kw, netdisk=None):
        if self._error:
            raise self._error
        return [r for r in self._results if not netdisk or r.netdisk == netdisk]


def test_aggregate_dedup_and_error_tolerance():
    dup = ShareLink(url="https://pan.quark.cn/s/same?from=tg", netdisk="quark", source="a")
    links, errors = aggregate_search(
        [
            FakeProvider("a", results=[dup, ShareLink(url="https://pan.quark.cn/s/same", netdisk="quark")]),
            FakeProvider("b", results=[ShareLink(url="https://www.alipan.com/s/x1", netdisk="aliyun")]),
            FakeProvider("c", error=RuntimeError("boom")),
        ],
        "kw",
    )
    assert errors and errors[0][0] == "c"
    quark_urls = [l.url for l in links if l.netdisk == "quark"]
    assert len(quark_urls) == 1  # 去重（忽略 query）
    assert links[0].netdisk == "quark"  # 可转存网盘（夸克/百度）排前面


def test_aggregate_transferable_first():
    links, _ = aggregate_search(
        [FakeProvider("a", results=[
            ShareLink(url="https://www.alipan.com/s/x", netdisk="aliyun"),
            ShareLink(url="https://pan.baidu.com/s/1bcd?pwd=1111", netdisk="baidu"),
            ShareLink(url="https://pan.quark.cn/s/q", netdisk="quark"),
        ])],
        "kw",
    )
    assert [l.netdisk for l in links][:2] == ["quark", "baidu"]  # 可转存的在前
    assert links[2].netdisk == "aliyun"  # 其他网盘垫底


def test_pansou_provider_parses_merged():
    def handler(request):
        assert request.url.params["kw"] == "沙丘"
        assert request.url.params["res"] == "merge"
        # 演示站把结果包在 data 里（顶层形态也兼容）
        return httpx.Response(200, json={"code": 0, "message": "success", "data": {
            "total": 2,
            "merged_by_type": {
                "quark": [{"url": "https://pan.quark.cn/s/q1", "password": "88ab",
                           "note": "沙丘2 4K", "datetime": "2026-08-01", "source": "tg:x"}],
                "aliyun": [{"url": "https://www.alipan.com/s/a1", "password": "", "note": "n"}],
            },
        }})

    p = PanSouProvider({"base_url": "https://pansou.test"}, transport=httpx.MockTransport(handler))
    links = p.search("沙丘")
    assert [(l.netdisk, l.passcode) for l in links] == [("quark", "88ab"), ("aliyun", "")]
    assert len(p.search("沙丘", netdisk="quark")) == 1


def test_pansou_provider_top_level_shape():
    def handler(request):
        return httpx.Response(200, json={
            "total": 1,
            "merged_by_type": {"quark": [{"url": "https://pan.quark.cn/s/q2"}]},
        })

    p = PanSouProvider({"base_url": "https://pansou.test"}, transport=httpx.MockTransport(handler))
    assert p.search("kw")[0].url == "https://pan.quark.cn/s/q2"


def test_yiso_provider_parses_filelist():
    def handler(request):
        assert request.url.params["name"] == "沙丘"
        return httpx.Response(200, json={
            "data": [{"fileList": [
                {"fileName": "沙丘.mkv", "url": "https://pan.quark.cn/s/y1"},
                {"fileName": "other", "url": "https://pan.baidu.com/s/y2"},
            ]}],
        })

    p = YisoProvider({"base_url": "https://yiso.test"}, transport=httpx.MockTransport(handler))
    links = p.search("沙丘")
    assert [(l.netdisk, l.title) for l in links] == [("quark", "沙丘.mkv"), ("baidu", "other")]


def test_token_relay_json_path():
    def handler(request):
        assert request.headers.get("Authorization") == "Bearer t"
        return httpx.Response(200, json={"code": 0, "data": {"cookie": "__puus=relay"}})

    relay = TokenRelay(
        {"url": "https://relay.test/cookie", "headers": {"Authorization": "Bearer t"},
         "json_path": "data.cookie"},
        transport=httpx.MockTransport(handler),
    )
    assert relay.fetch_cookie() == "__puus=relay"


def test_token_relay_plain_text():
    relay = TokenRelay(
        {"url": "https://relay.test/cookie"},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=" __puus=plain ")),
    )
    assert relay.fetch_cookie() == "__puus=plain"


def _target():
    return PlayTarget(file_name="movie.mkv", url="https://dl/x.mp4",
                      cookie="a=1", ua="UA-X")


def test_build_command_mpv_headers():
    cmd = build_command("mpv", "mpv.exe", _target(), title="标题")
    assert "--http-header-fields=User-Agent: UA-X" in cmd
    assert "--http-header-fields=Cookie: a=1" in cmd
    assert "--force-media-title=标题" in cmd
    assert cmd[-1] == "https://dl/x.mp4"


def test_build_command_custom_template():
    cmd = build_command(
        "custom",
        ["C:/mpv/mpv.exe", "--http-header-fields=User-Agent: {ua}; Cookie: {cookie}", "{url}"],
        _target(),
    )
    assert cmd == ["C:/mpv/mpv.exe", "--http-header-fields=User-Agent: UA-X; Cookie: a=1", "https://dl/x.mp4"]


def test_build_command_custom_string():
    cmd = build_command("custom", 'mpv --force-media-title="{title}" "{url}"', _target(), title="T")
    assert cmd == ["mpv", "--force-media-title=T", "https://dl/x.mp4"]
