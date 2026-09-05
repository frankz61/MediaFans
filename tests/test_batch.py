"""一键转存整季。

逐集各挑各的最高画质会拼出大杂烩——实测用户网盘里的末日地堡第二季就是
E01-E05 2160p、E06 掉到 1080p、E07 存了两份。而且 27 集意味着开 27 次分享、
发 27 次转存请求。所以按「一个来源能覆盖多少集」贪心分组。
"""

import pytest

from mediafans.agent import (
    BatchGroup, EpisodeRow, LocalFile, SourceFile, fetch_batch, plan_batch,
)
from mediafans.agent.probe import ProbeFile
from mediafans.models import DriveFile


def _pf(name, fid=None, height=1080, size=2 * 1024 ** 3, dir_fid="0"):
    return ProbeFile(fid=fid or name, name=name, size=size,
                     share_fid_token="TK-" + (fid or name), dir_fid=dir_fid,
                     height=height)


def _src(url, ep, height=1080, size=2 * 1024 ** 3, dir_fid="0", title=""):
    return SourceFile(share_title=title or url, share_url=url, passcode="",
                      file=_pf(f"{url}-E{ep:02d}.mkv", height=height, size=size,
                               dir_fid=dir_fid))


def _rows(spec):
    """spec: {集号: (本地有没有, [SourceFile])}"""
    out = []
    for ep, (has_local, srcs) in sorted(spec.items()):
        r = EpisodeRow(episode=ep, title=f"第{ep}集")
        if has_local:
            r.local = LocalFile(path=f"/d/E{ep:02d}.mkv", name=f"E{ep:02d}.mkv", size=1)
        r.sources = list(srcs)
        out.append(r)
    return out


# ---------------------------------------------------------------- 分组
def test_one_source_covering_everything_is_one_group():
    """整季来自同一个分享时只开一次、只发一次转存请求."""
    rows = _rows({e: (False, [_src("a", e)]) for e in range(1, 11)})
    groups = plan_batch(rows)
    assert len(groups) == 1
    assert groups[0].episodes == list(range(1, 11))
    assert len(groups[0].files) == 10


def test_prefers_the_source_that_covers_the_most():
    """挑覆盖最多的那个，剩下的再找下一个——别拼出画质不一的大杂烩."""
    rows = _rows({
        1: (False, [_src("full", 1, 1080), _src("solo1", 1, 2160)]),
        2: (False, [_src("full", 2, 1080)]),
        3: (False, [_src("full", 3, 1080)]),
        4: (False, [_src("solo4", 4, 2160)]),
    })
    groups = plan_batch(rows)
    assert [g.share_url for g in groups] == ["full", "solo4"]
    assert groups[0].episodes == [1, 2, 3]     # E01 跟着大部队，不为了画质拆出去
    assert groups[1].episodes == [4]


def test_ties_are_broken_by_quality():
    rows = _rows({
        1: (False, [_src("hd", 1, 2160), _src("sd", 1, 720)]),
        2: (False, [_src("hd", 2, 2160), _src("sd", 2, 720)]),
    })
    groups = plan_batch(rows)
    assert len(groups) == 1 and groups[0].share_url == "hd"


def test_ties_are_deterministic():
    """同覆盖同画质时结果要稳定，不然两次点会存出不同的东西."""
    rows = _rows({e: (False, [_src("bbb", e), _src("aaa", e)]) for e in (1, 2)})
    assert [plan_batch(rows)[0].share_url for _ in range(5)] == ["bbb"] * 5


def test_already_saved_episodes_are_not_planned():
    rows = _rows({1: (True, [_src("a", 1)]), 2: (False, [_src("a", 2)])})
    groups = plan_batch(rows)
    assert groups[0].episodes == [2]


def test_episodes_without_sources_are_skipped():
    rows = _rows({1: (False, []), 2: (False, [_src("a", 2)])})
    assert [g.episodes for g in plan_batch(rows)] == [[2]]


def test_nothing_to_do_is_no_groups():
    assert plan_batch(_rows({1: (True, [_src("a", 1)])})) == []


def test_same_share_different_dirs_are_separate_groups():
    """一次转存只能取同一层目录里的文件，不同层必须分开."""
    rows = _rows({1: (False, [_src("a", 1, dir_fid="d1")]),
                  2: (False, [_src("a", 2, dir_fid="d2")])})
    groups = plan_batch(rows)
    assert len(groups) == 2
    assert {g.dir_fid for g in groups} == {"d1", "d2"}


def test_duplicate_episode_in_one_bucket_keeps_the_best():
    rows = _rows({1: (False, [_src("a", 1, 720), _src("a", 1, 2160)])})
    groups = plan_batch(rows)
    assert len(groups[0].files) == 1 and groups[0].files[0].height == 2160


# ---------------------------------------------------------------- 转存
class FakeDrive:
    def __init__(self, existing=(), dead=(), listing=None):
        self.existing = list(existing)
        self.dead = set(dead)
        self.listing = listing
        self.saves = []
        self.opens = []

    def resolve_path(self, path):
        return "fid:dir"

    def list_files(self, fid):
        return [DriveFile(fid=n, name=n, is_dir=False, size=1) for n in self.existing]

    def open_share(self, url, passcode=""):
        self.opens.append(url)
        if url in self.dead:
            raise RuntimeError("分享已失效")
        return {"url": url}

    def list_share_files(self, ctx, dir_fid="0"):
        if self.listing is not None:
            return list(self.listing)
        return [DriveFile(fid=f"{ctx['url']}-E{e:02d}.mkv",
                          name=f"{ctx['url']}-E{e:02d}.mkv", is_dir=False,
                          size=1, share_fid_token="t") for e in range(1, 30)]

    def save_share_files(self, ctx, files, to_dir):
        self.saves.append({"url": ctx["url"], "names": [f.name for f in files],
                           "dir": to_dir})
        return [f.fid for f in files]


def test_a_whole_season_is_one_save_call():
    """10 集来自同一个来源 → 开一次分享、发一次转存请求，不是 10 次."""
    drive = FakeDrive()
    groups = plan_batch(_rows({e: (False, [_src("a", e)]) for e in range(1, 11)}))
    res = fetch_batch(lambda: drive, groups, "/MediaFans/剧")
    assert len(drive.opens) == 1 and len(drive.saves) == 1
    assert len(drive.saves[0]["names"]) == 10
    assert res.saved == list(range(1, 11)) and not res.failed


def test_existing_files_are_skipped_not_duplicated():
    """「一键」天然会被重复点，不查一下网盘会存出一堆 xxx(1).mkv."""
    drive = FakeDrive(existing=["a-E01.mkv", "a-E02.mkv"])
    groups = plan_batch(_rows({e: (False, [_src("a", e)]) for e in (1, 2, 3)}))
    res = fetch_batch(lambda: drive, groups, "/MediaFans/剧")
    assert res.skipped == [1, 2] and res.saved == [3]
    assert drive.saves[0]["names"] == ["a-E03.mkv"]


def test_everything_already_there_saves_nothing():
    drive = FakeDrive(existing=[f"a-E{e:02d}.mkv" for e in (1, 2)])
    groups = plan_batch(_rows({e: (False, [_src("a", e)]) for e in (1, 2)}))
    res = fetch_batch(lambda: drive, groups, "/MediaFans/剧")
    assert res.saved == [] and sorted(res.skipped) == [1, 2]
    assert drive.saves == []


def test_a_dead_source_does_not_kill_the_rest():
    """分享随时可能失效，不能让一个坏链毁掉整批."""
    drive = FakeDrive(dead={"bad"})
    rows = _rows({1: (False, [_src("bad", 1)]), 2: (False, [_src("bad", 2)]),
                  3: (False, [_src("good", 3)])})
    res = fetch_batch(lambda: drive, plan_batch(rows), "/MediaFans/剧")
    assert res.saved == [3]
    assert sorted(res.failed) == [1, 2]
    assert res.errors and "失效" in res.errors[0]


def test_missing_file_in_share_is_reported_per_episode():
    """分享里少了某个文件，只算那一集失败，别的照存."""
    drive = FakeDrive(listing=[DriveFile(fid="a-E02.mkv", name="a-E02.mkv",
                                         is_dir=False, size=1, share_fid_token="t")])
    rows = _rows({1: (False, [_src("a", 1)]), 2: (False, [_src("a", 2)])})
    res = fetch_batch(lambda: drive, plan_batch(rows), "/MediaFans/剧")
    assert res.saved == [2] and res.failed == [1]


def test_paths_come_back_for_the_saved_episodes():
    drive = FakeDrive()
    res = fetch_batch(lambda: drive,
                      plan_batch(_rows({1: (False, [_src("a", 1)])})), "/MediaFans/剧")
    assert res.paths[1] == "/MediaFans/剧/a-E01.mkv"


def test_progress_is_reported_per_source():
    drive = FakeDrive()
    steps = []
    rows = _rows({1: (False, [_src("a", 1)]), 2: (False, [_src("b", 2)])})
    fetch_batch(lambda: drive, plan_batch(rows), "/d",
                on_step=lambda stage, info: steps.append(info["message"]))
    assert sum(1 for m in steps if "转存" in m) >= 2
