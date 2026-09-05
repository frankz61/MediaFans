"""自动找片三层的测试：解析 / 探测 / 排序 / 编排。

重点盯住「避免空资源」这条主线——死链、空壳、纯广告的分享必须被排除掉，
而且这件事不能依赖 AI（AI 判断不了链接死活）。
"""

import httpx
import pytest

from mediafans.agent import (
    AutoResult, LLMPicker, ProbeResult, auto_fetch, parse_episode,
    probe_share, rank, score_one, summarize, title_match,
)
from mediafans.models import DriveFile, ShareLink


# ---------------------------------------------------------------- 文件名解析
@pytest.mark.parametrize("name,season,episode", [
    ("庆余年.S02E05.2160p.mkv", 2, 5),
    ("Show.S01EP12.1080p.mkv", 1, 12),
    ("Show.1x03.720p.avi", 1, 3),
    ("第1季第12集.mp4", 1, 12),
    ("漫长的季节 EP07.mkv", None, 7),
    ("第08集.mkv", None, 8),
    ("Some.Movie.2024.2160p.mkv", None, None),   # 电影没有集号，不能瞎猜
    ("花絮预告.mp4", None, None),
])
def test_parse_season_episode(name, season, episode):
    info = parse_episode(name)
    assert info.season == season
    assert info.episode == episode


def test_parse_quality_and_source():
    i = parse_episode("Show.S01E01.2160p.WEB-DL.内封简中.mkv")
    assert i.height == 2160 and i.source == "webdl" and i.chinese_sub is True
    assert parse_episode("Show.1080p.REMUX.mkv").source == "remux"
    assert parse_episode("Show.720p.HDTV.mkv").height == 720
    assert parse_episode("Show.枪版.mkv").source == "cam"


def test_year_is_not_mistaken_for_episode():
    """2024 这种年份不能被当成集号——错一个集号整季完整度判断就全错."""
    assert parse_episode("Some.Show.2024.1080p.WEB-DL.mkv").episode is None


def test_summarize_detects_missing_episodes():
    s = summarize(["S01E01 1080p WEB-DL.mkv", "S01E02 1080p WEB-DL.mkv",
                   "S01E04 1080p WEB-DL.mkv", "预告片.mp4"])
    assert s.episodes == [1, 2, 4]
    assert s.count == 3
    assert s.contiguous is False
    assert s.missing == [3]
    assert s.unparsed == 1              # 预告片没集号
    assert s.height == 1080 and s.source == "webdl"


def test_summarize_takes_modal_quality_not_max():
    """一堆 1080p 里混个 4K 花絮，不该把整季算成 4K."""
    s = summarize([f"S01E{i:02d}.1080p.mkv" for i in range(1, 6)] + ["彩蛋.2160p.mkv"])
    assert s.height == 1080


# ---------------------------------------------------------------- 探测
class FakeDrive:
    """假网盘：按 layout 返回分享内容，dead 里的链接打不开."""

    save_dir = "/MediaFans"

    def __init__(self, layout=None, dead=(), raise_on_list=False):
        self.layout = layout or {}
        self.dead = set(dead)
        self.raise_on_list = raise_on_list
        self.saved = None

    def open_share(self, url, passcode=""):
        if url in self.dead:
            raise RuntimeError("好友已取消了分享")
        return {"url": url}

    def list_share_files(self, ctx, dir_fid="0"):
        if self.raise_on_list:
            raise RuntimeError("列目录失败")
        return list(self.layout.get(ctx["url"], {}).get(dir_fid, []))

    def save_share_files(self, ctx, files, to_dir):
        self.saved = {"dir": to_dir, "names": [f.name for f in files]}
        return [f"new{i}" for i in range(len(files))]


def _f(name, is_dir=False, size=2 * 1024 ** 3, fid=None):
    return DriveFile(fid=fid or name, name=name, is_dir=is_dir, size=size,
                     share_fid_token="TK")


def _link(url, title=""):
    return ShareLink(url=url, netdisk="quark", title=title or url)


def test_probe_rejects_dead_share():
    drive = FakeDrive(dead={"u1"})
    r = probe_share(drive, _link("u1"))
    assert r.ok is False
    assert "取消了分享" in r.error


def test_probe_rejects_share_with_no_playable_files():
    """只有广告 txt 的分享必须判定为无效——这就是「空资源」的典型形态."""
    drive = FakeDrive({"u1": {"0": [
        _f("【请保存观看】不保存只能看两分钟", is_dir=True),
        _f("免费享全网资源.docx", size=1000),
        _f("说明.txt", size=100),
    ]}})
    r = probe_share(drive, _link("u1"))
    assert r.ok is False
    assert r.playable == 0
    assert "没有可播放文件" in r.error
    assert r.junk >= 2


def test_probe_descends_and_finds_videos():
    drive = FakeDrive({"u1": {
        "0": [_f("庆余年 第二季", is_dir=True, fid="D1")],
        "D1": [_f("4K原盘", is_dir=True, fid="D2"), _f("说明.txt", size=10)],
        "D2": [_f("庆余年.S02E01.2160p.WEB-DL.内封简中.mkv"),
               _f("庆余年.S02E02.2160p.WEB-DL.内封简中.mkv"),
               _f("广告.txt", size=10)],
    }})
    r = probe_share(drive, _link("u1"))
    assert r.ok is True
    assert r.playable == 2
    assert r.dir_fid == "D2"                       # 转存要存视频所在的那层
    assert r.summary.episodes == [1, 2]
    assert r.summary.height == 2160
    assert r.total_size == 4 * 1024 ** 3


def test_probe_skips_junk_directories():
    """引流目录不该被钻进去浪费请求."""
    drive = FakeDrive({"u1": {
        "0": [_f("【限时】新用户领1T容量", is_dir=True, fid="AD"),
              _f("正片", is_dir=True, fid="D1")],
        "AD": [_f("广告.mp4")],
        "D1": [_f("Show.S01E01.1080p.mkv")],
    }})
    r = probe_share(drive, _link("u1"))
    assert r.playable == 1                          # 广告目录里的 mp4 没被算进来
    assert r.names == ["Show.S01E01.1080p.mkv"]


def test_probe_survives_listing_error():
    r = probe_share(FakeDrive({"u1": {}}, raise_on_list=True), _link("u1"))
    assert r.ok is False and r.playable == 0


# ---------------------------------------------------------------- 排序
def _probe(title, names, junk=0, size=None):
    r = ProbeResult(link=_link("u" + title, title))
    r.ok, r.names, r.playable = True, names, len(names)
    r.junk = junk
    r.total_size = size if size is not None else len(names) * 2 * 1024 ** 3
    r.summary = summarize(names)
    return r


def test_title_match():
    assert title_match("庆余年", "庆余年 第二季 4K") == 1.0
    assert title_match("The Last of Us", "The.Last.of.Us.S01.1080p") > 0.9
    assert title_match("庆余年", "三体 全集") < 0.34


def test_ranking_prefers_complete_season():
    full = _probe("剧A 全集", [f"剧A.S01E{i:02d}.1080p.WEB-DL.中字.mkv" for i in range(1, 13)])
    partial = _probe("剧A 前三集", [f"剧A.S01E{i:02d}.1080p.WEB-DL.中字.mkv" for i in range(1, 4)])
    ranked = rank([partial, full], "剧A", want_episodes=12)
    assert ranked[0].probe is full


def test_ranking_penalizes_missing_episodes():
    whole = _probe("剧B", [f"剧B.S01E{i:02d}.1080p.mkv" for i in range(1, 11)])
    gapped = _probe("剧B", [f"剧B.S01E{i:02d}.1080p.mkv" for i in (1, 2, 3, 7, 8, 9, 10, 11, 12, 13)])
    assert score_one(whole, "剧B").score > score_one(gapped, "剧B").score


def test_ranking_relevance_beats_quality():
    """画质再好，不是要找的剧就不能选——相关性优先."""
    wrong = _probe("完全不相干的纪录片", ["Doc.S01E01.2160p.REMUX.中字.mkv"] * 1)
    right = _probe("庆余年 第二季", ["庆余年.S02E01.720p.HDTV.mkv"])
    ranked = rank([wrong, right], "庆余年")
    assert ranked[0].probe is right


def test_ranking_drops_cam_rips():
    cam = _probe("剧C 枪版", ["剧C.S01E01.枪版.mp4"])
    web = _probe("剧C", ["剧C.S01E01.1080p.WEB-DL.mkv"])
    assert score_one(web, "剧C").score > score_one(cam, "剧C").score


def test_ranking_penalizes_tiny_episodes():
    """单集才几十兆的多半是残片/预告合集."""
    tiny = _probe("剧D", [f"剧D.S01E{i:02d}.1080p.mkv" for i in range(1, 11)],
                  size=10 * 1024 ** 2)
    normal = _probe("剧D", [f"剧D.S01E{i:02d}.1080p.mkv" for i in range(1, 11)])
    assert score_one(normal, "剧D").score > score_one(tiny, "剧D").score


def test_unavailable_probe_ranks_last():
    bad = ProbeResult(link=_link("u9", "死链"))
    bad.ok, bad.error = False, "分享已失效"
    good = _probe("剧E", ["剧E.S01E01.1080p.mkv"])
    ranked = rank([bad, good], "剧E")
    assert ranked[0].probe is good
    assert ranked[-1].score < 0


# ---------------------------------------------------------------- 编排
def _search_fn(links, errors=()):
    def fn(kw, netdisk=None):
        out = [l for l in links if not netdisk or l.netdisk == netdisk]
        return out, list(errors)
    return fn


def test_auto_fetch_end_to_end_saves_best():
    layout = {
        "good": {"0": [_f("正片", is_dir=True, fid="D")],
                 "D": [_f(f"庆余年.S02E{i:02d}.2160p.WEB-DL.内封简中.mkv")
                       for i in range(1, 13)]},
        "thin": {"0": [_f("庆余年.S02E01.720p.HDTV.mkv")]},
    }
    drive = FakeDrive(layout, dead={"dead"})
    links = [_link("dead", "庆余年 第二季 4K 全集"),
             _link("thin", "庆余年 第二季 抢先版"),
             _link("good", "庆余年 第二季 2160p 全集")]
    steps = []
    res = auto_fetch(lambda: drive, _search_fn(links), "庆余年",
                     season=2, episodes=12, save_dir="/MediaFans",
                     on_step=lambda st, d: steps.append((st, d["message"])))
    assert res.ok is True
    assert res.picked.probe.link.url == "good"
    assert res.saved == 12
    assert res.saved_dir == "/MediaFans/庆余年"
    assert drive.saved["dir"] == "/MediaFans/庆余年"
    # 死链必须被明确排除掉，而且要让用户看见
    assert any("排除" in m for _, m in steps)
    assert {s for s, _ in steps} >= {"search", "probe", "rank", "save", "done"}


def test_auto_fetch_reports_when_everything_is_dead():
    drive = FakeDrive({}, dead={"a", "b"})
    res = auto_fetch(lambda: drive, _search_fn([_link("a"), _link("b")]), "某剧")
    assert res.ok is False
    assert "全部无效" in res.error
    assert res.saved == 0


def test_auto_fetch_reports_when_nothing_found():
    res = auto_fetch(lambda: FakeDrive(), _search_fn([]), "查无此剧")
    assert res.ok is False and "没有搜到" in res.error


def test_auto_fetch_filters_non_quark():
    """只有夸克能转存，别的网盘不该进探测环节浪费时间."""
    drive = FakeDrive({"q": {"0": [_f("剧.S01E01.1080p.mkv")]}})
    links = [ShareLink(url="ali", netdisk="aliyun", title="剧"),
             _link("q", "剧")]
    res = auto_fetch(lambda: drive, _search_fn(links), "剧")
    assert res.ok and res.picked.probe.link.url == "q"


def test_auto_fetch_can_skip_saving():
    drive = FakeDrive({"q": {"0": [_f("剧.S01E01.1080p.mkv")]}})
    res = auto_fetch(lambda: drive, _search_fn([_link("q", "剧")]), "剧", do_save=False)
    assert res.ok and res.saved == 0 and drive.saved is None


# ---------------------------------------------------------------- AI 裁决
class FakeAnthropic:
    def __init__(self, payload=None, boom=False):
        self.payload, self.boom = payload, boom
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("api down")
        import json as _json

        class B:
            type = "text"
            text = _json.dumps(self.payload)
        class R:
            content = [B()]
        return R()


def test_llm_picker_overrides_deterministic_choice():
    """AI 的价值在于识别「评分高但其实不是这部剧」的情况."""
    layout = {
        "a": {"0": [_f(f"某纪录片.S01E{i:02d}.2160p.REMUX.中字.mkv") for i in range(1, 13)]},
        "b": {"0": [_f("庆余年.S02E01.1080p.WEB-DL.mkv")]},
    }
    drive = FakeDrive(layout)
    picker = LLMPicker(client=FakeAnthropic(
        {"index": 1, "reject": False, "reason": "候选0是同名纪录片，不是剧集",
         "confidence": "high"}))
    res = auto_fetch(lambda: drive, _search_fn([_link("a", "庆余年 纪录片 4K 全12集"),
                                                _link("b", "庆余年 第二季")]),
                     "庆余年", picker=picker, do_save=False)
    assert res.ai_used is True
    assert res.picked.probe.link.url == "b"
    assert "纪录片" in res.verdict.reason
    # 模型只看到事实摘要，不该看到 URL
    sent = picker._client.calls[0]["messages"][0]["content"]
    assert "http" not in sent and "url" not in sent.lower()


def test_llm_reject_stops_the_pipeline():
    drive = FakeDrive({"a": {"0": [_f("别的剧.S01E01.1080p.mkv")]},
                       "b": {"0": [_f("还是别的剧.S01E01.1080p.mkv")]}})
    picker = LLMPicker(client=FakeAnthropic(
        {"index": -1, "reject": True, "reason": "都不是这部剧", "confidence": "high"}))
    res = auto_fetch(lambda: drive, _search_fn([_link("a"), _link("b")]),
                     "想看的剧", picker=picker)
    assert res.ok is False
    assert "AI 判断没有匹配" in res.error
    assert res.saved == 0


def test_llm_failure_falls_back_to_ranking():
    """AI 挂了整条链路必须照常工作——AI 是增强不是依赖."""
    drive = FakeDrive({"a": {"0": [_f("剧.S01E01.1080p.WEB-DL.中字.mkv")]},
                       "b": {"0": [_f("剧.S01E01.480p.mkv")]}})
    picker = LLMPicker(client=FakeAnthropic(boom=True))
    res = auto_fetch(lambda: drive, _search_fn([_link("a", "剧"), _link("b", "剧")]),
                     "剧", picker=picker, do_save=False)
    assert res.ok is True
    assert res.ai_used is False
    assert "未给出有效判断" in res.ai_note
    assert res.picked.probe.link.url == "a"      # 退回评分，仍然选对


def test_llm_out_of_range_index_is_ignored():
    picker = LLMPicker(client=FakeAnthropic(
        {"index": 99, "reject": False, "reason": "x", "confidence": "low"}))
    assert picker.pick({"标题": "x"}, [{"title": "a"}]) is None


def test_llm_unavailable_without_sdk():
    p = LLMPicker(api_key="")
    p._client = None
    p._why_unavailable = "未安装 anthropic"
    assert p.available is False
    assert p.pick({}, [{"title": "a"}]) is None


# ---------------------------------------------------------------- 完整度基准
def test_episode_yardstick_returns_all_season_lengths_when_unspecified():
    """网盘分享基本都是单季打包。拿全剧总集数当分母，一个完整单季只算 25% 完整；
    拿最长的一季当分母，短的那些季又会被误判成残缺。所以没指定季时返回各季集数。"""
    from mediafans.agent import episode_yardstick

    detail = {"total_episodes": 32, "seasons": [
        {"season": 1, "episodes": 8}, {"season": 2, "episodes": 8},
        {"season": 3, "episodes": 8}, {"season": 4, "episodes": 10}]}
    assert episode_yardstick(detail, None) == [8, 10]   # 去重后的各季集数
    assert episode_yardstick(detail, 2) == 8            # 指定季就用那一季
    assert episode_yardstick(detail, 9) == 0            # 不存在的季


def test_short_season_not_penalized_when_other_seasons_are_longer():
    """各季长度不一时，拿到完整的短季不该被判残缺——这曾让好资源全被拒."""
    yard = [8, 46]                       # 比如第一季 46 集、第二季 8 集
    short_full = _probe("剧 第二季全集",
                        [f"剧.S02E{i:02d}.1080p.WEB-DL.中字.mkv" for i in range(1, 9)])
    sc = score_one(short_full, "剧", want_episodes=yard)
    assert "集数 8/8" in "".join(sc.reasons)
    assert "全集" in "".join(sc.reasons)


def test_episode_yardstick_falls_back_to_total():
    from mediafans.agent import episode_yardstick

    assert episode_yardstick({"total_episodes": 12, "seasons": []}, None) == 12


def test_full_single_season_counts_as_complete():
    """8 集的季拿到 8 集就该算全集，不该因为全剧有 32 集而被判不完整."""
    from mediafans.agent import episode_yardstick

    detail = {"total_episodes": 32,
              "seasons": [{"season": i, "episodes": 8} for i in range(1, 5)]}
    yard = episode_yardstick(detail, None)      # [8]
    full = _probe("剧", [f"剧.S03E{i:02d}.2160p.WEB-DL.中字.mkv" for i in range(1, 9)])
    scored = score_one(full, "剧", want_episodes=yard)
    assert "全集" in "".join(scored.reasons)


def test_multi_season_pack_is_flagged():
    """多季混装时集号是跨季合并的，完整度判断不可靠，必须标出来."""
    mixed = _probe("剧 全四季合集",
                   [f"剧.S0{ss}E{e:02d}.1080p.mkv" for ss in (1, 2) for e in range(1, 5)])
    sc = score_one(mixed, "剧", want_episodes=8)
    assert any("多季合集" in r for r in sc.reasons)
    assert mixed.brief()["multi_season"] is True

    single = _probe("剧 第一季", [f"剧.S01E{e:02d}.1080p.mkv" for e in range(1, 5)])
    assert single.brief()["multi_season"] is False


def test_save_count_mismatch_is_explained():
    """网盘对已存在的文件会去重，请求数和落地数对不上要说清楚，不能让人以为丢文件了."""
    class Dedup(FakeDrive):
        def save_share_files(self, ctx, files, to_dir):
            self.saved = {"dir": to_dir, "names": [f.name for f in files]}
            return ["only-one"]          # 网盘只回了 1 个

    drive = Dedup({"q": {"0": [_f(f"剧.S01E{i:02d}.1080p.mkv") for i in range(1, 4)]}})
    steps = []
    res = auto_fetch(lambda: drive, _search_fn([_link("q", "剧")]), "剧",
                     on_step=lambda st, d: steps.append(d["message"]))
    assert res.saved == 1 and res.requested == 3
    assert any("请求 3 个" in m for m in steps)


def test_skips_saving_when_already_in_drive():
    """「点一下就好」的功能会被重复点。不查一下的话网盘会存出 xxx(1).mkv 副本."""
    class WithExisting(FakeDrive):
        def resolve_path(self, path):
            return "EXIST" if path == "/MediaFans/剧" else None

        def list_files(self, fid):
            return [_f("剧.S01E01.1080p.mkv"), _f("剧.S01E02.1080p.mkv")]

    drive = WithExisting({"q": {"0": [_f("剧.S01E01.1080p.mkv")]}})
    steps = []
    res = auto_fetch(lambda: drive, _search_fn([_link("q", "剧")]), "剧",
                     save_dir="/MediaFans",
                     on_step=lambda st, d: steps.append(d["message"]))
    assert res.already == 2
    assert res.saved == 0
    assert drive.saved is None                       # 没有重复转存
    assert res.saved_dir == "/MediaFans/剧"
    assert res.ok is True                            # 已经有了也算成功
    assert any("已经有" in m for m in steps)


def test_force_overrides_existing_check():
    class WithExisting(FakeDrive):
        def resolve_path(self, path):
            return "EXIST"

        def list_files(self, fid):
            return [_f("旧文件.mkv")]

    drive = WithExisting({"q": {"0": [_f("剧.S01E01.1080p.mkv")]}})
    res = auto_fetch(lambda: drive, _search_fn([_link("q", "剧")]), "剧",
                     save_dir="/MediaFans", force=True)
    assert res.saved == 1 and drive.saved is not None


@pytest.mark.parametrize("name,height", [
    ("silo.s02e08.1080p中英字幕.mp4", 1080),      # 实测漏过：中文紧跟在 p 后面
    ("剧.S01E01.2160p国语中字.mkv", 2160),
    ("剧.4K高码.mkv", 2160),
    ("Show.S01E01.1080p.WEB-DL.mkv", 1080),
    ("Show.720p中字.mkv", 720),
    ("不含画质标记.mkv", 0),
    ("Show.x1080pixel.mkv", 0),                  # 不能把 1080pixel 当画质
])
def test_quality_parsed_next_to_cjk(name, height):
    """中文字符在正则里算 \w，所以不能用 \b 界定画质标记的边界."""
    assert parse_episode(name).height == height


# ---------------------------------------------------------------- 裸编号集号
def test_bare_numbering_is_decided_per_batch():
    """`01.mp4` 单看可能是任何东西，一个目录里十几个纯编号视频就是集号。

    中文网盘分享里这是最常见的命名：实测《异人之下之决战！碧游村》整季 13 集
    是 `01x.mkv`…`13x.mkv`，剧版 27 集是 `01.mp4`…`27.mp4`，
    parse_episode 一个都认不出，结果是「5 个资源可用，但一集都补不上」。
    """
    from mediafans.agent import infer_bare_numbering

    got = infer_bare_numbering(["01x.mkv", "02x.mkv", "03x.ts", "13x.mkv"])
    assert got == {"01x.mkv": 1, "02x.mkv": 2, "03x.ts": 3, "13x.mkv": 13}


def test_bare_numbering_needs_enough_files():
    """一两个数字说明不了什么，别拿它当集号."""
    from mediafans.agent import infer_bare_numbering

    assert infer_bare_numbering(["01.mp4", "02.mp4"]) == {}


def test_bare_numbering_ignores_quality_names():
    """`4K 高码.mkv`、`2024.IMAX.2160p.mkv` 里的数字不是集号."""
    from mediafans.agent import infer_bare_numbering

    assert infer_bare_numbering(
        ["4K 高码.mkv", "4K.mkv", "2024.IMAX.2160p.WEB-DL.mkv", "1080P.mkv"]) == {}


def test_bare_numbering_rejects_duplicates():
    """编号撞了说明那些数字是别的意思（画质、分P），不是集号."""
    from mediafans.agent import infer_bare_numbering

    assert infer_bare_numbering(["01.mp4", "01x.mkv", "02.mp4", "02x.mkv"]) == {}


def test_bare_episode_bounds():
    from mediafans.agent import bare_episode

    assert bare_episode("5.ts") == 5
    assert bare_episode("300.mp4") is None          # 集号不会有 300
    assert bare_episode("异人之下HD1080P.国语中字.mp4") is None
