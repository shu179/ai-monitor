"""
Surfaced 应用版本与品牌元数据。

后续如果需要做打包升级、版本比对或升级提示，优先只改这里。
"""

from __future__ import annotations

APP_NAME = "Surfaced"
APP_SLUG = "surfaced"
APP_VERSION = "2026.04.12"
APP_CHANNEL = "stable"


def get_version_label() -> str:
    """返回适合展示给用户的版本标签。"""
    suffix = "" if APP_CHANNEL == "stable" else f"-{APP_CHANNEL}"
    return f"v{APP_VERSION}{suffix}"


def get_version_title() -> str:
    """返回带版本号的完整标题。"""
    return f"{APP_NAME} {get_version_label()}"


def get_http_server_version(product: str = "Web") -> str:
    """返回 HTTP Server 头使用的服务端版本字符串。"""
    return f"{APP_NAME}{product}/{APP_VERSION}"


def get_version_payload() -> dict[str, str]:
    """返回统一的版本信息结构，方便后端直接透传给前端。"""
    return {
        "appName": APP_NAME,
        "slug": APP_SLUG,
        "version": APP_VERSION,
        "channel": APP_CHANNEL,
        "label": get_version_label(),
        "full": get_version_title(),
    }
