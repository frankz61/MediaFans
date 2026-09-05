"""同名不同作的区分。

真实案例：《一人之下》是动画（2016，6 季 94 集），真人版叫《异人之下》
（2023 剧版 27 集 / 2024 影版 / 2025 第二季）。两者连分季名都撞——
动画第 5 季「决战·碧游村篇」，真人剧第 2 季「决战！碧游村」。

而且改查询词治不了：实测搜到的分享里有
「异人之下 第二季 / 决战碧游村 (2025) 更新13 完结 附第一季 动漫全系列」——
一份分享里同时装着真人剧和整套动画，集号完全重叠。
下面这些路径形状都取自实际搜索结果。
"""

import pytest

from mediafans.agent.identity import (
    Work, belongs, kind_verdict, share_matches, strip_tech, title_verdict,
)

LIVE = Work(titles=["异人之下"], animation=False)     # 真人剧
ANIME = Work(titles=["一人之下"], animation=True)     # 动画


# ---------------------------------------------------------------- 有没有片名
@pytest.mark.parametrize("name", ["01.mp4", "12x.mkv", "E05.mp4", "第二季", "第五季"])
def test_bare_episode_names_carry_no_title(name):
    """`01.mp4`、`第二季` 这种在中文分享里满地都是。

    把它们当成「有片名但对不上」，会把 `异人之下（2023）全27集 4K/01.mp4`
    这种正片整份误杀。
    """
    assert title_verdict(name, ["异人之下"]) is None


def test_real_directory_name_carries_a_title():
    assert title_verdict("异人之下（2023）全27集 4K", ["异人之下"]) is True
    assert title_verdict("一人之下 第五季", ["异人之下"]) is False


def test_chinese_numeral_seasons_are_stripped():
    """中文数字季号不剥掉，`第二季/` 目录会被当成对不上的片名."""
    assert strip_tech("第二季") == "" and strip_tech("第十三集") == ""


# ---------------------------------------------------------------- 类型标记
def test_kind_marks():
    assert kind_verdict("动漫全系列", animation=True) is True
    assert kind_verdict("动漫全系列", animation=False) is False
    assert kind_verdict("异人之下 剧版完整 4K", animation=False) is True
    assert kind_verdict("异人之下 剧版完整 4K", animation=True) is False
    assert kind_verdict("第二季", animation=False) is None      # 没标记
    assert kind_verdict("动漫全系列", animation=None) is None    # 不知道目标类型


def test_both_marks_in_one_segment_decide_nothing():
    """`异人之下 剧版 附动漫全系列` 是打包目录，说明不了具体文件属于哪边."""
    assert kind_verdict("异人之下 剧版 附动漫全系列", animation=False) is None


# ---------------------------------------------------------------- 逐段判断
def test_bundled_share_anime_subtree_is_rejected_for_live_target():
    """一份分享同时装着真人剧和整套动画，靠路径把动画那半边拦掉."""
    path = "异人之下 第二季 附第一季及动漫全系列/动漫全系列/一人之下 第一季/E05.mp4"
    assert belongs(path, LIVE) == "动画版"
    assert belongs(path, ANIME) is None


def test_a_note_in_the_share_title_does_not_taint_the_root_files():
    """分享标题写着「附动漫全系列」，那说的是旁边那个目录。

    整条路径拼起来判会把根下这个真人剧正片当动画拒掉。
    """
    assert belongs("异人之下 第二季 附动漫全系列/E07.mp4", LIVE) is None
    assert belongs("异人之下 第二季 附动漫全系列/第二季/E07.mp4", LIVE) is None


def test_title_can_only_admit_never_reject():
    """片名对不上不能单独拒——中文分享的文件名几乎从不带片名."""
    for name in ("异人之下（2023）全27集 4K/01.mp4",
                 "Y .异.R.之.下. 4K[臻彩]/4K 高码.mkv",
                 "异人之下之决战x碧游村/12x.mkv"):
        assert belongs(name, LIVE) is None, name


def test_live_action_marks_reject_the_anime_target():
    assert belongs("异人之下 剧版完整 4K/E12.mp4", ANIME) == "真人版"


def test_unknown_kind_does_not_reject():
    """TMDB 没给类型时（work.animation=None）不按类型拦."""
    w = Work(titles=["异人之下"], animation=None)
    assert belongs("动漫全系列/一人之下 第一季/E05.mp4", w) is None


# ---------------------------------------------------------------- 分享粒度
def test_share_that_never_mentions_the_work():
    """一份分享从头到尾没提过要找的片名，多半是同名的另一部."""
    anime_paths = ["一人之下 第五季/EP01.mkv", "一人之下 第五季/EP02.mkv"]
    assert share_matches(anime_paths, ["异人之下"]) is False
    assert share_matches(anime_paths, ["一人之下"]) is True


def test_share_matches_on_any_layer():
    """片名常常只写在分享标题或目录名上，里面的文件叫 01.mp4."""
    assert share_matches(["异人之下（2023）全27集 4K/01.mp4"], ["异人之下"]) is True


# ---------------------------------------------------------------- 接到来源索引上
class _Link:
    def __init__(self, title):
        self._t, self.url, self.passcode = title, "u/" + title, ""

    def display_title(self):
        return self._t


def _probe(title, files):
    from mediafans.agent import ProbeFile, ProbeResult

    pr = ProbeResult(link=_Link(title), ok=True)
    for crumb, name, ep in files:
        pr.files.append(ProbeFile(fid=name, name=name, size=10 ** 9,
                                  share_fid_token="t", crumb=crumb, episode=ep))
    return pr


def test_index_sources_drops_the_anime_half_of_a_bundle():
    from mediafans.agent import index_sources

    pr = _probe("异人之下 第二季 附第一季及动漫全系列", [
        ("异人之下 第二季", "E01.mp4", 1),
        ("动漫全系列/一人之下 第一季", "E01.mp4", 1),
        ("动漫全系列/一人之下 第一季", "E02.mp4", 2),
    ])
    dropped = []
    by_ep = index_sources([pr], None, LIVE, dropped)
    assert [s.file.crumb for s in by_ep[1]] == ["异人之下 第二季"]
    assert 2 not in by_ep and len(dropped) == 2


def test_index_sources_drops_a_whole_foreign_share():
    from mediafans.agent import index_sources

    good = _probe("异人之下 全27集", [("异人之下（2023）4K", "01.mp4", 1)])
    bad = _probe("一人之下 第五季", [("一人之下 第五季", "01.mp4", 1)])
    dropped = []
    by_ep = index_sources([good, bad], None, LIVE, dropped)
    assert len(by_ep[1]) == 1 and by_ep[1][0].share_title == "异人之下 全27集"
    assert any("整份是别的作品" in d for d in dropped)


def test_index_sources_keeps_everything_when_no_share_names_the_work():
    """发布组用 TMDB 里没有的别名时，一份都对不上——这时不能全拒掉."""
    from mediafans.agent import index_sources

    pr = _probe("The Knockout 全39集", [("The.Knockout.2023", "01.mp4", 1)])
    dropped = []
    by_ep = index_sources([pr], None, Work(titles=["狂飙"], animation=False), dropped)
    assert len(by_ep[1]) == 1 and not dropped


def test_index_sources_without_a_work_is_unfiltered():
    from mediafans.agent import index_sources

    pr = _probe("随便什么", [("一人之下 第五季", "01.mp4", 1)])
    assert len(index_sources([pr], None)[1]) == 1


def test_title_rejection_is_scoped_to_local_directories():
    """两种粒度对「片名对不上」的处理必须不同。

    本地目录里是转存下来的完整发布名，片名可靠——《绅士们》混进「末日地堡」
    目录就得靠它拒。网盘分享里文件名是裸的，同一条规则会把正片整份误杀。
    """
    silo = Work(titles=["末日地堡", "Silo"], animation=None)
    gentlemen = "The.Gentlemen.2024.S01E02.2160p.Web.DV.HDR.H265.mkv"
    assert belongs(gentlemen, silo, title_can_reject=True) == "别的作品"
    assert belongs(gentlemen, silo) is None      # 分享粒度不据此拒


# ---------------------------------------------------------------- 合集里蹭进来的
def test_short_drama_is_not_the_show():
    """短剧合集常有一层恰好带上热播剧的名字，里面按「第N集」编号，集号完全重叠。

    实测搜末日地堡时，「进击的巨人之末日地堡（60集）Ai短剧」整份被当成了来源，
    30MB 一集的竖屏短剧排进了正片的集列表。TMDB 上的正片不会是短剧。
    """
    from mediafans.agent import Work, belongs

    w = Work(titles=["末日地堡", "Silo"], animation=False)
    assert belongs("15/进击的巨人之末日地堡（60集）Ai短剧/第1集.mp4", w) == "短剧"
    assert belongs("M 末日地堡（羊毛战记）/第3季/Silo.S03E01.mkv", w) is None


def test_require_title_rejects_unrelated_files_in_a_mixed_share():
    """分享粒度的判断太松：合集里只要有一层带剧名，整份文件都会拿到资格。

    实测一个短剧合集因此让「十八年后被认亲，我被太子爹爹宠上天」也进了
    末日地堡的集列表——那是一部毫不相干的剧，只是文件叫「第2集.mp4」。
    """
    from mediafans.agent import Work, belongs

    w = Work(titles=["末日地堡", "Silo"], animation=False)
    unrelated = "某合集/14/十八年后被认亲，我被太子爹爹宠上天（65集）/第2集.mp4"
    assert belongs(unrelated, w, require_title=True) == "路径里没提到本剧"
    assert belongs(unrelated, w, require_title=False) is None   # 老行为不变


def test_require_title_still_accepts_a_matching_ancestor():
    """片名写在目录上、文件名是裸编号，是最常见的形态，不能误杀."""
    from mediafans.agent import Work, belongs

    w = Work(titles=["异人之下"], animation=False)
    assert belongs("异人之下（2023）全27集 4K/01.mp4", w, require_title=True) is None


def test_require_title_is_off_when_titles_are_unusable():
    """发布组用 TMDB 没有的别名时片名信号不可用，这条规则不能生效，
    否则会把整份合法资源判成「没提到本剧」。"""
    from mediafans.agent import Work, belongs

    w = Work(titles=["狂飙"], animation=False)
    assert belongs("The.Knockout.S01E01.mkv", w,
                   trust_titles=False, require_title=True) is None
