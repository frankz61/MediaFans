from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .errors import ConfigError
from .utils import json_path_get

CONFIG_TEMPLATE = """\
# MediaFans 配置文件
# 路径查找顺序: --config 参数 > 环境变量 MEDIAFANS_CONFIG > ./config.yaml > ~/.mediafans/config.yaml

tmdb:
  # themoviedb.org 免费申请的 API Key（v3 key 或 v4 Bearer Token 均可）
  api_key: ""
  # 追剧榜和搜索优先华语内容。TMDB 的榜单是全球榜，被各国日播肥皂剧刷屏，
  # 华语基本挤不进去；关掉就是原样的全球榜。
  prefer_chinese: true

search:
  timeout: 30
  # 搜索源。PanSou 服务端本身就聚合了几十个 TG 频道和插件，所以「源更丰富」主要靠
  # 把服务端的开关用起来 + 配多个实例做故障转移，而不是在客户端堆站点。
  #
  # 强烈建议自部署（公共实例实测同一关键词三次分别返回 13 / 74 / 0 条，很不稳）：
  #   docker run -d --name pansou -p 8888:8888 \
  #     -e CHANNELS="tgsearchers3,yunpanx,yunpanpan,yunpanall,tianyifc,shareAliyun" \
  #     -e ENABLED_PLUGINS="pansearch,hunhepan,qupansou,panwiki,panta,jikepan" \
  #     ghcr.io/fish2018/pansou
  # 然后把 base_url 换成 http://127.0.0.1:8888
  sources:
    - type: pansou
      base_url: https://so.252035.xyz    # 公共实例，不稳
      fallback_urls:                     # 前一个报错或没结果就换下一个
        - https://pansou.app
      # 可选开关（留空即用服务端默认）：
      # cloud_types: quark       # 只要指定网盘，服务端过滤；留空则按搜索时选的网盘自动传
      # src: tg                  # all | tg | plugin，实测 tg 的夸克命中率最高
      # channels: [tgsearchers3, yunpanx]   # 指定 TG 频道
      # plugins: [pansearch, hunhepan]      # 指定插件
      # refresh: true            # 跳过服务端缓存，慢但更新
    # yiso.work 的接口已失效（返回 HTML 不是 JSON），保留类型仅作参考
    # - type: yiso
    #   base_url: https://yiso.work

drive:
  quark:
    # 凭据优先级: token_provider > mediafans login 扫码缓存(quark.cookie) > 下面的静态 cookie
    # 推荐: 直接运行 mediafans login 扫码登录，无需手动填 cookie
    #
    # 方式一：直接粘贴完整 cookie（浏览器登录 pan.quark.cn 后 F12 -> Network -> 任一请求的 Cookie 头，
    # 关键字段是 __puus）
    cookie: ""
    # 方式二：从你的 token 中转站实时拉取（优先级高于 cookie）
    # token_provider:
    #   url: https://your-relay.example.com/api/quark/cookie
    #   method: GET                # 默认 GET
    #   headers:                   # 中转站要求的鉴权头
    #     Authorization: "Bearer xxx"
    #   json_path: data.cookie     # 响应 JSON 中 cookie 字段的路径；留空则按纯文本处理
    # 转存目标目录（不存在会自动创建）
    save_dir: "/MediaFans"

  baidu:
    # 百度网盘（可选，建议 SVIP 账号：非会员直链限速看不动视频）。凭据分两套：
    #
    # ① cookie —— 打开分享 / 转存用（低频）。浏览器登录 pan.baidu.com 后 F12 ->
    #    Network -> 任一请求的 Cookie 头，必须同时包含 BDUSS 和 STOKEN（转存要 STOKEN）。
    #    推荐 mediafans login --netdisk baidu 粘贴保存，也可以配 token_provider 中转站。
    cookie: ""
    # ② OAuth —— 列目录 / 取直链用（官方稳定通道）。到 pan.baidu.com/union 免费注册
    #    个人应用拿 AppKey/SecretKey 填到下面，然后运行 mediafans login --netdisk baidu
    #    完成一次授权。refresh_token 只需首次填写，之后自动轮换保存在 baidu.json。
    app_key: ""
    secret_key: ""
    refresh_token: ""
    # 转存目标目录（不存在会自动创建）
    save_dir: "/MediaFans"

ai:
  # 可选。配了就用 Claude 复核「哪个资源才是你要的剧」，处理别名、合集混装、
  # 季号写法混乱这类规则搞不定的情况。不配也能用——资源验证和排序都是确定性的，
  # AI 只是在已验证的候选里多做一次裁决。
  # 需要先装依赖: pip install "mediafans[ai]"
  enabled: false
  api_key: ""          # 留空则读环境变量 ANTHROPIC_API_KEY
  model: claude-opus-5
  # 指向自建网关时填这里（要求支持 Anthropic 的 /v1/messages 格式）。
  # 注意：很多网关只是「长得像 Anthropic」——实测有的会丢掉 system 参数、
  # 也不理会 output_config 的结构化输出。代码已按这种情况写了兼容和容错解析。
  base_url: ""

player:
  # 直链请求 UA；留空则自动使用夸克 PC 端 UA（实测直链主要校验 Cookie）
  ua: ""
  # 播放命令。留空自动探测 mpv -> PotPlayer -> VLC -> 系统默认。
  # mpv/自定义命令直连直链；VLC/PotPlayer/系统播放器会自动走本地流式代理（补 Cookie）。
  # 支持字符串或数组形式，可用占位符 {url} {ua} {cookie} {title}
  # 例（数组，推荐）:
  # command: ["mpv", "--force-media-title={title}", "{url}"]
  command: ""
"""


class Config:
    """薄封装：加载 YAML 并提供点路径访问."""

    def __init__(self, path: Optional[Path], raw: Dict[str, Any]):
        self.path = path
        self.raw = raw or {}

    def get(self, dotted: str, default: Any = None) -> Any:
        v = json_path_get(self.raw, dotted)
        return default if v is None else v

    def require(self, dotted: str, what: str) -> Any:
        v = self.get(dotted)
        if v in (None, "", [], {}):
            raise ConfigError(f"缺少配置项 {dotted}（{what}）。配置文件: {self.path}")
        return v


def _candidate_paths() -> list:
    cands = []
    env = os.environ.get("MEDIAFANS_CONFIG")
    if env:
        cands.append(Path(env))
    cands.append(Path.cwd() / "config.yaml")
    cands.append(Path.home() / ".mediafans" / "config.yaml")
    return cands


def discover_config_path(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise ConfigError(f"配置文件不存在: {p}")
        return p
    for p in _candidate_paths():
        if p.exists():
            return p
    return None


def cookie_file_for(cfg: Config, netdisk: str = "quark") -> Path:
    """登录凭据 cookie 缓存文件位置（与配置文件同目录，便于一起管理）."""
    base = cfg.path.parent if cfg.path else (Path.home() / ".mediafans")
    return base / f"{netdisk}.cookie"


def tv_token_file_for(cfg: Config) -> Path:
    """TV 版扫码登录的 token 缓存（含 device_id，refresh 时要用同一个）."""
    base = cfg.path.parent if cfg.path else (Path.home() / ".mediafans")
    return base / "quark_tv.json"


def baidu_token_file_for(cfg: Config) -> Path:
    """百度 OAuth token 缓存（refresh_token 每次刷新都会轮换，必须落盘）."""
    base = cfg.path.parent if cfg.path else (Path.home() / ".mediafans")
    return base / "baidu.json"


def watch_file_for(cfg: Config) -> Path:
    """播放进度（与配置文件同目录，跟凭据一起管理）."""
    base = cfg.path.parent if cfg.path else (Path.home() / ".mediafans")
    return base / "watch.json"


def load_config(explicit: Optional[str] = None) -> Config:
    path = discover_config_path(explicit)
    if path is None:
        return Config(None, {})
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件 YAML 解析失败: {path}\n{e}")
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是映射: {path}")
    return Config(path, raw)


def init_config(explicit: Optional[str] = None, force: bool = False) -> Path:
    """写入配置模板，返回路径."""
    if explicit:
        path = Path(explicit)
    else:
        path = Path.home() / ".mediafans" / "config.yaml"
    if path.exists() and not force:
        raise ConfigError(f"配置文件已存在（--force 覆盖）: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return path
