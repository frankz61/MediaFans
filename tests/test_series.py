"""剧集矩阵测试。

真实场景（用户网盘里的末日地堡第二季）：E01-05 是 2160p、E06 掉到 1080p、
E07 存了两份画质不同的、**E08 整个缺失**、E09-10 又是 1080p。
整季打包转存必然产生这种拼盘，按集铺开才看得清、才能精确补。
"""

import pytest

from mediafans.agent import (
    LocalFile, SeriesCache, build_series, fetch_episode, index_sources,
    probe_share, scan_local,
)
from mediafans.models import DriveFile, ShareLink


class FakeDrive:
    save_dir = "/MediaFans"

    def __init__(self, local=None, shares=None, dead=()):
        self.local = local or {}          # path -> [DriveFile]
        self.shares = shares or {}        # url -> {dir_fid: [DriveFile]}
        self.dead = set(dead)
        self.saved = []

    def resolve_path(self, path):
        return f"fid:{path}" if path in self.local else None

    def list_files(self, fid):
        return list(self.local.get(str(fid).replace("fid:", ""), []))

    def open_share(self, url, passcode=""):
        if url in self.dead:
            raise RuntimeError("分享已失效")
        return {"url": url}

    def list_share_files(self, ctx, dir_fid="0"):
        return list(self.shares.get(ctx["url"], {}).get(dir_fid, []))

    def save_share_files(self, ctx, files, to_dir):
        self.saved.append({"dir": to_dir, "names": [f.name for f in files]})
        return [f.fid for f in files]


def _f(name, size=2 * 1024 ** 3, is_dir=False, fid=None):
    return DriveFile(fid=fid or name, name=name, is_dir=is_dir, size=size,
                     share_fid_token="TK-" + (fid or name))


def _link(url, title=""):
    return ShareLink(url=url, netdisk="quark", title=title or url)


# ---------------------------------------------------------------- 扫本地
def test_scan_local_maps_episodes():
    drive = FakeDrive({"/MediaFans/剧": [
        _f("Silo.S02E01.2160p.WEB-DL.mkv"),
        _f("Silo.S02E02.1080p.WEB-DL.mkv"),
        _f("说明.txt", size=100),
        _f("子目录", is_dir=True),
    ]})
    local = scan_local(drive, "/MediaFans/剧", 2)
    assert sorted(local) == [1, 2]
    assert local[1][0].height == 2160 and local[2][0].height == 1080
    assert local[1][0].path == "/MediaFans/剧/Silo.S02E01.2160p.WEB-DL.mkv"


def test_scan_local_keeps_every_copy_best_first():
    """同一集存了两份时两份都留着，画质高的排前面。

    以前这里只保留最好的那一份、其余当重复丢掉。但「最好」只是按分辨率和
    体积猜的——播起来才知道原盘 MKV 浏览器解不了。留着全部，播放器才能
    一键换一份，不用回去重新转存。
    """
    drive = FakeDrive({"/MediaFans/剧": [
        _f("Silo.S02E07.1080p.WEB.h264.mkv", size=3 * 1024 ** 3),
        _f("Silo.S02E07.2160p.WEB-DL.H265.mkv", size=9 * 1024 ** 3),
    ]})
    local = scan_local(drive, "/MediaFans/剧", 2)
    assert len(local) == 1                      # 还是一集
    assert [c.height for c in local[7]] == [2160, 1080]
    assert local[7][0].size == 9 * 1024 ** 3


def test_scan_local_filters_by_season():
    drive = FakeDrive({"/MediaFans/剧": [
        _f("Show.S01E01.1080p.mkv"), _f("Show.S02E01.1080p.mkv")]})
    assert sorted(scan_local(drive, "/MediaFans/剧", 2)) == [1]
    assert scan_local(drive, "/MediaFans/剧", 2)[1][0].name.startswith("Show.S02")


def test_scan_local_keeps_files_without_season_marker():
    """单季目录里常见「第08集.mkv」这种不标季号的，不能因此丢掉."""
    drive = FakeDrive({"/MediaFans/剧": [_f("第08集.mkv"), _f("EP09.mkv")]})
    assert sorted(scan_local(drive, "/MediaFans/剧", 2)) == [8, 9]


def test_scan_local_skips_files_from_a_different_show():
    """同一个目录里混进别的剧，不能按集号算成本剧的。

    实测用户网盘 /MediaFans/末日地堡 里躺着 The.Gentlemen.S01E01~E08，
    修复前第一季被凑成「已存 10 集」，点播放会放出《绅士们》。
    """
    drive = FakeDrive({"/MediaFans/末日地堡": [
        _f("Silo.S01E01.1080p.WEB-DL.mkv"),
        _f("The.Gentlemen.2024.S01E02.2160p.Web.DV.HDR.H265.mkv"),
        _f("The.Gentlemen.2024.S01E03.2160p.Web.DV.HDR.H265.mkv"),
    ]})
    foreign = []
    local = scan_local(drive, "/MediaFans/末日地堡", 1, ["末日地堡", "Silo"], foreign)
    assert sorted(local) == [1]
    assert [n[:13] for n in foreign] == ["The.Gentlemen", "The.Gentlemen"]


def test_scan_local_keeps_bare_filenames_when_matching_titles():
    """裸文件名没有片名可比，按所在目录归属算，不能被片名过滤误杀."""
    drive = FakeDrive({"/MediaFans/末日地堡": [
        _f("S01E01.mp4"), _f("第09集.mkv"), _f("EP10.mkv"),
        _f("末日地堡.S01E02.1080p.mkv"), _f("silo.s01e03.1080p中英字幕.mp4"),
    ]})
    foreign = []
    local = scan_local(drive, "/MediaFans/末日地堡", 1, ["末日地堡", "Silo"], foreign)
    assert sorted(local) == [1, 2, 3, 9, 10]
    assert not foreign


def test_scan_local_without_titles_keeps_everything():
    """没传片名就是老行为：只按集号归位，不做片名判断."""
    drive = FakeDrive({"/MediaFans/剧": [_f("The.Gentlemen.2024.S01E02.2160p.mkv")]})
    assert sorted(scan_local(drive, "/MediaFans/剧", 1)) == [2]


def test_scan_local_ignores_titles_when_nothing_in_the_dir_matches():
    """发布组用的别名不在 TMDB 里时不能过滤——静默吃掉合法文件会让人重复转存.

    `狂飙` 的资源常命名成 The.Knockout，TMDB 的中文名和原名都对不上，
    这时整个目录没有一个文件匹配，说明片名信号不可用，应当全部保留。
    """
    drive = FakeDrive({"/MediaFans/狂飙": [
        _f("The.Knockout.S01E01.2160p.mkv"), _f("The.Knockout.S01E02.2160p.mkv")]})
    foreign = []
    local = scan_local(drive, "/MediaFans/狂飙", 1, ["狂飙"], foreign)
    assert sorted(local) == [1, 2]
    assert not foreign


def test_scan_local_missing_dir_is_empty():
    assert scan_local(FakeDrive(), "/MediaFans/不存在", 1) == {}


# ---------------------------------------------------------------- 来源索引
def test_index_sources_groups_by_episode_and_sorts_by_quality():
    drive = FakeDrive(shares={
        "a": {"0": [_f("Show.S02E08.1080p.mkv", size=3 * 1024 ** 3, fid="a8")]},
        "b": {"0": [_f("Show.S02E08.2160p.mkv", size=9 * 1024 ** 3, fid="b8"),
                    _f("Show.S02E09.2160p.mkv", fid="b9")]},
    })
    probes = [probe_share(drive, _link("a", "源A")), probe_share(drive, _link("b", "源B"))]
    by_ep = index_sources(probes, 2)
    assert sorted(by_ep) == [8, 9]
    # 同一集内部按画质排，最好的在前
    assert [s.file.height for s in by_ep[8]] == [2160, 1080]
    assert by_ep[8][0].share_title == "源B"
    assert by_ep[8][0].file.share_fid_token == "TK-b8"   # 转存要用它


def test_index_sources_skips_other_seasons():
    drive = FakeDrive(shares={"a": {"0": [
        _f("Show.S01E08.1080p.mkv"), _f("Show.S02E08.1080p.mkv")]}})
    by_ep = index_sources([probe_share(drive, _link("a"))], 2)
    assert list(by_ep) == [8]


# ---------------------------------------------------------------- 组装视图
class FakeTmdb:
    def __init__(self, episodes=10):
        self.n = episodes

    def tv_detail(self, tmdb_id):
        return {"title": "末日地堡", "total_seasons": 3, "total_episodes": 30,
                "seasons": [{"season": s, "episodes": 10, "name": f"第 {s} 季",
                             "air_date": ""} for s in (1, 2, 3)]}

    def season_detail(self, tmdb_id, season):
        return {"season": season, "name": f"第 {season} 季", "air_date": "",
                "episodes": [{"episode": i, "name": f"第{i}集", "air_date": "",
                              "runtime": 50, "overview": "", "still": ""}
                             for i in range(1, self.n + 1)]}


def _search(links):
    return lambda kw, netdisk=None: (list(links), [])


def test_build_series_marks_saved_available_and_missing():
    """还原用户的真实处境：E08 缺，别的来源能补上."""
    drive = FakeDrive(
        local={"/MediaFans/末日地堡": [
            _f(f"Silo.S02E{i:02d}.2160p.WEB-DL.mkv") for i in range(1, 8)]
            + [_f("Silo.S02E09.1080p.mkv"), _f("Silo.S02E10.1080p.mkv")]},
        shares={"s1": {"0": [_f("Silo.S02E08.2160p.WEB-DL.mkv", fid="e8")]}},
    )
    view = build_series(lambda: drive, _search([_link("s1", "补档源")]),
                        FakeTmdb(), 125988, 2)
    d = view.as_dict()
    assert d["counts"] == {"total": 10, "saved": 9, "available": 1, "missing": 0}
    e8 = next(e for e in d["episodes"] if e["episode"] == 8)
    assert e8["status"] == "available"
    assert e8["local"] is None
    assert e8["sources"][0]["label"] == "源1"
    assert e8["sources"][0]["height"] == 2160
    e1 = next(e for e in d["episodes"] if e["episode"] == 1)
    assert e1["status"] == "saved" and e1["local"]["path"].endswith("E01.2160p.WEB-DL.mkv")


def test_build_series_reports_truly_missing_episodes():
    """所有来源都没有的集要明确说出来，别让人以为在加载."""
    drive = FakeDrive(local={"/MediaFans/末日地堡": []},
                      shares={"s1": {"0": [_f("Silo.S02E01.1080p.mkv")]}})
    view = build_series(lambda: drive, _search([_link("s1")]), FakeTmdb(3), 1, 2)
    d = view.as_dict()
    assert d["counts"]["missing"] == 2
    assert any("E02、E03" in n for n in d["notes"])


def test_build_series_skips_search_when_season_complete():
    """齐了就别再去搜——搜索和探测都很贵."""
    drive = FakeDrive(local={"/MediaFans/末日地堡": [
        _f(f"Silo.S02E{i:02d}.2160p.mkv") for i in range(1, 4)]})
    called = []

    def search(kw, netdisk=None):
        called.append(kw)
        return [], []

    view = build_series(lambda: drive, search, FakeTmdb(3), 1, 2)
    assert called == []
    assert view.as_dict()["counts"]["saved"] == 3
    assert any("已经齐了" in n for n in view.notes)


def test_build_series_without_sources_is_local_only():
    """只看本地时不该发起任何搜索——首屏要快."""
    drive = FakeDrive(local={"/MediaFans/末日地堡": [_f("Silo.S02E01.1080p.mkv")]})
    called = []
    view = build_series(lambda: drive, lambda *a, **k: called.append(1) or ([], []),
                        FakeTmdb(3), 1, 2, with_sources=False)
    assert called == []
    assert view.as_dict()["counts"] == {"total": 3, "saved": 1, "available": 0, "missing": 2}


def test_build_series_reports_search_failure_distinctly():
    """全部查询词都挂了 != 真没这个资源，提示要能区分."""
    drive = FakeDrive(local={"/MediaFans/末日地堡": []})

    def boom(kw, netdisk=None):
        raise RuntimeError("搜索源全挂了")

    view = build_series(lambda: drive, boom, FakeTmdb(2), 1, 2)
    assert any("搜索失败" in n and "搜索源全挂了" in n for n in view.notes)
    assert view.as_dict()["counts"]["total"] == 2


def test_build_series_says_not_found_when_search_works_but_empty():
    drive = FakeDrive(local={"/MediaFans/末日地堡": []})
    view = build_series(lambda: drive, lambda kw, netdisk=None: ([], []), FakeTmdb(2), 1, 2)
    assert any("没搜到" in n for n in view.notes)
    assert not any("搜索失败" in n for n in view.notes)


def test_build_series_continues_when_some_queries_fail():
    """一个查询词挂了不该拖垮整体——多路查询就是为了容错."""
    drive = FakeDrive(local={"/MediaFans/末日地堡": []},
                      shares={"ok": {"0": [_f("Silo.S02E01.1080p.mkv")]}})

    def flaky(kw, netdisk=None):
        if "S0" in kw or kw.endswith("季"):
            raise RuntimeError("这个词挂了")
        return [_link("ok", "能用的源")], []

    view = build_series(lambda: drive, flaky, FakeTmdb(2), 1, 2)
    assert view.as_dict()["counts"]["available"] == 1


# ---------------------------------------------------------------- 按集转存
def test_fetch_episode_saves_only_that_file():
    """按集补是这套设计的重点：整季打包会带进重复集和拼盘画质."""
    drive = FakeDrive(shares={"s1": {"0": [
        _f("Silo.S02E07.1080p.mkv", fid="e7"),
        _f("Silo.S02E08.2160p.mkv", fid="e8"),
        _f("Silo.S02E09.1080p.mkv", fid="e9")]}})
    probes = [probe_share(drive, _link("s1", "源"))]
    src = index_sources(probes, 2)[8][0]
    path = fetch_episode(lambda: drive, src, "/MediaFans/末日地堡")
    assert drive.saved == [{"dir": "/MediaFans/末日地堡",
                            "names": ["Silo.S02E08.2160p.mkv"]}]   # 只存这一集
    assert path == "/MediaFans/末日地堡/Silo.S02E08.2160p.mkv"


def test_fetch_episode_errors_when_file_vanished():
    drive = FakeDrive(shares={"s1": {"0": [_f("Silo.S02E08.2160p.mkv", fid="e8")]}})
    src = index_sources([probe_share(drive, _link("s1"))], 2)[8][0]
    drive.shares["s1"]["0"] = []          # 分享内容变了
    with pytest.raises(ValueError, match="找不到"):
        fetch_episode(lambda: drive, src, "/MediaFans/末日地堡")


# ---------------------------------------------------------------- 缓存
def test_series_cache_ttl_and_drop():
    cache = SeriesCache(ttl=0.05)
    cache.put(("a", 1), "view")
    assert cache.get(("a", 1)) == "view"
    cache.drop(("a", 1))
    assert cache.get(("a", 1)) is None

    cache.put(("b", 1), "v2")
    import time

    time.sleep(0.08)
    assert cache.get(("b", 1)) is None      # 过期了要重新探测


def test_series_cache_evicts_oldest():
    cache = SeriesCache(cap=2)
    for i in range(3):
        cache.put(("s", i), f"v{i}")
    assert cache.get(("s", 0)) is None
    assert cache.get(("s", 2)) == "v2"


# ---------------------------------------------------------------- 查询词
def test_cn_season_uses_chinese_numerals():
    """实测「末日地堡 第2季」搜到 0 条、「第二季」搜到 8 条——必须用中文数字."""
    from mediafans.agent import cn_season

    assert cn_season(1) == "一"
    assert cn_season(2) == "二"
    assert cn_season(10) == "十"
    assert cn_season(11) == "十一"


def test_search_queries_covers_multiple_spellings():
    from mediafans.agent import search_queries

    qs = search_queries("末日地堡", "Silo", 2)
    assert "末日地堡" in qs                    # 不带季号的命中最多
    assert "末日地堡 第二季" in qs              # 中文数字，不是「第2季」
    assert "Silo" in qs                        # 英文原名
    assert "Silo S02" in qs
    assert not any("第2季" in q for q in qs)


def test_search_queries_skips_season_for_s1_and_dupe_original():
    from mediafans.agent import search_queries

    # 第一季不加季号（「第一季」反而搜不到全集包）；原名和中文名相同就不重复列
    assert search_queries("剧名", "剧名", 1) == ["剧名", "剧名 S01"]
    assert search_queries("剧名", "", None) == ["剧名"]


def test_multi_search_merges_and_dedupes():
    from mediafans.agent import multi_search

    def fn(kw, netdisk=None):
        return ({"末日地堡": [_link("a"), _link("b")],
                 "Silo": [_link("b?x=1"), _link("c")]}.get(kw, []), [])

    merged = multi_search(fn, ["末日地堡", "Silo"])
    assert sorted(l.url for l in merged) == ["a", "b", "c"]    # b 去重


def test_multi_search_survives_one_failing_query():
    from mediafans.agent import multi_search

    def fn(kw, netdisk=None):
        if kw == "boom":
            raise RuntimeError("挂了")
        return ([_link("ok")], [])

    assert [l.url for l in multi_search(fn, ["boom", "good"])] == ["ok"]


def test_build_series_probes_more_when_the_first_wave_is_all_dead():
    """搜索源返回的前几条经常是死链。

    实测「异人之下」前 6 条全是「好友已取消了分享」，只探一批就会得到
    「0 个资源可用」，功能等于没有。
    """
    dead = {f"d{i}" for i in range(6)}
    drive = FakeDrive(
        local={"/MediaFans/末日地堡": []},
        shares={"good": {"0": [_f(f"Silo.S02E{i:02d}.1080p.mkv", fid=f"g{i}")
                               for i in (1, 2, 3)]}},
        dead=dead)
    links = [_link(f"d{i}") for i in range(6)] + [_link("good")]
    view = build_series(lambda: drive, lambda kw, netdisk=None: (links, []),
                        FakeTmdb(3), 1, 2, probe_top=6)
    d = view.as_dict()
    assert d["counts"]["available"] == 3, "第一批全死就该再探下一批"
    assert view.dead == 6 and view.probed == 1


def test_build_series_stops_at_the_first_wave_when_it_covers_everything():
    """够用就停——正常情况下不该白等后面几批."""
    drive = FakeDrive(
        local={"/MediaFans/末日地堡": []},
        shares={"a": {"0": [_f(f"Silo.S02E{i:02d}.1080p.mkv", fid=f"a{i}")
                            for i in (1, 2, 3)]},
                "b": {"0": [_f(f"Silo.S02E{i:02d}.720p.mkv", fid=f"b{i}")
                            for i in (1, 2, 3)]},
                "later": {"0": [_f("Silo.S02E01.2160p.mkv", fid="z1")]}},
    )
    opened = []
    real_open = drive.open_share

    def spy(url, passcode=""):
        opened.append(url)
        return real_open(url, passcode)

    drive.open_share = spy
    links = [_link("a"), _link("b")] + [_link("later")] * 8
    build_series(lambda: drive, lambda kw, netdisk=None: (links, []),
                 FakeTmdb(3), 1, 2, probe_top=2)
    assert opened == ["a", "b"], "两个来源就够挑了，不该再探后面的"


def test_copies_rank_recognized_video_above_higher_resolution():
    """「画质最高」不等于「最该先播」。

    网盘没认成视频的那些根本没有转码档（夸克实测：一批 .mkv 被标成 image/png，
    `/file` streaming 回「21005 not video」），只剩原画一档，浏览器碰上
    HEVC/DTS-HD 就是黑屏。哪怕它标着 2160p，也该让位给有转码档的 1080p。
    """
    from mediafans.agent import EpisodeRow, LocalFile

    row = EpisodeRow(episode=1)
    row.add_copy(LocalFile(path="/d/a.mkv", name="a.mkv", size=9, height=2160,
                           category="image"))
    row.add_copy(LocalFile(path="/d/b.mp4", name="b.mp4", size=3, height=1080,
                           category="video"))
    assert [c.name for c in row.copies] == ["b.mp4", "a.mkv"]
    assert row.local.name == "b.mp4"          # 主副本跟着换


def test_unknown_category_is_treated_as_playable():
    """网盘没说类型时按能转码算——宁可少排序，也别误伤。"""
    from mediafans.agent import LocalFile

    assert LocalFile(path="/d/a.mp4", name="a", size=1).transcodable is True
    assert LocalFile(path="/d/a.mp4", name="a", size=1,
                     category="video").transcodable is True
    assert LocalFile(path="/d/a.mkv", name="a", size=1,
                     category="image").transcodable is False


def test_scan_local_carries_category_through():
    from mediafans.agent import scan_local
    from mediafans.models import DriveFile

    class D:
        def resolve_path(self, p):
            return "fid"

        def list_files(self, fid):
            return [DriveFile(fid="1", name="Show.S01E01.2160p.mkv", size=9,
                              category="image"),
                    DriveFile(fid="2", name="Show.S01E01.1080p.mp4", size=3,
                              category="video")]

    got = scan_local(D(), "/d", 1)[1]
    assert [c.category for c in got] == ["video", "image"]
