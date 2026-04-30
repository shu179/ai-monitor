"""
UI界面模块
"""

from .status_palette import (
    DANGER_RED,
    SUCCESS_GREEN,
    WARNING_YELLOW,
    get_percentage_color,
    get_percentage_level,
    get_percentage_palette,
    get_percentage_style,
)
from .home_copy import (
    build_home_copy_context,
    get_home_messages,
    pick_home_message,
)

__all__ = [
    "SUCCESS_GREEN",
    "WARNING_YELLOW",
    "DANGER_RED",
    "get_percentage_palette",
    "get_percentage_color",
    "get_percentage_level",
    "get_percentage_style",
    "build_home_copy_context",
    "get_home_messages",
    "pick_home_message",
]
