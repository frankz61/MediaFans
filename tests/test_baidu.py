"""百度驱动全流程测试：MockTransport 模拟网页端接口，不访问真实网络。

全部走 cookie 通道。官方 OAuth/xpan 接口把第三方应用锁死在 /apps/{应用名} 里，
用户自己的 /MediaFans 属于「权限外目录」，文档明令禁止查询和转存——
那条路对这个项目走不通，所以驱动里也没有留。
"""

import json
import time

import httpx
import pytest

from mediafans.drive import create_drive, supported_netdisks
from mediafans.drive.baidu import BaiduDrive
from mediafans.errors import ConfigError, DriveError
from mediafans.utils import parse_baidu_share

# ---------------------------------------------------------------- 链接解析

def test_parse_baidu_share_shorturl():
    url = "https://pan.baidu.com/s/1AbCdEfGhI?pwd=9xk2"
    assert parse_baidu_share(url) == ("AbCdEfGhI", "9xk2", None)


def test_parse_baidu_share_init_url():
    url = "https://pan.baidu.com/share/init?surl=Zz99Yy"
    assert parse_baidu_share(url) == ("Zz99Yy", "", None)


def test_parse_baidu_share_legacy():
    url = "https://pan.baidu.com/share/link?shareid=123456&uk=987654"
    assert parse_baidu_share(url) == ("", "", {"shareid": "123456", "uk": "987654"})


def test_parse_baidu_share_invalid():
    assert parse_baidu_share("https://example.com/s/1abc") is None


# ---------------------------------------------------------------- 驱动注册

def test_registry_has_baidu():
    assert "baidu" in supported_netdisks()
    drive = create_drive("baidu", {"cookie": "BDUSS=x", "access_token": "T0"},
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert isinstance(drive, BaiduDrive)
    assert drive.name == "baidu"


def test_cookie_optional_at_construction():
    """百度凭据是惰性的：只配 OAuth 也能建实例（打开分享时才要求 cookie）."""
    drive = BaiduDrive({"access_token": "T0"},
                       transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert drive.cookie == ""


# ---------------------------------------------------------------- 自己的网盘（cookie）
#
# 全部走网页端接口，不用 OAuth。官方 xpan 接口把第三方应用锁死在
# /apps/{应用名} 里，用户自己的 /MediaFans 属于「权限外目录」，
# 文档明令禁止查询和转存，所以那条路对这个项目根本走不通。

WEB_COOKIE = "BDUSS=x; STOKEN=y"


def _tmplvar(fields_json, bdstoken="BD1", username="小明"):
    out = {}
    if "bdstoken" in fields_json:
        out["bdstoken"] = bdstoken
    if "username" in fields_json:
        out["username"] = username
    return httpx.Response(200, json={"errno": 0, "result": out})


def _cookie_drive(handler):
    return BaiduDrive({"cookie": WEB_COOKIE}, transport=httpx.MockTransport(handler))


def test_account_name_from_template_vars():
    def handler(request):
        assert request.url.path == "/api/gettemplatevariable"
        assert "BDUSS=x" in request.headers.get("cookie", "")
        return _tmplvar(request.url.params["fields"])

    assert _cookie_drive(handler).account_name() == "小明"


def test_expired_web_session_says_rescan_not_errno():
    """这个接口比 api/list 挑剔：登录态稍有问题就 -6，而 api/list 还是好的。

    实测扫码后十几分钟就会这样。只丢一个 errno=-6 给用户等于什么都没说。
    """
    drive = _cookie_drive(
        lambda r: httpx.Response(200, json={"errno": -6, "result": []}))
    with pytest.raises(ConfigError, match="重新扫码"):
        drive.account_name()


def test_list_files_uses_web_api_not_xpan():
    calls = []

    def handler(request):
        assert request.url.path == "/api/list", "必须走网页端，xpan 够不到用户目录"
        calls.append(request.url.params.get("start", "0"))
        if request.url.params.get("start", "0") in ("0", 0):
            return httpx.Response(200, json={"errno": 0, "list": [
                {"fs_id": 111, "path": "/MediaFans/剧", "server_filename": "剧",
                 "isdir": 1, "size": 0, "server_mtime": 1700000000},
                {"fs_id": 222, "path": "/MediaFans/e01.mkv", "server_filename": "e01.mkv",
                 "isdir": 0, "size": 2048, "server_mtime": 1700000100},
            ]})
        return httpx.Response(200, json={"errno": 0, "list": []})

    files = _cookie_drive(handler).list_files("/MediaFans")
    assert [f.name for f in files] == ["剧", "e01.mkv"]
    assert files[0].is_dir and files[0].fid == "/MediaFans/剧"    # 目录 fid = 路径
    assert not files[1].is_dir and files[1].fid == "222"          # 文件 fid = fs_id
    assert len(calls) == 1      # 单页不足 1000 条就停


def test_list_files_reports_a_missing_dir():
    drive = _cookie_drive(
        lambda r: httpx.Response(200, json={"errno": -9, "list": []}))
    with pytest.raises(DriveError, match="目录不存在"):
        drive.list_files("/没有这个目录")


def test_resolve_path_asks_directly_for_a_dir():
    """百度按路径寻址，目录问一次就知道，不用像夸克那样逐段下钻."""
    seen = []

    def handler(request):
        d = request.url.params["dir"]
        seen.append(d)
        if d == "/MediaFans":
            return httpx.Response(200, json={"errno": 0, "list": [
                {"fs_id": 1, "path": "/MediaFans/x", "server_filename": "x",
                 "isdir": 0, "size": 1}]})
        return httpx.Response(200, json={"errno": -9, "list": []})

    assert _cookie_drive(handler).resolve_path("/MediaFans") == "/MediaFans"
    assert seen == ["/MediaFans"], "列表非空就能断定是目录，只该问一次"


def test_resolve_path_falls_back_to_parent_for_a_file():
    def handler(request):
        d = request.url.params["dir"]
        if d == "/MediaFans/movie.mkv":
            return httpx.Response(200, json={"errno": -9, "list": []})
        if d == "/MediaFans":
            return httpx.Response(200, json={"errno": 0, "list": [
                {"fs_id": 42, "path": "/MediaFans/movie.mkv",
                 "server_filename": "movie.mkv", "isdir": 0, "size": 99}]})
        return httpx.Response(200, json={"errno": -9, "list": []})

    drive = _cookie_drive(handler)
    assert drive.resolve_path("/MediaFans/movie.mkv") == "42"
    assert drive.resolve_path("/不存在/x") is None


def test_ensure_dir_creates_via_web_api():
    seen = {}

    def handler(request):
        p = request.url.path
        if p == "/api/gettemplatevariable":
            return _tmplvar(request.url.params["fields"])
        if p == "/api/list":
            return httpx.Response(200, json={"errno": -9, "list": []})   # 不存在
        if p == "/api/create":
            from urllib.parse import parse_qs

            seen["form"] = {k: v[0] for k, v in
                            parse_qs(request.content.decode()).items()}
            seen["bdstoken"] = request.url.params.get("bdstoken")
            return httpx.Response(200, json={"errno": 0, "path": seen["form"]["path"]})
        raise AssertionError(f"unexpected {p}")

    assert _cookie_drive(handler).ensure_dir("/MediaFans") == "/MediaFans"
    assert seen["form"]["path"] == "/MediaFans"
    assert seen["form"]["isdir"] == "1"
    assert seen["bdstoken"] == "BD1", "建目录要带 bdstoken"


def test_play_target_needs_the_pan_ua():
    """直链必须带 User-Agent: pan.baidu.com，浏览器 UA 直接 403。

    但**不需要 cookie**，302 之后支持 Range——所以本机中继只补这一个头就够，
    比夸克 PC 直链还简单（那边还得转发 cookie）。
    """
    seen = {}

    def handler(request):
        if request.url.path == "/api/gettemplatevariable":
            raise AssertionError("取直链不该依赖会过期的网页登录态")
        if request.url.path == "/file/abc":
            # 取直链后会先把 302 解开，好让代理直连 CDN，少一跳
            seen["dlink_ua"] = request.headers.get("user-agent")
            seen["dlink_cookie"] = request.headers.get("cookie", "")
            return httpx.Response(302, headers={
                "Location": "https://xafj-cm11.baidupcs.com/file/abc"})
        assert request.url.path == "/api/filemetas"
        assert request.url.params["dlink"] == "1"
        assert request.url.params["fsids"] == "[222]"   # 按 fs_id 取，不用反查路径
        return httpx.Response(200, json={"errno": 0, "info": [
            {"dlink": "https://d.pcs.baidu.com/file/abc?fid=1",
             "size": 4096, "server_filename": "e01.mkv"}]})

    t = _cookie_drive(handler).get_play_target("222", "e01.mkv")
    assert seen["dlink_ua"] == "pan.baidu.com", "解 302 那一跳也得带 UA"
    # 不带 cookie 的话 302 照样返回，但 Location 的签名是坏的，下载直接 403，
    # 而错误出现在后面的 CDN 上，很难联想到是这里少了个头
    assert "BDUSS=x" in seen["dlink_cookie"], "解 302 必须带 cookie"
    assert t.url == "https://xafj-cm11.baidupcs.com/file/abc"
    assert t.download_url.startswith("https://d.pcs.baidu.com/")
    assert t.ua == "pan.baidu.com"
    assert t.variants[0].size == 4096 and t.variants[0].origin


def test_play_target_surfaces_a_blocked_file():
    def handler(request):
        if request.url.path == "/api/gettemplatevariable":
            return _tmplvar(request.url.params["fields"])
        return httpx.Response(200, json={"errno": 0, "info": [{"dlink": ""}]})

    with pytest.raises(DriveError, match="违规或受限"):
        _cookie_drive(handler).get_play_target("222", "e01.mkv")


# ---------------------------------------------------------------- 分享（web 接口）

def _wxlist_body(request):
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_open_share_and_list(tmp_path):
    state = {}

    def handler(request):
        p = request.url.path
        if p == "/share/wxlist":
            assert request.headers["user-agent"] == "netdisk"
            assert "BDUSS=x" in request.headers.get("cookie", "")
            body = _wxlist_body(request)
            state["wxlist"] = body
            if body.get("page") == "1":
                return httpx.Response(200, json={"errno": 0, "data": {
                    "shareid": 900001, "uk": 800002, "seckey": "SK-abc_def~ghi",
                    "list": [
                        {"fs_id": 777, "server_filename": "剧名 S01", "isdir": 1,
                         "size": 0, "path": "/share/剧名 S01"},
                        {"fs_id": 888, "server_filename": "E01.mkv", "isdir": 0,
                         "size": 4096, "path": "/share/E01.mkv"},
                    ]}})
            return httpx.Response(200, json={"errno": 0, "data": {"list": []}})
        raise AssertionError(f"unexpected {p}")

    drive = BaiduDrive({"cookie": "BDUSS=x; STOKEN=y"},
                       transport=httpx.MockTransport(handler))
    url = "https://pan.baidu.com/s/1ShortCode?pwd=ab12"
    ctx = drive.open_share(url, passcode="")
    assert state["wxlist"]["shorturl"] == "1ShortCode"
    assert state["wxlist"]["pwd"] == "ab12"
    assert state["wxlist"]["root"] == "1"
    assert ctx.extra["shareid"] == "900001"
    assert ctx.extra["uk"] == "800002"
    assert ctx.extra["sekey"] == "SK+abc/def=ghi"
    assert ctx.passcode == "ab12"

    files = drive.list_share_files(ctx)
    assert files[0].is_dir and files[0].fid == "/share/剧名 S01"
    assert not files[1].is_dir and files[1].fid == "888"
    assert files[1].share_fid_token == "888"


def test_open_share_without_code_gets_password_error():
    """开放分享不传提取码也能开；需要提取码的由 wxlist 报 mispw."""

    def handler(request):
        return httpx.Response(200, json={"errno": -9, "show_msg": "mispw_9"})

    drive = BaiduDrive({"cookie": "BDUSS=x"},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(DriveError, match="提取码"):
        drive.open_share("https://pan.baidu.com/s/1ShortCode", passcode="")


def test_open_share_wrong_password_message():
    def handler(request):
        return httpx.Response(200, json={"errno": -9, "show_msg": "mispw_9"})

    drive = BaiduDrive({"cookie": "BDUSS=x"},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(DriveError, match="提取码"):
        drive.open_share("https://pan.baidu.com/s/1AbCdEfG?pwd=9999", passcode="9999")


def test_open_share_expired_message():
    def handler(request):
        return httpx.Response(200, json={"errno": 105})

    drive = BaiduDrive({"cookie": "BDUSS=x"},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(DriveError, match="失效"):
        drive.open_share("https://pan.baidu.com/s/1AbCdEfG?pwd=9999", passcode="9999")


def test_save_share_files_transfer(tmp_path):
    state = {}

    def handler(request):
        p = request.url.path
        params = request.url.params
        if p == "/api/gettemplatevariable":
            return httpx.Response(200, json={
                "errno": 0, "result": {"bdstoken": "BDT1"}})
        if p == "/api/list":
            # ensure_dir 的 resolve 与转存后的目标目录列表
            return httpx.Response(200, json={"errno": 0, "list": [
                {"fs_id": 3, "path": params.get("dir"), "server_filename":
                 params.get("dir", "/").rsplit("/", 1)[-1] or "MediaFans", "isdir": 1}]})
        if p == "/api/create":
            return httpx.Response(200, json={"errno": 0, "path": params.get("path")})
        if p == "/share/transfer":
            state["transfer"] = {
                "params": dict(params),
                "body": _wxlist_body(request),
                "cookie": request.headers.get("cookie", ""),
            }
            return httpx.Response(200, json={"errno": 0, "extra": {}})
        raise AssertionError(f"unexpected {p}")

    drive = BaiduDrive({"cookie": "BDUSS=x; STOKEN=y"},
                       transport=httpx.MockTransport(handler))
    from mediafans.drive.base import ShareContext
    from mediafans.models import DriveFile

    ctx = ShareContext(url="u", pwd_id="AbC", passcode="ab12", extra={
        "shareid": "900001", "uk": "800002", "sekey": "SK1"})
    saved = drive.save_share_files(
        ctx, [DriveFile(fid="888", name="E01.mkv")], "/MediaFans")
    t = state["transfer"]
    assert t["params"]["shareid"] == "900001"
    assert t["params"]["from"] == "800002"
    assert t["params"]["bdstoken"] == "BDT1"
    assert t["body"]["fsidlist"] == "[888]"
    assert t["body"]["path"] == "/MediaFans"
    assert "BDCLND=SK1" in t["cookie"]      # sekey 以 BDCLND cookie 传递
    assert "STOKEN=y" in t["cookie"]
    assert saved  # 转存后列了目标目录


def test_save_share_files_error_mapping():
    def handler(request):
        p = request.url.path
        if p == "/api/gettemplatevariable":
            return httpx.Response(200, json={"errno": 0, "result": {"bdstoken": "B"}})
        if p == "/api/list":
            return httpx.Response(200, json={"errno": 0, "list": []})
        if p == "/api/create":
            return httpx.Response(200, json={"errno": 0, "path": "/d"})
        if p == "/share/transfer":
            return httpx.Response(200, json={"errno": -10})
        raise AssertionError(f"unexpected {p}")

    drive = BaiduDrive({"cookie": "BDUSS=x; STOKEN=y"},
                       transport=httpx.MockTransport(handler))
    from mediafans.drive.base import ShareContext
    from mediafans.models import DriveFile

    ctx = ShareContext(url="u", pwd_id="A", passcode="c",
                       extra={"shareid": "1", "uk": "2", "sekey": "S"})
    with pytest.raises(DriveError, match="空间不足"):
        drive.save_share_files(ctx, [DriveFile(fid="1", name="f.mkv")], "/d")


def test_share_seckey_is_urlsafe_base64():
    """wxlist 的 seckey 是 URL-safe base64，padding 还用了 `~`：三种字符都要换回来。

    这四对是线上逐字比对出来的：同一个分享，wxlist 的 seckey 对 share/verify
    的 randsk（正统网页端路子），差异集恰好是 `-`→`+`、`_`→`/`、`~`→`=`。

    只还原 `~` 的话，seckey 里不含 `/` `+` 的分享照样能转存——所以这个 bug 会
    随分享「时好时坏」。挂的时候百度报 errno 200025「提取码输入错误」，
    跟提取码毫无关系，照着错误信息查会一直走岔路。
    """
    from mediafans.drive.baidu import BaiduDrive

    def handler(request):
        if request.url.path == "/share/wxlist":
            return httpx.Response(200, json={"errno": 0, "data": {
                "shareid": 1, "uk": 2,
                "seckey": "DYKHGNNTJ2bBWJZUHF1yTzF30mdc6cJ_ekw6EYofXd8~",
                "list": []}})
        return httpx.Response(200, json={"errno": 0})

    drive = BaiduDrive({"cookie": "BDUSS=x; STOKEN=y"},
                       transport=httpx.MockTransport(handler))
    ctx = drive.open_share("https://pan.baidu.com/s/1abcdefg", "pwd1")
    # 同一分享 share/verify 返回的 randsk，urldecode 之后就是这个
    assert ctx.extra["sekey"] == "DYKHGNNTJ2bBWJZUHF1yTzF30mdc6cJ/ekw6EYofXd8="


def test_seckey_restores_dash_to_plus():
    """`-`→`+` 单独钉一遍：这一对最容易漏，含 `-` 的 seckey 才会踩到."""
    from mediafans.drive.baidu import _restore_sekey

    assert _restore_sekey("KjwXNB9crQtdhQl5wh5btuvH2hundPVknQid560-6Ds~") ==         "KjwXNB9crQtdhQl5wh5btuvH2hundPVknQid560+6Ds="


def test_dead_share_errno_130_reads_as_expired():
    """-130：share/verify 说提取码没错，但 share/list 报 -21（内容已删）。

    也就是「分享还在、东西没了」。不翻译的话页面上只会甩出一个 errno=-130，
    看起来像我们的接口调错了。
    """
    from mediafans.drive.baidu import BaiduDrive
    from mediafans.errors import DriveError

    def handler(request):
        return httpx.Response(200, json={"errno": -130, "errtype": 1,
                                         "data": {"fileNums": 0, "list": []}})

    drive = BaiduDrive({"cookie": "BDUSS=x; STOKEN=y"},
                       transport=httpx.MockTransport(handler))
    with pytest.raises(DriveError, match="失效"):
        drive.open_share("https://pan.baidu.com/s/1abcdefg", "pwd1")


def test_resolve_path_does_not_mistake_a_file_for_a_dir():
    """`errno 0 + 空列表` 是二义的：真文件和空目录长得一模一样。

    实测 api/list 对文件路径返回 errno 0、list 为空；当成目录的话，
    fid 会变成路径字符串，播放时拿去当 fs_id 直接失败。
    """
    from mediafans.drive.baidu import BaiduDrive

    def handler(request):
        d = request.url.params["dir"]
        if d == "/MediaFans/movie.mkv":
            return httpx.Response(200, json={"errno": 0, "list": []})   # 文件
        if d == "/MediaFans":
            return httpx.Response(200, json={"errno": 0, "list": [
                {"fs_id": 333, "path": "/MediaFans/movie.mkv",
                 "server_filename": "movie.mkv", "isdir": 0, "size": 1}]})
        return httpx.Response(200, json={"errno": -9, "list": []})

    drive = BaiduDrive({"cookie": "BDUSS=x; STOKEN=y"},
                       transport=httpx.MockTransport(handler))
    assert drive.resolve_path("/MediaFans/movie.mkv") == "333"


def test_resolve_path_handles_an_empty_dir():
    """空目录也是 errno 0 + 空列表，得从父目录认出它是目录."""
    from mediafans.drive.baidu import BaiduDrive

    def handler(request):
        d = request.url.params["dir"]
        if d == "/MediaFans/空":
            return httpx.Response(200, json={"errno": 0, "list": []})
        if d == "/MediaFans":
            return httpx.Response(200, json={"errno": 0, "list": [
                {"fs_id": 5, "path": "/MediaFans/空", "server_filename": "空",
                 "isdir": 1, "size": 0}]})
        return httpx.Response(200, json={"errno": -9, "list": []})

    drive = BaiduDrive({"cookie": "BDUSS=x; STOKEN=y"},
                       transport=httpx.MockTransport(handler))
    assert drive.resolve_path("/MediaFans/空") == "/MediaFans/空"
