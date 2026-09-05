from __future__ import annotations

import hashlib
import sys
import time
import uuid
from typing import Callable, Dict, Optional, Tuple

import httpx

from .errors import DriveError
from .utils import pick_cookies

# 夸克网页版扫码登录（CAS 协议，参数与开源实现 QuarkPan 对齐）
_QR_API = "https://uop.quark.cn/cas"
_CLIENT_ID = "532"
_V = "1.2"
_QR_URL_BASE = "https://su.quark.cn/4_eMHBJ"
_QR_URL_PARAMS = (
    "&ssb=weblogin&uc_param_str=&uc_biz_str=S%3Acustom%7COPT%3ASAREA%400"
    "%7COPT%3AIMMERSIVE%401%7COPT%3ABACK_BTN_STYLE%400"
)

_BROWSER_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
}

_STATUS_WAITING = 50004001
_STATUS_OK = 2000000
_STATUS_FAILED = (50004002, 50004003, 50004004)


class QuarkQRLogin:
    """夸克扫码登录：获取二维码 -> 轮询状态 -> 用 service_ticket 换 cookie."""

    def __init__(self, timeout: float = 15.0, transport=None):
        self.timeout = timeout
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers=_BROWSER_HEADERS, timeout=self.timeout,
            transport=self.transport, follow_redirects=True,
        )

    def get_qr_code(self) -> Tuple[str, str]:
        """返回 (qr_token, 二维码内容 URL)."""
        with self._client() as c:
            r = c.get(f"{_QR_API}/ajax/getTokenForQrcodeLogin", params={
                "client_id": _CLIENT_ID, "v": _V, "request_id": str(uuid.uuid4()),
            })
            r.raise_for_status()
            body = r.json()
        if body.get("status") != _STATUS_OK:
            raise DriveError(f"获取登录二维码失败: {body.get('message') or body}")
        token = ((body.get("data") or {}).get("members") or {}).get("token")
        if not token:
            raise DriveError(f"登录二维码响应里没有 token: {str(body)[:200]}")
        qr_url = f"{_QR_URL_BASE}?token={token}&client_id={_CLIENT_ID}{_QR_URL_PARAMS}"
        return token, qr_url

    def poll_once(self, token: str) -> Tuple[str, Optional[str]]:
        """查一次状态: ("waiting", None) | ("success", service_ticket) | ("failed", 原因)."""
        with self._client() as c:
            r = c.get(f"{_QR_API}/ajax/getServiceTicketByQrcodeToken", params={
                "client_id": _CLIENT_ID, "v": _V, "token": token,
                "request_id": str(uuid.uuid4()),
            })
            r.raise_for_status()
            body = r.json()
        status = body.get("status")
        if status == _STATUS_OK:
            ticket = ((body.get("data") or {}).get("members") or {}).get("service_ticket")
            if ticket:
                return "success", str(ticket)
            return "failed", f"响应缺少 service_ticket: {str(body)[:150]}"
        if status == _STATUS_WAITING:
            return "waiting", None
        msg = body.get("message") or f"status={status}"
        if status in _STATUS_FAILED or any(
            k in str(msg).lower() for k in ("expired", "failed", "error", "timeout", "invalid")
        ):
            return "failed", str(msg)
        return "waiting", None  # 未知状态按等待处理，靠总超时兜底

    def exchange_ticket(self, service_ticket: str) -> str:
        """用 service_ticket 换取账号 cookie（来自 account/info 响应的 Set-Cookie）."""
        with self._client() as c:
            r = c.get("https://pan.quark.cn/account/info", params={
                "st": service_ticket, "lw": "scan", "fr": "pc", "platform": "pc",
            })
            r.raise_for_status()
            pairs = []
            for raw in r.headers.get_list("set-cookie"):
                first = raw.split(";", 1)[0].strip()
                if first and "=" in first:
                    pairs.append(first)
        cookie = "; ".join(pairs)
        if "__puus" not in cookie and "__pus" not in cookie:
            raise DriveError(
                f"登录票据未换取到有效 cookie（需要 __pus/__puus）: {cookie[:100] or 'Set-Cookie 为空'}"
            )
        return cookie

    def login(self, qr_renderer: Optional[Callable[[str], bool]] = None,
              on_event: Optional[Callable[[str], None]] = None,
              poll_interval: float = 2.0, timeout: float = 180.0) -> str:
        """完整登录流程，返回 cookie 字符串."""
        tell = on_event or (lambda m: None)
        token, qr_url = self.get_qr_code()
        rendered = qr_renderer(qr_url) if qr_renderer else False
        if not rendered:
            tell(f"无法在终端渲染二维码，请用其他工具把该 URL 转成二维码后扫描:\n{qr_url}")
        else:
            tell("请用夸克 APP 扫描上方二维码（约 3 分钟内有效）…")

        deadline = time.time() + timeout
        while time.time() < deadline:
            state, info = self.poll_once(token)
            if state == "success":
                tell("扫码确认成功，正在换取 cookie…")
                return self.exchange_ticket(info)
            if state == "failed":
                raise DriveError(f"扫码登录失败: {info}")
            time.sleep(poll_interval)
        raise DriveError(f"等待扫码超时（{timeout:.0f} 秒），二维码已失效，请重试")


def render_qr_ascii(url: str) -> bool:
    """终端渲染 ASCII 二维码，成功返回 True（qrcode 未安装时返回 False）."""
    try:
        import qrcode
    except ImportError:
        return False
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True, out=sys.stdout)
    sys.stdout.flush()
    return True


# --------------------------------------------------------------------- 夸克 TV
# TV 版是另一套接口：OAuth + access_token（query 参数），完全不用 cookie。
# 参数与 OpenList 的 quark_uc_tv 驱动对齐。
_TV_API = "https://open-api-drive.quark.cn"
_TV_CLIENT_ID = "d3194e61504e493eb6222857bccfed94"
_TV_SIGN_KEY = "kw2dvtd7p4t3pjl2d9ed9yc8yej8kw2d"
_TV_APP_VER = "1.8.2.2"
_TV_CHANNEL = "GENERAL"
_TV_PLATFORM = "tv"

# code 换 token 需要 TV 客户端内置的 client_secret，官方接口拿不到，只能过社区中转。
# OpenList 默认写的是明文 http，这里固定用 https（实测该中转支持 TLS）。
# 注意：授权 code 与换回来的 token 都会经过这台第三方服务器。
TV_CODE_API = "https://api.extscreen.com/quarkdrive"

_TV_ERRNO_WAITING = 11003   # 用户未确认授权
_TV_ERRNO_BAD_CODE = 11004  # 授权码无效

# 中转要求的设备指纹字段，缺一个就报「公参缺失」
_TV_DEVICE = {
    "device_brand": "Xiaomi",
    "device_name": "M2004J7AC",
    "device_model": "M2004J7AC",
    "build_device": "M2004J7AC",
    "build_product": "M2004J7AC",
    "device_gpu": "Adreno (TM) 618",
    "activity_rect": "{}",
}


def tv_sign(method: str, pathname: str, timestamp: str) -> str:
    """x-pan-token = SHA256(方法 & 路径 & 毫秒时间戳 & 签名密钥)."""
    raw = f"{method}&{pathname}&{timestamp}&{_TV_SIGN_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()


class QuarkTVLogin:
    """夸克 TV 版扫码登录：拿二维码 -> 轮询授权 -> code 换 access/refresh token.

    和网页版扫码的区别：换回来的是 token 而不是 cookie，device_id 要长期保持一致
    （refresh 时要用），所以和 token 存在一起。
    """

    def __init__(self, device_id: str = "", timeout: float = 20.0,
                 transport=None, code_api: str = TV_CODE_API):
        self.device_id = device_id or hashlib.md5(uuid.uuid4().bytes).hexdigest()
        self.timeout = timeout
        self.transport = transport
        self.code_api = code_api.rstrip("/")

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport,
                            follow_redirects=True)

    def _signed(self, method: str, pathname: str) -> Tuple[dict, dict]:
        """返回 (签名头, 公共查询参数)；req_id 与签名共用同一个时间戳."""
        tm = str(int(time.time() * 1000))
        headers = {
            "x-pan-tm": tm,
            "x-pan-token": tv_sign(method, pathname, tm),
            "x-pan-client-id": _TV_CLIENT_ID,
        }
        params = {
            "req_id": hashlib.md5((self.device_id + tm).encode()).hexdigest(),
            "app_ver": _TV_APP_VER,
            "device_id": self.device_id,
            "platform": _TV_PLATFORM,
            "channel": _TV_CHANNEL,
        }
        return headers, params

    def get_qr_code(self) -> Tuple[str, str]:
        """返回 (query_token, 二维码 PNG 的 data URI)。TV 版直接下发图片而不是 URL."""
        headers, params = self._signed("GET", "/oauth/authorize")
        params.update({"auth_type": "code", "client_id": _TV_CLIENT_ID,
                       "scope": "netdisk", "qrcode": "1",
                       "qr_width": "460", "qr_height": "460"})
        with self._client() as c:
            body = c.get(f"{_TV_API}/oauth/authorize", headers=headers, params=params).json()
        qr, query_token = body.get("qr_data"), body.get("query_token")
        if not qr or not query_token:
            raise DriveError(f"获取 TV 登录二维码失败: {body.get('error_info') or str(body)[:200]}")
        return str(query_token), "data:image/png;base64," + str(qr)

    def poll_once(self, query_token: str) -> Tuple[str, Optional[str]]:
        """查一次: ("waiting", None) | ("success", code) | ("failed", 原因)."""
        headers, params = self._signed("GET", "/oauth/code")
        params.update({"client_id": _TV_CLIENT_ID, "scope": "netdisk",
                       "query_token": query_token})
        with self._client() as c:
            body = c.get(f"{_TV_API}/oauth/code", headers=headers, params=params).json()
        code = body.get("code")
        if code:
            return "success", str(code)
        if body.get("errno") == _TV_ERRNO_WAITING:
            return "waiting", None
        info = body.get("error_info") or str(body)[:150]
        return "failed", str(info)

    def exchange_code(self, code: str) -> Dict[str, str]:
        """code 换 token（经第三方中转，强制 https）."""
        return self._token_request({"code": code})

    def refresh(self, refresh_token: str) -> Dict[str, str]:
        """用 refresh_token 换一对新 token."""
        return self._token_request({"refresh_token": refresh_token})

    def _token_request(self, extra: dict) -> Dict[str, str]:
        tm = str(int(time.time() * 1000))
        payload = {
            "req_id": hashlib.md5((self.device_id + tm).encode()).hexdigest(),
            "app_ver": _TV_APP_VER,
            "device_id": self.device_id,
            "platform": _TV_PLATFORM,
            "channel": _TV_CHANNEL,
        }
        payload.update(_TV_DEVICE)
        payload.update(extra)
        with self._client() as c:
            body = c.post(f"{self.code_api}/token", json=payload).json()
        data = body.get("data") or {}
        access, refresh = data.get("access_token"), data.get("refresh_token")
        if not access or not refresh:
            raise DriveError(
                f"TV token 换取失败: {data.get('error_info') or body.get('message') or str(body)[:200]}"
            )
        return {
            "access_token": str(access),
            "refresh_token": str(refresh),
            "device_id": self.device_id,
            "expires_in": int(data.get("expires_in") or 0),
            "saved_at": int(time.time()),
        }

    def login(self, on_qr: Optional[Callable[[str], None]] = None,
              on_event: Optional[Callable[[str], None]] = None,
              poll_interval: float = 2.0, timeout: float = 180.0) -> Dict[str, str]:
        """完整流程，返回 token 字典."""
        tell = on_event or (lambda m: None)
        query_token, qr_data_uri = self.get_qr_code()
        if on_qr:
            on_qr(qr_data_uri)
        tell("请用夸克 APP 扫描二维码并确认授权（约 3 分钟内有效）…")
        deadline = time.time() + timeout
        while time.time() < deadline:
            state, info = self.poll_once(query_token)
            if state == "success":
                tell("授权成功，正在换取 token…")
                return self.exchange_code(info)
            if state == "failed":
                raise DriveError(f"TV 扫码登录失败: {info}")
            time.sleep(poll_interval)
        raise DriveError(f"等待扫码超时（{timeout:.0f} 秒），二维码已失效，请重试")


# ---------------------------------------------------------------------------
# 百度网盘扫码登录（passport 的标准扫码通道，tpl=netdisk）
# ---------------------------------------------------------------------------
_BD_PASSPORT = "https://passport.baidu.com"
_BD_PAN_HOME = "https://pan.baidu.com/disk/home"


def _bd_gid() -> str:
    """passport 用的设备标识：大写十六进制 UUID。整个登录过程要用同一个。"""
    return str(uuid.uuid4()).upper()


def _jsonp(text: str) -> dict:
    """passport 的响应可能裹在 JSONP 回调里，也可能是裸 JSON。"""
    import json as _json

    s = (text or "").strip()
    if not s:
        return {}
    if not s.startswith("{"):
        l, r = s.find("("), s.rfind(")")
        if l >= 0 and r > l:
            s = s[l + 1:r]
    try:
        return _json.loads(s)
    except ValueError:
        return {}


class BaiduQRLogin:
    """百度扫码登录：取二维码 -> 轮询 -> 用临时票据换正式 cookie。

    和网页版「扫码登录」走的是同一套 passport 接口。拿到的 cookie 必须同时含
    **BDUSS 和 STOKEN**——转存接口 share/transfer 要 STOKEN，只有 BDUSS 会报 errno -4，
    所以最后要再访问一次网盘首页把 STOKEN 领回来。

    整个流程共用一个 cookie jar：passport 分几步下发 cookie，
    每步都新建 client 的话前一步的 BDUSS 会丢。
    """

    def __init__(self, timeout: float = 15.0, transport=None):
        self.timeout = timeout
        self.transport = transport
        self.gid = _bd_gid()
        self.jar = httpx.Cookies()

    def _client(self, timeout: Optional[float] = None) -> httpx.Client:
        headers = dict(_BROWSER_HEADERS)
        headers["referer"] = "https://pan.baidu.com/"
        return httpx.Client(
            headers=headers, timeout=timeout or self.timeout,
            transport=self.transport, cookies=self.jar, follow_redirects=True,
        )

    def get_qr_code(self) -> Tuple[str, str]:
        """返回 (sign, 二维码图片 URL)。百度直接给图片地址，不用本地渲染。"""
        ts = int(time.time() * 1000)
        with self._client() as c:
            r = c.get(f"{_BD_PASSPORT}/v2/api/getqrcode", params={
                "lp": "pc", "qrloginfrom": "pc", "gid": self.gid,
                "apiver": "v3", "tt": ts, "tpl": "netdisk", "_": ts,
            })
            r.raise_for_status()
            body = _jsonp(r.text)
        sign = str(body.get("sign") or "")
        img = str(body.get("imgurl") or "")
        if not sign or not img:
            raise DriveError(f"获取百度登录二维码失败: {str(body)[:200] or '响应为空'}")
        if not img.startswith("http"):
            img = "https://" + img.lstrip("/")
        return sign, img

    # unicast 是长轮询：没事件时会一直挂着连接。用短超时问一次，
    # 超时就是「还没动静」，不是错误——否则每次轮询都要卡住调用方几十秒。
    POLL_TIMEOUT = 6.0

    def poll_once(self, sign: str) -> Tuple[str, Optional[str]]:
        """查一次: ("waiting", 提示) | ("success", 临时票据) | ("failed", 原因)."""
        ts = int(time.time() * 1000)
        try:
            with self._client(timeout=self.POLL_TIMEOUT) as c:
                r = c.get(f"{_BD_PASSPORT}/channel/unicast", params={
                    "channel_id": sign, "gid": self.gid, "tpl": "netdisk",
                    "apiver": "v3", "tt": ts, "_": ts,
                })
                r.raise_for_status()
                body = _jsonp(r.text)
        except httpx.TimeoutException:
            return "waiting", None
        except httpx.HTTPError as e:
            # 网络抖动不该把二维码作废，交给上层的总超时兜底
            return "waiting", None if not str(e) else None
        errno = body.get("errno")
        if errno not in (0, None):
            # 1 = 还没人扫，这是等待不是失败
            if str(errno) == "1":
                return "waiting", None
            return "failed", f"errno={errno} {body.get('errmsg') or ''}".strip()
        inner = _jsonp(str(body.get("channel_v") or ""))
        status = inner.get("status")
        if status == 0 and inner.get("v"):
            return "success", str(inner["v"])
        if status == 1:
            return "waiting", "已扫码，请在手机上确认登录"
        return "waiting", None

    @staticmethod
    def clean_cookie(pairs) -> str:
        """把扫码拿到的一堆 cookie 收敛成一行，同名的挑对的那个。

        接受 `(name, value)` 或 `(name, value, domain)`。**带上域名很重要**：
        百度会在 passport 域和 pan 域各发一个**值不同的 STOKEN**，而网盘接口
        只认 pan 那个。丢掉域名信息去重就是在两个里随便挑一个——挑中 passport
        的那次，`api/list` 一切正常，但 `gettemplatevariable`（转存要的
        bdstoken 从它拿）一路 errno -6「用户未登录」。

        这个 bug 会伪装成「登录态过期特别快」：刚扫完能用一会儿，然后就一直 -6，
        重扫又好了。实测同一账号 passport 版 STOKEN 报 -6、pan 版 errno 0。
        BaiduPCS-Rust v2.1.6 修的是同一个问题。

        另外扔掉 `*_BFESS` 重复键（BFESS 是百度边缘缓存用的副本），
        只有 `X_BFESS` 而没有 `X` 时把它改名留下，别把凭据丢了。
        """
        got = pick_cookies(pairs)
        out = {}
        for name, value in got.items():
            base = name[:-6] if name.endswith("_BFESS") else name
            if name.endswith("_BFESS") and base in got:
                continue          # 正本还在，副本丢掉
            out.setdefault(base, value)
        return "; ".join(f"{k}={v}" for k, v in out.items())

    def exchange(self, tmp_ticket: str) -> str:
        """用扫码票据换正式 cookie（BDUSS + STOKEN）."""
        ts = int(time.time() * 1000)
        with self._client() as c:
            c.get(f"{_BD_PASSPORT}/v3/login/main/qrbdusslogin", params={
                "v": ts, "bduss": tmp_ticket, "u": _BD_PAN_HOME,
                "loginVersion": "v4", "qrcode": "1", "tpl": "netdisk",
                "apiver": "v3", "tt": ts, "traceid": "", "time": int(ts / 1000),
                "alg": "v3",
            })
            # STOKEN 是访问网盘域名时才下发的，转存接口离了它会报 -4。
            # 两个地址都走一趟：根路径下发的是 pan 域那个 STOKEN（网盘接口
            # 认的就是它），/disk/home 是登录后的落地页，少一个都可能拿不全。
            c.get("https://pan.baidu.com/")
            c.get(_BD_PAN_HOME)
            jar = c.cookies
        cookie = self.clean_cookie(
            [(c.name, c.value, c.domain) for c in jar.jar])
        if "BDUSS=" not in cookie:
            raise DriveError(
                f"扫码票据没换到 BDUSS（cookie: {cookie[:120] or '空'}）")
        if "STOKEN=" not in cookie:
            raise DriveError(
                "只拿到 BDUSS 没拿到 STOKEN，转存会失败。"
                "这通常是百度对该账号做了额外校验，请改用「粘贴 cookie」方式")
        return cookie

    def login(self, on_qr: Optional[Callable[[str], None]] = None,
              on_event: Optional[Callable[[str], None]] = None,
              poll_interval: float = 2.0, timeout: float = 180.0) -> str:
        tell = on_event or (lambda m: None)
        sign, img = self.get_qr_code()
        if on_qr:
            on_qr(img)
        tell("请用百度网盘 APP 扫描二维码…")
        deadline = time.time() + timeout
        said = False
        while time.time() < deadline:
            state, info = self.poll_once(sign)
            if state == "success":
                tell("扫码确认成功，正在换取 cookie…")
                return self.exchange(info)
            if state == "failed":
                raise DriveError(f"百度扫码登录失败: {info}")
            if info and not said:
                tell(info)
                said = True
            time.sleep(poll_interval)
        raise DriveError(f"等待扫码超时（{timeout:.0f} 秒），请重试")
