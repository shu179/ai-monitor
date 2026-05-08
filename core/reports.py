"""
品牌提及率周报 / 月报
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

from .app_paths import resolve_app_path
from .diagnostics import get_events
from .history import (
    get_all_task_names,
    get_current_task_names,
    get_records,
    get_task_brand_names,
    is_date_in_optimization_period,
    is_manual_test_failure_record,
    is_success_record,
)
from .time_utils import local_now, local_today


REPORT_DIR = resolve_app_path("reports")


def generate_period_report(config: dict, period: str = "weekly") -> dict:
    days = 7 if period == "weekly" else 30
    period_label = "周报" if period == "weekly" else "月报"
    today = local_today()
    since = today - timedelta(days=days - 1)

    task_stats = []
    overall_total = 0
    overall_success = 0
    platform_counts = defaultdict(lambda: {"total": 0, "success": 0})
    pending_reviews = 0

    task_names = get_current_task_names(config) or get_all_task_names()
    task_lookup: dict[str, dict] = {}
    for task in (config or {}).get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("name") or "").strip()
        if not task_name:
            task_name = next(iter(get_task_brand_names(task)), "")
        if task_name and task_name not in task_lookup:
            task_lookup[task_name] = task

    for task_name in task_names:
        task = task_lookup.get(task_name, {})
        records = [
            record for record in get_records(task_name, task_id=str(task.get("task_id") or "").strip())
            if _record_date(record) >= since and not is_manual_test_failure_record(record)
        ]
        current_brands = get_task_brand_names(task)
        if current_brands:
            records = [record for record in records if str(record.get("brand", "")).strip() in current_brands]
        # 过滤非优化期记录
        records = [record for record in records if is_date_in_optimization_period(task_name, _record_date(record))]
        if not records:
            continue

        total = len(records)
        success = sum(1 for record in records if is_success_record(record))
        pending_reviews += sum(
            1 for record in records
            if record.get("review_status") == "pending" and record.get("rank", 99) != 99
        )
        overall_total += total
        overall_success += success

        for record in records:
            plat = str(record.get("platform", "")).strip() or "unknown"
            platform_counts[plat]["total"] += 1
            if is_success_record(record):
                platform_counts[plat]["success"] += 1

        rate = round(success / total * 100, 1) if total else 0.0
        task_stats.append({
            "task_name": task_name,
            "total": total,
            "success": success,
            "rate": rate,
        })

    task_stats.sort(key=lambda item: (-item["rate"], -item["success"], item["task_name"]))
    diagnostics = [
        item for item in get_events(limit=200, include_resolved=False)
        if _event_date(item) >= since
    ]
    top_platforms = []
    for platform, counts in platform_counts.items():
        total = counts["total"]
        success = counts["success"]
        rate = round(success / total * 100, 1) if total else 0.0
        top_platforms.append({
            "platform": platform,
            "total": total,
            "success": success,
            "rate": rate,
        })
    top_platforms.sort(key=lambda item: (-item["rate"], -item["success"], item["platform"]))

    overall_rate = round(overall_success / overall_total * 100, 1) if overall_total else 0.0
    lines = [
        f"AI 品牌监控{period_label}",
        "",
        f"统计周期: {since.isoformat()} 至 {today.isoformat()}",
        f"总检测次数: {overall_total}",
        f"命中次数: {overall_success}",
        f"整体提及率: {overall_rate}%",
        f"待复核提及: {pending_reviews}",
        f"未处理诊断: {len(diagnostics)}",
        "",
        "任务表现 Top 5:",
    ]
    if task_stats:
        for item in task_stats[:5]:
            lines.append(
                f"- {item['task_name']}: {item['success']}/{item['total']}，提及率 {item['rate']}%"
            )
    else:
        lines.append("- 本周期暂无历史数据")

    lines.append("")
    lines.append("平台表现:")
    if top_platforms:
        for item in top_platforms[:5]:
            lines.append(
                f"- {item['platform']}: {item['success']}/{item['total']}，提及率 {item['rate']}%"
            )
    else:
        lines.append("- 本周期暂无平台数据")

    if diagnostics:
        lines.append("")
        lines.append("需要关注:")
        for item in diagnostics[:5]:
            message = str(item.get("message", "")).strip()
            lines.append(f"- {item.get('category', 'general')}: {message[:70]}")

    text = "\n".join(lines)
    image_path = render_report_card(
        title=f"品牌提及率{period_label}",
        lines=lines,
        output_path=str(REPORT_DIR / f"{period}_{today.isoformat()}.jpg"),
    )
    text_path = REPORT_DIR / f"{period}_{today.isoformat()}.txt"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")

    return {
        "period": period,
        "title": f"品牌提及率{period_label}",
        "text": text,
        "image_path": image_path,
        "text_path": str(text_path),
        "overall_total": overall_total,
        "overall_success": overall_success,
        "overall_rate": overall_rate,
        "pending_reviews": pending_reviews,
        "diagnostics_count": len(diagnostics),
        "task_stats": task_stats,
        "platform_stats": top_platforms,
    }


def render_report_card(title: str, lines: list[str], output_path: str) -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    width = 1200
    padding_x = 40
    padding_y = 28
    line_gap = 14
    title_font = _font(34, bold=True)
    body_font = _font(22)
    meta_font = _font(18)

    temp_img = Image.new("RGB", (width, 800), "#0F172A")
    draw = ImageDraw.Draw(temp_img)
    y = padding_y
    y += _text_height(draw, title, title_font) + 10
    rendered_at = local_now().strftime("%Y-%m-%d %H:%M:%S")
    y += _text_height(draw, rendered_at, meta_font) + 18
    for line in lines[2:]:
        y += _text_height(draw, line, body_font) + line_gap
    height = max(420, y + padding_y)

    img = Image.new("RGB", (width, height), "#0F172A")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((18, 18, width - 18, height - 18), radius=26, fill="#111827", outline="#334155", width=2)
    draw.text((padding_x, padding_y), title, fill="#E5EEF8", font=title_font)
    draw.text((padding_x, padding_y + 54), rendered_at, fill="#94A3B8", font=meta_font)

    cursor_y = padding_y + 96
    for idx, line in enumerate(lines[2:], start=2):
        fill = "#E5EEF8" if idx < 8 else "#CBD5E1"
        if line.endswith(":"):
            fill = "#F59E0B"
        draw.text((padding_x, cursor_y), line, fill=fill, font=body_font)
        cursor_y += _text_height(draw, line, body_font) + line_gap

    img.save(output_path, format="JPEG", quality=88, optimize=True)
    return output_path


def _record_date(record: dict) -> date:
    try:
        return datetime.strptime(str(record.get("ts", "")), "%Y-%m-%d %H:%M").date()
    except Exception:
        return date.min


def _event_date(event: dict) -> date:
    try:
        return datetime.strptime(str(event.get("ts", "")), "%Y-%m-%d %H:%M:%S").date()
    except Exception:
        return date.min


def _text_height(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[3] - box[1]


def _font(size: int, *, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/PingFang.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()
