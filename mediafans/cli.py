from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import Config, cookie_file_for, init_config, load_config
from .drive import create_drive
from .errors import MediaFansError
from .models import DriveFile, ShareLink
from .player import detect_player, launch as launch_player
from .search import aggregate_search, build_providers
from .utils import classify_netdisk, fmt_size, norm_path, parse_quark_share, truncate

app = typer.Typer(
    help="MediaFans — 网盘搜索 / 转存 / 直链播放一体化 CLI",
    no_args_is_help=True,
    add_completion=False,
)
err_console = Console(stderr=True)
console = Console()

# 由 callback 写入，命令里通过 _cfg() 读取
_CONFIG_ARG: Optional[str] = None


def _cfg() -> Config:
    return load_config(_CONFIG_ARG)


def _quark(cfg: Config):
    section = dict(cfg.get("drive.quark") or {})
    section.setdefault("cookie_file", str(cookie_file_for(cfg)))
    return create_drive("quark", section)


def _fail(e: Exception) -> None:
    # 错误里常带接口路径（如 [/user]），不转义会被 rich 当成标记语言解析而炸掉
    from rich.markup import escape

    err_console.print(f"[red]✗ {escape(str(e))}[/red]")
    raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(
        None, "--config", envvar="MEDIAFANS_CONFIG", help="配置文件路径"
    ),
    version: bool = typer.Option(False, "--version", help="显示版本"),
):
    global _CONFIG_ARG
    _CONFIG_ARG = config
    if version:
        console.print(f"mediafans {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


# ============================================================ config
@app.command("config")
def config_cmd(
    action: str = typer.Argument("path", help="path | init"),
    force: bool = typer.Option(False, "--force", help="init 时覆盖已有文件"),
    target: Optional[str] = typer.Option(None, "--target", help="init 时指定路径"),
):
    """查看/生成配置文件."""
    try:
        if action == "path":
            cfg = _cfg()
            console.print(cfg.path or "[yellow]未找到（可用 config init 生成）[/yellow]")
        elif action == "init":
            p = init_config(target or _CONFIG_ARG, force=force)
            console.print(f"[green]✓[/green] 配置模板已写入: {p}")
            console.print("  接下来填写 tmdb.api_key / 搜索源 / 夸克 cookie（或 token 中转站）")
        else:
            raise MediaFansError(f"未知子命令: {action}（可选: path | init）")
    except MediaFansError as e:
        _fail(e)


# ============================================================ login
@app.command()
def login(
    netdisk: str = typer.Option("quark", "--netdisk", help="当前支持 quark"),
    timeout: float = typer.Option(180, "--timeout", help="等待扫码的秒数"),
    tv: bool = typer.Option(False, "--tv", help="改用夸克 TV 版扫码，存 token 而不是 cookie"),
):
    """扫码登录网盘账号，cookie 自动保存到本地（推荐）."""
    from .auth import QuarkQRLogin, render_qr_ascii

    if netdisk.lower() != "quark":
        _fail(MediaFansError(f"暂不支持 {netdisk} 扫码登录（当前: quark）"))
    if tv:
        _login_tv(timeout)
        return
    try:
        console.print("[bold]夸克扫码登录[/bold] （用夸克 APP 扫描终端里的二维码）")
        flow = QuarkQRLogin()
        cookie = flow.login(
            qr_renderer=render_qr_ascii,
            on_event=lambda m: console.print(f"[dim]{m}[/dim]"),
            poll_interval=2,
            timeout=timeout,
        )
        cfg = _cfg()
        cookie_path = cookie_file_for(cfg)
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(cookie, encoding="utf-8")
        try:
            nickname = _quark(cfg).account_name()
        except MediaFansError as e:
            nickname = ""  # cookie 已保存，昵称获取失败不阻断
            console.print(f"[yellow]! cookie 已保存，但账号校验未通过: {truncate(str(e), 100)}[/yellow]")
    except MediaFansError as e:
        _fail(e)
    console.print(f"[green]✓ 登录成功[/green] 账号: {nickname or '（未获取到昵称）'}")
    console.print(f"cookie 已保存: {cookie_path}")
    console.print("[dim]下次直接使用即可；失效后重新 mediafans login 覆盖[/dim]")


def _login_tv(timeout: float) -> None:
    """TV 版扫码：二维码是 PNG 而不是 URL，终端里存成文件让用户打开扫。"""
    import base64
    import json as _json

    from .auth import QuarkTVLogin
    from .config import tv_token_file_for

    cfg = _cfg()
    token_path = tv_token_file_for(cfg)
    device_id = ""
    if token_path.exists():  # 复用旧 device_id，换绑设备会让 refresh 失效
        try:
            device_id = str(_json.loads(token_path.read_text(encoding="utf-8")).get("device_id") or "")
        except (OSError, ValueError):
            device_id = ""

    console.print("[bold]夸克 TV 版扫码登录[/bold]")
    console.print("[yellow]! code 换 token 这一步会经过第三方中转 api.extscreen.com（已强制 https）；"
                  "换回来的 refresh_token 等于长期访问权，介意就别用这条路[/yellow]")
    png_path = token_path.with_name("quark_tv_qrcode.png")

    def _show(data_uri: str) -> None:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(base64.b64decode(data_uri.split(",", 1)[1]))
        console.print(f"二维码已保存: [cyan]{png_path}[/cyan] —— 打开它用夸克 APP 扫")

    try:
        flow = QuarkTVLogin(device_id=device_id)
        token = flow.login(on_qr=_show,
                           on_event=lambda m: console.print(f"[dim]{m}[/dim]"),
                           poll_interval=2, timeout=timeout)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(_json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
    except MediaFansError as e:
        _fail(e)
    finally:
        if png_path.exists():
            png_path.unlink(missing_ok=True)
    console.print(f"[green]✓ TV 登录成功[/green] token 已保存: {token_path}")
    console.print("[dim]验证 TV 直链是否免 Cookie: mediafans tv-check[/dim]")


# ============================================================ tv-check
@app.command("tv-check")
def tv_check(
    target: str = typer.Argument(..., help="网盘里的文件路径或关键词"),
):
    """验证 TV 版直链到底要不要凭据 —— 这是 TV 登录唯一真正的价值所在。

    PC 版直链必须带 Cookie（实测），所以浏览器只能走本地转发。
    如果 TV 版直链裸请求就能 206，那 302 直连和 .strm 直连播放就都成立了。
    """
    import httpx

    from .config import tv_token_file_for
    from .drive.quark_tv import QUARK_TV_UA, QuarkTVClient

    try:
        cfg = _cfg()
        tv = QuarkTVClient(tv_token_file_for(cfg))
        try:
            console.print(f"[dim]TV 账号: {tv.account_name() or '（未获取到昵称）'}[/dim]")
        except MediaFansError as e:
            # 昵称只是好看；取不到也要继续测直链，那才是这个命令的目的
            from rich.markup import escape as _esc

            console.print(f"[yellow]! 账号信息读取失败（不影响取直链）: "
                          f"{_esc(truncate(str(e), 90))}[/yellow]")
        # fid 用 PC 版解析（同一个网盘，fid 通用），省得再摸 TV 版的列目录接口
        drive = _quark(cfg)
        path, fid = _resolve_target(drive, target)
        console.print(f"[dim]文件: {path}[/dim]")
        with console.status("向 TV 接口取直链…"):
            pt = tv.get_play_target(fid, name=path.rsplit("/", 1)[-1])
    except MediaFansError as e:
        _fail(e)

    table = Table(title="TV 版直链：裸请求能不能过", show_lines=False)
    table.add_column("档位", style="bold")
    table.add_column("CDN 主机")
    table.add_column("什么头都不带")
    table.add_column("只带 UA")
    ua = QUARK_TV_UA

    def _probe(url: str, headers: dict) -> str:
        try:
            r = httpx.get(url, headers=dict(headers, Range="bytes=0-99"),
                          timeout=25, follow_redirects=True)
            return f"[green]{r.status_code}[/green]" if r.status_code in (200, 206)                 else f"[red]{r.status_code}[/red]"
        except Exception as e:  # 网络问题也要看得见，不能吞
            return f"[red]{type(e).__name__}[/red]"

    naked_ok = True
    for v in pt.variants:
        host = httpx.URL(v.url).host
        bare = _probe(v.url, {})
        table.add_row(v.display(), host, bare, _probe(v.url, {"User-Agent": ua}))
        if "green" not in bare:
            naked_ok = False
    console.print(table)

    if naked_ok:
        console.print("[green]✓ TV 版直链免凭据[/green] —— 可以 302 直连，"
                      ".strm 也能直接写 CDN 地址，本地转发对 TV 这条路不再必需")
    else:
        console.print("[yellow]✗ TV 版直链同样需要凭据[/yellow] —— "
                      "和 PC 版一样只能走本地转发，302/.strm 直连仍然不成立")
    console.print("[dim]对照：PC 版直链裸请求一律 412（已实测）[/dim]")


# ============================================================ doctor
@app.command()
def doctor(offline: bool = typer.Option(False, "--offline", help="跳过网络检查")):
    """体检：配置、网盘账号、搜索源、播放器."""
    table = Table(title="MediaFans 体检", show_lines=False)
    table.add_column("检查项", style="bold")
    table.add_column("状态")
    table.add_column("说明", overflow="fold")

    def add(item, ok, msg, warn=False):
        mark = "[green]✓[/green]" if ok else ("[yellow]![/yellow]" if warn else "[red]✗[/red]")
        table.add_row(item, mark, msg)

    cfg = _cfg()
    add("配置文件", cfg.path is not None, str(cfg.path) if cfg.path else "未找到，运行 mediafans config init")

    # 网盘
    quark_cfg = dict(cfg.get("drive.quark") or {})
    quark_cfg.setdefault("cookie_file", str(cookie_file_for(cfg)))
    has_provider = bool(quark_cfg.get("token_provider"))
    has_login = bool(quark_cfg.get("cookie_file") and Path(quark_cfg["cookie_file"]).exists())
    has_cookie = bool(quark_cfg.get("cookie")) or has_provider or has_login
    if has_provider:
        cred_desc = "token_provider（中转站）"
    elif has_login:
        cred_desc = "扫码登录缓存（quark.cookie）"
    elif quark_cfg.get("cookie"):
        cred_desc = "静态 cookie"
    else:
        cred_desc = "未配置（mediafans login 扫码 / cookie / token_provider 三选一）"
    add("夸克凭据", has_cookie, cred_desc)
    if has_cookie and not offline:
        try:
            drive = create_drive("quark", quark_cfg)
            name = drive.account_name()
            add("夸克账号", bool(name), f"昵称: {name}" if name else "cookie 无效或已过期")
        except MediaFansError as e:
            add("夸克账号", False, truncate(str(e), 90))

    # TMDB
    tmdb_key = cfg.get("tmdb.api_key")
    add("TMDB", bool(tmdb_key), "已配置 api_key" if tmdb_key else "未配置 tmdb.api_key（discover 命令需要）")
    if tmdb_key and not offline:
        try:
            from .metadata.tmdb import TmdbClient

            TmdbClient(str(tmdb_key)).trending("movie")
            add("TMDB 连通", True, "接口正常")
        except Exception as e:
            add("TMDB 连通", False, truncate(str(e), 90))

    # 搜索源
    try:
        providers = build_providers(cfg.get("search") or {})
        add("搜索源", True, ", ".join(p.name for p in providers))
        if not offline:
            for p in providers:
                try:
                    ok = p.health()
                    add(f"  {p.name}", ok, "可用" if ok else "健康检查失败", warn=not ok)
                except Exception as e:
                    add(f"  {p.name}", False, truncate(str(e), 90))
    except MediaFansError as e:
        add("搜索源", False, str(e))

    # 播放器
    kind, exe = detect_player()
    add("播放器", kind is not None, f"{kind}: {exe}" if kind else "未找到 mpv/PotPlayer/VLC，play 命令将只打印直链", warn=kind is None)

    console.print(table)


# ============================================================ discover
@app.command()
def discover(
    media_type: str = typer.Option("all", "--type", help="all | movie | tv"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="按名称搜索（不走热门榜）"),
    limit: int = typer.Option(15, "--limit", "-n"),
):
    """最近的影视热门（TMDB），或按名称精确搜索."""
    from .metadata.tmdb import TmdbClient

    try:
        cfg = _cfg()
        client = TmdbClient(str(cfg.require("tmdb.api_key", "discover 命令需要")))
        items = client.search(query) if query else client.trending(media_type)
    except MediaFansError as e:
        _fail(e)
    items = items[:limit]
    if not items:
        console.print("[yellow]没有结果[/yellow]")
        return
    t = Table(title=f"{'搜索' if query else '热门'}结果")
    t.add_column("类型")
    t.add_column("名称", style="bold")
    t.add_column("年份")
    t.add_column("评分")
    t.add_column("简介", overflow="fold")
    for i in items:
        t.add_row("剧" if i.media_type == "tv" else "影", i.title, i.year or "-",
                  f"{i.rating}", truncate(i.overview, 60))
    console.print(t)
    console.print("[dim]下一步: mediafans search \"名称\" --netdisk quark[/dim]")


# ============================================================ search
@app.command("search")
def search_cmd(
    keyword: str = typer.Argument(..., help="资源关键词，如 '沙丘2 4K'"),
    netdisk: Optional[str] = typer.Option(None, "--netdisk", "-d", help="只看指定网盘，如 quark"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """聚合搜索网盘分享链接."""
    try:
        cfg = _cfg()
        providers = build_providers(cfg.get("search") or {})
        links, errors = aggregate_search(providers, keyword, netdisk=netdisk)
    except MediaFansError as e:
        _fail(e)
    for name, e in errors:
        console.print(f"[yellow]! 搜索源 {name} 失败: {truncate(str(e), 100)}[/yellow]")
    links = links[:limit]
    if not links:
        console.print("[yellow]没有搜到结果，换个关键词或增加搜索源试试[/yellow]")
        return
    t = Table(title=f"搜索结果: {keyword}")
    t.add_column("#", justify="right")
    t.add_column("网盘")
    t.add_column("标题/备注", overflow="fold")
    t.add_column("提取码")
    t.add_column("时间")
    for idx, s in enumerate(links, 1):
        t.add_row(str(idx), s.netdisk, truncate(s.display_title(), 70), s.passcode or "-", s.datetime or "-")
    console.print(t)
    console.print("[dim]下一步: mediafans share <链接> 查看文件，或 mediafans save <链接> 直接转存[/dim]")
    for s in links[:3]:
        code = f" --code {s.passcode}" if s.passcode else ""
        console.print(f"[dim]  mediafans save {s.url}{code}[/dim]")


# ============================================================ share
@app.command()
def share(
    url: str = typer.Argument(..., help="夸克分享链接"),
    code: str = typer.Option("", "--code", help="提取码（链接里带 pwd= 可省略）"),
):
    """查看分享链接里的文件列表."""
    try:
        drive = _quark(_cfg())
        ctx = drive.open_share(url, passcode=code)
        files = drive.list_share_files(ctx)
    except MediaFansError as e:
        _fail(e)
    _print_share_files(url, files)


def _print_share_files(url: str, files: List[DriveFile]) -> None:
    if not files:
        console.print("[yellow]分享里没有文件[/yellow]")
        return
    t = Table(title=f"分享文件 ({len(files)} 个)")
    t.add_column("类型")
    t.add_column("名称", style="bold", overflow="fold")
    t.add_column("大小", justify="right")
    for f in files:
        t.add_row("目录" if f.is_dir else "文件", f.name, fmt_size(f.size))
    console.print(t)


# ============================================================ save
@app.command()
def save(
    url: str = typer.Argument(..., help="夸克分享链接"),
    code: str = typer.Option("", "--code", help="提取码"),
    name: str = typer.Option("", "--name", help="只转存文件名包含该关键词的文件（多关键词空格分隔）"),
    to: str = typer.Option("", "--to", help="转存目标目录，默认用配置里的 save_dir"),
):
    """把分享链接里的文件转存到自己的网盘."""
    from .utils import match_tokens

    try:
        cfg = _cfg()
        drive = _quark(cfg)
        target_dir = norm_path(to or drive.save_dir)
        console.print(f"[dim]打开分享…[/dim]")
        ctx = drive.open_share(url, passcode=code)
        files = drive.list_share_files(ctx)
        if name:
            files = [f for f in files if match_tokens(name, f.name)]
        if not files:
            console.print("[yellow]分享里没有匹配的文件[/yellow]")
            _print_share_files(url, drive.list_share_files(ctx))
            return
        _print_share_files(url, files)
        console.print(f"[dim]转存到 {target_dir} …[/dim]")
        with console.status("转存中…"):
            fids = drive.save_share_files(ctx, files, target_dir)
    except MediaFansError as e:
        _fail(e)
    saved_count = len(fids)
    if saved_count:
        console.print(f"[green]✓ 转存完成[/green] {saved_count} 个顶级条目 -> {target_dir}")
    else:
        console.print(f"[yellow]转存任务完成，但未返回新文件（可能此前已转存过）[/yellow] -> {target_dir}")
    console.print(f"[dim]下一步: mediafans ls {target_dir}[/dim]")
    console.print(f"[dim]      或 mediafans play <文件名关键词>[/dim]")


# ============================================================ ls
@app.command("ls")
def ls_cmd(
    path: str = typer.Argument("/", help="网盘内路径，如 /MediaFans"),
):
    """列出自己网盘目录."""
    try:
        drive = _quark(_cfg())
        p = norm_path(path)
        fid = drive.resolve_path(p)
        if not fid:
            raise MediaFansError(f"路径不存在: {p}")
        files = drive.list_files(fid)
    except MediaFansError as e:
        _fail(e)
    if not files:
        console.print(f"[yellow]{norm_path(path)} 下没有文件[/yellow]（若这是文件路径，用 mediafans url 取直链）")
        return
    t = Table(title=norm_path(path))
    t.add_column("类型")
    t.add_column("名称", style="bold", overflow="fold")
    t.add_column("大小", justify="right")
    t.add_column("更新时间")
    for f in files:
        t.add_row("目录" if f.is_dir else "文件", f.name, "-" if f.is_dir else fmt_size(f.size), f.updated_at or "-")
    console.print(t)


# ============================================================ find
@app.command()
def find(
    name: str = typer.Argument(..., help="关键词（空格分隔，全部命中才匹配）"),
    root: str = typer.Option("/", "--root", help="搜索起始目录"),
    depth: int = typer.Option(3, "--depth"),
):
    """在网盘里按文件名找文件."""
    try:
        drive = _quark(_cfg())
        root_path = norm_path(root or drive.save_dir)
        with console.status("搜索中…"):
            hits = drive.find(name, root=root_path, depth=depth)
    except MediaFansError as e:
        _fail(e)
    if not hits:
        console.print(f"[yellow]没有找到匹配 '{name}' 的文件[/yellow]")
        return
    t = Table(title=f"找到 {len(hits)} 项")
    t.add_column("类型")
    t.add_column("路径", style="bold", overflow="fold")
    t.add_column("大小", justify="right")
    for h in hits:
        t.add_row("目录" if h.file.is_dir else "文件", h.path, "-" if h.file.is_dir else fmt_size(h.file.size))
    console.print(t)
    console.print("[dim]下一步: mediafans play <路径或关键词>[/dim]")


# ============================================================ url / play
def _resolve_target(drive, target: str):
    """target 是绝对路径 -> 直接解析；否则在 save_dir 下模糊查找."""
    p = norm_path(target)
    if target.startswith("/") or target.startswith("\\"):
        fid = drive.resolve_entry(p)
        if not fid:
            raise MediaFansError(f"路径不存在: {p}")
        return p, fid
    hits = drive.find(target, root=drive.save_dir, depth=3)
    if not hits:
        hits = drive.find(target, root="/", depth=2)
    files = [h for h in hits if not h.file.is_dir] or hits
    if not files:
        raise MediaFansError(f"没找到匹配 '{target}' 的文件，先 mediafans ls / find 看看")
    if len(files) == 1:
        return files[0].path, files[0].file.fid
    t = Table(title=f"'{target}' 匹配到多个文件，请用完整路径")
    t.add_column("路径", overflow="fold")
    for h in files[:20]:
        t.add_row(h.path)
    console.print(t)
    raise typer.Exit(1)


@app.command()
def url(
    target: str = typer.Argument(..., help="网盘内路径或文件名关键词"),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON（含请求头，供脚本使用）"),
):
    """获取直链（不播放）。注意夸克直链有时效，随取随用。"""
    _play_impl(target, no_play=True, as_json=as_json, prefer="none")


@app.command()
def play(
    target: str = typer.Argument(..., help="网盘内路径或文件名关键词"),
    no_play: bool = typer.Option(False, "--no-play", help="只打印直链不启动播放器"),
    player: str = typer.Option("auto", "--player", help="auto | custom | mpv | potplayer | vlc | system | none"),
    as_json: bool = typer.Option(False, "--json"),
    proxy: Optional[bool] = typer.Option(
        None, "--proxy/--no-proxy",
        help="强制启用/禁用本地流式代理（默认：播放器带不了 Cookie 时自动启用）",
    ),
):
    """获取直链并启动播放器（自动带上 UA/Cookie 校验头）。"""
    _play_impl(target, no_play=no_play, as_json=as_json, prefer=player, proxy_opt=proxy)


def _play_impl(target: str, no_play: bool, as_json: bool, prefer: str,
               proxy_opt: Optional[bool] = None) -> None:
    import dataclasses

    from .player import detect_player
    from .proxy import StreamProxy, should_use_proxy

    stream_proxy = None
    proxy_url = ""
    try:
        cfg = _cfg()
        drive = _quark(cfg)
        path, fid = _resolve_target(drive, target)
        with console.status("获取直链…"):
            pt = drive.get_play_target(fid, name=path.rsplit("/", 1)[-1])
        if no_play or prefer == "none":
            kind, cmd, launched, proc = "none", [pt.url], False, None
        else:
            player_cfg = cfg.get("player") or {}
            # 预判播放器能力：带不了 Cookie 的播放器走本地流式代理
            kind_guess = "custom" if player_cfg.get("command") else (detect_player()[0] or "system")
            use_proxy = proxy_opt if proxy_opt is not None else should_use_proxy(pt, kind_guess)
            play_pt = pt
            if use_proxy:
                stream_proxy = StreamProxy(pt)
                proxy_url = stream_proxy.start()
                play_pt = dataclasses.replace(pt, url=proxy_url, cookie="", ua="")
            kind, cmd, launched, proc = launch_player(
                play_pt, player_cfg, title=pt.file_name, prefer=prefer
            )
    except MediaFansError as e:
        if stream_proxy:
            stream_proxy.stop()
        _fail(e)

    if as_json:
        body = {
            "file": pt.file_name, "url": pt.url, "download_url": pt.download_url,
            "headers": pt.headers(), "default": pt.default_key,
            "variants": [dataclasses.asdict(v) for v in pt.variants],
        }
        if proxy_url:
            body["proxy_url"] = proxy_url
        console.print_json(json.dumps(body, ensure_ascii=False, indent=2))
        if stream_proxy:
            _serve_and_stop(stream_proxy, proc)
        return

    console.print(Panel(pt.url, title=f"直链: {pt.file_name}", border_style="cyan"))
    if pt.cookie:
        console.print("[dim]直链需携带 Cookie 访问（--json 查看完整请求头）[/dim]")
    if no_play or prefer == "none":
        console.print('[dim]测试: mpv --http-header-fields="User-Agent: <ua>; Cookie: <cookie>" "<url>"[/dim]')
        return

    state = "[green]已启动[/green]" if launched else "[yellow]未找到播放器，仅打印直链[/yellow]"
    console.print(f"播放器 [{kind}] {state}")
    console.print(f"[dim]{' '.join(cmd)}[/dim]")
    if stream_proxy:
        if launched:
            console.print(f"[green]本地流式代理[/green] {proxy_url}（播放器连本地地址，代理自动补 Cookie）")
            _serve_and_stop(stream_proxy, proc)
        else:
            stream_proxy.stop()


def _serve_and_stop(stream_proxy, proc) -> None:
    """代理模式下保持进程存活：等播放器退出或用户 Ctrl+C，再关代理。"""
    import time

    console.print("[dim]关闭播放器或按 Ctrl+C 结束代理…[/dim]")
    try:
        if proc is not None:
            proc.wait()
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stream_proxy.stop()


def _picker(cfg):
    """按配置建 AI 裁决器。没开或建不起来都返回 None，链路照常跑。"""
    from .agent import LLMPicker

    ai = cfg.get("ai") or {}
    if not ai.get("enabled"):
        return None
    return LLMPicker(api_key=str(ai.get("api_key") or ""),
                     model=str(ai.get("model") or "claude-opus-5"),
                     base_url=str(ai.get("base_url") or ""))


@app.command()
def daily(
    kind: str = typer.Option("airing", "--kind", help="airing 今日播出 | onair 一周在播 | popular 热门"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """每日剧集榜（TMDB）。"""
    from .metadata.tmdb import TmdbClient

    try:
        cfg = _cfg()
        client = TmdbClient(str(cfg.require("tmdb.api_key", "daily 命令需要")))
        items = {"airing": client.airing_today, "onair": client.on_the_air,
                 "popular": client.popular_tv}.get(kind, client.airing_today)()
    except MediaFansError as e:
        _fail(e)
    t = Table(title={"airing": "今日播出", "onair": "一周在播"}.get(kind, "热门剧集"))
    t.add_column("剧名", style="bold")
    t.add_column("年份")
    t.add_column("评分")
    t.add_column("简介", overflow="fold")
    for i in items[:limit]:
        t.add_row(i.title, i.year or "-", f"{i.rating}", truncate(i.overview, 56))
    console.print(t)
    console.print('[dim]下一步: mediafans auto "剧名"[/dim]')


@app.command()
def auto(
    name: str = typer.Argument(..., help="剧名/片名"),
    season: Optional[int] = typer.Option(None, "--season", "-s", help="第几季"),
    no_save: bool = typer.Option(False, "--no-save", help="只找不转存"),
    to: str = typer.Option("", "--to", help="转存目标目录（默认配置里的 save_dir）"),
    probe: int = typer.Option(6, "--probe", help="实际打开验证前几个候选"),
):
    """一键找片：搜索 → 逐个打开验证 → 挑最合适的 → 转存。

    「避免空资源」靠的是真去打开每个候选看里面有没有可播放文件，
    不是靠猜。配了 ai.enabled 会再让 Claude 复核一次相关性。
    """
    from .agent import auto_fetch
    from .metadata.tmdb import TmdbClient

    try:
        cfg = _cfg()
        drive_factory = lambda: _quark(cfg)   # noqa: E731  每个探测线程要独立实例

        def _do_search(kw, netdisk=None):
            providers = build_providers(cfg.get("search") or {})
            return aggregate_search(providers, kw, netdisk=netdisk)

        # 有 TMDB 就拿总集数当完整度基准
        year, episodes = "", 0
        tmdb_key = str(cfg.get("tmdb.api_key") or "")
        if tmdb_key:
            try:
                hits = TmdbClient(tmdb_key).search(name, "tv")
                if hits:
                    detail = TmdbClient(tmdb_key).tv_detail(hits[0].tmdb_id)
                    from .agent import episode_yardstick

                    year = detail.get("year") or ""
                    episodes = episode_yardstick(detail, season)
                    console.print(f"[dim]TMDB: {detail['title']} ({year}) "
                                  f"共 {detail['total_seasons']} 季 "
                                  f"{detail['total_episodes']} 集[/dim]")
            except Exception:
                pass   # 元数据只是加分项，拿不到不影响主流程

        def on_step(stage, info):
            mark = {"error": "[red]✗", "done": "[green]✓", "ai": "[magenta]∴"}.get(stage, "[dim]·")
            from rich.markup import escape

            console.print(f"  {mark} {escape(info['message'])}[/]")

        res = auto_fetch(drive_factory, _do_search, name,
                         year=year, season=season, episodes=episodes,
                         save_dir=to, picker=_picker(cfg),
                         probe_top=probe, do_save=not no_save,
                         on_step=on_step)
    except MediaFansError as e:
        _fail(e)

    if not res.ok:
        _fail(MediaFansError(res.error or "没有找到合适的资源"))

    from rich.markup import escape

    t = Table(title="候选资源（已实际验证）", show_lines=False)
    t.add_column("分", justify="right")
    t.add_column("资源", style="bold", overflow="fold")
    t.add_column("内容")
    t.add_column("判断依据", overflow="fold")
    for sc in res.scored[:6]:
        pr = sc.probe
        content = (f"{pr.playable} 个 · {pr.size_h}" if pr.ok else f"[red]{pr.error}[/red]")
        mark = " [green]←选中[/green]" if sc is res.picked else ""
        t.add_row(f"{sc.score:.0f}", escape(truncate(sc.title, 44)) + mark,
                  content, escape(truncate("、".join(sc.reasons), 46)))
    console.print(t)
    if res.ai_used and res.verdict:
        console.print(f"[magenta]AI 裁决[/magenta]（{res.verdict.confidence}）: "
                      f"{escape(res.verdict.reason)}")
    elif res.ai_note:
        console.print(f"[dim]{escape(res.ai_note)}[/dim]")
    if res.saved:
        console.print(f"[green]✓ 已转存 {res.saved} 个文件[/green] → {res.saved_dir}")
        console.print(f'[dim]下一步: mediafans web  或  mediafans ls "{res.saved_dir}"[/dim]')


# ============================================================ web
@app.command()
def web(
    path: str = typer.Option("", "--path", help="初始目录（默认配置里的 save_dir）"),
    port: int = typer.Option(0, "--port", help="监听端口（默认随机）"),
    host: str = typer.Option("127.0.0.1", "--host",
                             help="监听地址。手机/电视要看就用 0.0.0.0（会自动加访问令牌）"),
    lan: bool = typer.Option(False, "--lan", help="等价于 --host 0.0.0.0 --port 8799"),
    token: str = typer.Option("", "--token", help="自定义访问令牌（默认随机生成，重启会变）"),
    no_tv: bool = typer.Option(False, "--no-tv",
                               help="不用 TV 直链，全部走本地转发（TV 直链有问题时用）"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
):
    """启动本地网页播放器：浏览器里浏览网盘、点文件即播。"""
    import secrets
    import time
    import webbrowser

    from .web import WebApp

    if lan:
        host = "0.0.0.0"
        port = port or 8799
    is_local = host in ("127.0.0.1", "localhost", "::1")
    if not is_local and not token:
        # 这个服务能浏览网盘、能转存、能用你的 cookie 取流，绝不能裸奔到局域网
        token = secrets.token_urlsafe(9)
    try:
        cfg = _cfg()

        def _do_search(kw, netdisk=None):
            providers = build_providers(cfg.get("search") or {})
            return aggregate_search(providers, kw, netdisk=netdisk)

        from .config import tv_token_file_for, watch_file_for

        app_ = WebApp(lambda: _quark(cfg), search_fn=_do_search, host=host, port=port,
                      cookie_path=cookie_file_for(cfg),
                      tv_token_path=tv_token_file_for(cfg), token=token,
                      use_tv=not no_tv,
                      tmdb_key=str(cfg.get("tmdb.api_key") or ""),
                      picker=_picker(cfg),
                      watch_path=watch_file_for(cfg),
                      prefer_chinese=bool(cfg.get("tmdb.prefer_chinese", True)))
        local_url = app_.page_url(path, host="127.0.0.1")
        app_.start()
    except (MediaFansError, OSError) as e:
        _fail(e if isinstance(e, MediaFansError) else
              MediaFansError(f"监听 {host}:{port} 失败: {e}"))
    if not no_browser:
        webbrowser.open(local_url)
    console.print("[green]✓ 网页播放器已启动[/green]")
    console.print(f"  本机   {local_url}")
    if not is_local:
        addrs = _lan_addresses()
        lan_url = app_.page_url(path, host=addrs[0])
        console.print(f"  局域网 [cyan]{lan_url}[/cyan]")
        console.print("[dim]  手机扫下面的二维码即可打开（令牌已带在链接里）[/dim]")
        from .auth import render_qr_ascii

        if not render_qr_ascii(lan_url):
            console.print("[dim]  （未能渲染二维码，手动复制上面的链接）[/dim]")
        if len(addrs) > 1:
            # 有 VPN/WSL/Hyper-V 时本机会有好几个地址，猜不准就都列出来
            others = "  ".join(addrs[1:])
            console.print(f"[dim]  连不上就换本机的其他网卡地址试试: {others}[/dim]")
        console.print("[yellow]! 已对局域网开放：任何拿到这个链接的人都能浏览你的网盘、"
                      "转存文件。令牌每次重启都会变，用 --token 可固定[/yellow]")
    console.print("[dim]Ctrl+C 停止服务[/dim]")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        app_.stop()
        console.print("[dim]已停止[/dim]")


def _ip_rank(ip: str) -> int:
    """给本机地址排个优先级：越像「手机能连上的那块网卡」越靠前。

    Windows 上一台机器常有六七个 IPv4：Wi-Fi、以太网、VPN 虚拟网卡、WSL、Hyper-V、
    Docker……而且 VPN 往往占着默认路由，所以「连一下外网看用哪块网卡」会选错。
    """
    import re

    last = ip.rsplit(".", 1)[-1]
    if ip.startswith(("169.254.",)):          # 链路本地，没配上 DHCP
        return 9
    if ip.startswith(("198.18.", "198.19.")):  # 基准测试段，Clash/Tailscale 之类爱用
        return 8
    if ip.startswith("100."):                  # 运营商级 NAT
        return 7
    private = (ip.startswith("192.168.") or ip.startswith("10.")
               or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip))
    if not private:
        return 6
    # 末位是 .1 的多半是虚拟网卡的网关地址（WSL/Hyper-V/VMware），不是本机在局域网里的地址
    base = 0 if ip.startswith("192.168.") else 2
    return base + (1 if last == "1" else 0)


def _lan_addresses() -> List[str]:
    """列出本机所有可能的局域网地址，按「最像真网卡」排序。"""
    import socket

    found = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:  # 默认出口网卡（有 VPN 时会指向 VPN，所以只当候选之一）
        s.connect(("223.5.5.5", 80))
        found.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    seen, out = set(), []
    for ip in found:
        if ip and not ip.startswith("127.") and ip not in seen:
            seen.add(ip)
            out.append(ip)
    out.sort(key=_ip_rank)
    return out or ["127.0.0.1"]


# ============================================================ entry
def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    app()


if __name__ == "__main__":
    main()
