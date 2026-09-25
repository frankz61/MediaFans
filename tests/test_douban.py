"""榜单的华语段换成豆瓣，外语段仍用 TMDB。

TMDB 的华语数据「有，但不热」：它按全球用户的 popularity 排，国内正在追的剧、
正在上映的国产片排不上来，综艺基本空白。豆瓣是国内的口径。
但点进作品页之后的一切都挂在 tmdb_id 上，所以豆瓣条目得先对上 TMDB。
"""

import httpx
import pytest

from mediafans.metadata.douban import (DoubanClient, DoubanItem, norm_title,
                                       pick_match, query_variants)
from mediafans.metadata.tmdb import TmdbClient
from mediafans.models import MediaItem


def _mi(title, lang="zh", tid=1, year="2026", mtype="tv", original=""):
    return MediaItem(title=title, original_title=original or title, original_language=lang,
                     media_type=mtype, year=year, tmdb_id=tid, rating=6.0,
                     poster=f"https://image.tmdb.org/t/p/w342/{tid}.jpg")


def _db(title, did="1", year="2026", mtype="tv", regions=("中国大陆",), rating=7.5):
    return DoubanItem(douban_id=did, title=title, media_type=mtype, year=year,
                      rating=rating, regions=list(regions))


# ---------------------------------------------------------------- 接口解析
def test_collection_parses_year_regions_and_sends_referer():
    """口碑榜的条目没有 year 字段，年份和地区都只能从 card_subtitle 里取。
    不带 Referer 豆瓣直接拒。"""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["referer"] = request.headers.get("referer")
        return httpx.Response(200, json={"subject_collection_items": [
            {"id": "37822829", "title": "开庭", "type": "tv", "year": None,
             "rating": {"value": 8.6},
             "card_subtitle": "2026 / 中国香港 / 剧情 / 吴家伟 / 邱士缙 谷祖琳"},
            {"id": "38603647", "title": "大哥小助理", "type": "tv", "year": "2026",
             "rating": None, "card_subtitle": "2026 / 中国大陆 / 真人秀"},
            {"id": "10756537", "title": "杀死比尔：血色全传", "type": "movie",
             "year": "2004", "rating": {"value": 8.7},
             "card_subtitle": "2004 / 美国 / 动作 犯罪"},
        ]})

    items = DoubanClient(transport=httpx.MockTransport(handler)).collection(
        "tv_chinese_best_weekly")
    assert "/subject_collection/tv_chinese_best_weekly/items" in seen["url"]
    assert seen["referer"] and "douban.com" in seen["referer"]
    assert [(i.title, i.year, i.rating) for i in items] == [
        ("开庭", "2026", 8.6), ("大哥小助理", "2026", 0.0), ("杀死比尔：血色全传", "2004", 8.7)]
    assert [i.chinese for i in items] == [True, True, False]
    assert items[2].media_type == "movie"


def test_recent_hot_asks_for_chinese_movies():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"items": [
            {"id": "1", "title": "抓特务", "type": "movie",
             "card_subtitle": "2026 / 中国大陆 / 剧情"}]})

    items = DoubanClient(transport=httpx.MockTransport(handler)).recent_hot("movie")
    assert seen["params"]["type"] == "华语" and seen["params"]["category"] == "热门"
    assert items[0].year == "2026" and items[0].media_type == "movie"


# ---------------------------------------------------------------- 对到 TMDB
def test_season_is_split_off_for_tv():
    """豆瓣每季一个条目，TMDB 一部剧挂多季——得搜整部剧名，再落到那一季。"""
    v = query_variants(_db("花儿与少年 第八季"))
    assert v[-1] == ("花儿与少年", 8, False)       # 剥了季号不核对年份：TMDB 的年份是第一季的
    assert query_variants(_db("脱口秀和Ta的朋友们 第三季"))[-1][:2] == ("脱口秀和Ta的朋友们", 3)
    assert query_variants(_db("某剧 第十二季"))[-1][1] == 12


def test_variety_year_and_sequel_suffixes():
    """实测综艺对不上的多半是这两种写法。"""
    assert query_variants(_db("歌手2026"))[-1] == ("歌手", None, False)
    assert query_variants(_db("一饭封神2"))[-1] == ("一饭封神", 2, False)
    # 片名本身带数字的别剥坏
    assert query_variants(_db("9-1-1")) == [("9-1-1", None, True)]
    assert query_variants(_db("1923")) == [("1923", None, True)]


def test_movie_titles_are_searched_as_is():
    """电影续集在 TMDB 里是另一部片，「年会不能停！2」不能剥成第一部。"""
    assert query_variants(_db("年会不能停！2", mtype="movie")) == [("年会不能停！2", None, True)]


def test_invisible_chars_are_stripped():
    """豆瓣片名里偶尔夹着 U+200E，实测《神探之痕迹》就是因为它没对上。"""
    assert query_variants(_db("神探之痕迹‎", mtype="movie"))[0][0] == "神探之痕迹"
    assert norm_title("年会不能停！2") == norm_title("年会不能停!2")


def test_pick_match_is_exact_title_only():
    """宁可丢，不能对错——对错了点进去是另一部剧。"""
    cands = [_mi("深渊", tid=1), _mi("深渊无间", tid=2)]
    assert pick_match("深渊无间", "2026", True, cands).tmdb_id == 2
    assert pick_match("深渊无", "2026", True, cands) is None


def test_pick_match_checks_year_unless_told_not_to():
    old = [_mi("交锋", tid=9, year="2011")]
    assert pick_match("交锋", "2026", True, old) is None
    assert pick_match("交锋", "2026", False, old).tmdb_id == 9
    assert pick_match("交锋", "2025", True, [_mi("交锋", tid=3, year="2026")]).tmdb_id == 3


def test_pick_match_accepts_original_title():
    c = [_mi("The Wandering Earth", original="流浪地球", tid=5, mtype="movie")]
    assert pick_match("流浪地球", "2026", False, c).tmdb_id == 5


# ---------------------------------------------------------------- 榜单拼装
class _Douban:
    def __init__(self, items=(), error=None):
        self.items, self.error, self.calls = list(items), error, 0

    def collection(self, name, count=50):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.items

    def recent_hot(self, media="movie", **kw):
        return self.collection(media)


class _Tmdb:
    is_chinese = staticmethod(TmdbClient.is_chinese)
    is_animation = staticmethod(TmdbClient.is_animation)

    def __init__(self, catalog=(), glob=(), search_error=None):
        self.catalog, self.glob = list(catalog), list(glob)
        self.search_error = search_error
        self.searched = []

    def search(self, query, media_type="multi", prefer_chinese=True):
        self.searched.append(query)
        if self.search_error:
            raise RuntimeError(self.search_error)
        return [i for i in self.catalog if query in (i.title, i.original_title)]

    def popular_tv(self, page=1):
        return self.glob

    now_playing = popular_movie = upcoming = popular_tv


def _app(douban, tmdb, tmp_path, prefer=True):
    from mediafans.web import WebApp

    app = WebApp(lambda: None, prefer_chinese=prefer, watch_path=tmp_path / "w.json")
    app._tmdb, app._douban, app.tmdb_key = tmdb, douban, "k"
    return app


def test_douban_first_then_foreign_only_from_tmdb(tmp_path):
    """华语段是豆瓣的，外语段是 TMDB 的——TMDB 榜里混着的华语不再出现，
    不然同一部剧会出现两次，或者豆瓣的排序被 TMDB 的打乱。"""
    tmdb = _Tmdb(catalog=[_mi("深渊无间", tid=1), _mi("早春晴朗", tid=2)],
                 glob=[_mi("Silo", "en", tid=10), _mi("凡人修仙传", "zh", tid=11)])
    app = _app(_Douban([_db("深渊无间", "a"), _db("早春晴朗", "b")]), tmdb, tmp_path)
    items = app.api_discover("popular")["items"]
    assert [i["title"] for i in items] == ["深渊无间", "早春晴朗", "Silo"]
    assert [i["chinese"] for i in items] == [True, True, False]
    assert items[0]["douban_id"] == "a" and items[0]["tmdb_id"] == 1


def test_douban_year_and_rating_win_but_poster_is_tmdb(tmp_path):
    """海报用 TMDB 的：豆瓣图片不带 Referer 回 418，而网页是 no-referrer。
    年份用豆瓣的：第八季该显示 2026，不是第一季的 2014。"""
    tmdb = _Tmdb(catalog=[_mi("花儿与少年", tid=7, year="2014")])
    app = _app(_Douban([_db("花儿与少年 第八季", "x", year="2026", rating=6.1)]), tmdb, tmp_path)
    it = app.api_discover("variety")["items"][0]
    assert it["title"] == "花儿与少年" and it["season"] == 8
    assert it["year"] == "2026" and it["rating"] == 6.1
    assert it["poster"].startswith("https://image.tmdb.org/")


def test_no_douban_rating_means_no_star(tmp_path):
    """豆瓣 0 分是「评价人数不足」。退回 TMDB 的分会误导——
    实测《一瓯春》豆瓣暂无评分，TMDB 上几个人打出 9.5。"""
    tmdb = _Tmdb(catalog=[_mi("一瓯春", tid=6)])          # TMDB 分 6.0
    app = _app(_Douban([_db("一瓯春", "a", rating=0)]), tmdb, tmp_path)
    assert app.api_discover("variety")["items"][0]["rating"] == 0


def test_talk_shows_and_news_are_kept_out_of_foreign_tv(tmp_path):
    """TMDB 剧集热门榜被美国深夜秀、新闻刷屏，外语单独成段后就是一整行脱口秀。"""
    talk = _mi("斯蒂芬·科尔伯特深夜秀", "en", tid=20)
    talk.genres = [10767]
    news = _mi("每日新闻", "en", tid=21)
    news.genres = [10763, 35]
    drama = _mi("Slow Horses", "en", tid=22)
    drama.genres = [18]
    app = _app(_Douban([]), _Tmdb(glob=[talk, news, drama]), tmp_path)
    assert [i["title"] for i in app.api_discover("popular")["items"]] == ["Slow Horses"]


def test_matches_survive_a_restart(tmp_path):
    """每次部署都重启。不落盘的话重启后头一回打开，每个榜都要重搜几十趟（实测单榜 13 秒）。"""
    tmdb = _Tmdb(catalog=[_mi("花儿与少年", tid=7, year="2014")])
    _app(_Douban([_db("花儿与少年 第八季", "x")]), tmdb, tmp_path).api_discover("variety")
    tmdb2 = _Tmdb(catalog=[_mi("花儿与少年", tid=7, year="2014")])
    it = _app(_Douban([_db("花儿与少年 第八季", "x")]), tmdb2, tmp_path) \
        .api_discover("variety")["items"][0]
    assert tmdb2.searched == []
    assert (it["tmdb_id"], it["season"], it["title"]) == (7, 8, "花儿与少年")


def test_misses_survive_a_restart_until_they_expire(tmp_path):
    tmdb = _Tmdb()
    _app(_Douban([_db("圆桌晚晴派", "m")]), tmdb, tmp_path).api_discover("variety")
    searched = len(tmdb.searched)
    app = _app(_Douban([_db("圆桌晚晴派", "m")]), tmdb, tmp_path)
    app.api_discover("variety")
    assert len(tmdb.searched) == searched                # 重启后没再搜
    app._discover_cache.clear()
    app._douban_map["m"] = (0.0, None)                    # 过了重试期
    app.api_discover("variety")
    assert len(tmdb.searched) > searched


def test_unmatched_entries_are_dropped(tmp_path):
    """对不上 TMDB 的点进去也打不开，不如不出现。"""
    tmdb = _Tmdb(catalog=[_mi("交锋", tid=3)])
    app = _app(_Douban([_db("交锋", "a"), _db("圆桌晚晴派", "b")]), tmdb, tmp_path)
    assert [i["title"] for i in app.api_discover("variety")["items"]] == ["交锋"]


def test_foreign_entries_in_mixed_douban_lists_are_skipped(tmp_path):
    """影院热映是全部地区混排的，华语段只取华语。"""
    tmdb = _Tmdb(catalog=[_mi("空枪", tid=4, mtype="movie"),
                          _mi("奥德赛", "en", tid=5, mtype="movie")])
    app = _app(_Douban([_db("奥德赛", "a", mtype="movie", regions=("美国",)),
                        _db("空枪", "b", mtype="movie", regions=("中国大陆", "中国香港"))]),
               tmdb, tmp_path)
    assert [i["title"] for i in app.api_discover("now")["items"]] == ["空枪"]


def test_two_seasons_of_one_show_appear_once(tmp_path):
    """两季同时上榜会对到同一个 tmdb_id——电视端按 tmdb_id 当列表键，重复会直接崩。"""
    tmdb = _Tmdb(catalog=[_mi("密室大逃脱", tid=8)])
    app = _app(_Douban([_db("密室大逃脱 第七季", "a"), _db("密室大逃脱 第八季", "b")]),
               tmdb, tmp_path)
    items = app.api_discover("variety")["items"]
    assert [(i["tmdb_id"], i["season"]) for i in items] == [(8, 7)]


def test_matches_are_remembered_across_list_refreshes(tmp_path):
    """一个榜几十条，每条都要搜一趟 TMDB（走境外中转）——对上的不用再搜。"""
    tmdb = _Tmdb(catalog=[_mi("交锋", tid=3)])
    app = _app(_Douban([_db("交锋", "a")]), tmdb, tmp_path)
    app.api_discover("variety")
    app._discover_cache.clear()                 # 榜单缓存过期
    app.api_discover("variety")
    assert tmdb.searched == ["交锋"]


def test_a_search_error_is_not_remembered_as_a_miss(tmp_path):
    """中转抖一下不能让整段华语消失好几个小时。"""
    tmdb = _Tmdb(catalog=[_mi("交锋", tid=3)], search_error="relay 502")
    app = _app(_Douban([_db("交锋", "a")]), tmdb, tmp_path)
    d = app.api_discover("popular")
    assert "华语榜没拉到" in d["note"] and "502" in d["note"]
    tmdb.search_error = None
    app._discover_cache.clear()
    assert [i["title"] for i in app.api_discover("popular")["items"]] == ["交锋"]


def test_broken_douban_keeps_the_foreign_half(tmp_path):
    app = _app(_Douban(error="douban 403"), _Tmdb(glob=[_mi("Silo", "en", tid=10)]), tmp_path)
    d = app.api_discover("popular")
    assert [i["title"] for i in d["items"]] == ["Silo"]
    assert "华语榜没拉到" in d["note"]


def test_variety_has_no_foreign_half_so_it_must_say_it_failed(tmp_path):
    from mediafans.errors import MediaFansError

    app = _app(_Douban(error="douban 403"), _Tmdb(), tmp_path)
    with pytest.raises(MediaFansError):
        app.api_discover("variety")


def test_unknown_kind_falls_back_to_popular(tmp_path):
    app = _app(_Douban([]), _Tmdb(glob=[_mi("Silo", "en", tid=10)]), tmp_path)
    assert app.api_discover("乱来")["kind"] == "popular"


def test_web_tabs_match_server_kinds():
    """剧集档位改成 热门/口碑/综艺；今日播出/一周在播只留给老版本电视端。"""
    from mediafans.web import PAGE_HTML, WebApp

    assert "['popular', '热门'], ['praise', '口碑'], ['variety', '综艺']" in PAGE_HTML
    assert "'airing'" not in PAGE_HTML
    assert {"airing", "onair"} <= set(WebApp.DISCOVER)       # 1.1 的电视端还在要
    assert "openSeries(it, it.season)" in PAGE_HTML
