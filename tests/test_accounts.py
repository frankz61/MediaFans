"""账号、会话、片库。

分三层的理由在 accounts.py 的模块注释里：资源（网盘）共用，展示（片库+进度）
按人分，账号层负责换令牌。这里主要钉住**分开之后不该串**的那些地方。
"""

import time

import pytest

from mediafans.accounts import (
    Accounts,
    LibraryItem,
    hash_password,
    verify_password,
)


def _acc(tmp_path):
    return Accounts(tmp_path / "users.json")


# ---------------------------------------------------------------- 密码
def test_password_hash_is_salted_and_verifies():
    a, b = hash_password("hunter22"), hash_password("hunter22")
    assert a != b                       # 同一个密码两次哈希必须不同（盐）
    assert verify_password("hunter22", a)
    assert not verify_password("hunter2", a)
    assert not verify_password("", a)


def test_garbage_hash_never_verifies():
    """手改坏了 users.json 不该变成「谁都能登」."""
    for bad in ("", "x", "md5$aa$bb", "scrypt$zz$zz", "scrypt$61$"):
        assert verify_password("anything", bad) is False


# ---------------------------------------------------------------- 用户
def test_add_login_and_resolve(tmp_path):
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    assert acc.login("alice", "wrong") is None
    tok = acc.login("alice", "hunter22")
    assert tok
    assert acc.resolve(tok).name == "alice"
    assert acc.resolve("nope") is None


def test_sessions_survive_a_restart(tmp_path):
    """令牌存盘：每次部署都把所有设备踢下线是不能接受的."""
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    tok = acc.login("alice", "hunter22")
    again = Accounts(tmp_path / "users.json")
    assert again.resolve(tok).name == "alice"


def test_changing_password_kills_that_users_sessions(tmp_path):
    """改了密码旧令牌还能用的话，改密码就等于没改."""
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    acc.add("bob", "hunter22")
    ta, tb = acc.login("alice", "hunter22"), acc.login("bob", "hunter22")
    acc.set_password("alice", "newpass1")
    assert acc.resolve(ta) is None
    assert acc.resolve(tb).name == "bob"        # 别人的会话不受影响


def test_cannot_delete_the_last_admin(tmp_path):
    acc = _acc(tmp_path)
    acc.add("root", "hunter22", admin=True)
    with pytest.raises(ValueError, match="最后一个管理员"):
        acc.remove("root")
    acc.add("root2", "hunter22", admin=True)
    assert acc.remove("root") is True


def test_password_never_leaves_the_server(tmp_path):
    acc = _acc(tmp_path)
    u = acc.add("alice", "hunter22")
    assert "password" not in u.as_public()
    assert "hunter22" not in str(u.as_public())


def test_browse_defaults_to_off_and_admin_always_on(tmp_path):
    """网盘目录里什么都有：默认不给浏览，否则片库这一层白分了."""
    acc = _acc(tmp_path)
    assert acc.add("alice", "hunter22").browsable() is False
    assert acc.add("root", "hunter22", admin=True).browsable() is True
    assert acc.set_flags("alice", can_browse=True).browsable() is True


# ---------------------------------------------------------------- 防爆破
def test_repeated_failures_start_costing_time(tmp_path):
    """不封禁只拖延：封禁会让 NAT 后面整栋楼连坐，拖延对字典爆破一样致命."""
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    ip = "1.2.3.4"
    assert acc.throttle_delay(ip) == 0
    for _ in range(4):
        acc.login("alice", "no", ip=ip)
    assert acc.throttle_delay(ip) == 0          # 手滑几次不该被罚
    for _ in range(4):
        acc.login("alice", "no", ip=ip)
    assert acc.throttle_delay(ip) > 0
    # 登录成功就清零
    acc.login("alice", "hunter22", ip=ip)
    assert acc.throttle_delay(ip) == 0


def test_failures_are_tracked_per_ip(tmp_path):
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    for _ in range(9):
        acc.login("alice", "no", ip="1.1.1.1")
    assert acc.throttle_delay("1.1.1.1") > 0
    assert acc.throttle_delay("2.2.2.2") == 0   # 别人不该被连坐


# ---------------------------------------------------------------- 片库
def test_library_is_per_user(tmp_path):
    """同一份网盘资源，两个人的片库互不可见——这就是「展示层分开」."""
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    acc.add("bob", "hunter22")
    acc.add_to_library("alice", LibraryItem(tmdb_id=125988, title="末日地堡", season=2))
    assert [i.tmdb_id for i in acc.library("alice")] == [125988]
    assert acc.library("bob") == []
    assert acc.in_library("alice", 125988, "tv") is True
    assert acc.in_library("bob", 125988, "tv") is False


def test_library_dedupes_and_keeps_the_original_added_time(tmp_path):
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    acc.add_to_library("alice", LibraryItem(tmdb_id=1, title="旧", season=1))
    first = acc.library("alice")[0].added
    time.sleep(0.01)
    acc.add_to_library("alice", LibraryItem(tmdb_id=1, title="新", season=3))
    lib = acc.library("alice")
    assert len(lib) == 1                        # 同一部作品只留一条
    assert lib[0].title == "新" and lib[0].season == 3
    assert lib[0].added == first                # 加入时间保留最初那次


def test_same_tmdb_id_as_movie_and_tv_are_different_entries(tmp_path):
    """tmdb 的电影和剧集是两套 id 空间，同号不同作，不能当成一条."""
    acc = _acc(tmp_path)
    acc.add("alice", "hunter22")
    acc.add_to_library("alice", LibraryItem(tmdb_id=7, media_type="tv", title="剧"))
    acc.add_to_library("alice", LibraryItem(tmdb_id=7, media_type="movie", title="影"))
    assert len(acc.library("alice")) == 2
    acc.remove_from_library("alice", 7, "tv")
    assert [i.media_type for i in acc.library("alice")] == ["movie"]


def test_broken_store_does_not_take_the_service_down(tmp_path):
    """文件坏了、手改乱了，服务要能起来（起来之后是没人能登，那是另一回事）."""
    f = tmp_path / "users.json"
    f.write_text("{ not json", encoding="utf-8")
    acc = Accounts(f)
    assert acc.list_users() == []
    acc.add("alice", "hunter22")                # 还能重新建
    assert acc.login("alice", "hunter22")
