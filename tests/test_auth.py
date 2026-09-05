"""夸克扫码登录流程测试（MockTransport 模拟 uop.quark.cn，不访问真实网络）."""

import json

import httpx
import pytest

from mediafans.auth import QuarkQRLogin
from mediafans.drive.quark import QuarkDrive
from mediafans.errors import DriveError


def _seq_handler(steps, recorded):
    """按调用顺序依次返回 steps 中的响应工厂，超出后重复最后一个."""

    def handler(request):
        path = request.url.path
        if path.endswith("getTokenForQrcodeLogin"):
            recorded.append(("get_token", dict(request.url.params)))
            return httpx.Response(200, json={
                "status": 2000000, "message": "ok",
                "data": {"members": {"token": "TQR123"}},
            })
        if path.endswith("getServiceTicketByQrcodeToken"):
            recorded.append(("poll", dict(request.url.params)))
            steps["poll"] = steps.get("poll", 0) + 1
            if steps["poll"] <= steps.get("wait_rounds", 1):
                return httpx.Response(200, json={"status": 50004001, "message": "waiting"})
            return httpx.Response(200, json={
                "status": 2000000, "message": "ok",
                "data": {"members": {"service_ticket": "STK456"}},
            })
        if path.endswith("/account/info"):
            recorded.append(("exchange", dict(request.url.params)))
            return httpx.Response(
                200,
                headers=httpx.Headers([
                    ("set-cookie", "__pus=aaa; Path=/; Max-Age=2592000"),
                    ("set-cookie", "__puus=bbb; Path=/; Max-Age=2592000"),
                ]),
                json={"status": 200, "data": {"nickname": "qr-user"}},
            )
        raise AssertionError(f"unexpected {path}")

    return handler


def test_qr_login_full_flow():
    recorded = []
    flow = QuarkQRLogin(
        transport=httpx.MockTransport(_seq_handler({"wait_rounds": 1}, recorded))
    )
    events = []
    cookie = flow.login(
        qr_renderer=lambda url: events.append(url) or True,
        on_event=events.append,
        poll_interval=0,
        timeout=5,
    )
    assert cookie == "__pus=aaa; __puus=bbb"
    # 二维码 URL 用 getTokenForQrcodeLogin 返回的 token 拼接
    assert events[0].startswith("https://su.quark.cn/4_eMHBJ?token=TQR123&client_id=532")
    # 轮询带了 token，换票带了 st 和 lw=scan
    polls = [r for r in recorded if r[0] == "poll"]
    assert polls and polls[0][1]["token"] == "TQR123"
    exchange = [r for r in recorded if r[0] == "exchange"][0]
    assert exchange[1]["st"] == "STK456" and exchange[1]["lw"] == "scan"


def test_qr_login_failure_status():
    def handler(request):
        if path := request.url.path:
            if path.endswith("getTokenForQrcodeLogin"):
                return httpx.Response(200, json={
                    "status": 2000000, "data": {"members": {"token": "T"}}})
            if path.endswith("getServiceTicketByQrcodeToken"):
                return httpx.Response(200, json={"status": 50004002, "message": "二维码已失效"})
        raise AssertionError(path)

    flow = QuarkQRLogin(transport=httpx.MockTransport(handler))
    with pytest.raises(DriveError, match="二维码已失效"):
        flow.login(qr_renderer=lambda u: True, poll_interval=0, timeout=3)


def test_exchange_rejects_empty_cookie():
    def handler(request):
        return httpx.Response(200, json={"status": 200, "data": {}})

    flow = QuarkQRLogin(transport=httpx.MockTransport(handler))
    with pytest.raises(DriveError, match="未换取到有效 cookie"):
        flow.exchange_ticket("STK")


def test_drive_uses_login_cookie_file(tmp_path):
    """凭据优先级: 登录缓存文件 > 静态 cookie."""
    cookie_file = tmp_path / "quark.cookie"
    cookie_file.write_text("__puus=from-login", encoding="utf-8")

    def handler(request):
        return httpx.Response(200, json={"status": 200, "data": {"nickname": "n"}})

    drive = QuarkDrive(
        {"cookie": "__puus=from-config", "cookie_file": str(cookie_file)},
        transport=httpx.MockTransport(handler),
    )
    assert drive.cookie == "__puus=from-login"
    assert drive.cookie_source == "login"


def test_drive_falls_back_to_static_cookie(tmp_path):
    """没有登录缓存时用静态 cookie."""
    def handler(request):
        return httpx.Response(200, json={"status": 200, "data": {"nickname": "n"}})

    drive = QuarkDrive(
        {"cookie": "__puus=from-config", "cookie_file": str(tmp_path / "none.cookie")},
        transport=httpx.MockTransport(handler),
    )
    assert drive.cookie == "__puus=from-config"
    assert drive.cookie_source == "config"
