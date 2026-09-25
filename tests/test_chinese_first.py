"""追剧榜和搜索的华语优先。

TMDB 的 /tv/airing_today 是全球榜，被各国日播肥皂剧刷屏——实测「今日播出」
前 8 条里只有 1 条华语，其余是西班牙、德国、荷兰、南非的日播节目。
榜单端点不支持按语言过滤，只有 /discover/tv 支持。
"""

import json

import httpx
import pytest

from mediafans.metadata.tmdb import TmdbClient
from mediafans.models import MediaItem


def _item(title, lang, original="", mtype="tv"):
    return MediaItem(title=title, original_title=original or title,
                     original_language=lang, media_type=mtype)


# ---------------------------------------------------------------- 语言判定
def test_cantonese_counts_as_chinese():
    """TMDB 把华语拆成 zh（国语）和 cn（粤语），只认 zh 会漏掉整个港片库."""
    assert TmdbClient.is_chinese(_item("无间道", "cn"))
    assert TmdbClient.is_chinese(_item("三体", "zh"))
    assert not TmdbClient.is_chinese(_item("Silo", "en"))
    assert not TmdbClient.is_chinese(_item("弱小英雄", "ko"))


# ---------------------------------------------------------------- 搜索排序
def test_chinese_original_beats_foreign_remake():
    """搜「无间道」，斯科塞斯的《无间道风云》原本排在港版前面."""
    got = TmdbClient.rank_chinese_first("无间道", [
        _item("无间道风云", "en"), _item("无间道", "cn"), _item("无间道2", "cn")])
    # 完全同名的先出；剩下的里华语仍然优先，所以外语翻拍落到最后
    assert [i.title for i in got] == ["无间道", "无间道2", "无间道风云"]


def test_exact_title_wins_over_language():
    """张艺谋《长城》在 TMDB 里 original_language 是 en。

    硬按语言排会让《巨兵长城传》顶掉正主，所以完全同名的先保住。
    """
    got = TmdbClient.rank_chinese_first("长城", [
        _item("长城", "en"), _item("巨兵长城传", "zh"), _item("野长城", "zh")])
    assert [i.title for i in got] == ["长城", "巨兵长城传", "野长城"]


def test_all_foreign_query_is_untouched():
    """搜「沙丘」全是英语片，顺序不该被动过."""
    src = [_item("沙丘", "en"), _item("沙丘", "en"), _item("沙丘2", "en"),
           _item("沙丘：预言", "en")]
    assert TmdbClient.rank_chinese_first("沙丘", src) == src


def test_partial_query_still_prefers_chinese():
    """搜「木兰」没有完全同名的，这时才轮到语言说话."""
    got = TmdbClient.rank_chinese_first("木兰", [
        _item("甜木兰", "en"), _item("花木兰", "en"), _item("花木兰", "zh")])
    assert got[0].original_language == "zh"


def test_ranking_is_stable_within_a_group():
    """同一档里保持 TMDB 的原始相关性顺序，不要自己发明排序."""
    src = [_item(f"剧{i}", "zh") for i in range(6)]
    assert TmdbClient.rank_chinese_first("查不到的词", src) == src


def test_original_title_also_counts_as_exact():
    got = TmdbClient.rank_chinese_first("silo", [
        _item("末日堡垒", "zh"), _item("末日地堡", "en", original="Silo")])
    assert got[0].original_title == "Silo"


# ---------------------------------------------------------------- discover 请求
class _Recorder(httpx.BaseTransport):
    def __init__(self, results):
        self.results = results
        self.calls = []

    def handle_request(self, request):
        self.calls.append(request.url)
        body = json.dumps({"results": self.results}).encode()
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/json"})


def test_chinese_tv_filters_by_language_and_date():
    t = _Recorder([{"id": 1, "name": "凡人修仙传", "original_language": "zh"}])
    client = TmdbClient("k", transport=t)
    items = client.chinese_tv("airing")
    url = str(t.calls[0])
    assert "/discover/tv" in url          # 榜单端点不支持语言过滤，必须走 discover
    assert "zh%7Ccn" in url or "zh|cn" in url
    assert "air_date.gte" in url and "air_date.lte" in url
    assert items[0].title == "凡人修仙传" and items[0].media_type == "tv"


def test_onair_spans_a_week():
    t = _Recorder([])
    TmdbClient("k", transport=t).chinese_tv("onair")
    q = dict(httpx.URL(str(t.calls[0])).params)
    assert q["air_date.gte"] < q["air_date.lte"]


def test_popular_has_no_date_window():
    """热门是「一直热」，加日期窗口会把它变成另一个在播榜."""
    t = _Recorder([])
    TmdbClient("k", transport=t).chinese_tv("popular")
    q = dict(httpx.URL(str(t.calls[0])).params)
    assert "air_date.gte" not in q and q["sort_by"] == "popularity.desc"
    # 华语热度榜按 popularity 排会顶上来没人评分的擦边条目，要有评分基数
    assert q["vote_count.gte"] == "10"


def test_new_shows_are_not_filtered_by_vote_count():
    """今日播出/一周在播是新番，本来就没票数，加门槛会把它们全滤掉."""
    for kind in ("airing", "onair"):
        t = _Recorder([])
        TmdbClient("k", transport=t).chinese_tv(kind)
        assert "vote_count.gte" not in dict(httpx.URL(str(t.calls[0])).params)


def test_today_is_china_time():
    """「今日播出」得按国内的今天算，不是 UTC 的今天."""
    import datetime

    cn = (datetime.datetime.now(datetime.timezone.utc)
          + datetime.timedelta(hours=8)).date().isoformat()
    assert TmdbClient._cn_date() == cn


# ---------------------------------------------------------------- 榜单合并
class _Tmdb:
    def __init__(self, cn=(), glob=(), cn_error=None):
        self.cn, self.glob, self.cn_error = list(cn), list(glob), cn_error

    def chinese_tv(self, kind, page=1):
        if self.cn_error:
            raise RuntimeError(self.cn_error)
        return self.cn

    def airing_today(self, page=1):
        return self.glob

    on_the_air = popular_tv = airing_today
    is_chinese = staticmethod(TmdbClient.is_chinese)
    is_animation = staticmethod(TmdbClient.is_animation)


def _app(tmdb, prefer=True, tmp_path=None):
    from mediafans.web import WebApp

    app = WebApp(lambda: None, prefer_chinese=prefer,
                 watch_path=(tmp_path / "w.json") if tmp_path else None)
    app._tmdb = tmdb
    app.tmdb_key = "k"
    return app


def _named(title, lang, tid):
    it = _item(title, lang)
    it.tmdb_id = tid
    return it


def test_discover_puts_chinese_first_but_keeps_the_rest(tmp_path):
    """是「优先」不是「只要」——外语新剧还在，只是不占满第一屏."""
    app = _app(_Tmdb(cn=[_named("凡人修仙传", "zh", 1)],
                     glob=[_named("Gran hermano", "es", 2),
                           _named("末日地堡", "en", 3)]), tmp_path=tmp_path)
    items = app.api_discover("airing")["items"]
    assert [i["title"] for i in items] == ["凡人修仙传", "Gran hermano", "末日地堡"]
    assert [i["chinese"] for i in items] == [True, False, False]


def test_discover_dedupes_across_the_two_lists(tmp_path):
    """华语剧也可能挤进全球榜，不能出现两张一样的海报."""
    app = _app(_Tmdb(cn=[_named("早春晴朗", "zh", 7)],
                     glob=[_named("早春晴朗", "zh", 7), _named("末日地堡", "en", 3)]),
               tmp_path=tmp_path)
    items = app.api_discover("airing")["items"]
    assert [i["tmdb_id"] for i in items] == [7, 3]


def test_discover_survives_a_broken_chinese_slice(tmp_path):
    """华语那档挂了不该让整个榜打不开，但也不能不吭声."""
    app = _app(_Tmdb(cn_error="discover 429", glob=[_named("末日地堡", "en", 3)]),
               tmp_path=tmp_path)
    d = app.api_discover("airing")
    assert [i["title"] for i in d["items"]] == ["末日地堡"]
    assert "华语榜没拉到" in d["note"] and "429" in d["note"]


def test_prefer_chinese_can_be_turned_off(tmp_path):
    app = _app(_Tmdb(cn=[_named("凡人修仙传", "zh", 1)], glob=[_named("末日地堡", "en", 3)]),
               prefer=False, tmp_path=tmp_path)
    assert [i["title"] for i in app.api_discover("airing")["items"]] == ["末日地堡"]


# ---------------------------------------------------------------- 配置默认值
def test_config_get_honours_its_default():
    """Config.get 原来忽略 default 参数，缺配置项时返回 None 而不是默认值."""
    from mediafans.config import Config

    cfg = Config(None, {"tmdb": {"api_key": "k"}})
    assert cfg.get("tmdb.prefer_chinese", True) is True
    assert cfg.get("tmdb.api_key", "fallback") == "k"
    assert cfg.get("完全没有的项") is None


def test_tmdb_base_url_is_configurable():
    """国内服务器到 api.themoviedb.org 不通，接口得能指到一个境外反代。

    只有接口需要中转——海报站 image.tmdb.org 从国内是通的，poster_url 不受影响。
    """
    import httpx

    from mediafans.metadata.tmdb import TmdbClient

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": []})

    c = TmdbClient("k", transport=httpx.MockTransport(handler),
                   base_url="https://relay.example/tmdb-abc/3/")
    c.trending("movie")
    assert seen[0].startswith("https://relay.example/tmdb-abc/3/trending/movie/week")
    # 不填就直连官方
    c2 = TmdbClient("k", transport=httpx.MockTransport(handler))
    c2.trending("movie")
    assert seen[1].startswith("https://api.themoviedb.org/3/trending/movie/week")
    # 海报永远走官方图片站
    assert TmdbClient.poster_url("/x.jpg").startswith("https://image.tmdb.org/")


# ------------------------------------------------- 中转抖一下不该让作品页报错
# 2026-09-24 20:42-22:11 线上实录：/api/series?tmdb_id=282326 连续 400，
# 正文是「读取剧集信息失败: The read operation timed out」；同一时刻
# /api/search/media 也 400。境外中转的 nginx 把 api.themoviedb.org 的 8 个
# AAAA 记录塞进了 upstream 池，而那台机器只有 link-local 的 IPv6，
# 撞上就一直没响应，直到这边 15 秒读超时。中转已按 IPv4 修好，
# 但这条链路本来就长，客户端也得能扛一次抖动。


def test_transient_failure_is_retried_once():
    """超时重试一次：重试会让中转重新挑一个 upstream 地址，实测能救回来."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, json={"results": [
            {"id": 1, "name": "交锋", "original_language": "zh"}]})

    c = TmdbClient("k", transport=httpx.MockTransport(handler))
    got = c.popular_tv()
    assert len(calls) == 2                      # 第一趟超时，第二趟拿到了
    assert [i.title for i in got] == ["交锋"]


def test_relay_5xx_is_retried_but_4xx_is_not():
    """5xx 是中转在说「上游挂了」，值得再来一次；4xx 再问一次还是同一个答案."""
    for status, want_calls in ((502, 2), (503, 2), (401, 1), (404, 1)):
        calls = []

        def handler(request, status=status):
            calls.append(1)
            return httpx.Response(status, json={"status_message": "no"})

        c = TmdbClient("k", transport=httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            c.tv_detail(282326)
        assert len(calls) == want_calls, status


def test_retry_gives_up_and_raises_the_real_error():
    """两趟都不行就把原始异常抛出去——上层要靠它的文字写进报错提示."""
    def handler(request):
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    c = TmdbClient("k", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout, match="read operation timed out"):
        c.season_detail(282326, 1)


def test_detail_endpoints_all_go_through_the_retrying_fetch():
    """四个入口以前各写一份请求样板，重试只加在一处就会漏。

    这里盯的是「有没有漏」：任何一个端点绕过 _fetch，它的超时就不会被重试。
    """
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"results": [], "id": 1, "seasons": [],
                                         "genres": [], "episodes": []})

    c = TmdbClient("k", transport=httpx.MockTransport(handler))
    fetched = []
    real = c._fetch
    c._fetch = lambda path, params=None: (fetched.append(path), real(path, params))[1]
    c.search("交锋", "tv")
    c.movie_detail(1089598)
    c.tv_detail(282326)
    c.season_detail(282326, 1)
    assert fetched == ["/search/tv", "/movie/1089598", "/tv/282326", "/tv/282326/season/1"]
    assert len(seen) == 4        # 都真的发出去了，没有谁只是记了个名字


def test_retry_does_not_stretch_the_total_wait():
    """timeout 是整个调用的预算，按趟数平分——加了重试，上层不该等更久。

    作品页要连着打 tv_detail + season_detail 两趟；要是每趟都给满 15 秒、
    再各重试一次，最坏要等一分钟，「读取中…」比原来还难看。
    """
    seen = []

    def handler(request):
        seen.append(request.extensions.get("timeout", {}))
        return httpx.Response(200, json={"results": []})

    c = TmdbClient("k", timeout=15.0, transport=httpx.MockTransport(handler))
    c.trending("movie")
    assert c.TRIES == 2
    # 每趟 7.5 秒 × 2 趟 = 原来的 15 秒
    assert seen[0]["read"] == pytest.approx(7.5)
    assert c.TRIES * seen[0]["read"] == pytest.approx(15.0)
