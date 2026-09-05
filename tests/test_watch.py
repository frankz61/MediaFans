"""播放进度与最近观看测试。

进度存服务端是有意的：手机、平板、电脑一起用时，进度只记在某一台上等于没记。
"""

import time

import pytest

from mediafans.watch import Mark, WatchStore


@pytest.fixture
def store(tmp_path):
    return WatchStore(tmp_path / "watch.json")


def test_save_and_resume(store):
    store.save("/a/E01.mkv", position=600, duration=2700, name="E01.mkv")
    m = store.get("/a/E01.mkv")
    assert m.resume_at == 600 and m.percent == 22 and not m.finished


def test_beginning_does_not_resume(store):
    """点错了、试个画质都会留下记录，从那儿续播很莫名."""
    store.save("/a/E01.mkv", position=12, duration=2700)
    assert store.get("/a/E01.mkv").resume_at == 0


def test_tail_counts_as_finished_and_restarts(store):
    """片尾曲基本不看：剩 20 秒内算看完，下次从头而不是从结尾续."""
    store.save("/a/E01.mkv", position=2690, duration=2700)
    m = store.get("/a/E01.mkv")
    assert m.finished and m.percent == 100 and m.resume_at == 0


def test_survives_restart(tmp_path):
    WatchStore(tmp_path / "w.json").save("/a/E01.mkv", position=600, duration=2700)
    assert WatchStore(tmp_path / "w.json").get("/a/E01.mkv").resume_at == 600


def test_broken_file_does_not_break_playback(tmp_path):
    p = tmp_path / "w.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    s = WatchStore(p)
    assert s.get("/a/x.mkv") is None
    s.save("/a/x.mkv", position=100, duration=1000)      # 还能继续写
    assert s.get("/a/x.mkv").position == 100


def test_metadata_is_kept_across_updates(store):
    store.save("/a/E01.mkv", position=60, duration=2700,
               tmdb_id=125988, title="末日地堡", season=2, episode=1, poster="p.jpg")
    store.save("/a/E01.mkv", position=900, duration=2700)   # 心跳只带进度
    m = store.get("/a/E01.mkv")
    assert (m.tmdb_id, m.title, m.season, m.episode, m.poster) == (
        125988, "末日地堡", 2, 1, "p.jpg")
    assert m.position == 900


# ---------------------------------------------------------------- 最近观看
def _watch(store, path, ep, **kw):
    store.save(path, position=kw.pop("position", 600), duration=2700,
               tmdb_id=kw.pop("tmdb_id", 125988), title=kw.pop("title", "末日地堡"),
               season=2, episode=ep)
    time.sleep(0.002)      # 保证 updated 严格递增


def test_recent_keeps_one_row_per_show(store):
    """连看十集会把列表冲满，那样最近观看就没法用了."""
    for ep in range(1, 11):
        _watch(store, f"/a/E{ep:02d}.mkv", ep)
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0].episode == 10


def test_recent_orders_by_last_watched(store):
    _watch(store, "/a/E01.mkv", 1)
    _watch(store, "/b/E01.mkv", 1, tmdb_id=456, title="别的剧")
    assert [r.title for r in store.recent()] == ["别的剧", "末日地堡"]


def test_recent_skips_files_opened_and_closed(store):
    store.save("/a/试一下.mkv", position=5, duration=2700)
    _watch(store, "/a/E01.mkv", 1)
    assert [r.path for r in store.recent()] == ["/a/E01.mkv"]


def test_recent_keeps_standalone_movies_separate(store):
    """散片没有 tmdb_id，不能被聚合到一起."""
    store.save("/m/A.mkv", position=600, duration=7000, name="A.mkv")
    time.sleep(0.002)
    store.save("/m/B.mkv", position=600, duration=7000, name="B.mkv")
    assert [r.name for r in store.recent()] == ["B.mkv", "A.mkv"]


def test_finished_episode_still_shows_in_recent(store):
    """看完一集不该从列表消失——正是要靠它接着看下一集."""
    store.save("/a/E05.mkv", position=2699, duration=2700,
               tmdb_id=1, title="剧", season=1, episode=5)
    rows = store.recent()
    assert rows and rows[0].finished and rows[0].episode == 5


def test_store_is_pruned(tmp_path):
    s = WatchStore(tmp_path / "w.json", limit=5)
    for i in range(12):
        s.save(f"/a/{i}.mkv", position=600, duration=2700)
    assert len(s._load()) == 5


def test_forget(store):
    store.save("/a/E01.mkv", position=600, duration=2700)
    assert store.forget("/a/E01.mkv") is True
    assert store.forget("/a/E01.mkv") is False
    assert store.get("/a/E01.mkv") is None


def test_unknown_duration_is_not_finished(store):
    """时长未知时 0/0 不能判成看完——否则那一集会永久标着已看完."""
    store.save("/a/E01.mkv", position=0, duration=0)
    m = store.get("/a/E01.mkv")
    assert not m.finished and m.percent == 0


def test_unknown_duration_keeps_the_known_one(store):
    """时长未知的心跳不该把已知时长冲掉，不然进度条会归零."""
    store.save("/a/E01.mkv", position=600, duration=2700)
    store.save("/a/E01.mkv", position=700, duration=0)
    m = store.get("/a/E01.mkv")
    assert m.duration == 2700 and m.percent == 25
