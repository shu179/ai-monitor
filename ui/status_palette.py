"""
百分比结果配色接口

统一把结果映射成三档：
- 绿色：>= 80
- 黄色：60 ~ 79.9
- 红色：< 60

后续 UI 可以直接 import 这里的函数和常量。
"""

from dataclasses import dataclass
from typing import Optional


SUCCESS_GREEN = "#10B981"
WARNING_YELLOW = "#F59E0B"
DANGER_RED = "#EF4444"
NEUTRAL_GRAY = "#9CA3AF"

SUCCESS_BG = "#ECFDF5"
WARNING_BG = "#FFFBEB"
DANGER_BG = "#FEF2F2"
NEUTRAL_BG = "#F3F4F6"

SUCCESS_BORDER = "#A7F3D0"
WARNING_BORDER = "#FDE68A"
DANGER_BORDER = "#FECACA"
NEUTRAL_BORDER = "#D1D5DB"


@dataclass(frozen=True)
class PercentagePalette:
    level: str
    color: str
    bg: str
    border: str
    text: str
    min_value: Optional[float]
    max_value: Optional[float]


GREEN_PALETTE = PercentagePalette(
    level="green",
    color=SUCCESS_GREEN,
    bg=SUCCESS_BG,
    border=SUCCESS_BORDER,
    text=SUCCESS_GREEN,
    min_value=80.0,
    max_value=None,
)

YELLOW_PALETTE = PercentagePalette(
    level="yellow",
    color=WARNING_YELLOW,
    bg=WARNING_BG,
    border=WARNING_BORDER,
    text=WARNING_YELLOW,
    min_value=60.0,
    max_value=79.9999,
)

RED_PALETTE = PercentagePalette(
    level="red",
    color=DANGER_RED,
    bg=DANGER_BG,
    border=DANGER_BORDER,
    text=DANGER_RED,
    min_value=0.0,
    max_value=59.9999,
)

NEUTRAL_PALETTE = PercentagePalette(
    level="neutral",
    color=NEUTRAL_GRAY,
    bg=NEUTRAL_BG,
    border=NEUTRAL_BORDER,
    text=NEUTRAL_GRAY,
    min_value=None,
    max_value=None,
)


def get_percentage_palette(value: Optional[float], high_threshold: float = 80.0,
                           medium_threshold: float = 60.0) -> PercentagePalette:
    """根据百分比数值返回完整配色对象。"""
    if value is None:
        return NEUTRAL_PALETTE
    if value >= high_threshold:
        return GREEN_PALETTE
    if value >= medium_threshold:
        return YELLOW_PALETTE
    return RED_PALETTE


def get_percentage_color(value: Optional[float], high_threshold: float = 80.0,
                         medium_threshold: float = 60.0) -> str:
    """只取主色，适合图表线条、数字和状态点。"""
    return get_percentage_palette(value, high_threshold, medium_threshold).color


def get_percentage_level(value: Optional[float], high_threshold: float = 80.0,
                         medium_threshold: float = 60.0) -> str:
    """返回 green / yellow / red / neutral。"""
    return get_percentage_palette(value, high_threshold, medium_threshold).level


def get_percentage_style(value: Optional[float], high_threshold: float = 80.0,
                         medium_threshold: float = 60.0) -> dict:
    """返回适合 UI 直接消费的样式字典。"""
    palette = get_percentage_palette(value, high_threshold, medium_threshold)
    return {
        "level": palette.level,
        "color": palette.color,
        "bg": palette.bg,
        "border": palette.border,
        "text": palette.text,
        "min_value": palette.min_value,
        "max_value": palette.max_value,
    }
