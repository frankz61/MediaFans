from mediafans.utils import (
    classify_netdisk,
    fmt_size,
    join_set_cookies,
    json_path_get,
    match_tokens,
    norm_path,
    parse_quark_share,
    truncate,
)


def test_parse_quark_share_with_pwd():
    assert parse_quark_share("https://pan.quark.cn/s/abc123?pwd=8xk2#/list/share") == ("abc123", "8xk2")


def test_parse_quark_share_without_pwd():
    assert parse_quark_share("https://pan.quark.cn/s/XYZ_9-") == ("XYZ_9-", "")


def test_parse_quark_share_invalid():
    assert parse_quark_share("https://pan.baidu.com/s/xxxx") is None
    assert parse_quark_share("") is None


def test_classify_netdisk():
    assert classify_netdisk("https://pan.quark.cn/s/abc") == "quark"
    assert classify_netdisk("https://www.alipan.com/s/rst") == "aliyun"
    assert classify_netdisk("https://pan.baidu.com/s/1abc") == "baidu"
    assert classify_netdisk("https://example.com/none") == ""


def test_json_path_get():
    obj = {"data": {"cookie": "__puus=x"}, "list": [{"fid": "f1"}, {"fid": "f2"}]}
    assert json_path_get(obj, "data.cookie") == "__puus=x"
    assert json_path_get(obj, "list.1.fid") == "f2"
    assert json_path_get(obj, "data.nope") is None
    assert json_path_get(obj, "") == obj


def test_fmt_size():
    assert fmt_size(None) == "-"
    assert fmt_size(0) == "0B"
    assert fmt_size(2048) == "2.00KB"
    assert fmt_size(1024 ** 3) == "1.00GB"


def test_norm_path():
    assert norm_path("a/b/") == "/a/b"
    assert norm_path("\\x\\y") == "/x/y"
    assert norm_path("/") == "/"
    assert norm_path("") == "/"


def test_match_tokens():
    assert match_tokens("沙丘 4K", "沙丘2.2024.4K.BluRay.mkv")
    assert not match_tokens("沙丘 1080", "沙丘2.2024.4K.BluRay.mkv")
    assert match_tokens("", "anything")


def test_join_set_cookies():
    joined = join_set_cookies(["a=1; Path=/; HttpOnly", "b=2; Secure", "bad"])
    assert joined == "a=1; b=2"


def test_truncate():
    assert truncate("x" * 100, 10) == "x" * 9 + "…"
    assert truncate("ab", 10) == "ab"
