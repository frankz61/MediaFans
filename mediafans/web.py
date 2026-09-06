from __future__ import annotations

import base64
import dataclasses
import io
import json
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4

from .drive.base import BaseDrive
from .errors import MediaFansError
from .models import PlayTarget
from .proxy import QuietThreadingHTTPServer, relay_stream
from .utils import classify_netdisk, fmt_size, is_playable, media_kind, norm_path, normalize_netdisk

# 转码档只给容器名，原画给的是网盘的 format_type（已是 mime）
_CONTAINER_MIME = {"mp4": "video/mp4", "fmp4": "video/mp4",
                   "m3u8": "application/vnd.apple.mpegurl"}


def stream_mime(fmt: str) -> str:
    """归一成 <video>.canPlayType() 认得的 mime，认不出就留空（前端不做判断）."""
    fmt = (fmt or "").strip().lower()
    if "/" in fmt:
        return fmt
    return _CONTAINER_MIME.get(fmt, "")


# 分享里最多自动往下钻几层空壳目录
SHARE_AUTO_DESCEND_MAX = 6


class _PlayRegistry:
    """播放令牌 -> 直链目标（内存态，服务重启即失效，配合直链短时效）."""

    def __init__(self, cap: int = 100):
        self._cap = cap
        self._items: "OrderedDict[str, PlayTarget]" = OrderedDict()

    def add(self, target: PlayTarget) -> str:
        token = uuid4().hex
        self._items[token] = target
        while len(self._items) > self._cap:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> Optional[PlayTarget]:
        return self._items.get(token)


def qr_svg_data_uri(text: str) -> str:
    """把 URL 渲染成内联 SVG 二维码（SvgPathImage 是纯 Python，不需要 PIL）."""
    import qrcode
    import qrcode.image.svg

    buf = io.BytesIO()
    qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


class _LoginSessions:
    """扫码会话（内存态，服务重启即失效）."""

    def __init__(self, cap: int = 8):
        self._cap = cap
        self._items: "OrderedDict[str, dict]" = OrderedDict()

    def add(self, item: dict) -> str:
        sid = uuid4().hex
        self._items[sid] = item
        while len(self._items) > self._cap:
            self._items.popitem(last=False)
        return sid

    def get(self, sid: str) -> Optional[dict]:
        return self._items.get(sid)


class _AutoJobs:
    """一键找片是个跑几十秒的活儿，扔后台线程里跑，前端轮询进度。

    用轮询而不是 SSE：这个 HTTP 服务是 ThreadingHTTPServer，
    长连接推送在它上面要多一堆状态管理，轮询几行就够且更耐断线。
    """

    def __init__(self, cap: int = 20):
        self._cap = cap
        self._lock = threading.Lock()
        self._jobs: "OrderedDict[str, dict]" = OrderedDict()

    def start(self, runner: Callable[[Callable], dict]) -> str:
        job_id = uuid4().hex
        job = {"id": job_id, "steps": [], "done": False, "result": None, "error": ""}
        with self._lock:
            self._jobs[job_id] = job
            while len(self._jobs) > self._cap:
                self._jobs.popitem(last=False)

        def on_step(stage, info):
            with self._lock:
                job["steps"].append({"stage": stage, "message": info.get("message", "")})

        def work():
            try:
                job["result"] = runner(on_step)
            except Exception as e:
                job["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            finally:
                job["done"] = True

        threading.Thread(target=work, daemon=True).start()
        return job_id

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id or "")
            return dict(job, steps=list(job["steps"])) if job else None


class WebApp:
    """本地网页播放器：浏览器里浏览网盘 + 点播。

    浏览器的 <video> 发不出 Cookie，所以 /stream 内部走 relay_stream 补头转发，
    页面和播放地址同源（127.0.0.1），对使用者完全透明。
    """

    def __init__(self, drive_factory: Callable[..., BaseDrive],
                 search_fn: Optional[Callable] = None,
                 host: str = "127.0.0.1", port: int = 0,
                 cookie_path: Optional[Path] = None,
                 cookie_paths: Optional[dict] = None,
                 tv_token_path: Optional[Path] = None,
                 baidu_token_path: Optional[Path] = None,
                 baidu_oauth_cfg: Optional[dict] = None,
                 token: str = "", use_tv: bool = True,
                 tmdb_key: str = "", picker=None,
                 watch_path: Optional[Path] = None,
                 prefer_chinese: bool = True):
        # drive_factory(nd) -> BaseDrive，按网盘建驱动（nd: quark | baidu）
        self.drive_factory = drive_factory
        self.search_fn = search_fn  # fn(kw, netdisk) -> (links, errors)
        self.host = host
        # 绑到非回环地址时必须带令牌：这个服务能浏览整个网盘、能往里转存、
        # 还能用你的 cookie 取流，裸奔到局域网等于把网盘交出去
        self.token = token or ""
        self.cookie_path = Path(cookie_path) if cookie_path else None
        # 各网盘的 cookie 落盘位置（重新登录后失效对应驱动用）
        self.cookie_paths = {k: Path(v) for k, v in (cookie_paths or {}).items()
                             if v}
        if self.cookie_path and "quark" not in self.cookie_paths:
            self.cookie_paths["quark"] = self.cookie_path
        self.tv_token_path = Path(tv_token_path) if tv_token_path else None
        self.baidu_token_path = Path(baidu_token_path) if baidu_token_path else None
        self.baidu_oauth_cfg = dict(baidu_oauth_cfg or {})
        self.use_tv = use_tv
        self.tmdb_key = tmdb_key or ""
        # 追剧榜和搜索都优先华语内容（config: tmdb.prefer_chinese）
        self.prefer_chinese = bool(prefer_chinese)
        self.picker = picker
        self._tv = None
        self._tv_failed = ""     # TV 这条路挂了的原因
        self._tv_failed_at = 0.0  # 什么时候挂的——过了冷却期要再给它一次机会
        self.registry = _PlayRegistry()
        self.logins = _LoginSessions()
        self.jobs = _AutoJobs()
        self._tmdb = None
        from .agent import SeriesCache

        self.series_cache = SeriesCache()
        # 进度存服务端而不是浏览器：手机、平板、电脑一起用时，
        # 进度只记在某一台上等于没记
        from .watch import WatchStore

        self.watch = WatchStore(Path(watch_path) if watch_path
                                else Path.home() / ".mediafans" / "watch.json")
        self._drives: dict = {}
        self._thread: Optional[threading.Thread] = None
        self._server = QuietThreadingHTTPServer((host, port), self._make_handler())

    # ------------------------------------------------------------------ app
    SUPPORTED_NETDISKS = ("quark", "baidu")

    def _drive_for(self, netdisk: str) -> BaseDrive:
        """按网盘取（并缓存）驱动。凭据失效重登后调 invalidate_drive."""
        nd = normalize_netdisk(netdisk or "quark")
        if nd not in self.SUPPORTED_NETDISKS:
            raise MediaFansError(f"暂不支持网盘 '{nd}'（当前: {' | '.join(self.SUPPORTED_NETDISKS)}）")
        if nd not in self._drives:
            self._drives[nd] = self.drive_factory(nd)
        return self._drives[nd]

    def _nd_of(self, val) -> str:
        """只归一化+校验网盘名，不实例化驱动（跑后台任务时再按需建）."""
        nd = normalize_netdisk(str(val or "quark"))
        if nd not in self.SUPPORTED_NETDISKS:
            raise MediaFansError(f"暂不支持网盘 '{nd}'（当前: {' | '.join(self.SUPPORTED_NETDISKS)}）")
        return nd

    def invalidate_drive(self, netdisk: str) -> None:
        self._drives.pop(normalize_netdisk(netdisk or "quark"), None)

    @property
    def drive(self) -> BaseDrive:
        return self._drive_for("quark")

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def page_url(self, path: str = "", host: str = "") -> str:
        url = f"http://{host or self.host}:{self.port}/"
        args = []
        if path:
            args.append("path=" + quote(norm_path(path)))
        if self.token:
            args.append("token=" + quote(self.token))
        return url + ("?" + "&".join(args) if args else "")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # TV 挂了之后多久再试一次。
    # 不能永久记住失败：TV 直链是浏览器直连 CDN（实测 5-7 MB/s），
    # 退回代理要经这台服务器中转，实测只有 0.85 MB/s 还带秒级停顿。
    # 一次网络抖动就把之后**所有**播放永久降级到那条慢路上，
    # 表现就是「有时候会卡，重启就好了」——这种粘性失败最难查。
    TV_RETRY_AFTER = 300.0

    @property
    def tv(self):
        """TV 版客户端（拿得到免凭据直链）。挂过就暂时走代理兜底，冷却后重试。"""
        if not self.use_tv:
            return None
        if self._tv_failed:
            if time.time() - self._tv_failed_at < self.TV_RETRY_AFTER:
                return None
            self._tv_failed = ""      # 冷却结束，重新试一次
            self._tv = None
        if self._tv is None:
            if not self.tv_token_path or not self.tv_token_path.exists():
                self._tv_failed = "没有 TV token（mediafans login --tv 可扫码获取）"
                self._tv_failed_at = time.time()
                return None
            try:
                from .drive.quark_tv import QuarkTVClient

                self._tv = QuarkTVClient(self.tv_token_path)
            except Exception as e:
                self._tv_failed = str(e)
                self._tv_failed_at = time.time()
                return None
        return self._tv

    # ------------------------------------------------------------------ api
    @staticmethod
    def _watch_key(path: str, nd: str) -> str:
        """进度键。夸克用裸路径（兼容旧 watch.json），其他网盘加前缀防撞."""
        nd = normalize_netdisk(nd or "quark")
        return path if nd == "quark" else f"{nd}:{path}"

    @staticmethod
    def _watch_parse(mark_dict: dict) -> dict:
        """把进度记录里的网盘前缀拆出来，前端才知道去哪个盘接着播."""
        path = str(mark_dict.get("path") or "")
        for nd in ("baidu",):
            if path.startswith(f"{nd}:"):
                mark_dict = dict(mark_dict, netdisk=nd,
                                 play_path=path[len(nd) + 1:])
                return mark_dict
        return dict(mark_dict, netdisk="quark", play_path=path)

    def api_list(self, raw_path: str, nd: str = "quark") -> dict:
        drive = self._drive_for(nd)
        nd = drive.name
        path = norm_path(unquote(raw_path)) if raw_path else norm_path(drive.save_dir)
        fid = drive.resolve_path(path)
        if not fid:
            raise MediaFansError(f"目录不存在: {path}")
        files: List[dict] = []
        for f in drive.list_files(fid):
            files.append({
                "name": f.name,
                "is_dir": f.is_dir,
                "kind": "dir" if f.is_dir else media_kind(f.name),
                "playable": (not f.is_dir) and is_playable(f.name),
                "size": f.size,
                "size_h": "-" if f.is_dir else fmt_size(f.size),
                "updated_at": f.updated_at,
                "path": (path.rstrip("/") + "/" + f.name) if path != "/" else "/" + f.name,
            })
            if files[-1]["playable"]:
                w = self._mark_of(files[-1]["path"], nd)
                if w:
                    files[-1]["watched"] = w
        files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {"path": path, "netdisk": nd, "files": files}

    def api_play(self, raw_path: str, nd: str = "quark") -> dict:
        """取播放地址。优先走 TV 版直链，浏览器直连网盘 CDN，不经本机转发。

        PC 版直链必须带 Cookie，而 <video> 发不出 Cookie，所以只能本地转发；
        TV 版走 OAuth，直链本身免凭据（实测裸请求 206），浏览器可以直接播。
        每一档同时给出直链和代理地址，直链万一被拒（比如 Referer 泄漏）前端能就地回退。
        """
        drive = self._drive_for(nd)
        nd = drive.name
        path = norm_path(unquote(raw_path))
        name = path.rsplit("/", 1)[-1]
        fid = drive.resolve_entry(path)
        if not fid:
            raise MediaFansError(f"文件不存在: {path}")

        direct = False
        target = None
        tv = self.tv if nd == "quark" else None
        if tv is not None:
            try:
                target = tv.get_play_target(fid, name=name)
                direct = True
            except Exception as e:
                # TV 挂了（token 过期、设备数超限…）先退回代理，冷却后自己再试
                self._tv_failed = str(e)
                self._tv_failed_at = time.time()
                target = None
        if target is None:
            target = drive.get_play_target(fid, name=name)

        streams = []
        for v in target.variants:
            token = self.registry.add(dataclasses.replace(target, url=v.url))
            streams.append({
                "key": v.key,
                "label": v.display(),
                # 直链模式下 url 是网盘 CDN 的绝对地址，proxy_url 只作回退用
                "url": v.url if direct else f"/stream?t={token}",
                "proxy_url": f"/stream?t={token}",
                "direct": direct,
                "height": v.height,
                "size_h": fmt_size(v.size) if v.size else "",
                "bitrate": round(v.bitrate),
                "origin": v.origin,
                "mime": stream_mime(v.fmt),
            })
        default_key = target.default_key or (streams[0]["key"] if streams else "")
        default_url = next((s["url"] for s in streams if s["key"] == default_key),
                           streams[0]["url"] if streams else "")
        return {
            "file_name": target.file_name or name,
            "netdisk": nd,
            "stream_url": default_url,
            "streams": streams,
            "default_key": default_key,
            "has_transcode": any(not s["origin"] for s in streams),
            "needs_cookie": bool(target.cookie),
            "direct": direct,
            "fallback_reason": "" if direct else (self._tv_failed or ""),
        }

    # ------------------------------------------------------------------ 扫码登录
    def api_login_start(self, kind: str) -> dict:
        """开一个扫码会话。quark = 网页版（存 cookie），tv = TV 版（存 token）."""
        from .auth import BaiduQRLogin, QuarkQRLogin, QuarkTVLogin

        kind = (kind or "quark").strip().lower()
        if kind == "baidu":
            if not self.cookie_paths.get("baidu"):
                raise MediaFansError("没有配置百度 cookie 存放位置")
            flow = BaiduQRLogin()
            handle, qr = flow.get_qr_code()      # 百度直接给图片地址
        elif kind == "tv":
            if self.tv_token_path is None:
                raise MediaFansError("没有配置 TV token 存放位置")
            flow = QuarkTVLogin(device_id=self._existing_device_id())
            handle, qr = flow.get_qr_code()          # TV 直接下发 PNG
        elif kind == "quark":
            if self.cookie_path is None:
                raise MediaFansError("没有配置 cookie 存放位置")
            flow = QuarkQRLogin()
            handle, qr_url = flow.get_qr_code()      # 网页版给的是 URL，本地渲染成二维码
            qr = qr_svg_data_uri(qr_url)
        else:
            raise MediaFansError(f"未知登录方式: {kind}")
        sid = self.logins.add({"kind": kind, "flow": flow, "handle": handle})
        return {"sid": sid, "kind": kind, "qr": qr}

    def _existing_device_id(self) -> str:
        """复用已存的 device_id —— 换设备号会让旧 refresh_token 失效."""
        if self.tv_token_path and self.tv_token_path.exists():
            try:
                return str(json.loads(
                    self.tv_token_path.read_text(encoding="utf-8")).get("device_id") or "")
            except (OSError, ValueError):
                pass
        return ""

    def api_login_poll(self, sid: str) -> dict:
        """轮询一次；成功即落盘并让下次请求用上新凭据."""
        item = self.logins.get(sid or "")
        if item is None:
            raise MediaFansError("登录会话已失效，请重新点登录")
        state, info = item["flow"].poll_once(item["handle"])
        if state == "waiting":
            # 「已扫码，等确认」也是等待，但得让用户知道该去手机上点确认
            return {"state": "waiting", "message": info or ""}
        if state == "failed":
            return {"state": "failed", "message": info or "扫码失败"}
        if item["kind"] == "baidu":
            cookie = item["flow"].exchange(info)
            path = self.cookie_paths["baidu"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(cookie, encoding="utf-8")
            self.invalidate_drive("baidu")
            name = ""
            try:
                name = self._drive_for("baidu").account_name()
            except Exception:
                # cookie 已经落盘了，昵称取不到多半是还没配 OAuth，
                # 不该把登录报成失败
                pass
            return {"state": "success", "kind": "baidu",
                    "message": f"百度已登录{('：' + name) if name else ''}，"
                               f"cookie 已保存（可打开分享、转存）"}
        if item["kind"] == "tv":
            token = item["flow"].exchange_code(info)
            self.tv_token_path.parent.mkdir(parents=True, exist_ok=True)
            self.tv_token_path.write_text(
                json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"state": "success", "kind": "tv",
                    "message": f"TV token 已保存到 {self.tv_token_path}"}
        cookie = item["flow"].exchange_ticket(info)
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_path.write_text(cookie, encoding="utf-8")
        self.invalidate_drive("quark")  # 让下次请求用新 cookie 重建驱动
        name = ""
        try:
            name = self.drive.account_name()
        except Exception:
            pass  # cookie 已经落盘了，昵称只是好看，取不到也不能把登录报成失败
        return {"state": "success", "kind": "quark",
                "message": f"已登录{('：' + name) if name else ''}，cookie 已保存"}

    def api_login_baidu(self, payload: Optional[dict] = None) -> dict:
        """百度「粘贴 cookie」——扫码失败时的备用通道。

        主路是扫码（api_login_start('baidu')）。这里不再有 OAuth：官方开放平台
        把第三方应用锁死在 /apps/{应用名}，够不到用户自己的目录，那条路走不通。

        GET 语义（payload 为空）：返回当前状态，前端据此渲染表单。
        POST 语义：存 cookie 并让驱动缓存失效。
        """
        from .auth import BaiduQRLogin

        payload = payload or {}
        path = self.cookie_paths.get("baidu")
        has_cookie = bool(path and path.exists())
        if not payload:
            return {"has_cookie": has_cookie}
        cookie = str(payload.get("cookie") or "").strip()
        if not cookie:
            raise MediaFansError("请粘贴 Cookie")
        if "BDUSS=" not in cookie:
            raise MediaFansError("cookie 里没有 BDUSS，请复制完整的 Cookie 头")
        if not path:
            raise MediaFansError("没有配置百度 cookie 存放位置")
        pairs = [(kv.split("=", 1)[0].strip(), kv.split("=", 1)[1].strip())
                 for kv in cookie.split(";") if "=" in kv]
        cookie = BaiduQRLogin.clean_cookie(pairs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cookie, encoding="utf-8")
        self.invalidate_drive("baidu")
        msg = "cookie 已保存"
        if "STOKEN=" not in cookie:
            msg += "，但缺 STOKEN，转存会失败（浏览和播放不受影响）"
        return {"ok": True, "message": msg}

    # ------------------------------------------------------------------ 追剧
    @property
    def tmdb(self):
        if self._tmdb is None and self.tmdb_key:
            from .metadata.tmdb import TmdbClient

            self._tmdb = TmdbClient(self.tmdb_key)
        return self._tmdb

    # 榜单：剧集三档 + 电影三档。值是 (华语档参数, 全球榜端点名, media_type)
    DISCOVER = {
        "airing": ("airing", "airing_today", "tv"),
        "onair": ("onair", "on_the_air", "tv"),
        "popular": ("popular", "popular_tv", "tv"),
        "now": ("now", "now_playing", "movie"),
        "upcoming": ("upcoming", "upcoming", "movie"),
        "hot": ("hot", "popular_movie", "movie"),
    }

    def api_discover(self, kind: str = "airing") -> dict:
        """榜单，华语在前。剧集和电影共用一个端点，靠 kind 分。

        TMDB 的榜单是全球榜，被各国日播肥皂剧刷屏，华语内容基本挤不进去。
        所以华语单独用 /discover 取一档摆在前面，全球榜接在后面——是「优先」
        不是「只要」，外语新片新剧还在，只是不占满第一屏。
        """
        if not self.tmdb:
            raise MediaFansError("未配置 tmdb.api_key，无法拉取榜单")
        cn_kind, global_fn, media = self.DISCOVER.get(kind) or self.DISCOVER["airing"]
        chinese_fn = self.tmdb.chinese_movie if media == "movie" else self.tmdb.chinese_tv
        fn = getattr(self.tmdb, global_fn)
        what = "电影" if media == "movie" else "剧集"
        items, note = [], ""
        if self.prefer_chinese:
            try:
                items = chinese_fn(cn_kind)
            except Exception as e:
                # 华语这一档挂了不该让整个榜打不开，但也不能不吭声
                note = f"华语榜没拉到（{str(e)[:60]}），下面是全球榜"
        try:
            seen = {i.tmdb_id for i in items}
            items += [i for i in fn() if i.tmdb_id not in seen]
        except Exception as e:
            if not items:
                raise MediaFansError(f"拉取{what}榜失败: {str(e)[:120]}")
            note = f"全球榜没拉到（{str(e)[:60]}），下面只有华语"
        return {"kind": kind, "media_type": media, "note": note, "items": [{
            "title": i.title, "original": i.original_title, "year": i.year,
            "rating": i.rating, "overview": i.overview, "media_type": media,
            "chinese": self.tmdb.is_chinese(i),
            "animation": self.tmdb.is_animation(i),
            "poster": i.poster, "tmdb_id": i.tmdb_id,
        } for i in items]}

    def api_search_media(self, query: str) -> dict:
        """按作品搜（TMDB），不是搜分享链接。

        用户找的是「这部剧」，不是「某个网盘链接」。先定位到作品，
        才有权威的季/集结构可用，后面找资源、补缺集才有基准。
        """
        query = (query or "").strip()
        if not query:
            raise MediaFansError("缺少搜索关键词")
        if not self.tmdb:
            raise MediaFansError("未配置 tmdb.api_key，无法按作品搜索")
        try:
            items = self.tmdb.search(query, "multi", prefer_chinese=self.prefer_chinese)
        except Exception as e:
            raise MediaFansError(f"搜索失败: {str(e)[:120]}")
        return {"query": query, "items": [{
            "title": i.title, "original": i.original_title,
            "media_type": i.media_type, "year": i.year, "rating": i.rating,
            "chinese": self.tmdb.is_chinese(i),
            "animation": self.tmdb.is_animation(i),
            "overview": i.overview, "poster": i.poster, "tmdb_id": i.tmdb_id,
        } for i in items if i.tmdb_id]}

    def _mark_of(self, path: str, nd: str = "quark") -> Optional[dict]:
        m = self.watch.get(self._watch_key(path, nd)) if path else None
        if not m:
            return None
        return {"percent": m.percent, "finished": m.finished,
                "resume_at": m.resume_at, "position": m.position,
                "duration": m.duration}

    def _attach_watch(self, data: dict, nd: str = "quark") -> dict:
        """把「看到哪儿了」附到列表上。

        列出可播放文件的地方都带上，进度才在哪儿都看得见——
        不然用户得先点进去播一下才知道自己看没看过。

        一集可能存了好几份（见 EpisodeRow.copies），进度是记在**具体文件**上的。
        只看主副本的话，用户中途换过来源就会显示成「没看过」。所以每一份都查，
        取最近更新的那条。
        """
        for ep in data.get("episodes", []):
            paths = [c.get("path", "") for c in (ep.get("copies") or [])]
            if not paths and ep.get("local"):
                paths = [ep["local"].get("path", "")]
            marks = [m for m in (self.watch.get(self._watch_key(p, nd))
                                 for p in paths if p) if m]
            if marks:
                m = max(marks, key=lambda x: x.updated)
                ep["watched"] = {"percent": m.percent, "finished": m.finished,
                                 "resume_at": m.resume_at, "position": m.position,
                                 "duration": m.duration, "path": m.path}
        return data

    def api_watch_save(self, payload: dict) -> dict:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise MediaFansError("缺少 path")
        meta = {k: payload.get(k) for k in
                ("name", "size_h", "title", "year", "poster", "ep_title")}
        meta["media_type"] = "movie" if payload.get("media_type") == "movie" else "tv"
        for k in ("tmdb_id", "season", "episode"):
            try:
                meta[k] = int(payload[k]) if payload.get(k) is not None else None
            except (TypeError, ValueError):
                meta[k] = None
        key = self._watch_key(path, str(payload.get("netdisk") or "quark"))
        m = self.watch.save(key, float(payload.get("position") or 0),
                            float(payload.get("duration") or 0), **meta)
        return {"ok": True, "mark": m.as_dict()}

    def api_watch_get(self, path: str, nd: str = "quark") -> dict:
        m = self.watch.get(self._watch_key(unquote(path or ""), nd))
        return {"mark": m.as_dict() if m else None}

    def api_watch_recent(self, limit: int = 12) -> dict:
        try:
            n = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            n = 12
        return {"items": [self._watch_parse(m.as_dict())
                          for m in self.watch.recent(n)]}

    def api_watch_forget(self, payload: dict) -> dict:
        # 前端传的 path 就是存储键（baidu 带前缀），按原样删
        return {"ok": self.watch.forget(str(payload.get("path") or ""))}

    def api_series(self, tmdb_id: int, season: int, refresh: bool = False,
                   nd: str = "quark", media: str = "tv") -> dict:
        """一季的剧集矩阵，或者一部电影。

        只扫网盘、不搜资源——首屏要快（1 秒内）。找来源很慢（要开分享、
        递归列目录），交给 /api/series/scan 单独跑。

        电影走同一个端点、同一份缓存：它就是只有一行的「季」（season=0），
        下游的播放、进度、转存因此完全不用分两套。
        """
        from .agent import build_movie, build_series

        if not self.tmdb:
            raise MediaFansError("未配置 tmdb.api_key")
        nd = self._nd_of(nd)
        is_movie = str(media or "tv") == "movie"
        season = 0 if is_movie else int(season)
        key = (nd, int(tmdb_id), season)
        if not refresh:
            cached = self.series_cache.get(key)
            if cached is not None:
                # 进度变得快，缓存的是剧集结构，看没看过每次都现取
                return self._attach_watch(dict(cached.as_dict(), cached=True), nd)
        try:
            if is_movie:
                view = build_movie(lambda: self._drive_for(nd), None, self.tmdb,
                                   int(tmdb_id), with_sources=False, netdisk=nd)
            else:
                view = build_series(lambda: self._drive_for(nd), None, self.tmdb,
                                    int(tmdb_id), season, with_sources=False,
                                    netdisk=nd)
        except MediaFansError:
            raise
        except Exception as e:
            what = "影片" if is_movie else "剧集"
            raise MediaFansError(f"读取{what}信息失败: {str(e)[:120]}")
        self.series_cache.put(key, view)
        return self._attach_watch(dict(view.as_dict(), cached=False), nd)

    def api_series_scan(self, payload: dict) -> dict:
        """开一个后台任务：搜资源并把每一集（电影就是那一部）能从哪补铺开。"""
        from .agent import build_movie, build_series

        tmdb_id = int(payload.get("tmdb_id") or 0)
        is_movie = str(payload.get("media") or "tv") == "movie"
        season = 0 if is_movie else int(payload.get("season") or 0)
        if not tmdb_id or (not season and not is_movie):
            raise MediaFansError("缺少 tmdb_id / season")
        if not self.tmdb:
            raise MediaFansError("未配置 tmdb.api_key")
        if self.search_fn is None:
            raise MediaFansError("未配置搜索源")
        nd = self._nd_of(payload.get("netdisk"))

        def runner(on_step):
            if is_movie:
                view = build_movie(lambda: self._drive_for(nd), self.search_fn,
                                   self.tmdb, tmdb_id, on_step=on_step, netdisk=nd)
            else:
                view = build_series(lambda: self._drive_for(nd), self.search_fn,
                                    self.tmdb, tmdb_id, season, on_step=on_step,
                                    netdisk=nd)
            self.series_cache.put((nd, tmdb_id, season), view)
            return self._attach_watch(view.as_dict(), nd)

        return {"job": self.jobs.start(runner)}

    def api_episode_fetch(self, payload: dict) -> dict:
        """把某一集从指定来源转存下来。按集补，不动别的集。"""
        from .agent import fetch_episode

        tmdb_id, season = int(payload.get("tmdb_id") or 0), int(payload.get("season") or 0)
        episode = int(payload.get("episode") or 0)
        index = int(payload.get("index") or 0)
        nd = self._nd_of(payload.get("netdisk"))
        view = self.series_cache.get((nd, tmdb_id, season))
        if view is None:
            raise MediaFansError("这一季的资源信息已过期，请重新扫描")
        row = next((r for r in view.rows if r.episode == episode), None)
        if row is None:
            raise MediaFansError(f"没有第 {episode} 集")
        if row.local:
            return {"path": row.local.path, "already": True}
        if not (0 <= index < len(row.sources)):
            raise MediaFansError("这个来源不存在了，请重新扫描")
        src = row.sources[index]
        try:
            path = fetch_episode(lambda: self._drive_for(nd), src, view.local_dir)
        except Exception as e:
            raise MediaFansError(f"转存失败: {str(e)[:140]}")
        # 存完就地更新缓存，前端不用重新扫
        from .agent import LocalFile

        row.add_copy(LocalFile(path=path, name=src.file.name, size=src.file.size,
                               height=src.file.height, source=src.file.source))
        self.series_cache.put((nd, tmdb_id, season), view)
        return {"path": path, "already": False, "name": src.file.name}

    def api_episode_fetch_all(self, payload: dict) -> dict:
        """把这一集（或这部片）能找到的版本都转存下来，供播放时切换。

        开后台任务：一个来源就是一次「开分享 + 递归列目录 + 转存」，
        转五个版本几十秒是常事，同步请求会顶到浏览器超时。
        """
        from .agent import LocalFile, fetch_all

        tmdb_id = int(payload.get("tmdb_id") or 0)
        is_movie = str(payload.get("media") or "tv") == "movie"
        season = 0 if is_movie else int(payload.get("season") or 0)
        episode = int(payload.get("episode") or 0)
        nd = self._nd_of(payload.get("netdisk"))
        view = self.series_cache.get((nd, tmdb_id, season))
        if view is None:
            raise MediaFansError("资源信息已过期，请重新扫描")
        row = next((r for r in view.rows if r.episode == episode), None)
        if row is None:
            raise MediaFansError(f"没有第 {episode} 集")
        if not row.sources:
            raise MediaFansError("还没有找到来源，先点「找资源」")
        self._drive_for(nd).check_transfer_ready()
        sources = list(row.sources)
        have = [c.name for c in row.copies]
        local_dir = view.local_dir

        def runner(on_step):
            res = fetch_all(lambda: self._drive_for(nd), sources, local_dir,
                            have=have, on_step=on_step)
            # 存完就地补进缓存，前端不用重新扫一遍
            live = self.series_cache.get((nd, tmdb_id, season)) or view
            r = next((x for x in live.rows if x.episode == episode), None)
            if r is not None:
                for f in res["saved"]:
                    r.add_copy(LocalFile(path=f["path"], name=f["name"],
                                         size=f["size"], height=f["height"],
                                         source=f["source"]))
                self.series_cache.put((nd, tmdb_id, season), live)
            out = self._attach_watch(live.as_dict(), nd)
            out["fetched"] = res
            return out

        return {"job": self.jobs.start(runner)}

    def api_season_fetch(self, payload: dict) -> dict:
        """一键把这一季能补的集全补上。开后台任务，进度靠轮询。

        27 集逐个点太折磨人，而且逐集各挑各的最高画质会拼出画质不一的大杂烩。
        分组逻辑在 agent/series.py 的 plan_batch()。
        """
        from .agent import LocalFile, fetch_batch, plan_batch

        tmdb_id, season = int(payload.get("tmdb_id") or 0), int(payload.get("season") or 0)
        nd = self._nd_of(payload.get("netdisk"))
        view = self.series_cache.get((nd, tmdb_id, season))
        if view is None:
            raise MediaFansError("这一季的资源信息已过期，请重新扫描")
        want = [int(e) for e in (payload.get("episodes") or [])]
        rows = [r for r in view.rows if not want or r.episode in want]
        groups = plan_batch(rows)
        if not groups:
            raise MediaFansError("没有可以补的集（都已存在，或还没找到来源）")
        # 登录态先问一句：40 集的活跑到一半才发现要重登，前面的等待全白费
        self._drive_for(nd).check_transfer_ready()

        def runner(on_step):
            on_step("plan", {"message":
                             f"用 {len(groups)} 个来源补 "
                             f"{sum(len(g.episodes) for g in groups)} 集"})
            res = fetch_batch(lambda: self._drive_for(nd), groups, view.local_dir, on_step)
            # 存完就地更新缓存，前端不用重新扫一遍
            live = self.series_cache.get((nd, tmdb_id, season)) or view
            done = set(res.saved) | set(res.skipped)
            for r in live.rows:
                if r.episode in done and not r.local:
                    src = next((x for x in r.sources
                                if x.file.name == res.names.get(r.episode)), None)
                    f = src.file if src else None
                    r.local = LocalFile(
                        path=res.paths[r.episode], name=res.names[r.episode],
                        size=f.size if f else 0, height=f.height if f else 0,
                        source=f.source if f else "")
            self.series_cache.put((nd, tmdb_id, season), live)
            out = self._attach_watch(live.as_dict())
            out["batch"] = res.as_dict()
            return out

        return {"job": self.jobs.start(runner)}

    def api_auto_start(self, payload: dict) -> dict:
        """开一个「自动找片」任务，立刻返回 job id，进度靠轮询。"""
        name = str(payload.get("name") or "").strip()
        if not name:
            raise MediaFansError("缺少剧名")
        if self.search_fn is None:
            raise MediaFansError("未配置搜索源（config 里 search.sources）")
        season = payload.get("season")
        season = int(season) if season not in (None, "", 0) else None
        tmdb_id = int(payload.get("tmdb_id") or 0)
        do_save = bool(payload.get("save", True))
        nd = self._nd_of(payload.get("netdisk"))

        def runner(on_step):
            from .agent import auto_fetch

            # 同名不同作靠这个分开：《一人之下》是动画，真人版叫《异人之下》，
            # 而且网盘分享经常把两者打包在一起
            from .agent import Work

            work = Work(titles=[t for t in (name, str(payload.get("original") or "")) if t],
                        animation=(bool(payload["animation"])
                                   if payload.get("animation") is not None else None))
            year, episodes = "", 0
            if tmdb_id and self.tmdb and str(payload.get("media_type") or "tv") == "tv":
                try:
                    d = self.tmdb.tv_detail(tmdb_id)
                    from .agent import episode_yardstick

                    year = d.get("year") or ""
                    episodes = episode_yardstick(d, season)
                    if d.get("animation") is not None:
                        work.animation = bool(d["animation"])
                    if d.get("original_title"):
                        work.titles.append(d["original_title"])
                    on_step("meta", {"message":
                                     f"TMDB：{d['title']} 共 {d['total_seasons']} 季 "
                                     f"{d['total_episodes']} 集"})
                except Exception:
                    pass    # 元数据只是加分项
            res = auto_fetch(lambda: self._drive_for(nd), self.search_fn, name,
                             year=year, season=season, episodes=episodes,
                             netdisk=nd,
                             picker=self.picker, do_save=do_save, work=work,
                             on_step=lambda st, info: on_step(st, info))
            return {
                "ok": res.ok,
                "error": res.error,
                "netdisk": nd,
                "saved": res.saved,
                "already": res.already,
                "saved_dir": res.saved_dir,
                "ai_used": res.ai_used,
                "ai_note": res.ai_note,
                "verdict": ({"reason": res.verdict.reason,
                             "confidence": res.verdict.confidence}
                            if res.verdict else None),
                "picked": ({"title": res.picked.title,
                            "score": res.picked.score,
                            "reasons": res.picked.reasons,
                            "playable": res.picked.probe.playable,
                            "size_h": res.picked.probe.size_h}
                           if res.picked else None),
                "candidates": [{
                    "title": sc.title, "score": sc.score,
                    "ok": sc.probe.ok, "error": sc.probe.error,
                    "playable": sc.probe.playable, "size_h": sc.probe.size_h,
                    "reasons": sc.reasons,
                } for sc in res.scored[:8]],
            }

        return {"job": self.jobs.start(runner)}

    def api_auto_status(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise MediaFansError("任务不存在或已过期")
        return job

    # ------------------------------------------------------------------ 搜索/转存
    def api_search(self, kw: str, netdisk: str) -> dict:
        from .models import ShareLink  # noqa: F401  (类型提示用)

        kw = (kw or "").strip()
        if not kw:
            raise MediaFansError("缺少搜索关键词")
        if self.search_fn is None:
            raise MediaFansError("未配置搜索源（config 里 search.sources）")
        links, errors = self.search_fn(kw, (netdisk or "").strip() or None)
        return {
            "results": [{
                "netdisk": l.netdisk,
                "title": l.display_title(),
                "note": l.note,
                "url": l.url,
                "passcode": l.passcode,
                "datetime": l.datetime,
                "source": l.source,
            } for l in links[:60]],
            "errors": [f"{name}: {e}" for name, e in errors],
        }

    def api_share(self, url: str, code: str = "", dir_fid: str = "",
                  crumb: str = "") -> dict:
        """列分享里某一层的内容。

        分享常见形态是「一层套一层的空壳目录」（分享 -> 片名/ -> 4K/ -> 真文件），
        以前只列顶层，用户看到一个空文件夹就卡住了。这里会自动往下钻过那些
        「没有文件、只有唯一子目录」的层，直接把人送到有东西的地方，路径记在面包屑里。
        """
        nd = classify_netdisk(url or "")
        if nd not in self.SUPPORTED_NETDISKS:
            raise MediaFansError("不是有效的夸克/百度分享链接")
        drive = self._drive_for(nd)
        ctx = drive.open_share(url, passcode=code or "")
        fid = dir_fid or "0"
        trail = [x for x in (crumb or "").split("/") if x]
        entries = drive.list_share_files(ctx, fid)
        for _ in range(SHARE_AUTO_DESCEND_MAX):
            dirs = [f for f in entries if f.is_dir]
            if any(not f.is_dir for f in entries) or len(dirs) != 1:
                break
            fid = dirs[0].fid
            trail.append(dirs[0].name)
            entries = self.drive.list_share_files(ctx, fid)

        files = []
        for f in entries:
            kind = "dir" if f.is_dir else media_kind(f.name)
            files.append({
                "name": f.name,
                "is_dir": f.is_dir,
                "fid": f.fid,
                "kind": kind,
                "playable": (not f.is_dir) and is_playable(f.name),
                "size": f.size,
                "size_h": "-" if f.is_dir else fmt_size(f.size),
            })
        files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        playable = [f for f in files if f["playable"]]
        return {
            "url": url, "code": code or "", "netdisk": nd,
            "dir_fid": fid, "crumb": "/".join(trail),
            "files": files,
            "counts": {
                "playable": len(playable),
                "dirs": sum(1 for f in files if f["is_dir"]),
                "junk": sum(1 for f in files if not f["is_dir"] and not f["playable"]),
                "size_h": fmt_size(sum(f["size"] for f in playable)) if playable else "-",
            },
        }

    def api_save(self, payload: dict) -> dict:
        url = str(payload.get("url") or "")
        if not url:
            raise MediaFansError("缺少 url")
        nd = classify_netdisk(url)
        if nd not in self.SUPPORTED_NETDISKS:
            raise MediaFansError("不是有效的夸克/百度分享链接")
        drive = self._drive_for(nd)
        code = str(payload.get("code") or "")
        wanted = {str(x) for x in (payload.get("fids") or [])}
        to_dir = str(payload.get("to") or "").strip() or drive.save_dir
        ctx = drive.open_share(url, passcode=code)
        # share_fid_token 只在「列出该层」时才拿得到，所以必须列用户当前所在的那一层，
        # 否则从子目录里勾选的文件会因为找不到 token 而转存失败
        files = drive.list_share_files(ctx, str(payload.get("dir_fid") or "0"))
        if wanted:
            files = [f for f in files if f.fid in wanted]
        if not files:
            raise MediaFansError("没有可转存的文件")
        saved = drive.save_share_files(ctx, files, to_dir)
        return {"saved": len(saved), "dir": norm_path(to_dir), "netdisk": nd}

    # ------------------------------------------------------------------ http
    def _make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True  # 视频块要立刻上路，别等凑包

            def log_message(self, *args):  # 静音访问日志
                pass

            def _authorized(self, query: dict) -> bool:
                """没设令牌 = 只监听本机，放行；设了令牌则本机免验、外部认令牌。

                注意「本机免验」只对真正的本机浏览器成立。放在 nginx 反代后面时，
                请求也是从 127.0.0.1 过来的，照免不误就等于认证被绕过——
                所以带了 X-Forwarded-For 的一律当外部请求处理。
                """
                if not app.token:
                    return True
                proxied = bool(self.headers.get("X-Forwarded-For"))
                if not proxied and self.client_address[0] in ("127.0.0.1", "::1"):
                    return True
                if query.get("token") == app.token:
                    return True
                for pair in (self.headers.get("Cookie") or "").split(";"):
                    name, _, value = pair.strip().partition("=")
                    if name == "mf_token" and value == app.token:
                        return True
                return False

            def _deny(self) -> None:
                self._json(403, {"error": "需要访问令牌：请用启动时打印的完整链接（带 token）打开"})

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if not self._authorized(query):
                    self._deny()
                    return
                try:
                    if parsed.path == "/api/series/scan":
                        self._json(200, app.api_series_scan(self._body()))
                    elif parsed.path == "/api/watch":
                        self._json(200, app.api_watch_save(self._body()))
                    elif parsed.path == "/api/watch/forget":
                        self._json(200, app.api_watch_forget(self._body()))
                    elif parsed.path == "/api/season/fetch":
                        self._json(200, app.api_season_fetch(self._body()))
                    elif parsed.path == "/api/episode/fetch/all":
                        self._json(200, app.api_episode_fetch_all(self._body()))
                    elif parsed.path == "/api/episode/fetch":
                        self._json(200, app.api_episode_fetch(self._body()))
                    elif parsed.path == "/api/login/baidu":
                        self._json(200, app.api_login_baidu(self._body()))
                    elif parsed.path == "/api/auto":
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length) if length else b"{}"
                        try:
                            payload = json.loads(raw.decode("utf-8") or "{}")
                        except ValueError:
                            raise MediaFansError("请求体不是合法 JSON")
                        self._json(200, app.api_auto_start(payload))
                    elif parsed.path == "/api/login/start":
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length) if length else b"{}"
                        try:
                            payload = json.loads(raw.decode("utf-8") or "{}")
                        except ValueError:
                            raise MediaFansError("请求体不是合法 JSON")
                        self._json(200, app.api_login_start(str(payload.get("kind") or "")))
                    elif parsed.path == "/api/save":
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length) if length else b"{}"
                        try:
                            payload = json.loads(raw.decode("utf-8") or "{}")
                        except ValueError:
                            raise MediaFansError("请求体不是合法 JSON")
                        self._json(200, app.api_save(payload))
                    else:
                        self._json(404, {"error": "not found"})
                except MediaFansError as e:
                    self._json(400, {"error": str(e)})
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except Exception as e:  # 兜底：不让单个请求拖垮服务
                    try:
                        self._json(500, {"error": f"{type(e).__name__}: {e}"})
                    except Exception:
                        pass

            @property
            def server_version(self):
                return "MediaFansWeb/1.0"

            def _send(self, status: int, body: bytes, content_type: str,
                      extra_headers=None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                # 页面的 JS 是内联的，每次部署都变；不禁缓存的话浏览器会按
                # 启发式缓存一直跑旧代码，看起来就像「部署了但没生效」。
                # 接口返回的是网盘现状和播放进度，也没有一条该被缓存。
                self.send_header("Cache-Control", "no-store, must-revalidate")
                for name, value in (extra_headers or []):
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: dict) -> None:
                self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    raise MediaFansError("请求体不是合法 JSON")

            def _stream(self, token: str, send_body: bool) -> None:
                target = app.registry.get(token)
                if target is None:
                    self._json(404, {"error": "播放令牌无效或已过期，刷新页面重选文件"})
                    return
                relay_stream(self, target, send_body=send_body)

            def do_HEAD(self):  # noqa: N802
                """部分播放内核先 HEAD 探测是否支持 Range，再决定要不要边下边播。"""
                parsed = urlparse(self.path)
                if parsed.path != "/stream":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if not self._authorized(query):
                    self._deny()
                    return
                try:
                    self._stream(query.get("t", ""), send_body=False)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if not self._authorized(query):
                    self._deny()
                    return
                try:
                    if parsed.path in ("/", "/index.html"):
                        # 首页带对令牌就种个 cookie，之后 <video src> 之类的子请求
                        # 就不用每个都在 URL 里挂令牌了
                        extra = None
                        if app.token and query.get("token") == app.token:
                            extra = [("Set-Cookie",
                                      f"mf_token={app.token}; Path=/; SameSite=Lax; Max-Age=604800")]
                        self._send(200, PAGE_HTML.encode("utf-8"),
                                   "text/html; charset=utf-8", extra)
                    elif parsed.path == "/api/list":
                        self._json(200, app.api_list(query.get("path", ""),
                                                     query.get("nd", "quark")))
                    elif parsed.path == "/api/play":
                        if not query.get("path"):
                            raise MediaFansError("缺少 path 参数")
                        self._json(200, app.api_play(query["path"],
                                                     query.get("nd", "quark")))
                    elif parsed.path == "/api/series":
                        self._json(200, app.api_series(
                            query.get("tmdb_id", 0), query.get("season", 1),
                            query.get("refresh") == "1", query.get("nd", "quark"),
                            query.get("media", "tv")))
                    elif parsed.path == "/api/watch/recent":
                        self._json(200, app.api_watch_recent(query.get("limit", 12)))
                    elif parsed.path == "/api/watch":
                        self._json(200, app.api_watch_get(query.get("path", ""),
                                                          query.get("nd", "quark")))
                    elif parsed.path == "/api/search/media":
                        self._json(200, app.api_search_media(query.get("kw", "")))
                    elif parsed.path == "/api/discover":
                        self._json(200, app.api_discover(query.get("kind", "airing")))
                    elif parsed.path == "/api/auto/status":
                        self._json(200, app.api_auto_status(query.get("job", "")))
                    elif parsed.path == "/api/login/poll":
                        self._json(200, app.api_login_poll(query.get("sid", "")))
                    elif parsed.path == "/api/login/baidu":
                        self._json(200, app.api_login_baidu())
                    elif parsed.path == "/api/search":
                        self._json(200, app.api_search(query.get("kw", ""),
                                                       query.get("netdisk", "")))
                    elif parsed.path == "/api/share":
                        if not query.get("url"):
                            raise MediaFansError("缺少 url 参数")
                        self._json(200, app.api_share(
                            query["url"], query.get("code", ""),
                            query.get("dir_fid", ""), query.get("crumb", "")))
                    elif parsed.path == "/stream":
                        self._stream(query.get("t", ""), send_body=True)
                    else:
                        self._json(404, {"error": "not found"})
                except MediaFansError as e:
                    self._json(400, {"error": str(e)})
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except Exception as e:  # 兜底：不让单个请求拖垮服务
                    try:
                        self._json(500, {"error": f"{type(e).__name__}: {e}"})
                    except Exception:
                        pass

        return Handler


# ---------------------------------------------------------------------- page
PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<!-- TV 直链走的是网盘 CDN 的防盗链：带 Referer 会被 403（实测），
     而 <video> 没有元素级的 referrerpolicy，只能整页关掉。
     本页所有请求都是同源的，不发 Referer 没有副作用。 -->
<meta name="referrer" content="no-referrer">
<title>MediaFans 播放器</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --text:#e8eaf0; --dim:#8b93a5;
          --accent:#4f8cff; --ok:#3fbf6f; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font:14px/1.6 "Segoe UI",system-ui,sans-serif;
         height:100vh; height:100dvh; display:flex; flex-direction:column;
         -webkit-text-size-adjust:100%; }
  header { display:flex; align-items:center; gap:12px; padding:10px 16px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  header h1 { font-size:16px; font-weight:600; color:var(--accent); white-space:nowrap; }
  #goto input { background:var(--bg); border:1px solid var(--line); color:var(--text);
                border-radius:6px; padding:6px 10px; font-size:13px; width:100%; }
  .searchbar { flex:1; display:flex; gap:8px; }
  .searchbar input { flex:1; background:var(--bg); border:1px solid var(--line); color:var(--text);
                      border-radius:6px; padding:6px 10px; font-size:13px; }
  .searchbar select { background:var(--bg); border:1px solid var(--line); color:var(--text);
                      border-radius:6px; padding:6px; font-size:13px; }
  #tabs { display:flex; border-bottom:1px solid var(--line); flex:none; }
  .tab { flex:1; background:none; color:var(--dim); border:none; border-bottom:2px solid transparent;
         border-radius:0; padding:8px 0; font-size:13px; }
  .tab.active { color:var(--accent); border-bottom-color:var(--accent); }
  .tabpane { flex:1; display:flex; flex-direction:column; min-height:0; }
  #goto { display:flex; gap:8px; padding:8px; border-bottom:1px solid var(--line); flex:none; }
  #resultsWrap { flex:1; overflow-y:auto; padding:6px; }
  #shareView { flex:1; display:flex; flex-direction:column; min-height:0; }
  #shareFiles { flex:1; overflow-y:auto; padding:4px; }
  .sharebar { display:flex; align-items:center; gap:8px; padding:6px 8px; font-size:13px;
              border-bottom:1px solid var(--line); flex:none; }
  .sharebar input { background:var(--bg); border:1px solid var(--line); color:var(--text);
                    border-radius:6px; padding:4px 8px; width:90px; }
  .sharebar .backlink { color:var(--accent); cursor:pointer; white-space:nowrap; }
  .savebar { display:flex; align-items:center; gap:8px; padding:8px; border-top:1px solid var(--line);
             flex-wrap:wrap; flex:none; }
  .savebar input { flex:1; min-width:130px; background:var(--bg); border:1px solid var(--line);
                   color:var(--text); border-radius:6px; padding:6px 10px; font-size:13px; }
  .savebar .chk { display:flex; align-items:center; gap:4px; color:var(--dim); font-size:12px;
                  white-space:nowrap; }
  #saveStatus { padding:6px 8px; font-size:13px; flex:none; }
  .pastebar { display:flex; gap:8px; padding:8px; border-bottom:1px solid var(--line); flex:none; }
  .pastebar input { flex:1; background:var(--bg); border:1px solid var(--line); color:var(--text);
                    border-radius:6px; padding:6px 10px; font-size:13px; }
  .badge { flex:none; font-size:11px; padding:1px 8px; border-radius:4px;
           background:#23304d; color:#7fb0ff; }
  .b-quark { background:#204a33; color:#69d693; }
  .b-baidu { background:#233a8f; color:#8fa7ff; }
  .ok { color:var(--ok); }
  .dim { color:var(--dim); }
  .ellipsis { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pad { padding:4px 10px; font-size:12px; }
  button { background:var(--accent); border:none; color:#fff; border-radius:6px;
           padding:6px 14px; font-size:13px; cursor:pointer; }
  button:hover { filter:brightness(1.15); }
  main { flex:1; display:flex; min-height:0; }
  #browser { width:38%; min-width:300px; border-right:1px solid var(--line);
             display:flex; flex-direction:column; }
  #crumb { padding:8px 12px; color:var(--dim); font-size:12px; border-bottom:1px solid var(--line);
           word-break:break-all; }
  #crumb a { color:var(--accent); text-decoration:none; cursor:pointer; }
  #files { flex:1; overflow-y:auto; padding:6px; }
  .row { display:flex; align-items:center; gap:8px; padding:7px 10px; border-radius:6px;
         cursor:pointer; user-select:none; }
  .row:hover { background:#1e2330; }
  .row .icon { width:20px; text-align:center; flex:none; }
  .row .name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row .size { color:var(--dim); font-size:12px; flex:none; }
  #player { flex:1; display:flex; flex-direction:column; padding:14px; min-width:0; }
  /* 播放区和控制条一起进全屏，否则全屏后就没控制条可用了 */
  #stage { position:relative; flex:1; min-height:0; display:flex; flex-direction:column;
           background:#000; border-radius:8px; overflow:hidden;
           /* 竖滑要用来调亮度/音量，不能被页面滚动抢走 */
           touch-action:none; }
  #stage:fullscreen { border-radius:0; }
  /* 拿不到真全屏权限时的兜底：把播放区铺满整个窗口 */
  body.theater > header, body.theater #browser { display:none; }
  body.theater #player { padding:0; }
  body.theater #stage { border-radius:0; }
  #video { width:100%; height:100%; flex:1; min-height:0; background:#000; outline:none;
           /* 亮度手势调的是画面本身——浏览器没有调系统背光的 API */
           transition:filter .1s linear; }
  /* 控制条悬浮在画面上，不占高度。原来它是流式布局的一员，
     全屏时会把画面往上挤出一条黑边，等于永久遮挡。 */
  #ctrl { position:absolute; left:0; right:0; bottom:0; z-index:3;
          display:flex; align-items:center; gap:10px; padding:10px 12px 8px;
          background:linear-gradient(to top, rgba(0,0,0,.82) 0%,
                     rgba(0,0,0,.55) 55%, rgba(0,0,0,0) 100%);
          font-size:12px; color:#cfd6e4; user-select:none;
          opacity:1; transition:opacity .25s, transform .25s; }
  /* 播放中一段时间没动作就隐去，点一下画面再出来 */
  #stage.idle #ctrl { opacity:0; transform:translateY(8px); pointer-events:none; }
  #stage.idle { cursor:none; }
  #ctrl button { color:#fff; text-shadow:0 1px 3px rgba(0,0,0,.6); }
  #ctrl .time { text-shadow:0 1px 3px rgba(0,0,0,.6); }

  /* 手势反馈：调亮度/音量时中间浮一个数值 */
  #hud { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
         z-index:4; background:rgba(0,0,0,.72); color:#fff; border-radius:10px;
         padding:12px 18px; font-size:15px; letter-spacing:.5px; pointer-events:none;
         opacity:0; transition:opacity .15s; white-space:nowrap; }
  #hud.on { opacity:1; }
  #hud .bar { height:4px; background:rgba(255,255,255,.28); border-radius:2px;
              margin-top:8px; width:132px; }
  #hud .bar i { display:block; height:100%; background:var(--accent); border-radius:2px; }
  #ctrl button { background:none; border:none; color:var(--text); padding:2px 4px;
                 font-size:15px; line-height:1; }
  #ctrl select { background:rgba(0,0,0,.45); border:1px solid rgba(255,255,255,.25);
                 color:#fff; }
  #ctrl button:hover { color:var(--accent); }
  #ctrl .time { font-variant-numeric:tabular-nums; white-space:nowrap; }
  #trackWrap { flex:1; height:16px; display:flex; align-items:center; cursor:pointer;
               touch-action:none; }
  #track { position:relative; width:100%; height:5px; background:rgba(255,255,255,.3);
           border-radius:3px; transition:height .12s; }
  #trackWrap:hover #track, #trackWrap.dragging #track { height:8px; }
  #buf, #prog { position:absolute; left:0; top:0; bottom:0; border-radius:3px; }
  #buf { background:rgba(255,255,255,.45); }
  #prog { background:var(--accent); }
  #knob { position:absolute; top:50%; width:12px; height:12px; margin:-6px 0 0 -6px;
          background:#fff; border-radius:50%; opacity:0; transition:opacity .12s; }
  #trackWrap:hover #knob, #trackWrap.dragging #knob { opacity:1; }
  #vol { width:76px; accent-color:var(--accent); }
  #rate { background:var(--bg); border:1px solid var(--line); color:var(--text);
          border-radius:5px; padding:2px 4px; font-size:12px; }
  #rate.fast { color:var(--accent); border-color:var(--accent); }
  #quality { display:flex; gap:8px; padding:8px 4px 0; flex-wrap:wrap; }
  /* 「来源」和「画质档」是两回事：画质档是同一个文件的不同转码流，
     来源是网盘里同一集的不同文件（4K 原盘 / 1080p WEB-DL / 国语版…）。
     摆两排，别混成一排让人以为是同一维度。 */
  #sources { display:none; gap:8px; padding:8px 4px 0; flex-wrap:wrap;
             align-items:center; }
  #sources.on { display:flex; }
  #sources .lbl { color:var(--dim); font-size:12px; flex:none; }
  #sources.warn .lbl { color:#ffc46b; }
  .sbtn { background:#222836; color:var(--dim); border:1px solid var(--line);
          border-radius:20px; padding:3px 14px; font-size:12px; cursor:pointer;
          max-width:min(340px, 60vw); overflow:hidden; text-overflow:ellipsis;
          white-space:nowrap; }
  .sbtn:hover { color:var(--text); }
  .sbtn.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .sbtn.risky { border-style:dashed; }
  .qbtn { background:#222836; color:var(--dim); border:1px solid var(--line);
          border-radius:20px; padding:3px 14px; font-size:12px; cursor:pointer; }
  .qbtn:hover { color:var(--text); }
  .qbtn.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .qbtn.unplayable { opacity:.5; text-decoration:line-through; }
  .ep .pill.all { border-color:var(--accent); color:var(--accent); }
  #meta { padding:6px 4px 0; display:flex; align-items:baseline; gap:10px; }
  #meta .title { font-size:15px; font-weight:600; word-break:break-all; }
  #meta .sub { color:var(--dim); font-size:12px; }
  #hint { color:var(--dim); font-size:12px; padding:6px 4px; }
  #hint.error { color:#ff7a7a; }
  .empty { color:var(--dim); text-align:center; padding:30px 0; }
  /* 剧集：一个独立标签页，不是弹层——看剧时它是主界面，不该盖住播放器 */
  #seriesBox { flex:1; min-height:0; display:flex; flex-direction:column; }
  #seriesHead { padding:12px 16px 8px; border-bottom:1px solid var(--line); }
  #seriesHead h2 { font-size:16px; }
  #seasonTabs { display:flex; gap:6px; margin-top:8px; flex-wrap:wrap; }
  #seasonTabs button { background:#222836; color:var(--dim); border:1px solid var(--line);
                       border-radius:14px; padding:3px 12px; font-size:12px; }
  #seasonTabs button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  #seriesStat { color:var(--dim); font-size:12px; margin-top:6px; }
  #seriesStat b { color:var(--ok); }
  #seriesStat i { color:#e0b05a; font-style:normal; }
  #seriesStat u { color:#ff7a7a; text-decoration:none; }
  #epList { flex:1; overflow-y:auto; padding:6px 10px; }
  .ep { display:flex; align-items:center; gap:10px; padding:8px 6px;
        border-bottom:1px solid var(--line); }
  .ep .no { flex:none; width:42px; font-variant-numeric:tabular-nums; color:var(--dim);
            font-size:12px; }
  .ep .body { flex:1; min-width:0; }
  .ep .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }
  .ep .sub { color:var(--dim); font-size:11px; margin-top:1px; }
  .ep .act { flex:none; display:flex; gap:6px; align-items:center; }
  .ep.saved .no { color:var(--ok); }
  .ep.missing { opacity:.55; }
  .pill { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid var(--line);
          background:#222836; color:var(--dim); cursor:pointer; white-space:nowrap; }
  .pill.play { background:var(--accent); color:#fff; border-color:var(--accent); }
  .pill.src:hover { color:var(--text); border-color:var(--accent); }
  .pill.busy { opacity:.6; cursor:default; }
  #seriesFoot { padding:10px 16px 14px; border-top:1px solid var(--line);
                display:flex; gap:8px; align-items:center; flex:none; }
  #grabBtn { background:var(--accent); color:#fff; border-color:var(--accent); }
  #grabBtn:disabled { opacity:.55; }
  #seriesFoot .grow { flex:1; color:var(--dim); font-size:12px;
                      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  /* 看过的进度：集号下面一条细线，扫一眼就知道追到哪了 */
  .ep .bar { height:2px; background:#2a3142; border-radius:2px; margin-top:4px; }
  .ep .bar i { display:block; height:100%; background:var(--accent); border-radius:2px; }
  .ep.done .no::after { content:'✓'; color:var(--ok); margin-left:3px; font-size:10px; }
  .ep.playing { background:#1b2333; }
  .ep.playing .no { color:var(--accent); }

  /* 最近观看 */
  #watchList { flex:1; overflow-y:auto; padding:8px; }
  .wrow { display:flex; gap:10px; padding:8px; border-bottom:1px solid var(--line);
          cursor:pointer; align-items:center; }
  .wrow:hover { background:#1b2130; }
  .wrow img { width:46px; height:69px; object-fit:cover; border-radius:4px; flex:none;
              background:#222836; }
  .wrow .noposter { width:46px; height:69px; border-radius:4px; flex:none;
                    background:#222836; display:flex; align-items:center;
                    justify-content:center; font-size:10px; color:var(--dim);
                    text-align:center; padding:2px; }
  .wrow .body { flex:1; min-width:0; }
  .wrow .t { font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wrow .s { color:var(--dim); font-size:11px; margin-top:2px;
             overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .wrow .bar { height:3px; background:#2a3142; border-radius:2px; margin-top:6px; }
  .wrow .bar i { display:block; height:100%; background:var(--accent); border-radius:2px; }
  .wrow .x { flex:none; color:var(--dim); font-size:16px; padding:0 4px; background:none;
             border:none; cursor:pointer; }
  .wrow .x:hover { color:#ff7a7a; }
  #scanLog { padding:0 16px 8px; font-size:11px; color:var(--dim); max-height:76px;
             overflow-y:auto; }
  #discoverBar { display:flex; gap:6px; padding:8px; border-bottom:1px solid var(--line);
                 flex:none; align-items:center; }
  #discoverBar .seg { background:#222836; color:var(--dim); border:1px solid var(--line);
                      border-radius:6px; padding:6px 10px; font-size:12px; }
  #discoverBar .seg.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  /* 「剧集/电影」是作品类型，「今日播出/热门…」是榜单档位——两维不同，
     所以分成两组：类型贴左边固定宽，档位撑满剩下的空间 */
  #mediaSeg { display:flex; gap:4px; flex:none; padding-right:6px;
              border-right:1px solid var(--line); }
  #kindSeg { display:flex; gap:6px; flex:1; }
  #kindSeg .seg { flex:1; padding:6px 0; }
  #segFound { flex:none; }
  /* 凭据过期的提示条：错误本身没法自愈，得让人一键去重登 */
  #authBar { display:none; align-items:center; gap:10px; padding:8px 12px;
             background:#3a2a16; border-bottom:1px solid #5a4020; font-size:12px;
             color:#e8c98a; flex:none; }
  #authBar.on { display:flex; }
  #authBar .grow { flex:1; }
  #authBar button { background:var(--accent); color:#fff; border:none;
                    border-radius:4px; padding:4px 12px; font-size:12px; }
  #authBar .x { background:none; color:var(--dim); padding:2px 6px; }

  /* 华语和其他语种之间的分隔说明，占满整行 */
  .wall-note { grid-column:1/-1; color:var(--dim); font-size:11px; padding:6px 2px 2px;
               border-top:1px solid var(--line); margin-top:4px; }
  .wall-note:first-child { border-top:none; margin-top:0; }
  /* grid-auto-rows 必须写死成 max-content。
     海报是 `width:100% + aspect-ratio:2/3`，这种图**在算行高时贡献的高度是 0**
     （百分比宽度对着不定容器解不出来），于是默认的 auto 行只按标题那 41px 算，
     再被 align-content:stretch 平摊成每行 39px，海报被 overflow:hidden 裁成一条。
     手机上尤其明显：屏窄行多，629px 摊给 13 行。max-content 强制按内容取行高。 */
  #shows { flex:1; overflow-y:auto; padding:8px;
           display:grid; grid-template-columns:repeat(auto-fill,minmax(104px,1fr));
           grid-auto-rows:max-content; align-content:start; gap:10px; }
  .card { cursor:pointer; border-radius:8px; overflow:hidden; background:var(--panel);
          border:1px solid var(--line); display:flex; flex-direction:column; }
  .card:hover { border-color:var(--accent); }
  .card .poster { width:100%; aspect-ratio:2/3; object-fit:cover; background:#0c0f14;
                  display:block; }
  .card .noposter { width:100%; aspect-ratio:2/3; display:flex; align-items:center;
                    justify-content:center; color:var(--dim); font-size:11px; padding:6px;
                    text-align:center; }
  .card .cap { padding:5px 6px; font-size:12px; line-height:1.3; }
  .card .cap b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .card .cap span { color:var(--dim); font-size:11px; }
  .star { color:#e8b64c; }
  /* 一键找片的进度面板 */
  #autoPanel { position:fixed; inset:0; background:rgba(0,0,0,.72); display:none;
               align-items:center; justify-content:center; z-index:10; padding:16px; }
  #autoPanel.on { display:flex; }
  #autoBox { background:var(--panel); border:1px solid var(--line); border-radius:10px;
             width:520px; max-width:100%; max-height:86vh; display:flex; flex-direction:column; }
  #autoHead { padding:14px 16px 8px; border-bottom:1px solid var(--line); }
  #autoHead h2 { font-size:15px; }
  #autoHead .sub { color:var(--dim); font-size:12px; margin-top:2px; }
  #autoSteps { flex:1; overflow-y:auto; padding:10px 16px; font-size:13px; min-height:120px; }
  #autoSteps .st { display:flex; gap:8px; padding:3px 0; align-items:flex-start; }
  #autoSteps .st i { flex:none; width:15px; font-style:normal; }
  #autoSteps .st.error { color:#ff7a7a; }
  #autoSteps .st.done { color:var(--ok); }
  #autoSteps .st.ai { color:#c99bff; }
  #autoCands { padding:0 16px 8px; font-size:12px; }
  #autoCands table { width:100%; border-collapse:collapse; }
  #autoCands td { padding:3px 4px; border-top:1px solid var(--line); vertical-align:top; }
  #autoCands .bad { color:var(--dim); text-decoration:line-through; }
  #autoCands .pick { color:var(--ok); }
  #autoFoot { padding:10px 16px 14px; display:flex; gap:8px; align-items:center;
              border-top:1px solid var(--line); }
  #autoFoot .grow { flex:1; }
  #navbar { display:flex; align-items:center; gap:10px; padding:8px 4px 0; flex-wrap:wrap; }
  #navbar button { background:#222836; border:1px solid var(--line); color:var(--text); }
  #navbar button:disabled { opacity:.35; cursor:default; }
  #posLabel { color:var(--dim); font-size:12px; }
  #playlist { max-height:150px; overflow-y:auto; margin-top:6px;
              border-top:1px solid var(--line); }
  #playlist .row { padding:5px 8px; font-size:13px; }
  #playlist .row.cur { background:#1d2740; color:var(--accent); }
  .pl-head { font-size:12px; color:var(--dim); padding:6px 4px 2px; }
  .bar { height:3px; background:var(--line); border-radius:2px; margin-top:3px; }
  .bar > i { display:block; height:100%; background:var(--accent); border-radius:2px; }
  .crumbbar { padding:6px 10px; font-size:12px; color:var(--dim); flex:none;
              border-bottom:1px solid var(--line); word-break:break-all; }
  .crumbbar a { color:var(--accent); cursor:pointer; }
  .row.junk .name { color:var(--dim); }
  .kind { flex:none; font-size:10px; padding:0 5px; border-radius:3px;
          background:#2b3140; color:var(--dim); }
  #loginBtn { background:#222836; border:1px solid var(--line); color:var(--dim); flex:none; }
  #loginBtn:hover { color:var(--text); }
  #driveSel { background:#222836; border:1px solid var(--line); color:var(--dim);
              flex:none; border-radius:6px; padding:5px 6px; font:inherit; }
  #driveSel:hover { color:var(--text); }
  #mask { position:fixed; inset:0; background:rgba(0,0,0,.66); display:none;
          align-items:center; justify-content:center; z-index:9; }
  #mask.on { display:flex; }
  #loginBox { background:var(--panel); border:1px solid var(--line); border-radius:10px;
              padding:20px; width:340px; text-align:center; }
  #loginBox h2 { font-size:15px; margin-bottom:4px; }
  #loginBox .pick { display:flex; gap:10px; margin:16px 0 6px; }
  #loginBox .pick button { flex:1; }
  #loginBox .warn { color:#e0b05a; font-size:11px; line-height:1.5; text-align:left;
                    background:#2a2216; border-radius:6px; padding:7px 9px; margin-top:10px; }
  #qrWrap { margin:14px 0 6px; min-height:200px; display:flex; align-items:center;
            justify-content:center; }
  #qrWrap img { width:200px; height:200px; background:#fff; border-radius:6px; padding:6px; }
  #loginMsg { font-size:12px; color:var(--dim); min-height:34px; line-height:1.5; }
  #loginMsg.error { color:#ff7a7a; }
  #loginMsg.ok { color:var(--ok); }
  .linkbtn { background:none; border:none; color:var(--dim); font-size:12px;
             text-decoration:underline; padding:4px; }

  /* ---------------- 窄屏（手机/竖屏平板） ----------------
     原来是左右分栏：左栏 min-width:300px，375px 的手机上播放区只剩 75px，
     视频被压成 47x0，文字挤成竖排。窄屏下必须改成上下堆叠。 */
  @media (max-width: 820px) {
    /* 顶栏：标题和网盘选择让位，搜索框独占一行 */
    header { flex-wrap:wrap; gap:8px; padding:8px 10px; }
    header h1 { font-size:15px; }
    .searchbar { order:3; flex-basis:100%; }
    #loginBtn { margin-left:auto; }

    main { flex-direction:column; }

    /* 播放区置顶、按内容高度；列表占满剩余空间并滚动 */
    #player { order:-1; flex:none; padding:0; }
    #stage { flex:none; border-radius:0; }
    #video { aspect-ratio:16/9; flex:none; height:auto; max-height:52vh; }
    #browser { width:100%; min-width:0; flex:1; min-height:0;
               border-right:none; border-top:1px solid var(--line); }

    /* 控制条：进度条单独占一行，音量交给手机物理键 */
    #ctrl { flex-wrap:wrap; gap:8px; padding:8px 10px; }
    #trackWrap { order:-1; flex-basis:100%; height:22px; }
    #track { height:6px; }
    #knob { opacity:1; width:14px; height:14px; margin:-7px 0 0 -7px; }
    #vol, #muteBtn { display:none; }
    #ctrl button { font-size:18px; padding:4px 6px; }
    #fsBtn { margin-left:auto; }

    /* 播放器下方的信息区压缩，别把列表挤没了。
       清晰度和导航条原本会换行（实测各占两行、共 176px），改成横向滚动单行，
       文件列表的可见高度从 202px 涨到 300px 以上。 */
    #sources { padding:6px 8px; flex-wrap:nowrap; overflow-x:auto; }
    #sources > * { flex:none; white-space:nowrap; }
    #quality, #navbar { padding:6px 8px; flex-wrap:nowrap; overflow-x:auto;
                        scrollbar-width:none; }
    #quality::-webkit-scrollbar, #navbar::-webkit-scrollbar { display:none; }
    #quality > *, #navbar > * { flex:none; white-space:nowrap; }
    #meta { padding:4px 8px; }
    #meta .title { font-size:14px; overflow:hidden; text-overflow:ellipsis;
                   white-space:nowrap; word-break:normal; }
    #hint { padding:2px 8px 6px; font-size:11px; line-height:1.4;
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    #playlist { max-height:30vh; }

    /* 还没开始播时不给播放区留位置，整屏都给文件列表 */
    body:not(.has-video) #player { display:none; }

    /* 在「搜资源/转存」页时把视频压小、隐藏播放器的附属信息：
       否则播放区占掉 436px，分享文件列表会被挤成 0 高度，根本选不了文件。
       注意是压小不是 display:none —— 后者在部分浏览器上会把播放中断。 */
    body.tab-search #video { max-height:24vh; }
    body.tab-search #quality,
    body.tab-search #sources,
    body.tab-search #navbar,
    body.tab-search #playlist,
    body.tab-search #meta,
    body.tab-search #hint { display:none; }
    body.tab-search #ctrl { padding:4px 10px; }

    /* 分享视图：列表让 flex 自己分配高度。给它写死 min-height 会把「转存」按钮和
       错误提示挤出屏幕（实测顶到 814px，屏幕才 812），操作栏必须始终可见。 */
    #shareFiles { min-height:0; }
    #shareView { min-height:0; }
    .sharebar, .savebar, .pastebar { padding:6px 8px; }
    #saveStatus { padding:4px 8px; }

    /* 手机上靠点文件夹和面包屑导航，手打路径几乎用不上，藏起来把高度让给文件列表 */
    #goto { display:none; }
    #crumb, .crumbbar { white-space:nowrap; overflow-x:auto; scrollbar-width:none;
                        word-break:normal; }
    #crumb::-webkit-scrollbar, .crumbbar::-webkit-scrollbar { display:none; }

    /* 触摸目标放大到 44px 上下 */
    .row { padding:11px 10px; gap:10px; }
    .qbtn { padding:7px 16px; font-size:13px; }
    #navbar button, .savebar button, .pastebar button, #goto button { padding:9px 14px; }
    .tab { padding:12px 0; font-size:14px; }
    input[type=checkbox] { width:18px; height:18px; }

    /* 底部留出手势条的安全区 */
    #browser { padding-bottom:env(safe-area-inset-bottom); }
  }

  /* 横屏手机：宽度够但竖向空间不够（实测 812x400 下堆叠会把列表顶到屏幕外 162px），
     这种比例反而适合左右分栏，把上面的堆叠规则整体改回去。 */
  @media (max-width: 950px) and (orientation: landscape) and (max-height: 520px) {
    main { flex-direction:row; }
    #player { order:0; flex:1; min-width:0; }
    body:not(.has-video) #player { display:flex; }
    #stage { flex:1; }
    #video { aspect-ratio:auto; max-height:none; flex:1; }
    body.tab-search #video { max-height:none; }
    #browser { width:42%; min-width:220px; flex:none;
               border-right:1px solid var(--line); border-top:none; }
    #goto, #playlist, #meta { display:none; }
    #quality, #navbar { padding:4px 6px; }
    #hint { padding:2px 6px 4px; }
    #ctrl { padding:4px 8px; }
  }
</style>
</head>
<body>
<header>
  <h1>▶ MediaFans</h1>
  <div class="searchbar">
    <input id="kw" placeholder="搜剧名 / 片名，如：末日地堡 / 沙丘" spellcheck="false"
           onkeydown="if(event.key==='Enter')searchMedia()">
    <button onclick="searchMedia()">搜索</button>
  </div>
  <button id="loginBtn" onclick="openLogin()">登录</button>
  <select id="driveSel" title="当前网盘：我的网盘 / 剧集 / 一键找片都作用于此盘" onchange="setDrive(this.value)">
    <option value="quark">夸克</option>
    <option value="baidu">百度</option>
  </select>
</header>
<div id="autoPanel">
  <div id="autoBox">
    <div id="autoHead">
      <h2 id="autoTitle">自动找片</h2>
      <div class="sub" id="autoSub">搜索 → 逐个打开验证 → 挑最合适的 → 转存</div>
    </div>
    <div id="autoSteps"></div>
    <div id="autoCands"></div>
    <div id="autoFoot">
      <span class="grow dim" id="autoStat"></span>
      <button id="autoOpen" style="display:none" onclick="openAutoResult()">打开目录</button>
      <button class="linkbtn" onclick="closeAuto()">关闭</button>
    </div>
  </div>
</div>
<div id="mask" onclick="if(event.target===this)closeLogin()">
  <div id="loginBox">
    <h2>登录网盘</h2>
    <div class="dim" style="font-size:12px">夸克、百度都可以扫码</div>
    <div class="pick">
      <button onclick="startLogin('quark')">夸克·网页版扫码</button>
      <button onclick="startLogin('tv')">夸克·TV 版扫码</button>
    </div>
    <div class="pick">
      <button onclick="startLogin('baidu')">百度·扫码登录</button>
      <button onclick="showBaiduLogin()">百度·粘贴 cookie</button>
    </div>
    <div class="dim" style="font-size:11px;text-align:left">
      网页版存 cookie（现在浏览器播放用的就是它）；TV 版存 access_token，
      用来验证 TV 直链是否免凭据。
    </div>
    <div id="tvWarn" class="warn" style="display:none">
      ⚠ TV 版的 code 换 token 这一步会经过第三方中转 api.extscreen.com（已强制 https）。
      换回来的 refresh_token 等于网盘的长期访问权，介意就别用这条路。
    </div>
    <div id="baiduLogin" style="display:none;text-align:left"></div>
    <div id="qrWrap"></div>
    <div id="loginMsg"></div>
    <button class="linkbtn" onclick="closeLogin()">关闭</button>
  </div>
</div>
<main>
  <section id="browser">
    <div id="authBar">
      <span class="grow" id="authMsg"></span>
      <button id="authBtn" onclick="reloginFromBar()">重新扫码登录</button>
      <button class="x" onclick="hideAuthBar()">×</button>
    </div>
    <nav id="tabs">
      <button class="tab active" id="tabbtn-shows" onclick="switchTab('shows')">发现</button>
      <button class="tab" id="tabbtn-series" onclick="switchTab('series')">剧集</button>
      <button class="tab" id="tabbtn-watch" onclick="switchTab('watch')">最近观看</button>
      <button class="tab" id="tabbtn-search" onclick="switchTab('search')">搜资源</button>
      <button class="tab" id="tabbtn-mine" onclick="switchTab('mine')">我的网盘</button>
    </nav>
    <div id="tab-series" class="tabpane" style="display:none">
      <div id="seriesBox">
        <div id="seriesHead">
          <h2 id="seriesTitle">剧集</h2>
          <div id="seasonTabs"></div>
          <div id="seriesStat"></div>
        </div>
        <div id="epList"><div class="empty">从「发现」里点一部剧或电影，或在上面搜片名</div></div>
        <div id="scanLog"></div>
        <div id="seriesFoot">
          <span class="grow" id="seriesDir"></span>
          <button id="grabBtn" style="display:none" onclick="fetchSeason()">一键转存</button>
          <button id="scanBtn" style="display:none" onclick="scanSources()">找缺失的集</button>
        </div>
      </div>
    </div>
    <div id="tab-watch" class="tabpane" style="display:none">
      <div id="watchList"><div class="empty">还没有看过的记录</div></div>
    </div>
    <div id="tab-mine" class="tabpane" style="display:none">
      <div id="goto">
        <input id="pathInput" placeholder="/MediaFans" spellcheck="false">
        <button onclick="loadDir(document.getElementById('pathInput').value)">打开</button>
      </div>
      <label class="chk" style="padding:4px 10px">
        <input type="checkbox" id="videoOnly" onchange="renderFiles()"> 只看可播放的
        <span id="dirStats" class="dim" style="margin-left:auto"></span>
      </label>
      <div id="crumb"></div>
      <div id="files"><div class="empty">加载中…</div></div>
    </div>
    <div id="tab-search" class="tabpane" style="display:none">
      <div class="pastebar">
        <input id="diskKw" placeholder="直接搜网盘分享（原始结果）" spellcheck="false"
               onkeydown="if(event.key==='Enter')doSearch()">
        <select id="nd">
          <option value="quark">夸克</option>
          <option value="baidu">百度</option>
          <option value="">全部</option>
        </select>
        <button onclick="doSearch()">搜</button>
      </div>
      <div class="pastebar">
        <input id="pasteUrl" placeholder="或直接粘贴夸克/百度分享链接" spellcheck="false">
        <button onclick="openShare(document.getElementById('pasteUrl').value.trim(), '')">查看</button>
      </div>
      <div id="resultsWrap">
        <div id="searchStatus" class="empty">在上方搜索框输入关键词</div>
        <div id="results"></div>
      </div>
      <div id="shareView" style="display:none">
        <div class="sharebar">
          <a class="backlink" onclick="backToResults()">← 返回结果</a>
          <span id="shareUrlShow" class="dim ellipsis"></span>
        </div>
        <div class="sharebar">
          提取码 <input id="codeInput" size="6" maxlength="8">
          <button onclick="reloadShare()">刷新</button>
          <span id="shareStats" class="dim ellipsis"></span>
        </div>
        <div id="shareCrumb" class="crumbbar"></div>
        <div id="shareFiles"></div>
        <div class="savebar">
          <span>目标目录</span>
          <input id="toDir" value="" placeholder="/MediaFans（留空用默认目录）" spellcheck="false">
          <label class="chk"><input type="checkbox" id="selAll" checked
                 onchange="toggleAll(this.checked)"> 全选</label>
          <button id="saveBtn" onclick="saveShare()">转存选中</button>
        </div>
        <div id="saveStatus" class="dim"></div>
      </div>
    </div>
    <div id="tab-shows" class="tabpane" style="display:none">
      <div id="discoverBar">
        <span id="mediaSeg">
          <button class="seg on" data-media="tv" onclick="setDiscoverMedia('tv')">剧集</button>
          <button class="seg" data-media="movie" onclick="setDiscoverMedia('movie')">电影</button>
        </span>
        <span id="kindSeg"></span>
        <button class="seg" data-kind="found" id="segFound" style="display:none"
                onclick="renderFound()">搜索结果</button>
      </div>
      <div id="shows"><div class="empty">加载中…</div></div>
    </div>
  </section>
  <section id="player">
    <div id="stage">
      <video id="video" preload="auto" playsinline></video>
      <div id="hud"></div>
      <div id="ctrl">
        <button id="playBtn" onclick="togglePlay()" title="播放/暂停（空格）">▶</button>
        <span class="time" id="tCur">0:00</span>
        <div id="trackWrap"><div id="track">
          <div id="buf"></div><div id="prog"></div><div id="knob"></div>
        </div></div>
        <span class="time" id="tDur">0:00</span>
        <button id="muteBtn" onclick="toggleMute()" title="静音">🔊</button>
        <input type="range" id="vol" min="0" max="1" step="0.05" value="1"
               title="音量（↑/↓）">
        <select id="rate" title="播放速度" onchange="setRate(this.value)">
          <option value="0.5">0.5×</option>
          <option value="0.75">0.75×</option>
          <option value="1" selected>1.0×</option>
          <option value="1.25">1.25×</option>
          <option value="1.5">1.5×</option>
          <option value="1.75">1.75×</option>
          <option value="2">2.0×</option>
        </select>
        <button id="fsBtn" onclick="toggleFullscreen()" title="全屏（f）">⛶</button>
      </div>
    </div>
    <div id="quality"></div>
    <div id="sources"></div>
    <div id="navbar" style="display:none">
      <button id="prevBtn" onclick="playAdjacent(-1)">← 上一个</button>
      <button id="nextBtn" onclick="playAdjacent(1)">下一个 →</button>
      <span id="posLabel"></span>
      <label class="chk"><input type="checkbox" id="autoNext" checked> 播完自动下一集</label>
      <button class="linkbtn" onclick="togglePlaylist()" id="plToggle">展开列表</button>
    </div>
    <div id="playlist" style="display:none"></div>
    <div id="meta"><span class="title" id="title">选择左侧文件开始播放</span>
      <span class="sub" id="sub"></span></div>
    <div id="hint">提示：默认播网盘转码档（浏览器能直接解码）。原画 REMUX 码率极高，
      浏览器多半又卡又解不了，需要原画请用 <b>mediafans play &lt;文件&gt;</b> 调外部播放器</div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
const video = $('#video');
const params = new URLSearchParams(location.search);

let curFiles = [];    // 当前目录的全部条目
let playlist = [];    // 其中可播放的，用来做上一个/下一个和连播
let playIdx = -1;

// 当前操作盘：顶栏下拉，作用于「我的网盘 / 剧集 / 一键找片」。
// 搜资源页转存到哪个盘由分享链接本身决定（百度分享只能进百度盘），跟这里无关。
let curNd = 'quark';
let curPlayNd = 'quark';   // 正在播的这个文件属于哪个盘（记进度用）

function setDriveValue(nd) {
  curNd = nd || 'quark';
  const sel = $('#driveSel');
  if (sel) sel.value = curNd;
}

function setDrive(nd) {
  if (nd === curNd) return;
  setDriveValue(nd);
  // 已打开的页面跟着切过去
  if (series && series.data) loadSeason(series.tmdb_id, series.season, true);
  else if (mineLoaded) loadDir($('#pathInput').value || '');
}

// 凭据过期跟别的错不一样：它不会自己好，用户必须去重登一次。
// 百度的网页登录态尤其短（实测二十来分钟），只丢一句错误在角落里，
// 用户只会觉得「怎么又不好使了」。所以单独拎出来做成一条能点的提示。
const AUTH_HINTS = ['重新扫码', '登录态', '未配置 cookie', '登录已过期', 'cookie 已过期'];
let authBarNd = 'quark';

function isAuthError(msg) {
  const t = String(msg || '');
  return AUTH_HINTS.some(h => t.includes(h));
}

function showAuthBar(msg, nd) {
  authBarNd = nd || curNd || 'quark';
  const label = authBarNd === 'baidu' ? '百度网盘' : '夸克网盘';
  // 逐条错误常带着「哪个来源失败了」的前缀（分享标题），提示条只要后半句的原因
  let why = String(msg || '登录态已过期');
  const cut = why.lastIndexOf('：');
  if (cut > 0 && cut < why.length - 4) why = why.slice(cut + 1);
  $('#authMsg').textContent = label + '：' + why;
  $('#authBtn').textContent = '重新扫码登录' + label.slice(0, 2);
  $('#authBar').classList.add('on');
}

function hideAuthBar() { $('#authBar').classList.remove('on'); }

function reloginFromBar() {
  hideAuthBar();
  openLogin();
  startLogin(authBarNd);
}

// 报错的统一出口：凭据问题弹提示条，其余照原样交给调用方显示
function reportError(msg, nd) {
  if (isAuthError(msg)) {
    showAuthBar(msg, nd);
    return true;
  }
  return false;
}

function icon(f) {
  return { dir: '📁', video: '🎬', audio: '🎵', subtitle: '💬' }[f.kind] || '📄';
}

async function loadDir(path) {
  mineLoaded = true;
  $('#files').innerHTML = '<div class="empty">加载中…</div>';
  try {
    const r = await fetch('/api/list?path=' + encodeURIComponent(path || '')
                          + '&nd=' + curNd);
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    document.getElementById('pathInput').value = data.path;
    renderCrumb(data.path);
    curFiles = data.files || [];
    renderFiles();
  } catch (e) {
    curFiles = [];
    reportError(e.message, curNd);
    $('#files').innerHTML = '<div class="empty">加载失败：' + e.message + '</div>';
  }
}

function renderFiles() {
  const box = $('#files');
  const onlyPlayable = $('#videoOnly').checked;
  // 播放列表始终是全部可播放文件，不受「只看可播放的」这个显示开关影响
  playlist = curFiles.filter(f => f.playable);
  const shown = onlyPlayable ? curFiles.filter(f => f.playable || f.is_dir) : curFiles;
  const junk = curFiles.length - curFiles.filter(f => f.playable || f.is_dir).length;
  $('#dirStats').textContent = playlist.length
    ? playlist.length + ' 个可播放' + (junk ? '，' + junk + ' 个其他' : '')
    : (curFiles.length ? '没有可播放文件' : '');
  box.innerHTML = '';
  if (!curFiles.length) { box.innerHTML = '<div class="empty">空目录</div>'; return; }
  if (!shown.length) { box.innerHTML = '<div class="empty">这个目录没有可播放文件</div>'; return; }
  for (const f of shown) {
    const row = el('div', 'row' + (f.is_dir || f.playable ? '' : ' junk'));
    row.append(el('span', 'icon', icon(f)));
    const nm = el('span', 'name', f.name);
    nm.title = f.name;
    if (f.watched && f.watched.percent) {
      // 看过的在文件名下面留一条细进度线，不用点进去才知道
      const bar = el('div', 'bar');
      const fill = document.createElement('i');
      fill.style.width = f.watched.percent + '%';
      bar.appendChild(fill);
      nm.appendChild(bar);
      nm.title = f.name + '　（' + (f.watched.finished ? '已看完'
                 : '看到 ' + f.watched.percent + '%') + '）';
    }
    row.append(nm, el('span', 'size', f.size_h));
    row.onclick = () => f.is_dir ? loadDir(f.path) : playFile(f);
    box.appendChild(row);
  }
}

// ---------------- 播放位置记忆 ----------------
// 存服务端不存 localStorage：这个服务是手机、平板、电脑一起用的，
// 进度只记在某一台上等于没记——「继续观看」的价值全在跨设备接着看。
// playCtx 是「这次播的是哪部剧的哪一集」，进度带上它，最近观看才能按剧聚合。
let playCtx = null;

async function savePos(path, sec, dur) {
  if (!path || !dur) return;
  const body = Object.assign({ path: path, position: sec, duration: dur,
                               netdisk: curPlayNd },
                             playCtx && playCtx.path === path ? playCtx.meta : {});
  try {
    await fetch('/api/watch', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  } catch (e) {}      // 记不上进度不该影响正在看的这一集
}

async function resumePos(path) {
  try {
    const d = await (await fetch('/api/watch?path=' + encodeURIComponent(path)
                                 + '&nd=' + curPlayNd)).json();
    return (d.mark && d.mark.resume_at) || 0;
  } catch (e) { return 0; }
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}

// ---------------- 播放列表 ----------------
function renderNav(f) {
  playIdx = playlist.findIndex(x => x.path === f.path);
  // 在剧集页看的时候，「上一个/下一个」说的是上一集/下一集，
  // 跟当前网盘目录没关系——那时候目录里可能一个相关文件都没有
  const eps = episodeNav();
  if (eps) {
    $('#navbar').style.display = '';
    $('#prevBtn').textContent = '← 上一集';
    $('#nextBtn').textContent = '下一集 →';
    $('#prevBtn').disabled = !eps.prev;
    $('#nextBtn').disabled = !eps.next;
    // 灰掉的原因有两种，说清楚是哪一种
    $('#nextBtn').title = eps.next ? ''
      : (eps.hasNext ? '下一集还没存到网盘' : '这一季播完了');
    $('#prevBtn').title = eps.prev || !eps.hasPrev ? '' : '上一集还没存到网盘';
    $('#posLabel').textContent = '第 ' + eps.season + ' 季 · 第 ' + eps.episode
      + ' / ' + eps.total + ' 集';
    $('#plToggle').style.display = 'none';
    $('#playlist').style.display = 'none';
    return;
  }
  $('#prevBtn').textContent = '← 上一个';
  $('#nextBtn').textContent = '下一个 →';
  $('#prevBtn').title = $('#nextBtn').title = '';
  $('#plToggle').style.display = '';
  // 从别的目录点开文件时，当前列表跟它没关系，索性不显示
  $('#navbar').style.display = playIdx >= 0 ? '' : 'none';
  const has = playIdx >= 0 && playlist.length > 1;
  $('#prevBtn').disabled = !has || playIdx <= 0;
  $('#nextBtn').disabled = !has || playIdx >= playlist.length - 1;
  $('#posLabel').textContent = playIdx >= 0
    ? '第 ' + (playIdx + 1) + ' / ' + playlist.length + ' 个' : '';
  renderPlaylist();
}

// 当前这一集在这一季里的位置，以及前后各有没有能播的
function episodeNav() {
  if (!playCtx || !series || !series.data) return null;
  const d = series.data;
  if (d.season !== playCtx.season) return null;
  const eps = d.episodes || [];
  const at = eps.findIndex(e => e.episode === playCtx.episode);
  if (at < 0) return null;
  const near = (delta) => {
    const e = eps[at + delta];
    return e && e.local ? e : null;
  };
  return { season: d.season, episode: playCtx.episode, total: eps.length,
           prev: near(-1), next: near(1),
           hasPrev: !!eps[at - 1], hasNext: !!eps[at + 1] };
}

function togglePlaylist() {
  const box = $('#playlist');
  const open = box.style.display === 'none';
  box.style.display = open ? '' : 'none';
  $('#plToggle').textContent = open ? '收起列表' : '展开列表';
  if (open) renderPlaylist();
}

function renderPlaylist() {
  const box = $('#playlist');
  if (box.style.display === 'none') return;
  box.innerHTML = '';
  playlist.forEach((f, i) => {
    const row = el('div', 'row' + (i === playIdx ? ' cur' : ''));
    row.append(el('span', 'icon', i === playIdx ? '▶' : '🎬'));
    const nm = el('span', 'name', f.name);
    if (f.watched && f.watched.percent) {
      const bar = el('div', 'bar');
      const fill = document.createElement('i');
      fill.style.width = f.watched.percent + '%';
      bar.appendChild(fill);
      nm.appendChild(bar);
    }
    row.append(nm, el('span', 'size', f.size_h));
    row.onclick = () => playFile(f);
    box.appendChild(row);
  });
}

function playAdjacent(delta) {
  const eps = episodeNav();
  if (eps) {
    const target = delta > 0 ? eps.next : eps.prev;
    if (target) playEpisode(series.data, target, 0);
    return;
  }
  const next = playlist[playIdx + delta];
  if (next) playFile(next);
}

function renderCrumb(path) {
  const parts = path.split('/').filter(Boolean);
  const crumb = $('#crumb');
  crumb.innerHTML = '';
  const root = document.createElement('a');
  root.textContent = '根目录';
  root.onclick = () => loadDir('/');
  crumb.appendChild(root);
  let acc = '';
  for (const p of parts) {
    acc += '/' + p;
    crumb.appendChild(document.createTextNode(' / '));
    const a = document.createElement('a');
    a.textContent = p;
    const target = acc;
    a.onclick = () => loadDir(target);
    crumb.appendChild(a);
  }
}

// ---------------- 来源切换 ----------------
// 同一集在网盘里可能存了好几份（4K 原盘 / 1080p WEB-DL / 国语版…）。
// 「哪一份能播」只有播起来才知道：原盘 MKV 浏览器多半解不了、直链可能被 CDN 拒。
// 所以把所有副本摆出来，随时能换，换的时候接着当前进度播。
let curCopies = [];

function copyLabel(c) {
  const bits = [c.height ? c.height + 'p' : '', c.size_h || '', c.source || ''];
  return bits.filter(Boolean).join(' · ') || c.name;
}

// 这一份有多大可能播不动。两个信号：
//   1. 网盘没把它认成视频（category != video）——**这是硬信号**：夸克只给
//      video 转码，认成别的就只有原画一档。实测有一批 .mkv/.ts/.mp4 被标成
//      image/png，`/file` streaming 直接回「21005 not video」。
//   2. 扩展名是 .mkv——软信号，里面常是 HEVC/DTS-HD，浏览器解不了却不报错，
//      只会黑屏一直下载。很多 mkv 其实能播，所以只用来排序，不用来拒绝。
function risky(c) {
  if (c.transcodable === false) return true;
  return /\.mkv$/i.test(c.name || '');
}

function riskyWhy(c) {
  if (c.transcodable === false) return '网盘没认成视频，只有原画一档';
  if (/\.mkv$/i.test(c.name || '')) return 'mkv 常是 HEVC/DTS-HD，浏览器可能解不了';
  return '';
}

function renderSources(active) {
  const box = $('#sources');
  box.innerHTML = '';
  box.classList.remove('on', 'warn');
  if (curCopies.length < 2) return;      // 只有一份就没什么可切的
  box.classList.add('on');
  box.appendChild(el('span', 'lbl', '来源'));
  for (const c of curCopies) {
    const b = el('button', 'sbtn' + (c.path === active ? ' active' : '')
                           + (risky(c) ? ' risky' : ''));
    b.textContent = copyLabel(c);
    const why = riskyWhy(c);
    b.title = why ? c.name + `
（` + why + `）` : c.name;
    b.onclick = () => switchSource(c);
    box.appendChild(b);
  }
}

// 换来源 = 换文件，但还是同一集：接着当前进度播，剧集上下文原样留着
function switchSource(c) {
  if (c.path === curPath) return;
  const at = video.currentTime || 0;
  if (playCtx) playCtx.path = c.path;    // 进度要记到新文件上
  playFile({ path: c.path, name: c.name, size_h: c.size_h, nd: curPlayNd,
             keepCtx: true, startAt: at, copies: curCopies });
}

// 当前这份播不了时，换哪一份最有指望：优先不是 mkv 的，其次画质次一档的
function nextBestCopy() {
  const others = curCopies.filter(c => c.path !== curPath);
  return others.find(c => !risky(c)) || others[0] || null;
}

async function playFile(f) {
  // 从目录/播放列表点开的是散片，把剧集上下文清掉，免得进度记到别的剧上
  if (!f.keepCtx) playCtx = null;
  curCopies = f.copies || [];
  curPlayNd = f.nd || curNd;
  const hint = $('#hint');
  hint.className = ''; hint.textContent = '获取直链…';
  try {
    const r = await fetch('/api/play?path=' + encodeURIComponent(f.path)
                          + '&nd=' + curPlayNd);
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    $('#title').textContent = data.file_name;
    $('#sub').textContent = f.size_h || '';
    curPath = f.path;
    const pick = chooseStream(data);
    renderQuality(data, pick);
    renderSources(f.path);
    const at = f.startAt !== undefined ? f.startAt : await resumePos(f.path);
    playStream(data, pick, at);
    renderNav(f);
    hint.className = '';
    hint.textContent = describe(data, pick)
      + (at ? '　（从上次的 ' + fmtTime(at) + ' 继续）' : '');
  } catch (e) {
    reportError(e.message, curPlayNd);
    hint.className = 'error';
    hint.textContent = '播放失败：' + e.message;
  }
}

// 上次手动选过的档位（4k/super/high/low/origin），跨文件沿用
function savedQuality() {
  try { return localStorage.getItem('mf_quality') || ''; } catch (e) { return ''; }
}

// 优先用户上次选的档；该文件没有这一档时退到高度最接近且不超过它的一档；
// 都没有就用网盘建议的 default_key。
// 浏览器明确说解不了的档（MKV/HEVC 原盘就属于这种）不参与自动选择：
// Chrome 遇到这种流不会报错，只会一直黑屏下载，比报错更难查。
function decodable(s) {
  return !s.mime || video.canPlayType(s.mime) !== '';
}

function chooseStream(data) {
  const streams = data.streams || [];
  if (!streams.length) return null;
  const usable = streams.filter(decodable);
  const pool = usable.length ? usable : streams;
  const want = savedQuality();
  const exact = pool.find(s => s.key === want);
  if (exact) return exact;
  const wanted = pool.find(s => s.key === data.default_key) || pool[0];
  if (!want) return wanted;
  // 记住的档这个文件没有：退到高度不超过建议档的最高一档
  const below = pool.filter(s => !s.origin && s.height && s.height <= (wanted.height || 1e9));
  return (want === 'origin' && pool.find(s => s.origin)) || below[0] || wanted;
}

function renderQuality(data, active) {
  const box = $('#quality');
  box.innerHTML = '';
  const streams = data.streams || [];
  if (!streams.length) return;
  for (const s of streams) {
    const b = document.createElement('button');
    b.className = 'qbtn' + (active && s.key === active.key ? ' active' : '');
    b.textContent = s.label;
    b.title = [s.size_h, s.bitrate ? s.bitrate + ' kbps' : '',
               decodable(s) ? '' : '浏览器解不了这个格式'].filter(Boolean).join(' · ');
    if (!decodable(s)) b.classList.add('unplayable');
    b.onclick = () => switchQuality(data, s, b);
    box.appendChild(b);
  }
  if (streams.length === 1) {
    box.firstChild.title = '该文件只有这一档' + (box.firstChild.title ? '（' + box.firstChild.title + '）' : '');
  }
}

// 切档保留进度：同一部片子换的是同一时间轴上的另一条流
let stallTimer = null, gotData = false;
// 直链播放失败过的档，本次会话里改走代理，不再反复试
const proxyFallback = new Set();

function streamUrl(s) {
  return (s.direct && !proxyFallback.has(s.key)) ? s.url : (s.proxy_url || s.url);
}

// 直链被拒（防盗链、链接过期…）时退回本机转发，用户无感
function fallbackToProxy(data, s, why) {
  if (!s.direct || proxyFallback.has(s.key) || !s.proxy_url) return false;
  proxyFallback.add(s.key);
  const at = video.currentTime || 0;
  const hint = $('#hint');
  hint.className = '';
  hint.textContent = s.label + ' 直链不可用（' + why + '），已改用本机转发继续播放';
  playStream(data, s, at);
  return true;
}

// 数据一直在来说明网络没问题，卡的是解码
video.addEventListener('progress', () => { gotData = true; });

let curData = null, curStream = null;

function playStream(data, s, at) {
  if (!s) return;
  curData = data;
  curStream = s;
  const playing = at > 0;
  clearTimeout(stallTimer);
  gotData = false;
  video.src = streamUrl(s);
  video.addEventListener('loadedmetadata', () => {
    if (at > 0) { try { video.currentTime = at; } catch (e) {} }
    if (playing) video.play().catch(() => {});
  }, { once: true });
  video.load();
  document.body.classList.add('has-video');   // 还没播时窄屏不占地方
  if (!playing) video.play().catch(() => {});
  // 解不了的流（MKV/HEVC 原盘）浏览器既不报错也不播，只会一直黑屏往下载。
  // 15 秒还没出画面就分两种情况说清楚，别让人对着黑屏干等。
  stallTimer = setTimeout(() => {
    if (video.readyState !== 0 || video.error) return;
    // 直链一点数据都没来，多半是被 CDN 拒了或链接过期 —— 先退回代理再说
    if (!gotData && fallbackToProxy(data, s, '拿不到数据')) return;
    // 转码档一律是 h264+aac，浏览器不可能解不了 —— 慢只能是网络/seek 还没跟上，
    // 这种情况绝不能掐流（之前在片尾切档就被误杀过）。只有原画才可能真的解不了。
    if (!gotData) stalled(s);
    else if (s.origin) undecodable(data, s);
    else slowLoading(s);
  }, 15000);
}

function undecodable(data, s) {
  video.pause();
  video.removeAttribute('src');
  video.load();                       // 停掉后台那条永远读不完的下载
  if (savedQuality() === s.key) {
    try { localStorage.removeItem('mf_quality'); } catch (e) {}  // 别让这一档粘住
  }
  const hint = $('#hint');
  // 这一档确定解不了，而网盘里还存着这一集的别的版本 —— 直接换过去，
  // 不用让用户先看懂「HEVC/DTS-HD」再自己去点。没有转码档时尤其只有这一条路。
  const alt = nextBestCopy();
  if (alt) {
    hint.className = '';
    hint.textContent = '浏览器解不了这个文件（' + s.label
      + '：原盘 MKV / HEVC / DTS-HD 都属于这种），已自动换到「'
      + copyLabel(alt) + '」。';
    $('#sources').classList.add('warn');
    switchSource(alt);
    return;
  }
  hint.className = 'error';
  // 「没有转码档」本身也分两种，说清楚才知道下一步该干嘛
  const cur = curCopies.find(c => c.path === curPath);
  const notVideo = cur && cur.transcodable === false;
  hint.textContent = '浏览器解不了这一档（' + s.label
    + '：原盘 MKV / HEVC / DTS-HD 音轨都属于这种，已停止后台下载）。'
    + (data.has_transcode ? '点上面的转码档即可正常播放。'
       : notVideo
         ? '这个文件网盘没认成视频（多半是上传时伪装过类型），所以压根没转码档。'
           + '换一个来源，或在作品页点「全部 N 版」多存几个版本。'
         : '该文件没有转码档，请用 mediafans play <文件> 调外部播放器，'
           + '或在作品页点「全部 N 版」多存几个版本再换。');
}

function slowLoading(s) {
  // 只提示不掐流：让浏览器继续加载，通常再等一会就出画面了
  const hint = $('#hint');
  hint.className = '';
  hint.textContent = s.label + ' 加载较慢，还在缓冲…（跳到片尾附近或网络抖动时常见，'
    + '急的话可以切低一档）';
}

function stalled(s) {
  // 只提示，不掐流：慢也可能只是这会儿网差，让它自己 continue。
  // 但这时候「换个来源」往往比等更有用，所以把来源栏标黄提示一下。
  const hint = $('#hint');
  hint.className = 'error';
  const alt = nextBestCopy();
  hint.textContent = s.label + ' 迟迟没有数据，可能是直链过期或网络问题。'
    + '重新点一次文件可重取直链；仍不行就换低一档'
    + (alt ? '，或点下面「来源」换成「' + copyLabel(alt) + '」。' : '试试。');
  if (alt) $('#sources').classList.add('warn');
}

function switchQuality(data, s, btn) {
  const want = streamUrl(s);
  if (video.src === want || video.src === location.origin + want) return;
  try { localStorage.setItem('mf_quality', s.key); } catch (e) {}
  const at = video.currentTime, playing = !video.paused && !video.ended;
  playStream(data, s, playing || at > 0 ? at : 0);
  document.querySelectorAll('.qbtn').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  const hint = $('#hint');
  hint.className = '';
  hint.textContent = describe(data, s);
}

function sourceTag(data, s) {
  // 数据到底走没走本机，得如实说
  if (s && s.direct && !proxyFallback.has(s.key)) return '　· 直连网盘，不经本机转发';
  if (s && s.direct) return '　· 该档直链不可用，已回退本机转发';
  return data && data.fallback_reason ? '　· 本机转发（' + data.fallback_reason + '）' : '';
}

function describe(data, s) {
  if (!s) return '';
  if (!decodable(s)) {
    return '这一档是 ' + (s.mime || '原始封装') + '，浏览器解不了，点上面其他档播放';
  }
  if (s.origin) {
    return data.has_transcode
      ? '正在播放原画（未转码，码率高容易卡；卡就切到上面的转码档）'
      : '该文件没有转码档（REMUX 原盘常见），只能播原画；'
        + '若卡顿或黑屏，用 mediafans play <文件> 调外部播放器';
  }
  return `正在播放 ${s.label}${s.bitrate ? '（' + s.bitrate + ' kbps）' : ''}`
    + `，切换清晰度会保留进度` + sourceTag(data, s);
}

video.addEventListener('error', () => {
  if (!video.src) return;
  clearTimeout(stallTimer);
  if (curData && curStream && fallbackToProxy(curData, curStream, '播放报错')) return;
  try { localStorage.removeItem('mf_quality'); } catch (e) {}
  const hint = $('#hint');
  hint.className = 'error';
  hint.textContent = '浏览器无法解码这一档（原画 REMUX 的 HEVC/DTS-HD 音轨最常见）。'
    + '试试上面的转码档；仍不行就用 mediafans play <文件> 调外部播放器。';
});

video.addEventListener('loadeddata', () => {
  clearTimeout(stallTimer);
  const hint = $('#hint');
  // 重新算而不是用缓存：回退到转发之后，缓存里那句还写着「直连网盘」，是错的
  if (hint.textContent.indexOf('加载较慢') >= 0 || hint.textContent.indexOf('直链不可用') >= 0) {
    hint.className = '';
    hint.textContent = curData && curStream ? describe(curData, curStream) : '';
  }
});

// ---------------- 自定义控制条 ----------------
// 之前 CSS 把原生控制条整个隐藏了，等于没有进度条/全屏/倍速，这里自己实现一套。
const stage = $('#stage'), trackWrap = $('#trackWrap');
let dragging = false;

function togglePlay() {
  if (!video.src) return;
  video.paused ? video.play().catch(() => {}) : video.pause();
}

function toggleMute() {
  video.muted = !video.muted;
  syncVolume();
}

function setRate(r) {
  video.playbackRate = parseFloat(r) || 1;
  // 同步写回下拉框，不等 ratechange 事件（键盘/程序改速时下拉框会慢一拍）
  $('#rate').value = String(video.playbackRate);
  $('#rate').classList.toggle('fast', video.playbackRate !== 1);
  try { localStorage.setItem('mf_rate', String(video.playbackRate)); } catch (e) {}
}

function theater(on) {
  document.body.classList.toggle('theater', on);
  $('#fsBtn').textContent = on ? '⤢' : '⛶';
}

// ---------------- 控制条自动隐藏 ----------------
// 悬浮控制条盖在画面上，不隐的话全屏时一直压着字幕。
// 只在「正在播 + 没在操作控制条」时才隐——暂停着还自动消失会让人以为卡死了。
const IDLE_MS = 3000;
let idleTimer = null;

function showCtrl(rearm = true) {
  stage.classList.remove('idle');
  clearTimeout(idleTimer);
  if (rearm) armIdle();
}

function armIdle() {
  clearTimeout(idleTimer);
  if (video.paused || !video.src) return;
  idleTimer = setTimeout(() => {
    // 鼠标停在控制条上时不隐，不然想点的东西会跑掉
    if (!ctrlHot && !video.paused) stage.classList.add('idle');
  }, IDLE_MS);
}

let ctrlHot = false;
$('#ctrl').addEventListener('pointerenter', () => { ctrlHot = true; showCtrl(false); });
$('#ctrl').addEventListener('pointerleave', () => { ctrlHot = false; armIdle(); });
video.addEventListener('play', armIdle);
video.addEventListener('pause', () => showCtrl(false));
stage.addEventListener('mousemove', () => showCtrl());

// ---------------- 亮度 / 音量手势 ----------------
// 左半屏上下滑调亮度，右半屏调音量（手机播放器的通用手势）。
// 亮度改的是 video 的 CSS filter——**浏览器没有调系统背光的 API**，
// 能做的只有把画面本身调暗/调亮，观感接近但不是真背光。
const BRIGHT_MIN = 0.2, BRIGHT_MAX = 1.6;
let bright = 1;
try { bright = parseFloat(localStorage.getItem('mf_bright') || '1') || 1; } catch (e) {}

function applyBright() {
  bright = Math.max(BRIGHT_MIN, Math.min(BRIGHT_MAX, bright));
  video.style.filter = bright === 1 ? '' : `brightness(${bright.toFixed(2)})`;
  try { localStorage.setItem('mf_bright', String(bright)); } catch (e) {}
}
applyBright();

let hudTimer = null;

function showHud(icon, ratio, text) {
  const h = $('#hud');
  h.innerHTML = '';
  h.appendChild(el('div', null, icon + '　' + text));
  const bar = el('div', 'bar');
  const fill = document.createElement('i');
  fill.style.width = Math.round(Math.max(0, Math.min(1, ratio)) * 100) + '%';
  bar.appendChild(fill);
  h.appendChild(bar);
  h.classList.add('on');
  clearTimeout(hudTimer);
  hudTimer = setTimeout(() => h.classList.remove('on'), 700);
}

// 手势状态。用 pointer 事件，触摸和鼠标一套代码。
let g = null;
const GESTURE_SLOP = 14;   // 超过这个位移才算「在滑」，否则当点击

stage.addEventListener('pointerdown', e => {
  if (e.target.closest('#ctrl')) return;      // 控制条自己有拖拽逻辑
  if (!video.src) return;
  const r = stage.getBoundingClientRect();
  g = {
    id: e.pointerId, x: e.clientX, y: e.clientY, h: r.height || 1,
    side: (e.clientX - r.left) < r.width / 2 ? 'bright' : 'vol',
    startBright: bright, startVol: video.muted ? 0 : video.volume,
    moved: false,
  };
});

stage.addEventListener('pointermove', e => {
  if (!g || e.pointerId !== g.id) return;
  const dy = g.y - e.clientY;                 // 上滑为正
  if (!g.moved && Math.abs(dy) < GESTURE_SLOP) return;
  if (!g.moved) {
    g.moved = true;
    showCtrl(false);                          // 开始调节时把控制条留住
  }
  e.preventDefault();
  // 整屏高度对应满量程的 1.2 倍，滑起来不至于太灵敏
  const frac = dy / (g.h * 1.2);
  if (g.side === 'vol') {
    const v = Math.max(0, Math.min(1, g.startVol + frac));
    video.volume = v;
    video.muted = v === 0;
    syncVolume();
    showHud(v === 0 ? '🔇' : (v > 0.5 ? '🔊' : '🔉'), v, Math.round(v * 100) + '%');
  } else {
    bright = g.startBright + frac * (BRIGHT_MAX - BRIGHT_MIN);
    applyBright();
    const ratio = (bright - BRIGHT_MIN) / (BRIGHT_MAX - BRIGHT_MIN);
    showHud('☀', ratio, Math.round(bright * 100) + '%');
  }
});

function endGesture(e) {
  if (!g || (e && e.pointerId !== g.id)) return;
  const wasMove = g.moved;
  g = null;
  if (wasMove) { armIdle(); return; }
  // 没滑动 = 点了一下画面：控制条藏着就唤出来，露着就收起去
  if (stage.classList.contains('idle')) showCtrl();
  else if (!video.paused) stage.classList.add('idle');
}

stage.addEventListener('pointerup', endGesture);
stage.addEventListener('pointercancel', endGesture);

function toggleFullscreen() {
  if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
  if (document.body.classList.contains('theater')) { theater(false); return; }
  // 真全屏要浏览器给权限（需要用户手势，嵌入式/受限环境可能直接拒绝）。
  // 拒绝了就退化成铺满窗口的网页全屏，至少不能点了没反应。
  let p;
  try { p = stage.requestFullscreen ? stage.requestFullscreen() : Promise.reject(); }
  catch (e) { p = Promise.reject(e); }
  Promise.resolve(p).catch(() => theater(true));
}

function syncVolume() {
  $('#vol').value = video.muted ? 0 : video.volume;
  $('#muteBtn').textContent = (video.muted || !video.volume) ? '🔇'
    : (video.volume > 0.5 ? '🔊' : '🔉');
}

$('#vol').addEventListener('input', e => {
  video.volume = parseFloat(e.target.value);
  video.muted = video.volume === 0;
  syncVolume();
});

function duration() {
  const d = video.duration;
  return (isFinite(d) && d > 0) ? d : 0;
}

function paintProgress() {
  const d = duration();
  const pct = d ? Math.min(100, video.currentTime / d * 100) : 0;
  $('#prog').style.width = pct + '%';
  $('#knob').style.left = pct + '%';
  $('#tCur').textContent = fmtTime(video.currentTime);
  $('#tDur').textContent = d ? fmtTime(d) : '--:--';
  // 已缓冲区间：显示包含当前播放点的那一段，拖动时才知道能拖到哪
  let bufEnd = 0;
  for (let i = 0; i < video.buffered.length; i++) {
    if (video.buffered.start(i) <= video.currentTime + 0.5) bufEnd = video.buffered.end(i);
  }
  $('#buf').style.width = d ? Math.min(100, bufEnd / d * 100) + '%' : '0%';
}

function seekToEvent(e) {
  const d = duration();
  if (!d) return;
  const r = trackWrap.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  video.currentTime = ratio * d;
  paintProgress();
}

// 用 pointer 事件一套搞定鼠标和触屏；拖动时把指针捕获住，
// 拖出轨道范围也不会丢事件
trackWrap.addEventListener('pointerdown', e => {
  if (!duration()) return;
  dragging = true;
  trackWrap.classList.add('dragging');
  trackWrap.setPointerCapture(e.pointerId);
  seekToEvent(e);
});
trackWrap.addEventListener('pointermove', e => { if (dragging) seekToEvent(e); });
for (const ev of ['pointerup', 'pointercancel']) {
  trackWrap.addEventListener(ev, e => {
    if (!dragging) return;
    dragging = false;
    trackWrap.classList.remove('dragging');
    try { trackWrap.releasePointerCapture(e.pointerId); } catch (err) {}
  });
}

video.addEventListener('timeupdate', paintProgress);
video.addEventListener('progress', paintProgress);
video.addEventListener('seeking', paintProgress);
video.addEventListener('loadedmetadata', paintProgress);
video.addEventListener('emptied', paintProgress);
video.addEventListener('volumechange', syncVolume);
video.addEventListener('play', () => { $('#playBtn').textContent = '⏸'; });
video.addEventListener('pause', () => { $('#playBtn').textContent = '▶'; });
video.addEventListener('ratechange', () => { $('#rate').value = String(video.playbackRate); });
document.addEventListener('fullscreenchange', () => {
  if (document.fullscreenElement) theater(false);   // 真全屏成了就别叠着网页全屏
  $('#fsBtn').textContent = document.fullscreenElement ? '⤢' : '⛶';
});
// 换清晰度/换片会重建媒体，倍速要跟着带过去
video.addEventListener('loadstart', () => {
  const r = parseFloat(localStorage.getItem('mf_rate') || '1');
  if (r && r !== 1) video.playbackRate = r;
});
syncVolume();
paintProgress();

let curPath = '';
let lastSaved = 0;

video.addEventListener('timeupdate', () => {
  if (!curPath || !video.duration) return;
  if (Math.abs(video.currentTime - lastSaved) < 5) return;   // 5 秒记一次就够了
  lastSaved = video.currentTime;
  savePos(curPath, video.currentTime, video.duration);
});

video.addEventListener('pause', () => {
  if (curPath && video.duration) savePos(curPath, video.currentTime, video.duration);
});

video.addEventListener('ended', () => {
  if (curPath && video.duration) savePos(curPath, video.duration, video.duration);
  if (!$('#autoNext').checked) return;
  playAdjacent(1);      // 剧集页里它就是「下一集」，散片才按目录顺序走
});

// 快捷键：输入框里打字时不拦截
document.addEventListener('keydown', e => {
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const keys = {
    ' ': () => video.paused ? video.play().catch(() => {}) : video.pause(),
    ArrowRight: () => video.currentTime += 10,
    ArrowLeft: () => video.currentTime -= 10,
    ArrowUp: () => video.volume = Math.min(1, video.volume + 0.1),
    ArrowDown: () => video.volume = Math.max(0, video.volume - 0.1),
    f: toggleFullscreen,
    m: toggleMute,
    n: () => playAdjacent(1),
    p: () => playAdjacent(-1),
    '[': () => stepRate(-1),
    ']': () => stepRate(1),
    Escape: () => { if (document.body.classList.contains('theater')) theater(false); },
  };
  const fn = keys[e.key] || keys[e.key.toLowerCase()];
  if (fn && video.src) { e.preventDefault(); fn(); }
});

const RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

function stepRate(delta) {
  const i = RATES.indexOf(video.playbackRate);
  const next = RATES[Math.max(0, Math.min(RATES.length - 1, (i < 0 ? 2 : i) + delta))];
  setRate(next);
  const hint = $('#hint');
  hint.className = '';
  hint.textContent = '播放速度 ' + next + '×';
}

// ---------------- 最近观看 ----------------
async function renderWatch() {
  const box = $('#watchList');
  let items = [];
  try {
    const d = await (await fetch('/api/watch/recent?limit=20')).json();
    items = d.items || [];
  } catch (e) {
    box.innerHTML = '<div class="empty">读取失败：' + e.message + '</div>';
    return;
  }
  if (!items.length) {
    box.innerHTML = '<div class="empty">还没有看过的记录<br>'
      + '<span class="dim">看过的会自动出现在这里，换个设备也能接着看</span></div>';
    return;
  }
  box.innerHTML = '';
  for (const m of items) box.appendChild(watchRow(m));
}

function watchRow(m) {
  const row = el('div', 'wrow');
  if (m.poster) {
    const img = document.createElement('img');
    img.src = m.poster; img.loading = 'lazy'; img.alt = m.title || m.name;
    row.appendChild(img);
  } else {
    row.appendChild(el('div', 'noposter', m.title || '影片'));
  }
  const body = el('div', 'body');
  const isEp = !!(m.tmdb_id && m.season && m.episode);
  const isMovie = m.media_type === 'movie' && !!m.tmdb_id;
  body.appendChild(el('div', 't', m.title || m.name));
  // 看完了就直接说「接着看下一集」——用户要的是下一步动作，不是历史记录。
  // 电影没有下一集，看完了就是看完了。
  const where = isEp
    ? '第' + m.season + '季 第' + m.episode + '集'
      + (m.ep_title ? ' · ' + m.ep_title : '')
    : (isMovie ? ('电影' + (m.year ? ' · ' + m.year : '')) : (m.name || ''));
  const state = m.finished
    ? (isEp ? '已看完 · 接着看第' + (m.episode + 1) + '集' : '已看完')
    : '看到 ' + fmtTime(m.position) + (m.duration ? ' / ' + fmtTime(m.duration) : '');
  body.appendChild(el('div', 's', where + '　·　' + state));
  const bar = el('div', 'bar');
  const fill = document.createElement('i');
  fill.style.width = m.percent + '%';
  bar.appendChild(fill);
  body.appendChild(bar);
  row.appendChild(body);

  const x = el('button', 'x', '×');
  x.title = '从列表移除';
  x.onclick = function (e) {
    e.stopPropagation();
    fetch('/api/watch/forget', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: m.path }) }).then(renderWatch);
  };
  row.appendChild(x);
  // 百度的进度记录带 netdisk，回放时要去对应的盘
  if (m.netdisk && m.netdisk !== 'quark') {
    row.appendChild(el('span', 'badge b-' + m.netdisk, m.netdisk));
  }
  row.onclick = () => resumeWatch(m);
  return row;
}

async function resumeWatch(m) {
  if (m.tmdb_id && (m.season || m.media_type === 'movie')) {
    // 进作品页：剧集看完了就落到下一集，没看完就接着这一集；
    // 电影只有一行，openSeriesAt 会自己回到那一行
    setDriveValue(m.netdisk || 'quark');
    await openSeriesAt(m, m.finished ? (m.episode || 0) + 1 : m.episode);
    return;
  }
  switchTab('mine');
  playFile({ path: m.play_path || m.path, name: m.name, size_h: m.size_h || '',
             nd: m.netdisk || 'quark' });
}

// ---------------- 搜索 / 分享 / 转存 ----------------
let shareCtx = { url: '', code: '', dir_fid: '', crumb: '' };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

let showsLoaded = false;
let mineLoaded = false;
const TABS = ['shows', 'series', 'watch', 'search', 'mine'];

function switchTab(name) {
  for (const t of TABS) {
    $('#tab-' + t).style.display = t === name ? '' : 'none';
    $('#tabbtn-' + t).classList.toggle('active', t === name);
  }
  // 窄屏下靠这个类把播放区让给列表。剧集页和最近观看不加：
  // 那两页点一下就要看片，把播放器压到 24vh 反而挡事。
  document.body.classList.toggle('tab-search', name === 'search' || name === 'shows');
  if (name === 'shows' && !showsLoaded) { showsLoaded = true; setDiscoverMedia('tv'); }
  if (name === 'watch') renderWatch();
  if (name === 'mine' && !mineLoaded) { mineLoaded = true; loadDir(''); }
}

// 顶栏搜的是「作品」而不是「分享链接」：先定位到剧，才有权威的季/集结构，
// 后面找资源、补缺集才有基准。搜原始网盘分享挪到了「搜资源」页自己的输入框。
let foundItems = null;
// 海报墙同时只认最后一次请求：页面刚打开就输入关键词时，先发出的「今日播出」
// 会后回来把搜索结果盖掉；快速切榜单也一样。用序号丢弃过期响应。
let showsSeq = 0;

async function searchMedia() {
  const kw = $('#kw').value.trim();
  if (!kw) return;
  const seq = ++showsSeq;
  // 搜过一次之后就别再自动拉榜单了，否则切回「追剧」会把搜索结果冲掉
  showsLoaded = true;
  switchTab('shows');
  const box = $('#shows');
  box.innerHTML = '<div class="empty">搜索中…</div>';
  document.querySelectorAll('#kindSeg .seg').forEach(b => b.classList.remove('on'));
  $('#segFound').style.display = '';
  $('#segFound').classList.add('on');
  try {
    const d = await (await fetch('/api/search/media?kw=' + encodeURIComponent(kw))).json();
    if (seq !== showsSeq) return;
    if (d.error) throw new Error(d.error);
    foundItems = d.items;
    renderFound();
  } catch (e) {
    if (seq !== showsSeq) return;
    box.innerHTML = '<div class="empty">搜索失败：' + e.message + '</div>';
  }
}

function renderFound() {
  // 只动档位那一排；「剧集/电影」的切换态是另一维，别一起清掉
  document.querySelectorAll('#kindSeg .seg, #segFound').forEach(b =>
    b.classList.toggle('on', b.id === 'segFound'));
  const box = $('#shows');
  if (!foundItems || !foundItems.length) {
    box.innerHTML = '<div class="empty">没找到这部作品，换个名字试试'
      + '<br><span class="dim">（想直接搜网盘分享，去「搜资源 / 转存」页）</span></div>';
    return;
  }
  renderCards(foundItems);
}

async function doSearch() {
  const kw = ($('#diskKw') && $('#diskKw').value.trim()) || $('#kw').value.trim();
  if (!kw) return;
  switchTab('search');
  backToResults();
  $('#searchStatus').textContent = '搜索中…';
  $('#results').innerHTML = '';
  try {
    const nd = ($('#nd') || {}).value || '';
    let qs = 'kw=' + encodeURIComponent(kw) + (nd ? '&netdisk=' + nd : '');
    let data = await (await fetch('/api/search?' + qs)).json();
    if (data.error) throw new Error(data.error);
    let note = '';
    // 词组没结果时用核心词（第一个词）自动放宽重试
    if (!data.results.length && kw.split(/\s+/).length > 1) {
      const broader = kw.split(/\s+/)[0];
      qs = 'kw=' + encodeURIComponent(broader) + (nd ? '&netdisk=' + nd : '');
      const retry = await (await fetch('/api/search?' + qs)).json();
      if (!retry.error && retry.results.length) {
        data = retry;
        note = `“${kw}” 无结果，已用 “${broader}” 重试`;
      }
    }
    for (const e of (data.errors || [])) {
      $('#results').appendChild(el('div', 'dim pad', '! 搜索源失败：' + e));
    }
    if (!data.results.length) {
      $('#searchStatus').textContent = '没有搜到结果，换个关键词试试';
      return;
    }
    $('#searchStatus').className = note ? 'dim' : 'empty';
    $('#searchStatus').textContent = note;
    for (const item of data.results) {
      const row = el('div', 'row');
      row.appendChild(el('span', 'badge b-' + item.netdisk, item.netdisk));
      const name = el('span', 'name', item.title || item.url);
      name.title = (item.note || '') + '\n' + item.url;
      row.appendChild(name);
      if (item.datetime) row.appendChild(el('span', 'size', item.datetime.slice(0, 10)));
      if (item.passcode) row.appendChild(el('span', 'size', '码 ' + item.passcode));
      const url = item.url, code = item.passcode || '';
      row.onclick = () => openShare(url, code);
      $('#results').appendChild(row);
    }
  } catch (e) {
    $('#searchStatus').textContent = '搜索失败：' + e.message;
  }
}

function backToResults() {
  $('#shareView').style.display = 'none';
  $('#resultsWrap').style.display = '';
}

async function openShare(url, code) {
  if (!url) return;
  shareCtx = { url, code: code || '', dir_fid: '', crumb: '' };
  $('#pasteUrl').value = url;
  $('#resultsWrap').style.display = 'none';
  $('#shareView').style.display = '';
  $('#codeInput').value = shareCtx.code;
  reloadShare();
}

// 进分享里的子目录（后端会自动钻过只有一个子目录的空壳层）
function openShareDir(fid, crumb) {
  shareCtx.dir_fid = fid;
  shareCtx.crumb = crumb;
  reloadShare(true);
}

async function reloadShare(keepDir) {
  shareCtx.code = $('#codeInput').value.trim();
  if (!keepDir) { shareCtx.dir_fid = ''; shareCtx.crumb = ''; }
  $('#shareFiles').innerHTML = '<div class="empty">读取分享内容…</div>';
  const st = $('#saveStatus');
  st.className = 'dim'; st.textContent = '';
  try {
    const qs = 'url=' + encodeURIComponent(shareCtx.url)
      + '&code=' + encodeURIComponent(shareCtx.code)
      + '&dir_fid=' + encodeURIComponent(shareCtx.dir_fid || '')
      + '&crumb=' + encodeURIComponent(shareCtx.crumb || '');
    const data = await (await fetch('/api/share?' + qs)).json();
    if (data.error) throw new Error(data.error);
    shareCtx.dir_fid = data.dir_fid;
    shareCtx.crumb = data.crumb || '';
    shareCtx.nd = data.netdisk || 'quark';
    $('#shareUrlShow').textContent = shareCtx.url;
    renderShareCrumb(data);
    renderShareStats(data.counts);
    renderShareFiles(data.files);
  } catch (e) {
    $('#shareFiles').innerHTML = '';
    $('#shareCrumb').innerHTML = '';
    $('#shareStats').textContent = '';
    st.className = 'error';
    st.textContent = '读取失败：' + e.message + '（如需提取码，填写后点刷新）';
  }
}

function renderShareCrumb(data) {
  const box = $('#shareCrumb');
  box.innerHTML = '';
  const root = el('a', null, '分享根目录');
  root.onclick = () => { shareCtx.dir_fid = ''; shareCtx.crumb = ''; reloadShare(); };
  box.appendChild(root);
  const parts = (data.crumb || '').split('/').filter(Boolean);
  for (const part of parts) box.appendChild(document.createTextNode(' / ' + part));
}

function renderShareStats(c) {
  if (!c) { $('#shareStats').textContent = ''; return; }
  const bits = [];
  if (c.playable) bits.push(c.playable + ' 个可播放 · ' + c.size_h);
  if (c.dirs) bits.push(c.dirs + ' 个目录');
  if (c.junk) bits.push(c.junk + ' 个非媒体');
  $('#shareStats').textContent = bits.join('，');
}

const KIND_ICON = { dir: '📁', video: '🎬', audio: '🎵', subtitle: '💬', other: '📄' };
const KIND_TAG = { subtitle: '字幕', other: '非媒体' };

function renderShareFiles(files) {
  const box = $('#shareFiles');
  box.innerHTML = '';
  if (!files.length) {
    box.appendChild(el('div', 'empty', '这个目录是空的'));
    return;
  }
  if (!files.some(f => f.playable) && !files.some(f => f.is_dir)) {
    box.appendChild(el('div', 'pad dim', '! 这个目录里没有可播放的文件'));
  }
  for (const f of files) {
    if (f.is_dir) {
      // 目录点进去看；想整个目录转存就勾右边的框
      const row = el('div', 'row');
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.value = f.fid; cb.className = 'fchk';
      cb.title = '勾选可整个目录转存';
      cb.onclick = e => e.stopPropagation();
      row.append(el('span', 'icon', '📁'), el('span', 'name', f.name), cb);
      row.onclick = () => openShareDir(
        f.fid, (shareCtx.crumb ? shareCtx.crumb + '/' : '') + f.name);
      box.appendChild(row);
      continue;
    }
    const row = el('label', 'row' + (f.playable ? '' : ' junk'));
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = f.playable;   // 默认只勾能看的，说明.txt / 广告图不勾
    cb.value = f.fid; cb.className = 'fchk';
    row.append(cb, el('span', 'icon', KIND_ICON[f.kind] || '📄'),
               el('span', 'name', f.name));
    if (KIND_TAG[f.kind]) row.appendChild(el('span', 'kind', KIND_TAG[f.kind]));
    row.appendChild(el('span', 'size', f.size_h));
    box.appendChild(row);
  }
}

function toggleAll(on) {
  document.querySelectorAll('.fchk').forEach(x => x.checked = on);
}

async function saveShare() {
  const fids = [...document.querySelectorAll('.fchk')].filter(x => x.checked).map(x => x.value);
  const st = $('#saveStatus');
  if (!fids.length) {
    st.className = 'error'; st.textContent = '请先勾选要转存的文件';
    return;
  }
  $('#saveBtn').disabled = true;
  st.className = 'dim'; st.textContent = '转存中…';
  try {
    const resp = await fetch('/api/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: shareCtx.url,
        code: $('#codeInput').value.trim(),
        dir_fid: shareCtx.dir_fid || '0',
        fids: fids,
        to: $('#toDir').value.trim()
      })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    st.className = 'ok';
    st.textContent = '';
    st.append(`✓ 已转存 ${data.saved} 项到 ${data.dir} `, (() => {
      const b = document.createElement('button');
      b.textContent = '打开目录';
      b.onclick = () => {
        setDriveValue(data.netdisk || 'quark');
        loadDir(data.dir);
        switchTab('mine');
      };
      return b;
    })());
  } catch (e) {
    st.className = 'error';
    st.textContent = '转存失败：' + e.message;
  } finally {
    $('#saveBtn').disabled = false;
  }
}

// ---------------- 发现：剧集/电影榜 + 一键找片 ----------------
let autoTimer = null, autoDir = '';

// 剧集和电影各三档。电影的「正在上映」在后端往前推了 45 天——院线片的热度
// 窗口比剧集长，只取当天几乎是空的。
const DISCOVER_KINDS = {
  tv: [['airing', '今日播出'], ['onair', '一周在播'], ['popular', '热门']],
  movie: [['now', '正在上映'], ['upcoming', '即将上映'], ['hot', '热门']],
};
let discoverMedia = 'tv';

function setDiscoverMedia(media) {
  discoverMedia = media;
  document.querySelectorAll('#mediaSeg .seg').forEach(b =>
    b.classList.toggle('on', b.dataset.media === media));
  const box = $('#kindSeg');
  box.innerHTML = '';
  for (const [kind, label] of DISCOVER_KINDS[media]) {
    const b = el('button', 'seg', label);
    b.dataset.kind = kind;
    b.onclick = () => loadShows(kind);
    box.appendChild(b);
  }
  loadShows(DISCOVER_KINDS[media][0][0]);
}

async function loadShows(kind) {
  const seq = ++showsSeq;
  document.querySelectorAll('#kindSeg .seg, #segFound').forEach(b =>
    b.classList.toggle('on', b.dataset.kind === kind));
  const box = $('#shows');
  box.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await (await fetch('/api/discover?kind=' + kind)).json();
    if (seq !== showsSeq) return;
    if (data.error) throw new Error(data.error);
    if (!data.items.length) { box.innerHTML = '<div class="empty">没有数据</div>'; return; }
    renderCards(data.items, data.note);
  } catch (e) {
    if (seq !== showsSeq) return;
    box.innerHTML = '<div class="empty">加载失败：' + e.message + '</div>';
  }
}

function renderCards(items, note) {
  const box = $('#shows');
  box.innerHTML = '';
  if (note) box.appendChild(el('div', 'wall-note', '· ' + note));
  const splitAt = langSplitIndex(items);
  items.forEach((it, idx) => {
    // 华语排在前面，换到其他语种时给条分隔——不然会以为下面这些是漏排的
    if (idx === splitAt) box.appendChild(el('div', 'wall-note', '以下为其他语种'));
    const card = el('div', 'card');
    if (it.poster) {
      const img = document.createElement('img');
      img.className = 'poster'; img.loading = 'lazy'; img.alt = it.title;
      img.src = it.poster;
      card.appendChild(img);
    } else {
      card.appendChild(el('div', 'noposter', it.title));
    }
    const cap = el('div', 'cap');
    cap.appendChild(el('b', null, it.title));
    const meta = el('span', null, (it.year || ''));
    if (it.media_type === 'movie') meta.appendChild(el('span', 'kind', '影'));
    if (it.rating) meta.appendChild(el('span', 'star', '　★' + it.rating));
    cap.appendChild(meta);
    card.appendChild(cap);
    const isMovie = it.media_type === 'movie';
    card.title = (it.overview || it.title) + '\n\n'
      + (isMovie ? '点击查看这部片，右键一键找片'
                 : '点击按季/集查看，右键一键找全季');
    // 电影和剧集都进详情页：电影就是只有一行的「季」，转存/播放/进度全都一样。
    // 右键仍然是「别问了直接找一份存下来」的快捷方式。
    card.onclick = () => openSeries(it);
    card.oncontextmenu = (e) => { e.preventDefault(); startAuto(it); };
    box.appendChild(card);
  });
}

// 华语和其他语种的分界。只在确实分成干净两段时才给分隔条——
// 搜索结果不是严格按语言排的（完全同名的优先），搜「长城」第一条就是外语，
// 这时候画一条「以下为其他语种」会跑到最顶上，反而看不懂。
function langSplitIndex(items) {
  const first = items.findIndex(x => x.chinese === false);
  if (first <= 0) return -1;
  return items.slice(first).every(x => x.chinese === false) ? first : -1;
}

async function startAuto(item) {
  clearInterval(autoTimer);
  autoDir = '';
  $('#autoPanel').classList.add('on');
  $('#autoTitle').textContent = item.title + (item.year ? '（' + item.year + '）' : '');
  $('#autoSub').textContent = '搜索 → 逐个打开验证 → 挑最合适的 → 转存';
  $('#autoSteps').innerHTML = '';
  $('#autoCands').innerHTML = '';
  $('#autoStat').textContent = '正在开始…';
  $('#autoOpen').style.display = 'none';
  try {
    const r = await fetch('/api/auto', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // 把作品身份一起送过去：同名的动画版/真人版靠 animation 分开
      body: JSON.stringify({ name: item.title, tmdb_id: item.tmdb_id,
                             original: item.original || '',
                             animation: item.animation === undefined ? null : item.animation,
                             media_type: item.media_type || 'tv', save: true,
                             netdisk: curNd })
    });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    pollAuto(data.job);
  } catch (e) {
    addStep('error', '启动失败：' + e.message);
    $('#autoStat').textContent = '';
  }
}

const STEP_ICON = { search: '🔎', probe: '🧪', rank: '📊', ai: '∴', save: '💾',
                    done: '✓', error: '✗', meta: 'ℹ' };

function addStep(stage, message) {
  const row = el('div', 'st ' + (stage === 'error' ? 'error' : stage === 'done' ? 'done'
                 : stage === 'ai' ? 'ai' : ''));
  row.append(el('i', null, STEP_ICON[stage] || '·'), el('span', null, message));
  $('#autoSteps').appendChild(row);
  $('#autoSteps').scrollTop = $('#autoSteps').scrollHeight;
}

function pollAuto(job) {
  let shown = 0;
  autoTimer = setInterval(async () => {
    let data;
    try {
      data = await (await fetch('/api/auto/status?job=' + encodeURIComponent(job))).json();
      if (data.error && !data.steps) throw new Error(data.error);
    } catch (e) {
      clearInterval(autoTimer);
      addStep('error', '查询进度失败：' + e.message);
      return;
    }
    for (const st of (data.steps || []).slice(shown)) addStep(st.stage, st.message);
    shown = (data.steps || []).length;
    if (!data.done) { $('#autoStat').textContent = '处理中…'; return; }
    clearInterval(autoTimer);
    finishAuto(data);
  }, 900);
}

function finishAuto(data) {
  if (data.error) { addStep('error', data.error); $('#autoStat').textContent = ''; return; }
  const res = data.result || {};
  if (res.netdisk) setDriveValue(res.netdisk);
  renderCandidates(res);
  if (res.ok) {
    autoDir = res.saved_dir || '';
    $('#autoStat').textContent = res.saved ? `已转存 ${res.saved} 个文件`
      : (res.already ? `已在网盘里（${res.already} 个文件）` : '已选中资源');
    if (autoDir) $('#autoOpen').style.display = '';
  } else {
    $('#autoStat').textContent = '';
  }
}

function renderCandidates(res) {
  const box = $('#autoCands');
  box.innerHTML = '';
  if (!res.candidates || !res.candidates.length) return;
  if (res.verdict) {
    const v = el('div', 'pad', 'AI 判断（' + res.verdict.confidence + '）：' + res.verdict.reason);
    v.style.color = '#c99bff';
    box.appendChild(v);
  } else if (res.ai_note) {
    box.appendChild(el('div', 'pad dim', res.ai_note));
  }
  const t = document.createElement('table');
  for (const c of res.candidates) {
    const tr = document.createElement('tr');
    const picked = res.picked && c.title === res.picked.title && c.score === res.picked.score;
    const name = el('td', c.ok ? (picked ? 'pick' : '') : 'bad',
                    (picked ? '✓ ' : '') + c.title.slice(0, 34));
    const info = el('td', null, c.ok ? `${c.playable} 个 · ${c.size_h}` : (c.error || '不可用'));
    info.style.whiteSpace = 'nowrap';
    const why = el('td', 'dim', (c.reasons || []).slice(0, 3).join('、'));
    tr.append(name, info, why);
    t.appendChild(tr);
  }
  box.appendChild(t);
}

function openAutoResult() {
  closeAuto();
  // 一键找片跑在哪个盘，目录就去哪个盘开
  if (autoDir) loadDir(autoDir);
  switchTab('mine');
}

function closeAuto() {
  clearInterval(autoTimer);
  $('#autoPanel').classList.remove('on');
}

// ---------------- 剧集矩阵：按季/集铺开，缺的按集补 ----------------
let series = null, seriesJobTimer = null;

// 这部剧是谁：进度记录要靠它按剧聚合，所以海报、标题一路带下来
let seriesShow = null;

async function openSeries(item, season) {
  clearInterval(seriesJobTimer);
  switchTab('series');
  const media = item.media_type === 'movie' ? 'movie' : 'tv';
  seriesShow = { tmdb_id: item.tmdb_id, title: item.title, year: item.year || '',
                 poster: item.poster || '', media_type: media };
  $('#tabbtn-series').textContent = media === 'movie' ? '影片' : '剧集';
  $('#seriesTitle').textContent = item.title + (item.year ? '（' + item.year + '）' : '');
  $('#seasonTabs').innerHTML = '';
  $('#epList').innerHTML = '<div class="empty">读取中…</div>';
  $('#scanLog').textContent = '';
  $('#seriesStat').textContent = '';
  $('#seriesDir').textContent = '';
  // 电影没有季，用 season=0 占位；后端认 media 参数，season 只是缓存键的一部分
  await loadSeason(item.tmdb_id, media === 'movie' ? 0 : (season || 1), false, media);
}

// 从「最近观看」回到剧集页，并直接接着播目标集。
// 目标集没存下来就只把矩阵摆出来——那时候用户要做的是补这一集，不是播。
async function openSeriesAt(mark, episode) {
  const media = mark.media_type === 'movie' ? 'movie' : 'tv';
  await openSeries({ tmdb_id: mark.tmdb_id, title: mark.title, year: mark.year,
                     poster: mark.poster, media_type: media }, mark.season);
  const d = series && series.data;
  if (!d) return;
  // 电影只有一行，「下一集」这个概念不存在，永远回到那一行
  if (media === 'movie') episode = (d.episodes[0] || {}).episode;
  const ep = (d.episodes || []).find(e => e.episode === episode);
  // 上次看的是哪一份就接着那一份：一集存了多份时，主副本不一定是用户在看的
  if (ep && ep.copies && mark.play_path) {
    const same = ep.copies.find(c => c.path === mark.play_path);
    if (same) { playEpisode(d, ep, undefined, same); return; }
  }
  if (ep && ep.local) playEpisode(d, ep);
  else if (ep) {
    const row = $('#ep-' + episode);
    if (row) row.scrollIntoView({ block: 'center' });
  }
}

async function loadSeason(tmdbId, season, refresh, media) {
  media = media || (series && series.media) || 'tv';
  series = { tmdb_id: tmdbId, season, media };
  $('#epList').innerHTML = '<div class="empty">读取中…</div>';
  try {
    const qs = `tmdb_id=${tmdbId}&season=${season}&nd=${curNd}&media=${media}`
             + (refresh ? '&refresh=1' : '');
    const d = await (await fetch('/api/series?' + qs)).json();
    if (d.error) throw new Error(d.error);
    series.data = d;
    renderSeasons(d);
    renderEpisodes(d);
  } catch (e) {
    reportError(e.message, curNd);
    $('#epList').innerHTML = '<div class="empty">读取失败：' + e.message + '</div>';
  }
}

function renderSeasons(d) {
  const box = $('#seasonTabs');
  box.innerHTML = '';
  if (d.media_type === 'movie') {
    // 电影没有季可切，这一栏改放片长/上映年份，空着显得像加载失败
    const bits = [d.year, d.runtime ? d.runtime + ' 分钟' : ''].filter(Boolean);
    if (bits.length) box.appendChild(el('span', 'dim', bits.join('　·　')));
    return;
  }
  for (const s of d.seasons || []) {
    if (!s.episodes) continue;
    const b = el('button', s.season === d.season ? 'on' : null,
                 `第${s.season}季 · ${s.episodes}集`);
    b.onclick = () => loadSeason(d.tmdb_id, s.season);
    box.appendChild(b);
  }
}

function renderEpisodes(d) {
  const c = d.counts;
  const isMovie = d.media_type === 'movie';
  const row0 = d.episodes[0] || {};
  // 电影只有一行，「共 1 集 · 已存 1」这种说法没意义，直接说状态
  $('#seriesStat').innerHTML = isMovie
    ? (c.saved ? '<b>网盘里已经有了</b>'
               : (row0.sources && row0.sources.length
                  ? `<i>${row0.sources.length} 个版本可以转存</i>` : '<u>网盘里还没有</u>'))
    : `共 ${c.total} 集 · <b>已存 ${c.saved}</b>`
      + (c.available ? ` · <i>可补 ${c.available}</i>` : '')
      + (c.missing ? ` · <u>缺 ${c.missing}</u>` : '');
  $('#seriesDir').textContent = d.local_dir || '';
  $('#scanBtn').style.display = c.saved === c.total ? 'none' : '';
  $('#scanBtn').textContent = scanLabel();
  $('#scanBtn').disabled = false;
  // 有来源可补才给「一键转存」——没扫过的时候按了也没用。
  // 电影只有一行，逐个版本挑才是重点，批量按钮反而碍事。
  const grab = $('#grabBtn');
  grab.style.display = (!isMovie && c.available) ? '' : 'none';
  grab.textContent = '一键转存 ' + c.available + ' 集';
  grab.disabled = false;
  const box = $('#epList');
  box.innerHTML = '';
  if (!d.episodes.length) {
    box.innerHTML = '<div class="empty">' + (isMovie ? '没有影片信息' : '这一季还没有集信息')
                    + '</div>';
    return;
  }
  if (isMovie && d.overview) box.appendChild(el('div', 'pad dim', d.overview));
  for (const ep of d.episodes) box.appendChild(epRow(d, ep));
  for (const n of d.notes || []) box.appendChild(el('div', 'pad dim', '· ' + n));
}

function epRow(d, ep) {
  const w = ep.watched;
  const isMovie = d.media_type === 'movie';
  const playingHere = !!curPath && (ep.copies || []).some(c => c.path === curPath);
  const row = el('div', 'ep ' + ep.status + (w && w.finished ? ' done' : '')
                 + (playingHere ? ' playing' : ''));
  row.id = 'ep-' + ep.episode;
  row.appendChild(el('span', 'no', isMovie ? '影片'
                                           : 'E' + String(ep.episode).padStart(2, '0')));
  const body = el('div', 'body');
  body.appendChild(el('div', 'name',
                      ep.title || (isMovie ? d.title : '第 ' + ep.episode + ' 集')));
  const bits = [];
  const nCopies = (ep.copies || []).length;
  if (ep.local) {
    bits.push((ep.local.height ? ep.local.height + 'p · ' : '') + ep.local.size_h);
    // 存了不止一份就说清楚：播放时能在这几份之间切
    if (nCopies > 1) bits.push('已存 ' + nCopies + ' 个版本，播放时可切换');
  } else if (ep.sources.length) {
    bits.push(ep.sources.length + (isMovie ? ' 个版本可转存' : ' 个来源可补'));
  } else {
    bits.push('还没有来源');
  }
  if (w && !w.finished && w.percent) bits.push('看到 ' + fmtTime(w.position));
  if (ep.air_date) bits.push(ep.air_date);
  body.appendChild(el('div', 'sub', bits.join('　·　')));
  if (w && w.percent && !w.finished) {
    const bar = el('div', 'bar');
    const fill = document.createElement('i');
    fill.style.width = w.percent + '%';
    bar.appendChild(fill);
    body.appendChild(bar);
  }
  row.appendChild(body);

  const act = el('div', 'act');
  if (ep.local) {
    // 网盘里已经有了：直接生成直链播放，不用等转存
    const b = el('button', 'pill play',
                 w && !w.finished && w.resume_at ? '继续' : (w && w.finished ? '重看' : '播放'));
    b.onclick = () => playEpisode(d, ep);
    act.appendChild(b);
  }
  // 有多个来源就给「全部」：先各存一份，播不了的时候一键换，
  // 不用回来重新找资源。已经存过的会按文件名跳过。
  if (ep.sources.length > 1) {
    const n = Math.min(ep.sources.length, COPY_CAP);
    const b = el('button', 'pill all', '全部 ' + n + ' 版');
    b.title = '把这' + (isMovie ? '部片' : '集') + '的 ' + n
              + ' 个版本都转存下来，播放时可随时切换';
    b.onclick = () => fetchAllSources(d, ep, b);
    act.appendChild(b);
  }
  if (!ep.local) {
    for (const src of ep.sources.slice(0, isMovie ? 5 : 3)) {
      const b = el('button', 'pill src',
                   src.label + (src.height ? ' ' + src.height + 'p' : ''));
      b.title = `${src.share_title}\n${src.name}\n${src.size_h}` +
                (src.chinese_sub ? '\n有中文字幕' : '');
      b.onclick = () => fetchEpisode(d, ep, src, b);
      act.appendChild(b);
    }
  }
  row.appendChild(act);
  return row;
}

// 一集最多留几份。跟后端 agent/series.py 的 DEFAULT_COPY_CAP 必须一致，
// 有测试钉住，别单独改一边。
const COPY_CAP = 5;

// 把这一集/这部片能找到的版本都转存下来。为什么值得：「哪一份能播」只有
// 播起来才知道（原盘 MKV 解不了、直链被拒、某版没中文字幕），事后再回来
// 重新找资源很折腾。转存是秒传，占的是网盘配额不是上传时间。
function fetchAllSources(d, ep, btn) {
  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = '转存中…';
  $('#scanLog').textContent = '';
  clearInterval(seriesJobTimer);
  fetch('/api/episode/fetch/all', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tmdb_id: d.tmdb_id, season: d.season,
                           media: d.media_type || 'tv', episode: ep.episode,
                           netdisk: curNd })
  }).then(r => r.json()).then(j => {
    if (j.error) throw new Error(j.error);
    let shown = 0;
    seriesJobTimer = setInterval(async () => {
      const st = await (await fetch('/api/auto/status?job='
                                    + encodeURIComponent(j.job))).json();
      for (const step of (st.steps || []).slice(shown)) {
        $('#scanLog').appendChild(el('div', null, '· ' + step.message));
        $('#scanLog').scrollTop = $('#scanLog').scrollHeight;
      }
      shown = (st.steps || []).length;
      if (!st.done) return;
      clearInterval(seriesJobTimer);
      btn.disabled = false;
      btn.textContent = was;
      if (st.error) {
        reportError(st.error, curNd);
        $('#scanLog').appendChild(el('div', null, '✗ ' + st.error));
        return;
      }
      series.data = st.result;
      renderSeasons(st.result);
      renderEpisodes(st.result);
      // 正在播的就是这一集的话，把新存下来的版本直接补进来源栏
      const fresh = (st.result.episodes || []).find(x => x.episode === ep.episode);
      if (fresh && curCopies.length && fresh.copies) {
        curCopies = fresh.copies;
        renderSources(curPath);
      }
      for (const e of (st.result.fetched || {}).errors || []) {
        $('#scanLog').appendChild(el('div', null, '· ' + e));
        reportError(e, curNd);
      }
    }, 900);
  }).catch(e => {
    btn.disabled = false;
    btn.textContent = was;
    reportError(e.message, curNd);
    $('#scanLog').textContent = '启动失败：' + e.message;
  });
}

async function fetchEpisode(d, ep, src, btn) {
  btn.classList.add('busy');
  btn.textContent = '转存中…';
  try {
    const r = await fetch('/api/episode/fetch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tmdb_id: d.tmdb_id, season: d.season,
                             episode: ep.episode, index: src.index, netdisk: curNd })
    });
    const res = await r.json();
    if (res.error) throw new Error(res.error);
    await loadSeason(d.tmdb_id, d.season, true, d.media_type);
  } catch (e) {
    reportError(e.message, curNd);
    btn.classList.remove('busy');
    btn.textContent = '失败';
    btn.title = e.message;
  }
}

// 播这一集。带上剧集上下文，进度才能记成「末日地堡 S02E08」而不是一个孤零零的路径。
function playEpisode(d, ep, startAt, copy) {
  const show = seriesShow || {};
  const isMovie = d.media_type === 'movie';
  const use = copy || ep.local;      // copy：指定播这一集的哪一份
  // 电影的季/集留空：不是「第 0 季第 1 集」，是根本没有这个维度。
  // 「最近观看」靠这个决定显示成「第 2 季 第 8 集」还是就一个片名。
  playCtx = {
    path: use.path,
    tmdb_id: d.tmdb_id, season: isMovie ? null : d.season,
    episode: isMovie ? null : ep.episode,
    meta: { tmdb_id: d.tmdb_id, media_type: isMovie ? 'movie' : 'tv',
            season: isMovie ? null : d.season, episode: isMovie ? null : ep.episode,
            title: show.title || d.title || '', year: show.year || d.year || '',
            poster: show.poster || d.poster || '',
            ep_title: isMovie ? '' : (ep.title || ''),
            name: use.name, size_h: use.size_h },
  };
  const f = { path: use.path, name: use.name, size_h: use.size_h,
              keepCtx: true, copies: ep.copies || [] };
  if (startAt !== undefined) f.startAt = startAt;
  return playFile(f).then(() => { if (series && series.data) renderEpisodes(series.data); });
}

// 一键把这一季能补的集全补上。27 集逐个点太折磨人。
function fetchSeason() {
  if (!series) return;
  const btn = $('#grabBtn');
  const n = (series.data && series.data.counts.available) || 0;
  if (!n) return;
  btn.disabled = true;
  btn.textContent = '转存中…';
  $('#scanLog').textContent = '';
  clearInterval(seriesJobTimer);
  fetch('/api/season/fetch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tmdb_id: series.tmdb_id, season: series.season,
                           media: series.media || 'tv', netdisk: curNd })
  }).then(r => r.json()).then(d => {
    if (d.error) throw new Error(d.error);
    let shown = 0;
    seriesJobTimer = setInterval(async () => {
      const st = await (await fetch('/api/auto/status?job=' + encodeURIComponent(d.job))).json();
      for (const s of (st.steps || []).slice(shown)) {
        $('#scanLog').appendChild(el('div', null, '· ' + s.message));
        $('#scanLog').scrollTop = $('#scanLog').scrollHeight;
      }
      shown = (st.steps || []).length;
      if (!st.done) return;
      clearInterval(seriesJobTimer);
      btn.disabled = false;
      if (st.error) {
        reportError(st.error, curNd);
        btn.textContent = '重试转存';
        $('#scanLog').appendChild(el('div', null, '✗ ' + st.error));
        return;
      }
      series.data = st.result;
      renderSeasons(st.result);
      renderEpisodes(st.result);
      const b = st.result.batch || {};
      const bits = [];
      if (b.saved && b.saved.length) bits.push('补上 ' + b.saved.length + ' 集');
      if (b.skipped && b.skipped.length) bits.push(b.skipped.length + ' 集本来就有');
      if (b.failed && b.failed.length) bits.push(b.failed.length + ' 集失败');
      $('#scanLog').appendChild(el('div', null, '✓ ' + (bits.join('，') || '没有变化')));
      for (const e of (b.errors || [])) $('#scanLog').appendChild(el('div', null, '· ' + e));
      // 批量转存里单个来源失败不会让整个任务 error，凭据过期就藏在这些
      // 逐条错误里——不往上报的话，用户只看到「10 集失败」不知道该干嘛
      for (const e of (b.errors || [])) {
        if (reportError(e, curNd)) break;
      }
      $('#scanLog').scrollTop = $('#scanLog').scrollHeight;
    }, 900);
  }).catch(e => {
    btn.disabled = false;
    btn.textContent = '一键转存 ' + n + ' 集';
    $('#scanLog').textContent = '启动失败：' + e.message;
  });
}

// 「找资源」按钮的文案只有这一个出处：之前 renderEpisodes 和出错分支各写各的，
// 电影页一报错按钮就变回「找缺失的集」了
function scanLabel() {
  return (series && series.media === 'movie') ? '找资源' : '找缺失的集';
}

function scanSources() {
  if (!series) return;
  const btn = $('#scanBtn');
  btn.disabled = true;
  btn.textContent = '搜索中…';
  $('#grabBtn').style.display = 'none';
  $('#scanLog').textContent = '';
  fetch('/api/series/scan', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tmdb_id: series.tmdb_id, season: series.season,
                           media: series.media || 'tv', netdisk: curNd })
  }).then(r => r.json()).then(d => {
    if (d.error) throw new Error(d.error);
    let shown = 0;
    seriesJobTimer = setInterval(async () => {
      const st = await (await fetch('/api/auto/status?job=' + encodeURIComponent(d.job))).json();
      for (const s of (st.steps || []).slice(shown)) {
        $('#scanLog').appendChild(el('div', null, '· ' + s.message));
        $('#scanLog').scrollTop = $('#scanLog').scrollHeight;
      }
      shown = (st.steps || []).length;
      if (!st.done) return;
      clearInterval(seriesJobTimer);
      btn.disabled = false;
      btn.textContent = '重新搜索';
      if (st.error) {
        reportError(st.error, curNd);
        $('#scanLog').appendChild(el('div', null, '✗ ' + st.error));
        return;
      }
      series.data = st.result;
      renderSeasons(st.result);
      renderEpisodes(st.result);
    }, 900);
  }).catch(e => {
    btn.disabled = false;
    btn.textContent = scanLabel();
    $('#scanLog').textContent = '启动失败：' + e.message;
  });
}

// ---------------- 扫码登录 ----------------
let loginTimer = null;

function openLogin() {
  $('#mask').classList.add('on');
  $('#qrWrap').innerHTML = '';
  $('#tvWarn').style.display = 'none';
  $('#baiduLogin').style.display = 'none';
  setLoginMsg('', '');
}

function closeLogin() {
  clearTimeout(loginTimer);
  loginTimer = null;
  $('#mask').classList.remove('on');
}

function setLoginMsg(text, cls) {
  const el = $('#loginMsg');
  el.className = cls || '';
  el.textContent = text;
}

// 百度没有可用的扫码通道：表单分两步——粘贴 cookie（分享/转存）、
// 授权码换 token（列目录/直链）。两步独立，填一步存一步。
async function showBaiduLogin() {
  clearTimeout(loginTimer);
  $('#tvWarn').style.display = 'none';
  $('#qrWrap').innerHTML = '';
  setLoginMsg('', '');
  const box = $('#baiduLogin');
  box.style.display = '';
  box.innerHTML = '';
  let has = false;
  try {
    const d = await (await fetch('/api/login/baidu')).json();
    has = !!d.has_cookie;
  } catch (e) {}
  box.appendChild(el('div', 'dim',
    '扫码失败时的备用通道。浏览器登录 pan.baidu.com → F12 → Network → '
    + '任一请求的 Cookie 头整段复制（要含 BDUSS 和 STOKEN）'
    + (has ? '　✓ 已保存过' : '')));
  const input = document.createElement('textarea');
  input.rows = 3;
  input.style.width = '100%';
  input.style.margin = '6px 0';
  input.placeholder = 'BDUSS=...; STOKEN=...';
  box.appendChild(input);
  const btn = el('button', null, '保存 cookie');
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      const r = await fetch('/api/login/baidu', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cookie: input.value.trim() })
      });
      const d = await r.json();
      if (d.error) throw new Error(d.error);
      setLoginMsg('✓ ' + d.message, 'ok');
      box.style.display = 'none';
      loadDir(document.getElementById('pathInput').value);
    } catch (e) {
      setLoginMsg(e.message, 'error');
    }
    btn.disabled = false;
  };
  box.appendChild(btn);
}

async function startLogin(kind) {
  clearTimeout(loginTimer);
  $('#tvWarn').style.display = kind === 'tv' ? '' : 'none';
  $('#baiduLogin').style.display = 'none';
  $('#qrWrap').innerHTML = '';
  setLoginMsg('正在获取二维码…', '');
  try {
    const r = await fetch('/api/login/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind })
    });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    $('#qrWrap').innerHTML = '<img alt="扫码登录二维码">';
    $('#qrWrap img').src = data.qr;
    setLoginMsg('请用' + (kind === 'baidu' ? '百度网盘' : '夸克')
                + ' APP 扫描（约 3 分钟内有效）', '');
    pollLogin(data.sid);
  } catch (e) {
    setLoginMsg('获取二维码失败：' + e.message, 'error');
  }
}

function pollLogin(sid) {
  loginTimer = setTimeout(async () => {
    try {
      const data = await (await fetch('/api/login/poll?sid=' + encodeURIComponent(sid))).json();
      if (data.error) throw new Error(data.error);
      if (data.state === 'waiting') {
        if (data.message) setLoginMsg(data.message, '');
        pollLogin(sid);
        return;
      }
      if (data.state === 'failed') { setLoginMsg('登录失败：' + data.message, 'error'); return; }
      setLoginMsg('✓ ' + data.message, 'ok');
      $('#qrWrap').innerHTML = '';
      if (data.kind === 'quark' || data.kind === 'baidu') {
        loadDir(document.getElementById('pathInput').value);
      }
    } catch (e) {
      setLoginMsg('轮询失败：' + e.message, 'error');
    }
  }, 2000);
}

// 落在哪一页：带了 path 参数是冲着网盘目录来的；
// 否则有没看完的就落到「最近观看」（接着看是最常见的意图），没有就去「追剧」。
(async function boot() {
  const wantPath = params.get('path');
  if (wantPath) { loadDir(wantPath); switchTab('mine'); return; }
  const before = showsSeq;
  let has = false;
  try {
    const d = await (await fetch('/api/watch/recent?limit=1')).json();
    has = !!(d.items && d.items.length);
  } catch (e) {}
  // 开屏这一步是异步的。这期间用户很可能已经直接开搜了——
  // 那就别再把他切走、也别用榜单盖掉他的搜索结果。
  if (showsSeq !== before) return;
  switchTab(has ? 'watch' : 'shows');
})();
</script>
</body>
</html>
"""
