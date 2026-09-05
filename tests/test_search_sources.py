"""搜索源：多实例故障转移 + PanSou 参数透传。

公共 PanSou 实例很不稳（实测同一关键词三次分别返回 13 / 74 / 0 条），
所以「源更丰富」这件事里，可靠性和服务端过滤比堆站点更要紧。
"""

import httpx
import pytest

from mediafans.errors import SearchError
from mediafans.search import aggregate_search, build_providers
from mediafans.search.pansou import PanSouProvider


def _merged(quark=(), aliyun=()):
    return {"merged_by_type": {
        "quark": [{"url": u, "password": "", "note": "", "datetime": "", "source": "tg"}
                  for u in quark],
        "aliyun": [{"url": u} for u in aliyun],
    }}


def provider(cfg, handler):
    return PanSouProvider(cfg, timeout=5, transport=httpx.MockTransport(handler))


def test_falls_over_when_instance_errors():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "dead.example":
            return httpx.Response(502)
        return httpx.Response(200, json=_merged(quark=["https://pan.quark.cn/s/ok"]))

    p = provider({"base_url": "https://dead.example",
                  "fallback_urls": ["https://alive.example"]}, handler)
    links = p.search("沙丘")
    assert [l.url for l in links] == ["https://pan.quark.cn/s/ok"]
    assert seen == ["dead.example", "alive.example"]


def test_falls_over_when_instance_returns_nothing():
    """通了但 0 条也换下一个 —— 公共实例限流时就是这种表现，不是真没资源."""
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "empty.example":
            return httpx.Response(200, json=_merged())
        return httpx.Response(200, json=_merged(quark=["https://pan.quark.cn/s/found"]))

    p = provider({"base_url": "https://empty.example",
                  "fallback_urls": ["https://good.example"]}, handler)
    assert [l.url for l in p.search("沙丘")] == ["https://pan.quark.cn/s/found"]
    assert seen == ["empty.example", "good.example"]


def test_all_empty_is_not_an_error():
    """全都没结果是「没搜到」，不该抛异常搞成整个搜索失败."""
    def handler(request):
        return httpx.Response(200, json=_merged())

    p = provider({"base_url": "https://a.example", "fallback_urls": ["https://b.example"]},
                 handler)
    assert p.search("不存在的片子") == []


def test_all_instances_down_raises_with_detail():
    def handler(request):
        return httpx.Response(503)

    p = provider({"base_url": "https://a.example", "fallback_urls": ["https://b.example"]},
                 handler)
    with pytest.raises(SearchError, match="a.example"):
        p.search("沙丘")


def test_netdisk_filter_becomes_server_side_cloud_types():
    """指定网盘时交给服务端过滤：同一次请求能用的结果多得多."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_merged(quark=["https://pan.quark.cn/s/a"]))

    provider({"base_url": "https://a.example"}, handler).search("沙丘", netdisk="quark")
    assert seen["cloud_types"] == "quark"


def test_config_params_are_passed_through():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_merged(quark=["https://pan.quark.cn/s/a"]))

    provider({
        "base_url": "https://a.example",
        "src": "tg",
        "channels": ["tgsearchers3", "yunpanx"],   # 列表要拼成逗号分隔
        "plugins": "pansearch,hunhepan",
        "refresh": True,                            # 布尔要变成 true
        "conc": 30,
    }, handler).search("沙丘")
    assert seen["src"] == "tg"
    assert seen["channels"] == "tgsearchers3,yunpanx"
    assert seen["plugins"] == "pansearch,hunhepan"
    assert seen["refresh"] == "true"
    assert seen["conc"] == "30"


def test_explicit_cloud_types_wins_over_filter():
    """配置里写死了 cloud_types 就以配置为准，不被调用方覆盖."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_merged(quark=["https://pan.quark.cn/s/a"]))

    provider({"base_url": "https://a.example", "cloud_types": "quark,aliyun"},
             handler).search("沙丘", netdisk="quark")
    assert seen["cloud_types"] == "quark,aliyun"


def test_multiple_sources_aggregate_and_dedup():
    """配多个实例时结果合并去重，单源挂掉不影响整体."""
    def handler(request):
        if request.url.host == "a.example":
            return httpx.Response(200, json=_merged(
                quark=["https://pan.quark.cn/s/dup", "https://pan.quark.cn/s/only-a"]))
        if request.url.host == "b.example":
            return httpx.Response(200, json=_merged(
                quark=["https://pan.quark.cn/s/dup?x=1"]))   # 同一分享带参数
        return httpx.Response(500)

    providers = build_providers({
        "sources": [{"type": "pansou", "base_url": "https://a.example"},
                    {"type": "pansou", "base_url": "https://b.example"},
                    {"type": "pansou", "base_url": "https://boom.example"}],
        "timeout": 5,
    }, transport=httpx.MockTransport(handler))
    links, errors = aggregate_search(providers, "沙丘")
    # 去重按「去掉 query 后的地址」算：同一个分享只留一条，
    # 但各源是并发跑的，留下的是哪个变体不保证，所以断言归一化后的结果
    keys = sorted(l.url.split("?")[0] for l in links)
    assert keys == ["https://pan.quark.cn/s/dup", "https://pan.quark.cn/s/only-a"]
    assert len(errors) == 1 and "boom.example" in str(errors[0][1])
