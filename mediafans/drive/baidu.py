from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional, Sequence

import httpx

from ..errors import ConfigError, DriveError
from ..models import DriveFile, PlayTarget, StreamVariant
from ..tokens import TokenRelay
from ..utils import merge_cookies, norm_path, parse_baidu_share
from .base import BaseDrive, ShareContext

# 各端点要求的 UA 不一样：wxlist 要 netdisk，直链要 pan.baidu.com，其余用浏览器 UA
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)
NETDISK_UA = "netdisk"          # 只有 share/wxlist 认这个
# 直链只认这个 UA：浏览器 UA 403，netdisk UA 在 CDN 上报 sign error(31362)
DLINK_UA = "pan.baidu.com"

XPAN = "https://pan.baidu.com/rest/2.0"
WEB = "https://pan.baidu.com"

# 转存单批文件数上限（errno 12 超限）
TRANSFER_BATCH = 500
# 网页端接口的应用号。250528 是网盘 web 自己用的，配 cookie 走这一套就够了——
# 官方 xpan 接口把第三方应用锁在 /apps/{应用名} 里，够不到用户自己的目录。
WEB_APP_ID = "250528"

COOKIE_HELP = (
    "百度网盘未配置 cookie（打开分享/转存必需）。三种方式任选：\n"
    "  1. mediafans login --netdisk baidu   扫码登录（推荐）\n"
    "  2. drive.baidu.cookie    浏览器登录 pan.baidu.com 后 F12 -> Network 复制完整 Cookie\n"
    "     （必须同时包含 BDUSS 和 STOKEN，转存接口要 STOKEN）\n"
    "  3. drive.baidu.token_provider   对接 token 中转站"
)

# errno -> 人话（来自 baiduwp-php / hxz393 的错误表 + OpenList 黑名单错误码）
_ERRNO_MSG = {
    -12: "提取码错误",
    -130: "分享内容已被删除或失效",
    -9: "分享不存在或已失效",
    -8: "目标目录已有同名文件",
    -7: "文件名含非法字符",
    -6: "登录态失效（cookie 或 token 已过期）",
    -4: "登录态失效（STOKEN 缺失或过期，请更新完整 cookie）",
    -1: "分享链接违规",
    2: "转存失败（目标目录不存在？）",
    3: "分享内容违规被屏蔽",
    5: "分享不存在或提取码错误",
    10: "分享已过期",
    12: "转存文件数量超限（单批最多 500 个）",
    20: "网盘空间不足",
    -10: "网盘空间不足",
    -62: "操作过于频繁，触发百度限流，稍后再试",
    105: "分享链接不存在或已失效",
    110: "请求方 IP 被百度风控封禁",
    111: "access_token 已过期",
    116: "分享已被取消",
    118: "没有访问权限（sekey 校验失败）",
    8001: "账号被风控（普通账号无权调用该接口）",
    9013: "账号被风控（普通账号无权调用该接口）",
    9019: "账号被风控（cookie 异常或触发安全策略）",
    31119: "账号命中黑名单（hit black userlist）",
    31329: "账号命中黑名单（hit black userlist / illegal dlna）",
}


def _restore_sekey(sekey: str) -> str:
    """把 wxlist 的 seckey 还原成 BDCLND 要的标准 base64。

    wxlist 给的是 **URL-safe base64**，而且 padding 用的是 `~` 不是 `=`：
    `-`→`+`、`_`→`/`、`~`→`=`。三种替换缺一不可——四个分享逐字比对
    `share/verify` 的 randsk（正统网页端路子）确认过，差异集恰好就是这三对。

    只还原了 `~` 的话，seckey 里不含 `/` `+` 的分享照样能转存，含的就挂，
    而百度报的错是「提取码输入错误」(200025)——跟提取码毫无关系，
    照着错误信息查会一直走岔路，还会误以为是分享本身的问题。
    """
    return sekey.replace("-", "+").replace("_", "/").replace("~", "=")


def _errno_msg(errno, body: dict) -> str:
    extra = ""
    if isinstance(body, dict):
        extra = str(body.get("show_msg") or body.get("errmsg") or body.get("errtype") or "")
    base = _ERRNO_MSG.get(errno) or f"errno={errno}"
    return f"{base}（{extra}）" if extra and extra not in base else base



class BaiduDrive(BaseDrive):
    """百度网盘驱动，全程 cookie（BDUSS + STOKEN）鉴权。

    **不走开放平台 OAuth**：官方 xpan 接口把第三方应用锁死在 `/apps/{应用名}`，
    用户自己的 `/MediaFans` 属于「权限外目录」，文档明令禁止查询、下载和转存
    （见 https://pan.baidu.com/union/doc/使用入门/权限与配额/）。而且未过审应用
    只有 10 次/小时的配额。所以列目录、建目录、取直链全部走网页端接口。

    fid 语义：目录 = 网盘内绝对路径，文件 = fs_id（转存和取直链都用 fs_id）。
    """

    name = "baidu"

    def __init__(self, cfg: dict, transport=None):
        super().__init__(cfg)
        self.transport = transport
        self.cookie_source = ""
        self.save_dir = str(cfg.get("save_dir") or "/MediaFans")
        self.vip_type = -1  # uinfo 拿到后更新：0 普通 / 1 会员 / 2 SVIP
        try:
            self.cookie = self._acquire_cookie()
        except ConfigError:
            self.cookie = ""  # 惰性：只在打开分享/转存时才要求
        self._bdstoken_cache = ""
        self.client = httpx.Client(
            headers={"user-agent": BROWSER_UA},
            timeout=float(cfg.get("timeout") or 20),
            transport=transport,
        )

    # ------------------------------------------------------------------ 凭据
    def _acquire_cookie(self) -> str:
        """优先级: token 中转站 > 登录缓存文件 > 配置静态 cookie."""
        provider = (self.cfg or {}).get("token_provider")
        if provider:
            self.cookie_source = "token_provider"
            return TokenRelay(provider).fetch_cookie()
        cookie_file = (self.cfg or {}).get("cookie_file")
        if cookie_file:
            try:
                text = Path(cookie_file).read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                self.cookie_source = "login"
                return text
        cookie = str((self.cfg or {}).get("cookie") or "").strip()
        if cookie:
            self.cookie_source = "config"
            return cookie
        raise ConfigError(COOKIE_HELP)

    # ------------------------------------------------------------------ http
    def _request(self, method: str, url: str, *, params=None, data=None,
                 headers=None, cookie: str = "") -> tuple:
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = cookie
        try:
            r = self.client.request(method, url, params=params, data=data, headers=headers)
        except httpx.HTTPError as e:
            raise DriveError(f"百度接口网络错误 [{url.split('/')[-1]}]: {e}")
        if r.status_code >= 500:
            raise DriveError(f"百度接口 HTTP {r.status_code} [{url}]")
        return r

    def _web_api(self, method: str, path: str, *, params=None, data=None,
                 ua: str = BROWSER_UA, bdclnd: str = "") -> dict:
        """网页端接口（cookie 鉴权）。bdclnd 是分享验证 sekey，转存时必须带."""
        if not self.cookie:
            raise ConfigError(COOKIE_HELP)
        cookie = merge_cookies(self.cookie, f"BDCLND={bdclnd}") if bdclnd else self.cookie
        r = self._request(method, WEB + path, params=params, data=data,
                          headers={"User-Agent": ua, "Referer": "https://pan.baidu.com/disk/home"},
                          cookie=cookie)
        try:
            body = r.json()
        except ValueError:
            raise DriveError(f"百度网页接口返回非 JSON [{path}] HTTP {r.status_code}")
        return body

    def _bdstoken(self) -> str:
        if not self._bdstoken_cache:
            self._bdstoken_cache = str(
                self._template_vars(["bdstoken"]).get("bdstoken") or "")
            if not self._bdstoken_cache:
                raise DriveError("没取到 bdstoken（转存必需），请重新扫码登录")
        return self._bdstoken_cache

    def _require_stoken(self):
        if "STOKEN=" not in (self.cookie or ""):
            raise ConfigError(
                "转存需要 STOKEN：cookie 里只有 BDUSS 不够。"
                "请重新复制包含 BDUSS 和 STOKEN 的完整 Cookie（mediafans login --netdisk baidu）"
            )

    # ------------------------------------------------------------------ 账号
    def account_name(self) -> str:
        body = self._template_vars(["username", "bdstoken"])
        return str(body.get("username") or "")

    def _template_vars(self, fields: List[str]) -> dict:
        """网页端的「模板变量」接口，一次能取 bdstoken / username / uk。

        它比 api/list 挑剔：登录态稍有问题就报 errno -6「用户未登录」，
        而同一份 cookie 的 api/list 还是好的。所以这里的 -6 要单独说清楚是
        「登录过期，重新扫码」，不然用户只会看到一个没头没尾的 -6。
        """
        import json as _json

        body = self._web_api("GET", "/api/gettemplatevariable", params={
            "clienttype": "0", "app_id": WEB_APP_ID, "web": "1",
            "fields": _json.dumps(fields),
        })
        errno = body.get("errno")
        if errno == -6:
            raise ConfigError(
                "百度登录态已过期，请重新扫码登录"
                "（网页端登录态比 cookie 本身短命，转存前需要它）")
        if errno not in (0, None):
            raise DriveError(f"读取百度登录信息失败: {_errno_msg(errno, body)}")
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ 文件
    @staticmethod
    def _fmt_time(v) -> str:
        try:
            ts = int(v or 0)
        except (TypeError, ValueError):
            return ""
        if ts <= 0:
            return ""
        from datetime import datetime

        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return ""

    @staticmethod
    def _to_file(it: dict, in_share: bool = False) -> DriveFile:
        is_dir = int(it.get("isdir") or 0) == 1
        # 目录用绝对路径当 fid（列目录按 path），文件用 fs_id（转存/直链用）
        fid = str(it.get("path") or "") if is_dir else str(it.get("fs_id") or "")
        f = DriveFile(
            fid=fid,
            name=str(it.get("server_filename") or ""),
            is_dir=is_dir,
            size=int(it.get("size") or 0),
            updated_at=BaiduDrive._fmt_time(
                it.get("server_mtime") or it.get("server_ctime")
            ),
        )
        if in_share:
            f.share_fid_token = str(it.get("fs_id") or "")
        return f

    def list_files(self, dir_fid: str = "0") -> List[DriveFile]:
        """列目录。走网页端 api/list，cookie 鉴权，能看到整个网盘。

        官方 xpan 接口把第三方应用锁死在 /apps/{应用名} 里，
        用户自己的 /MediaFans 属于「权限外目录」，明令禁止查询和转存，
        所以这里只能用网页端接口。
        """
        path = norm_path(dir_fid) if dir_fid not in ("", "0") else "/"
        files: List[DriveFile] = []
        start = 0
        while True:
            body = self._web_api("GET", "/api/list", params={
                "dir": path, "order": "name", "desc": "0",
                "start": start, "limit": 1000,
                "web": "1", "clienttype": "0", "app_id": WEB_APP_ID,
            })
            errno = body.get("errno")
            if errno == -9:
                raise DriveError(f"目录不存在: {path}")
            if errno not in (0, None):
                raise DriveError(f"列目录失败 [{path}]: {_errno_msg(errno, body)}")
            lst = body.get("list") or []
            files.extend(self._to_file(x) for x in lst)
            if len(lst) < 1000 or len(files) >= 100000:
                break
            start += 1000
        files.sort(key=lambda f: (not f.is_dir, f.name))
        return files

    def resolve_path(self, path: str) -> Optional[str]:
        """目录的 fid 就是它的绝对路径；文件要拿 fs_id，得回父目录按名字找。

        百度按路径寻址，列一次就能确认存不存在——但 `errno 0 + 空列表` 是二义的：

            真目录  errno=0，list 有内容
            真文件  errno=0，list 为空     ← 拿它当目录，播放时会把路径当 fs_id
            不存在  errno=-9

        所以只有**列表非空**才敢断定是目录，其余一律回父目录找。
        代价是空目录和文件各多一次请求，换的是不会把文件误判成目录。
        """
        p = norm_path(path)
        if p == "/":
            return "/"
        body = self._web_api("GET", "/api/list", params={
            "dir": p, "order": "name", "start": 0, "limit": 1,
            "web": "1", "clienttype": "0", "app_id": WEB_APP_ID,
        })
        if body.get("errno") in (0, None) and (body.get("list") or []):
            return p
        parent, _, name = p.rpartition("/")
        try:
            for f in self.list_files(parent or "/"):
                if f.name == name:
                    return f.fid
        except DriveError:
            return None
        return None

    def ensure_dir(self, path: str) -> str:
        p = norm_path(path)
        if p == "/":
            return "/"
        fid = self.resolve_path(p)
        if fid:
            return fid
        body = self._web_api("POST", "/api/create", params={
            "a": "commit", "bdstoken": self._bdstoken(),
            "clienttype": "0", "app_id": WEB_APP_ID, "web": "1",
        }, data={"path": p, "isdir": "1", "block_list": "[]"})
        # -8 = 已存在（并发/重复创建）
        if int(body.get("errno") or 0) == 0:
            return str(body.get("path") or p)
        fid = self.resolve_path(p)
        if fid:
            return fid
        raise DriveError(f"创建百度网盘目录失败: {p}")

    # ------------------------------------------------------------------ 分享
    def open_share(self, share_url: str, passcode: str = "") -> ShareContext:
        parsed = parse_baidu_share(share_url or "")
        if not parsed:
            raise DriveError(f"无法从链接解析百度分享 ID: {share_url}")
        surl, url_pwd, legacy = parsed
        # 提取码缺不缺交给 wxlist 判断：开放分享 pwd 传空也能过，需要提取码会报 mispw
        code = passcode or url_pwd
        body = self._wxlist(surl=surl, legacy=legacy, code=code, root=True, page=1)
        data = body.get("data") or {}
        shareid = str(data.get("shareid") or "")
        uk = str(data.get("uk") or "")
        sekey = str(data.get("seckey") or "")
        if not shareid or not sekey:
            raise DriveError("分享信息不完整（缺少 shareid/seckey），分享可能已失效")
        sekey = _restore_sekey(sekey)
        return ShareContext(url=share_url, pwd_id=surl, passcode=code, extra={
            "surl": "1" + surl if surl else "",
            "legacy": legacy,       # {"uk":..,"shareid":..} 旧式链接
            "shareid": shareid,
            "uk": uk,
            "sekey": sekey,
        })

    def _wxlist(self, *, surl: str = "", legacy: Optional[dict] = None,
                code: str = "", root: bool = True, dir_path: str = "",
                page: int = 1) -> dict:
        # baiduwp-php 的形态：channel 等在 query，shorturl/dir/pwd 在 form body
        form = {}
        if legacy:
            form["uk"] = legacy.get("uk", "")
            form["shareid"] = legacy.get("shareid", "")
        else:
            form["shorturl"] = "1" + surl
        form.update({
            "dir": dir_path or "/",
            "root": "1" if root else "0",
            "pwd": code,
            "page": page,
            "num": 1000,
            "order": "time",
        })
        resp = self._web_api(
            "POST", "/share/wxlist",
            params={"channel": "weixin", "version": "2.2.2",
                    "clienttype": "25", "web": "1"},
            data=form,
            ua=NETDISK_UA,
        )
        errno = resp.get("errno")
        if errno in (0, None):
            return resp
        msg = str(resp.get("show_msg") or resp.get("errtype") or "")
        if "mispw" in msg or errno in (-9, -12, 5):
            raise DriveError(f"提取码错误或缺失: {msg or _errno_msg(errno, resp)}")
        if "mis_" in msg or errno in (105, 10, 116, -4, 3, 0, -130):
            raise DriveError(f"分享已失效或不可访问: {msg or _errno_msg(errno, resp)}")
        raise DriveError(f"打开百度分享失败: {_errno_msg(errno, resp)}")

    def list_share_files(self, ctx: ShareContext, dir_fid: str = "0") -> List[DriveFile]:
        root = dir_fid in ("", "0", "/")
        files: List[DriveFile] = []
        page = 1
        while True:
            body = self._wxlist(
                surl=ctx.pwd_id, legacy=ctx.extra.get("legacy"),
                code=ctx.passcode, root=root,
                dir_path="" if root else dir_fid, page=page,
            )
            data = body.get("data") or {}
            lst = data.get("list") or []
            files.extend(self._to_file(x, in_share=True) for x in lst)
            if len(lst) < 1000 or page > 50:
                break
            page += 1
        return files

    def save_share_files(self, ctx: ShareContext, files: Sequence[DriveFile],
                         to_dir: str) -> List[str]:
        self._require_stoken()
        to_path = norm_path(to_dir or self.save_dir)
        self.ensure_dir(to_path)
        fsids = [f.fid for f in files if f.fid and not f.is_dir]
        fsids += [f.share_fid_token for f in files if f.is_dir and f.share_fid_token]
        if not fsids:
            raise DriveError("没有可转存的文件（缺少 fs_id，请先用 list_share_files）")
        for i in range(0, len(fsids), TRANSFER_BATCH):
            resp = self._web_api(
                "POST", "/share/transfer",
                params={
                    "shareid": ctx.extra.get("shareid", ""),
                    "from": ctx.extra.get("uk", ""),
                    "bdstoken": self._bdstoken(),
                    "channel": "chunlei", "web": "1", "clienttype": "0",
                },
                data={
                    "fsidlist": json.dumps([int(x) for x in fsids[i:i + TRANSFER_BATCH]]),
                    "path": to_path,
                },
                bdclnd=ctx.extra.get("sekey", ""),
            )
            errno = resp.get("errno")
            if errno not in (0, None):
                raise DriveError(f"百度转存失败: {_errno_msg(errno, resp)}")
        # transfer 不返回新 fs_id，转存后列目标目录拿（失败不影响结果，仅影响返回值）
        try:
            return [f.fid for f in self.list_files(to_path)]
        except DriveError:
            return []

    # ------------------------------------------------------------------ 直链
    def get_play_target(self, fid: str, name: str = "") -> PlayTarget:
        """取直链。cookie 鉴权，不需要 OAuth。

        实测直链的要求很干脆：**必须带 `User-Agent: pan.baidu.com`**
        （浏览器 UA 直接 403），但**不需要 cookie**；302 到 baidupcs.com 之后
        支持 Range（206）。浏览器 `<video>` 发不了自定义 UA，所以走本机 /stream
        中继补这一个头就行——比夸克 PC 直链还简单，那边还得转发 cookie。
        """
        import json as _json

        # 按 fs_id 取，跟上层「文件 fid = fs_id」的约定一致；
        # 按路径也能取，但上层只拿得到 fid，不必多绕一次反查
        try:
            fsid = int(str(fid).strip())
        except (TypeError, ValueError):
            raise DriveError(f"百度取直链需要 fs_id，拿到的是: {fid!r}")
        # 刻意不带 bdstoken：实测取直链不需要它，而 bdstoken 依赖那个
        # 二十来分钟就过期的网页登录态。不依赖它，播放就能一直用下去，
        # 只有转存才需要重新扫码。
        body = self._web_api("GET", "/api/filemetas", params={
            "fsids": _json.dumps([fsid]),
            "dlink": "1", "web": "5", "clienttype": "0",
            "app_id": WEB_APP_ID,
        })
        errno = body.get("errno")
        if errno not in (0, None):
            raise DriveError(f"取百度直链失败: {_errno_msg(errno, body)}")
        info = (body.get("info") or [{}])[0]
        dlink = str(info.get("dlink") or "")
        if not dlink:
            raise DriveError(f"未拿到百度直链（文件可能违规或受限）: {str(info)[:160]}")
        size = int(info.get("size") or 0)
        file_name = name.rsplit("/", 1)[-1] or str(info.get("server_filename") or "")
        # 先把 302 解开，让代理直接连 CDN。并行 Range 时每个分片都要走一遍跳转，
        # 省掉这一跳对拖进度条的响应很明显。解不开就把 dlink 原样交出去，
        # 代理自己也会跟跳转，只是慢一点。
        #
        # 这一跳**必须带 cookie**：不带的话 302 照样返回，但 Location 里的签名是坏的，
        # 拿去下载直接 403——而错误出现在后面的 CDN 上，很难联想到是这里少了个头。
        # 最终 CDN 地址反过来不需要 cookie，只认 UA。
        final = dlink
        try:
            r = self.client.head(
                dlink, headers={"User-Agent": DLINK_UA, "Cookie": self.cookie},
                follow_redirects=False)
            if r.status_code < 400:
                final = r.headers.get("location") or dlink
        except httpx.HTTPError:
            pass
        return PlayTarget(
            file_name=file_name,
            url=final,
            download_url=dlink,
            cookie=self.cookie,
            ua=DLINK_UA,
            variants=[StreamVariant(
                key="origin", label="原画", url=final, size=size, origin=True,
            )],
            default_key="origin",
        )
