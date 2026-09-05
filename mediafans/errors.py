from __future__ import annotations


class MediaFansError(Exception):
    """项目内统一异常基类."""


class ConfigError(MediaFansError):
    """配置缺失或非法."""


class DriveError(MediaFansError):
    """网盘接口调用失败."""


class SearchError(MediaFansError):
    """资源搜索源调用失败."""


class PlayError(MediaFansError):
    """直链获取或播放器启动失败."""
