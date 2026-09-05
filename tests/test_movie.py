"""电影：搜索词、本地扫描、来源收集、整张视图。

电影跟剧集共用视图结构，所以这里主要盯**没有集号之后**会松掉的那些地方：
片名判断成了唯一闸门、预告片没法靠集号排除、同一部片的多个版本不该被去重。
"""

import pytest

from mediafans.agent import (
    MOVIE_EPISODE,
    Work,
    build_movie,
    movie_queries,
    movie_sources,
    probe_share,
    scan_local_movie,
)
from mediafans.agent.movie import MIN_MOVIE_BYTES
from mediafans.models import DriveFile, ShareLink

from test_series import FakeDrive, _f, _link


class FakeMovieTmdb:
    def __init__(self, title="流浪地球", original="The Wandering Earth",
                 year="2019", animation=False):
        self.info = {"title": title, "original_title": original, "year": year,
                     "release_date": f"{year}-02-05", "animation": animation,
                     "overview": "太阳要完了。", "poster": "p.jpg", "runtime": 125}

    def movie_detail(self, tmdb_id):
        return dict(self.info, tmdb_id=tmdb_id, total_seasons=0,
                    total_episodes=0, seasons=[])


def _search(links):
    return lambda kw, netdisk=None: (list(links), [])


BIG = 8 * 1024 ** 3


# ---------------------------------------------------------------- 查询词
def test_movie_queries_include_year_but_not_only_year():
    """同名重拍太多，年份能分开；但网盘标题经常不写年份，只用带年份的会漏召回。"""
    qs = movie_queries("无间道", "Infernal Affairs", "2002")
    assert qs[0] == "无间道"                    # 不带年份的必须在
    assert "无间道 2002" in qs
    assert "Infernal Affairs" in qs


def test_movie_queries_skip_original_when_same_as_title():
    assert movie_queries("流浪地球", "流浪地球", "2019") == ["流浪地球", "流浪地球 2019"]


# ---------------------------------------------------------------- 扫本地
def test_scan_local_movie_keeps_every_version_best_first():
    """同一部片存了几版，全都留着，画质最高的排前面——播不了才好换。"""
    drive = FakeDrive({"/MediaFans/流浪地球": [
        _f("流浪地球.2019.1080p.WEB-DL.mkv", size=BIG),      # 更大但更糊
        _f("流浪地球.2019.2160p.WEB-DL.mkv", size=BIG // 2),
        _f("说明.txt", size=100),
    ]})
    got = scan_local_movie(drive, "/MediaFans/流浪地球",
                           Work(titles=["流浪地球", "The Wandering Earth"]))
    assert [c.height for c in got] == [2160, 1080]
    assert got[0].path == "/MediaFans/流浪地球/流浪地球.2019.2160p.WEB-DL.mkv"


def test_scan_local_movie_rejects_another_film_in_the_same_folder():
    """没有集号兜底，片名判断是唯一的闸门——混进来的别的片必须挡住。"""
    foreign = []
    drive = FakeDrive({"/MediaFans/流浪地球": [
        _f("流浪地球.2019.1080p.WEB-DL.mkv", size=BIG),
        _f("The.Gentlemen.2024.2160p.WEB-DL.mkv", size=BIG),
    ]})
    got = scan_local_movie(drive, "/MediaFans/流浪地球",
                           Work(titles=["流浪地球", "The Wandering Earth"]), foreign)
    assert [c.name for c in got] == ["流浪地球.2019.1080p.WEB-DL.mkv"]
    assert foreign == ["The.Gentlemen.2024.2160p.WEB-DL.mkv"]


def test_scan_local_movie_on_missing_dir_is_empty():
    assert scan_local_movie(FakeDrive(), "/MediaFans/没有", Work()) == []


# ---------------------------------------------------------------- 来源
def _probe(drive, url, title=""):
    return probe_share(drive, _link(url, title))


def test_movie_sources_keep_every_version_sorted_by_quality():
    """4K / 1080p / 国语版都是合法候选，不是重复，别去重成一个。"""
    drive = FakeDrive(shares={"s1": {"0": [
        _f("流浪地球.2019.1080p.WEB-DL.mkv", size=BIG, fid="a"),
        _f("流浪地球.2019.2160p.WEB-DL.mkv", size=BIG, fid="b"),
    ]}})
    got = movie_sources([_probe(drive, "s1", "流浪地球 合集")],
                        Work(titles=["流浪地球"]))
    assert [s.file.height for s in got] == [2160, 1080]


def test_movie_sources_drop_trailers_by_size():
    """没有集号可以交叉验证，只能靠体积挡预告片。"""
    dropped = []
    drive = FakeDrive(shares={"s1": {"0": [
        _f("流浪地球.2019.2160p.WEB-DL.mkv", size=BIG, fid="a"),
        _f("流浪地球.预告片.mp4", size=MIN_MOVIE_BYTES - 1, fid="b"),
    ]}})
    got = movie_sources([_probe(drive, "s1", "流浪地球")],
                        Work(titles=["流浪地球"]), dropped)
    assert [s.file.name for s in got] == ["流浪地球.2019.2160p.WEB-DL.mkv"]
    assert any("预告片" in d for d in dropped)


def test_movie_sources_drop_a_share_that_is_another_work():
    """分享把同名的另一部打包进来时，整份排除——跟剧集那边同一个理由。"""
    dropped = []
    drive = FakeDrive(shares={
        "mine": {"0": [_f("流浪地球.2019.2160p.mkv", size=BIG, fid="a")]},
        "other": {"0": [_f("三体.2023.2160p.mkv", size=BIG, fid="b")]},
    })
    probes = [_probe(drive, "mine", "流浪地球"), _probe(drive, "other", "三体全集")]
    got = movie_sources(probes, Work(titles=["流浪地球"]), dropped)
    assert [s.file.name for s in got] == ["流浪地球.2019.2160p.mkv"]
    assert any("别的作品" in d for d in dropped)


# ---------------------------------------------------------------- 整张视图
def test_build_movie_finds_it_in_the_netdisk():
    drive = FakeDrive({"/MediaFans/流浪地球": [
        _f("流浪地球.2019.2160p.WEB-DL.mkv", size=BIG)]})
    d = build_movie(lambda: drive, _search([]), FakeMovieTmdb(), 300).as_dict()
    assert d["media_type"] == "movie"
    assert d["season"] == 0 and d["year"] == "2019" and d["runtime"] == 125
    assert d["counts"] == {"total": 1, "saved": 1, "available": 0, "missing": 0}
    assert d["episodes"][0]["episode"] == MOVIE_EPISODE
    assert d["episodes"][0]["local"]["height"] == 2160


def test_build_movie_lists_sources_when_not_saved_yet():
    drive = FakeDrive(shares={"s1": {"0": [
        _f("流浪地球.2019.2160p.WEB-DL.mkv", size=BIG, fid="a")]}})
    view = build_movie(lambda: drive, _search([_link("s1", "流浪地球 4K")]),
                       FakeMovieTmdb(), 300)
    d = view.as_dict()
    assert d["counts"]["available"] == 1
    assert d["episodes"][0]["sources"][0]["label"] == "源1"


def test_build_movie_does_not_search_when_already_saved():
    """网盘里已经有了就别去搜——探测很贵，而且用户要的是「播」不是「再找一份」。"""
    calls = []

    def search(kw, netdisk=None):
        calls.append(kw)
        return [], []

    drive = FakeDrive({"/MediaFans/流浪地球": [
        _f("流浪地球.2019.2160p.WEB-DL.mkv", size=BIG)]})
    view = build_movie(lambda: drive, search, FakeMovieTmdb(), 300)
    assert calls == []
    assert any("已经有了" in n for n in view.notes)


def test_build_movie_says_so_when_nothing_found():
    view = build_movie(lambda: FakeDrive(), _search([]), FakeMovieTmdb(), 300)
    d = view.as_dict()
    assert d["counts"] == {"total": 1, "saved": 0, "available": 0, "missing": 1}
    assert view.notes


def test_build_movie_without_search_is_scan_only():
    """首屏只扫网盘：搜资源很慢，不能挡着页面出不来。"""
    drive = FakeDrive(shares={"s1": {"0": [_f("流浪地球.2019.2160p.mkv", size=BIG)]}})
    view = build_movie(lambda: drive, _search([_link("s1")]), FakeMovieTmdb(), 300,
                       with_sources=False)
    assert view.rows[0].sources == []


# ---------------------------------------------------------------- 续集
def test_sequel_is_a_different_film():
    """《流浪地球》和《流浪地球2》是两部电影。

    片名判断分不开它俩——strip_tech 把数字当技术标记剥掉了，所以
    title_match 一定认为是同一部。实测线上搜 2019 版，4 个「版本」里混进了
    `流浪地球2.2023.2160p.BluRay.Remux…` 和续集的幕后花絮。
    """
    dropped = []
    drive = FakeDrive(shares={"s1": {"0": [
        _f("流浪地球 .2019.2160p.BluRay.Remux.HEVC.mkv", size=BIG, fid="a"),
        _f("流浪地球2.2023.2160p.BluRay.Remux.HEVC.mkv", size=BIG, fid="b"),
        _f("Inside the Wandering Earth2.mkv", size=BIG, fid="c"),
    ]}})
    work = Work(titles=["流浪地球", "The Wandering Earth"], strict_sequel=True)
    got = movie_sources([_probe(drive, "s1", "流浪地球 合集")], work, dropped)
    assert [s.file.name for s in got] == ["流浪地球 .2019.2160p.BluRay.Remux.HEVC.mkv"]
    assert sum("续集号" in d for d in dropped) == 2


def test_sequel_check_works_the_other_way_round():
    """反过来也要成立：找《流浪地球2》时，2019 那部才是错的那个。"""
    work = Work(titles=["流浪地球2"], strict_sequel=True)
    drive = FakeDrive(shares={"s1": {"0": [
        _f("流浪地球 .2019.2160p.mkv", size=BIG, fid="a"),
        _f("流浪地球2.2023.2160p.mkv", size=BIG, fid="b"),
    ]}})
    got = movie_sources([_probe(drive, "s1", "流浪地球2")], work, [])
    assert [s.file.name for s in got] == ["流浪地球2.2023.2160p.mkv"]


@pytest.mark.parametrize("name", [
    "流浪地球.2019.2160p.WEB-DL.mkv",        # 年份，4 位
    "流浪地球 2019 国语中字.mkv",             # 年份，带空格
    "流浪地球 2160p.mkv",                    # 分辨率
    "流浪地球 4K 高码.mkv",                  # 画质，数字后面跟着 K
    "流浪地球：飞跃2020特别版.重映版.mkv",     # 冒号隔开，不是紧跟
])
def test_numbers_that_are_not_sequel_numbers(name):
    """片名后面的数字大多不是续集号：年份、分辨率、画质。误伤这些比漏掉续集更糟。"""
    from mediafans.agent.identity import sequel_verdict

    assert sequel_verdict(name, ["流浪地球"]) is not False


def test_sequel_check_is_off_for_tv():
    """剧集不开这条：`末日地堡2 E01.mkv` 里的 2 常常是季号，开了会误杀正片。"""
    from mediafans.agent.identity import belongs

    tv = Work(titles=["末日地堡"])                       # strict_sequel 默认 False
    assert belongs("末日地堡2/E01.mkv", tv, title_can_reject=True) is None
    movie = Work(titles=["末日地堡"], strict_sequel=True)
    assert belongs("末日地堡2/E01.mkv", movie) == "续集号对不上（《片名2》不是《片名》）"
