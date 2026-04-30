"""Assistant analysis-context helpers for the local web backend."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from core.daily_task_state import derive_task_id
from core.history import get_all_task_names, get_records, is_success_record
from core.time_utils import local_now


_ASSISTANT_ANALYSIS_TRIGGER_PHRASES = (
    "查看数据",
    "深度聚合报告",
    "运营分析",
    "运行概况",
    "监测数据",
    "监测失败",
    "异常或空白",
    "文章趋势",
    "趋势分析",
    "聚合解析",
    "当前系统上下文",
)

_ASSISTANT_ANALYSIS_METRIC_KEYWORDS = (
    "失败率",
    "命中率",
    "稳定性",
    "趋势",
    "波动",
)

_ASSISTANT_ANALYSIS_SCOPE_KEYWORDS = (
    "系统",
    "监控",
    "监测",
    "任务",
    "文章",
    "平台",
    "品牌",
    "运行",
    "数据",
)

_ASSISTANT_ANALYSIS_ACTION_KEYWORDS = (
    "分析",
    "复盘",
    "报告",
    "概况",
    "总结",
    "诊断",
    "看看",
    "查看",
)

_ASSISTANT_CHAT_MODE_MARKERS = {
    "plain": "[SURFACED_CHAT_MODE:plain]",
    "greeting": "[SURFACED_CHAT_MODE:greeting]",
    "action": "[SURFACED_CHAT_MODE:action]",
    "analysis": "[SURFACED_CHAT_MODE:analysis]",
}


def _default_normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _default_platform_display_name(platform: str) -> str:
    labels = {
        "local_model": "本地模型",
        "doubao": "豆包",
        "deepseek": "DeepSeek",
        "kimi": "Kimi",
        "tongyi": "通义千问",
        "wenxin": "文心一言",
        "yuanbao": "元宝",
        "chatgpt": "ChatGPT",
        "claude": "Claude",
        "gemini": "Gemini",
    }
    text = str(platform or "").strip()
    return labels.get(text, text)


def _parse_history_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _failure_rate(failed: int, attempts: int) -> float:
    if attempts <= 0:
        return 0.0
    return round((float(failed) / float(attempts)) * 100.0, 1)


def _increment_bucket(
    bucket: dict[str, dict[str, Any]],
    key: str,
    *,
    failed: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    key = str(key or "").strip() or "未分类"
    entry = bucket.setdefault(
        key,
        {
            "name": key,
            "attempts": 0,
            "failed": 0,
        },
    )
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    if failed:
        entry["failed"] = int(entry.get("failed") or 0) + 1
    if extra:
        for extra_key, extra_value in extra.items():
            if extra_key not in entry:
                entry[extra_key] = extra_value


def _rank_failure_buckets(
    bucket: dict[str, dict[str, Any]],
    *,
    limit: int = 5,
    min_attempts: int = 2,
) -> list[dict[str, Any]]:
    qualified: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for raw in bucket.values():
        attempts = int(raw.get("attempts") or 0)
        failed = int(raw.get("failed") or 0)
        if attempts <= 0:
            continue
        item = {
            **raw,
            "attempts": attempts,
            "failed": failed,
            "success": max(0, attempts - failed),
            "failureRate": _failure_rate(failed, attempts),
        }
        fallback.append(item)
        if attempts >= min_attempts:
            qualified.append(item)

    source = qualified or fallback
    source.sort(
        key=lambda item: (
            float(item.get("failureRate") or 0.0),
            int(item.get("failed") or 0),
            int(item.get("attempts") or 0),
            str(item.get("name") or ""),
        ),
        reverse=True,
    )
    return source[: max(1, int(limit or 5))]


def _build_task_meta_map(
    config: dict[str, Any],
    *,
    normalize_string_list: Callable[[Any], list[str]] | None = None,
    task_names_loader: Callable[[], list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    normalize_values = normalize_string_list or _default_normalize_string_list
    load_task_names = task_names_loader or get_all_task_names

    task_meta: dict[str, dict[str, Any]] = {}
    for task in (config.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        task_name = str(task.get("name") or task.get("task_id") or derive_task_id(task)).strip()
        if not task_name:
            continue
        brand = str(task.get("brand") or task_name).strip() or task_name
        industries = normalize_values(task.get("industry_tags"))
        task_meta[task_name] = {
            "taskName": task_name,
            "taskId": str(task.get("task_id") or derive_task_id(task)).strip(),
            "brand": brand,
            "industries": industries,
        }

    for task_name in load_task_names():
        task_name = str(task_name or "").strip()
        if not task_name or task_name in task_meta:
            continue
        task_meta[task_name] = {
            "taskName": task_name,
            "taskId": "",
            "brand": task_name,
            "industries": [],
        }
    return task_meta


def _build_monitoring_analysis_payload(
    config: dict[str, Any],
    articles: list[dict[str, Any]] | None = None,
    *,
    normalize_string_list: Callable[[Any], list[str]] | None = None,
    platform_display_name: Callable[[str], str] | None = None,
    now: datetime | None = None,
    records_loader: Callable[..., list[dict[str, Any]]] | None = None,
    task_names_loader: Callable[[], list[str]] | None = None,
    success_checker: Callable[[dict], bool] | None = None,
) -> dict[str, Any]:
    normalize_values = normalize_string_list or _default_normalize_string_list
    display_platform = platform_display_name or _default_platform_display_name
    load_records = records_loader or get_records
    check_success = success_checker or is_success_record

    task_meta = _build_task_meta_map(
        config,
        normalize_string_list=normalize_values,
        task_names_loader=task_names_loader,
    )
    current_time = (now or local_now()).replace(tzinfo=None)
    monitor_window_start = current_time - timedelta(days=29)
    spike_window_start = current_time - timedelta(days=6)
    article_window_start = current_time.date() - timedelta(days=29)
    article_7d_start = current_time.date() - timedelta(days=6)

    industry_bucket: dict[str, dict[str, Any]] = {}
    brand_bucket: dict[str, dict[str, Any]] = {}
    keyword_bucket: dict[str, dict[str, Any]] = {}
    platform_bucket: dict[str, dict[str, Any]] = {}
    platform_hour_bucket: dict[str, dict[str, dict[str, int]]] = {}

    total_attempts = 0
    total_failed = 0
    observed_keywords: set[str] = set()
    observed_platforms: set[str] = set()
    observed_brands: set[str] = set()

    for task_name, meta in task_meta.items():
        try:
            records = load_records(task_name, task_id=str(meta.get("taskId") or "").strip())
        except Exception:
            records = []
        for record in records:
            execution_source = str(record.get("execution_source") or "").strip()
            if execution_source == "manual_test":
                continue
            ts = _parse_history_timestamp(record.get("ts"))
            if not ts or ts < monitor_window_start:
                continue

            failed = not check_success(record)
            brand = str(record.get("brand") or meta.get("brand") or task_name).strip() or task_name
            industries = list(meta.get("industries") or []) or ["未分类"]
            keyword = str(record.get("keyword") or "").strip() or "未填关键词"
            platform = display_platform(str(record.get("platform") or "").strip() or "未知平台")
            keyword_name = f"{brand} / {keyword}"

            total_attempts += 1
            total_failed += 1 if failed else 0
            observed_keywords.add(keyword_name)
            observed_platforms.add(platform)
            observed_brands.add(brand)

            for industry in industries:
                _increment_bucket(industry_bucket, industry, failed=failed)
            _increment_bucket(brand_bucket, brand, failed=failed)
            _increment_bucket(keyword_bucket, keyword_name, failed=failed, extra={"brand": brand, "keyword": keyword})
            _increment_bucket(platform_bucket, platform, failed=failed)

            if ts >= spike_window_start:
                hour_key = ts.strftime("%Y-%m-%d %H:00")
                platform_hours = platform_hour_bucket.setdefault(platform, {})
                stats = platform_hours.setdefault(hour_key, {"attempts": 0, "failed": 0})
                stats["attempts"] += 1
                if failed:
                    stats["failed"] += 1

    spikes: list[dict[str, Any]] = []
    for platform, series_map in platform_hour_bucket.items():
        ordered_hours = sorted(series_map.keys())
        normalized_series: list[dict[str, Any]] = []
        for hour_key in ordered_hours:
            stats = series_map.get(hour_key) or {}
            attempts = int(stats.get("attempts") or 0)
            failed = int(stats.get("failed") or 0)
            if attempts <= 0:
                continue
            normalized_series.append(
                {
                    "hour": hour_key,
                    "attempts": attempts,
                    "failed": failed,
                    "failureRate": _failure_rate(failed, attempts),
                }
            )
        for index, item in enumerate(normalized_series):
            previous = normalized_series[max(0, index - 6):index]
            if not previous:
                continue
            baseline_attempts = sum(int(prev.get("attempts") or 0) for prev in previous)
            if baseline_attempts <= 0:
                continue
            baseline_failed = sum(int(prev.get("failed") or 0) for prev in previous)
            baseline_rate = _failure_rate(baseline_failed, baseline_attempts)
            delta = round(float(item.get("failureRate") or 0.0) - baseline_rate, 1)
            if int(item.get("attempts") or 0) < 2:
                continue
            if float(item.get("failureRate") or 0.0) < 50.0:
                continue
            if delta < 25.0:
                continue
            spikes.append(
                {
                    "platform": platform,
                    "hour": item.get("hour"),
                    "attempts": int(item.get("attempts") or 0),
                    "failed": int(item.get("failed") or 0),
                    "failureRate": float(item.get("failureRate") or 0.0),
                    "baselineRate": baseline_rate,
                    "delta": delta,
                }
            )

    spikes.sort(
        key=lambda item: (
            float(item.get("delta") or 0.0),
            float(item.get("failureRate") or 0.0),
            int(item.get("failed") or 0),
            str(item.get("hour") or ""),
        ),
        reverse=True,
    )

    all_articles = list(articles or [])
    overall_article_series: dict[str, dict[str, int]] = {}
    brand_article_series: dict[str, dict[str, dict[str, int]]] = {}
    brand_article_totals: dict[str, dict[str, Any]] = {}
    unique_brands = []
    for task in (config.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        brand = str(task.get("brand") or task.get("name") or "").strip()
        if brand and brand not in unique_brands:
            unique_brands.append(brand)

    article_days = [
        article_window_start + timedelta(days=offset)
        for offset in range(30)
    ]
    for day in article_days:
        ds = day.isoformat()
        overall_article_series[ds] = {"total": 0, "authority": 0, "selfmedia": 0}
    for brand in unique_brands:
        brand_article_series[brand] = {
            day.isoformat(): {"total": 0, "authority": 0, "selfmedia": 0}
            for day in article_days
        }
        brand_article_totals[brand] = {
            "brand": brand,
            "total": 0,
            "last7Days": 0,
            "last30Days": 0,
            "authority": 0,
            "selfmedia": 0,
            "latestAt": "",
            "industry": "",
        }

    brand_industry_map = {}
    for task in (config.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        brand = str(task.get("brand") or task.get("name") or "").strip()
        if not brand:
            continue
        brand_industry_map[brand] = " / ".join(normalize_values(task.get("industry_tags")))
    for brand, industry in brand_industry_map.items():
        if brand in brand_article_totals:
            brand_article_totals[brand]["industry"] = industry

    unmatched_articles = 0
    for article in all_articles:
        ts = _parse_history_timestamp(article.get("ts"))
        if not ts:
            continue
        article_date = ts.date()
        if article_date < article_window_start:
            continue
        ds = article_date.isoformat()
        media_type = "authority" if str(article.get("media_type") or "").strip() == "authority" else "selfmedia"
        overall = overall_article_series.get(ds)
        if overall is not None:
            overall["total"] += 1
            overall[media_type] += 1

        matched_tasks = [
            str(task_name or "").strip()
            for task_name in (article.get("matched_tasks") or [])
            if str(task_name or "").strip()
        ]
        matched_brands = []
        for task_name in matched_tasks:
            brand = str((task_meta.get(task_name) or {}).get("brand") or task_name).strip()
            if brand and brand not in matched_brands:
                matched_brands.append(brand)
        if not matched_brands:
            unmatched_articles += 1

        for brand in matched_brands:
            brand_series = brand_article_series.setdefault(
                brand,
                {day.isoformat(): {"total": 0, "authority": 0, "selfmedia": 0} for day in article_days},
            )
            if ds in brand_series:
                brand_series[ds]["total"] += 1
                brand_series[ds][media_type] += 1
            brand_total = brand_article_totals.setdefault(
                brand,
                {
                    "brand": brand,
                    "total": 0,
                    "last7Days": 0,
                    "last30Days": 0,
                    "authority": 0,
                    "selfmedia": 0,
                    "latestAt": "",
                    "industry": brand_industry_map.get(brand, ""),
                },
            )
            brand_total["total"] = int(brand_total.get("total") or 0) + 1
            brand_total["last30Days"] = int(brand_total.get("last30Days") or 0) + 1
            if article_date >= article_7d_start:
                brand_total["last7Days"] = int(brand_total.get("last7Days") or 0) + 1
            brand_total[media_type] = int(brand_total.get(media_type) or 0) + 1
            current_latest = str(brand_total.get("latestAt") or "")
            article_ts = str(article.get("ts") or "")
            if article_ts and article_ts > current_latest:
                brand_total["latestAt"] = article_ts

    overall_series_points = []
    for ds in sorted(overall_article_series.keys()):
        overall_series_points.append(
            {
                "date": ds,
                **overall_article_series[ds],
            }
        )

    def _peak_day(points: list[dict[str, Any]]) -> dict[str, Any]:
        if not points:
            return {"date": "", "total": 0}
        return max(
            points,
            key=lambda item: (
                int(item.get("total") or 0),
                str(item.get("date") or ""),
            ),
        )

    brand_trends: list[dict[str, Any]] = []
    for brand in sorted(brand_article_series.keys()):
        points = [
            {"date": ds, **stats}
            for ds, stats in sorted((brand_article_series.get(brand) or {}).items())
        ]
        last7_points = points[-7:]
        last7_series = [int(point.get("total") or 0) for point in last7_points]
        peak = _peak_day(points)
        total_info = brand_article_totals.get(brand) or {
            "brand": brand,
            "total": 0,
            "last7Days": 0,
            "last30Days": 0,
            "authority": 0,
            "selfmedia": 0,
            "latestAt": "",
            "industry": brand_industry_map.get(brand, ""),
        }
        brand_trends.append(
            {
                "brand": brand,
                "industry": str(total_info.get("industry") or ""),
                "total": int(total_info.get("total") or 0),
                "last7Days": int(total_info.get("last7Days") or 0),
                "last30Days": int(total_info.get("last30Days") or 0),
                "authority": int(total_info.get("authority") or 0),
                "selfmedia": int(total_info.get("selfmedia") or 0),
                "latestAt": str(total_info.get("latestAt") or ""),
                "peakDay": {"date": str(peak.get("date") or ""), "total": int(peak.get("total") or 0)},
                "trend7d": last7_series,
            }
        )

    overall_peak = _peak_day(overall_series_points)
    overall_last7 = overall_series_points[-7:]
    overall_last30_total = sum(int(point.get("total") or 0) for point in overall_series_points)
    overall_last7_total = sum(int(point.get("total") or 0) for point in overall_last7)
    overall_authority = sum(int(point.get("authority") or 0) for point in overall_series_points)
    overall_selfmedia = sum(int(point.get("selfmedia") or 0) for point in overall_series_points)

    return {
        "generatedAt": current_time.isoformat(timespec="seconds"),
        "monitoringWindowDays": 30,
        "spikeWindowDays": 7,
        "articleWindowDays": 30,
        "monitoring": {
            "overall": {
                "attempts": total_attempts,
                "failed": total_failed,
                "success": max(0, total_attempts - total_failed),
                "failureRate": _failure_rate(total_failed, total_attempts),
                "brandCount": len(observed_brands),
                "keywordCount": len(observed_keywords),
                "platformCount": len(observed_platforms),
            },
            "topIndustries": _rank_failure_buckets(industry_bucket, limit=5, min_attempts=2),
            "topBrands": _rank_failure_buckets(brand_bucket, limit=8, min_attempts=2),
            "topKeywords": _rank_failure_buckets(keyword_bucket, limit=10, min_attempts=2),
            "topPlatforms": _rank_failure_buckets(platform_bucket, limit=8, min_attempts=2),
            "spikes": spikes[:8],
        },
        "articles": {
            "overall": {
                "total": overall_last30_total,
                "last7Days": overall_last7_total,
                "last30Days": overall_last30_total,
                "authority": overall_authority,
                "selfmedia": overall_selfmedia,
                "unmatched": unmatched_articles,
                "peakDay": {
                    "date": str(overall_peak.get("date") or ""),
                    "total": int(overall_peak.get("total") or 0),
                },
                "trend7d": [int(point.get("total") or 0) for point in overall_last7],
            },
            "brandTrends": brand_trends,
        },
    }


def _format_assistant_analysis_context(payload: dict[str, Any]) -> str:
    monitoring = (payload.get("monitoring") or {})
    monitoring_overall = monitoring.get("overall") or {}
    articles = (payload.get("articles") or {})
    article_overall = articles.get("overall") or {}
    top_industries = monitoring.get("topIndustries") or []
    top_brands = monitoring.get("topBrands") or []
    top_keywords = monitoring.get("topKeywords") or []
    top_platforms = monitoring.get("topPlatforms") or []
    spikes = monitoring.get("spikes") or []
    brand_trends = articles.get("brandTrends") or []

    hottest_brands = sorted(
        brand_trends,
        key=lambda item: (
            int(item.get("last7Days") or 0),
            int(item.get("last30Days") or 0),
            str(item.get("brand") or ""),
        ),
        reverse=True,
    )[:5]

    def _fmt_rate_item(item: dict[str, Any]) -> str:
        return (
            f"{item.get('name')}: 失败率 {float(item.get('failureRate') or 0.0):.1f}% "
            f"({int(item.get('failed') or 0)}/{int(item.get('attempts') or 0)})"
        )

    executive_summary: list[str] = []
    if top_platforms:
        executive_summary.append(f"当前失败率最高的平台是 {_fmt_rate_item(top_platforms[0])}")
    if top_brands:
        executive_summary.append(f"当前失败率最高的品牌是 {_fmt_rate_item(top_brands[0])}")
    if top_keywords:
        executive_summary.append(f"当前失败率最高的关键词是 {_fmt_rate_item(top_keywords[0])}")
    if spikes:
        first_spike = spikes[0]
        executive_summary.append(
            f"最近最明显的异常波动出现在 {first_spike.get('hour')} 的 {first_spike.get('platform')}，"
            f"失败率 {float(first_spike.get('failureRate') or 0.0):.1f}%，较基线 +{float(first_spike.get('delta') or 0.0):.1f}pct"
        )
    else:
        executive_summary.append("近 7 天未识别到明显的单小时失败率突增平台")
    if hottest_brands:
        hottest = hottest_brands[0]
        executive_summary.append(
            f"文章声量最高的品牌是 {hottest.get('brand')}，近 7 天 {int(hottest.get('last7Days') or 0)} 篇，"
            f"近 30 天 {int(hottest.get('last30Days') or 0)} 篇"
        )

    anomaly_lines: list[str] = []
    if top_platforms:
        anomaly_lines.append(f"平台风险：{_fmt_rate_item(top_platforms[0])}")
    if top_brands:
        anomaly_lines.append(f"品牌风险：{_fmt_rate_item(top_brands[0])}")
    if top_keywords:
        anomaly_lines.append(f"关键词风险：{_fmt_rate_item(top_keywords[0])}")
    if top_industries:
        anomaly_lines.append(f"行业风险：{_fmt_rate_item(top_industries[0])}")
    if spikes:
        for spike in spikes[:3]:
            anomaly_lines.append(
                f"时段异常：{spike.get('hour')} | {spike.get('platform')} | 失败率 {float(spike.get('failureRate') or 0.0):.1f}% | "
                f"较基线 +{float(spike.get('delta') or 0.0):.1f}pct"
            )

    action_lines: list[str] = []
    if top_platforms:
        item = top_platforms[0]
        action_lines.append(
            f"优先排查 {item.get('name')} 平台链路，当前失败率 {float(item.get('failureRate') or 0.0):.1f}%，"
            f"建议先检查登录态、页面结构变化、接口限流或模型配置。"
        )
    if top_keywords:
        item = top_keywords[0]
        action_lines.append(
            f"针对高失败关键词 {item.get('name')} 做专项复盘，核对提示词、品牌词映射和平台覆盖，必要时拆成更细关键词组。"
        )
    if hottest_brands:
        item = hottest_brands[0]
        action_lines.append(
            f"文章声量最高的品牌 {item.get('brand')} 可优先做内容结构复盘，近 7 天 {int(item.get('last7Days') or 0)} 篇，"
            f"适合继续加密监测与复用有效选题。"
        )
    while len(action_lines) < 3:
        action_lines.append("补齐低数据品牌的文章与监测样本，避免因为样本过少导致判断失真。")
    action_lines = action_lines[:3]

    lines = [
        "以下是基于程序底层真实数据生成的监测/文章聚合摘要，不是展示层回填数据。",
        (
            f"统计窗口：监测记录近 {int(payload.get('monitoringWindowDays') or 30)} 天，"
            f"异常波动近 {int(payload.get('spikeWindowDays') or 7)} 天，"
            f"文章趋势近 {int(payload.get('articleWindowDays') or 30)} 天。"
        ),
        (
            "监测总览："
            f"总尝试 {int(monitoring_overall.get('attempts') or 0)} 次，"
            f"失败 {int(monitoring_overall.get('failed') or 0)} 次，"
            f"失败率 {float(monitoring_overall.get('failureRate') or 0.0):.1f}%，"
            f"覆盖品牌 {int(monitoring_overall.get('brandCount') or 0)} 个，"
            f"关键词 {int(monitoring_overall.get('keywordCount') or 0)} 个，"
            f"平台 {int(monitoring_overall.get('platformCount') or 0)} 个。"
        ),
        "建议把最终回复组织成以下 4 段：1. 当前运行概况 2. 值得关注的异常或空白 3. 接下来最值得做的 3 个动作 4. 若要提升命中率/稳定性最该先改什么。",
        "可直接引用的结论：",
    ]
    if executive_summary:
        for item in executive_summary:
            lines.append(f"- {item}")

    section_map = (
        ("行业失败率 Top", top_industries),
        ("品牌失败率 Top", top_brands),
        ("关键词失败率 Top", top_keywords),
        ("平台失败率 Top", top_platforms),
    )
    for title, items in section_map:
        lines.append(f"{title}：")
        if not items:
            lines.append("- 暂无数据")
            continue
        for item in items:
            lines.append(f"- {_fmt_rate_item(item)}")

    lines.append("失败率突增时段 Top：")
    if not spikes:
        lines.append("- 近 7 天未检测到明显突增时段")
    else:
        for item in spikes:
            lines.append(
                f"- {item.get('hour')} | {item.get('platform')}: 失败率 {float(item.get('failureRate') or 0.0):.1f}% "
                f"({int(item.get('failed') or 0)}/{int(item.get('attempts') or 0)}), "
                f"较此前基线 +{float(item.get('delta') or 0.0):.1f}pct"
            )

    lines.extend(
        [
            (
                "文章总览："
                f"近 30 天总计 {int(article_overall.get('total') or 0)} 篇，"
                f"近 7 天 {int(article_overall.get('last7Days') or 0)} 篇，"
                f"权威媒体 {int(article_overall.get('authority') or 0)} 篇，"
                f"自媒体 {int(article_overall.get('selfmedia') or 0)} 篇，"
                f"未归类 {int(article_overall.get('unmatched') or 0)} 篇。"
            ),
            (
                f"所有品牌文章 7 日趋势：{', '.join(str(item) for item in (article_overall.get('trend7d') or [])) or '0'}。"
                f" 峰值日：{(article_overall.get('peakDay') or {}).get('date') or '无'} "
                f"（{int(((article_overall.get('peakDay') or {}).get('total') or 0))} 篇）"
            ),
            "各品牌文章趋势：",
        ]
    )

    if not brand_trends:
        lines.append("- 暂无品牌文章数据")
    else:
        for item in brand_trends:
            industry = str(item.get("industry") or "").strip()
            industry_text = f" | 行业 {industry}" if industry else ""
            lines.append(
                f"- {item.get('brand')}{industry_text}: 近 30 天 {int(item.get('last30Days') or 0)} 篇，"
                f"近 7 天 {int(item.get('last7Days') or 0)} 篇，"
                f"权威 {int(item.get('authority') or 0)} / 自媒体 {int(item.get('selfmedia') or 0)}，"
                f"7 日趋势 {', '.join(str(value) for value in (item.get('trend7d') or [])) or '0'}，"
                f"峰值日 {(item.get('peakDay') or {}).get('date') or '无'} "
                f"({int(((item.get('peakDay') or {}).get('total') or 0))} 篇)"
            )

    lines.append("优先关注的异常或空白：")
    if anomaly_lines:
        for item in anomaly_lines:
            lines.append(f"- {item}")
    else:
        lines.append("- 当前未识别到明显异常，可重点关注低样本品牌与低覆盖平台")

    lines.append("建议优先执行的 3 个动作：")
    for index, item in enumerate(action_lines, start=1):
        lines.append(f"- 动作 {index}: {item}")

    lines.append("分析要求：请优先基于以上底层真实数据做结论，避免只复述展示层摘要。")
    return "\n".join(lines)


def _should_attach_assistant_analysis(messages: list[dict[str, Any]]) -> bool:
    if not isinstance(messages, list) or not messages:
        return False
    has_mode_marker = False
    for message in messages:
        if str(message.get("role") or "").strip() != "system":
            continue
        content = str(message.get("content") or "")
        if not content:
            continue
        if any(marker in content for marker in _ASSISTANT_CHAT_MODE_MARKERS.values()):
            has_mode_marker = True
        if _ASSISTANT_CHAT_MODE_MARKERS["analysis"] in content:
            return True
    if has_mode_marker:
        return False

    last_user_content = ""
    for message in reversed(messages):
        if str(message.get("role") or "").strip() == "user":
            last_user_content = str(message.get("content") or "").strip()
            break
    if not last_user_content:
        return False
    normalized = "".join(last_user_content.lower().split())
    if any(keyword in normalized for keyword in _ASSISTANT_ANALYSIS_TRIGGER_PHRASES):
        return True

    has_metric = any(keyword in normalized for keyword in _ASSISTANT_ANALYSIS_METRIC_KEYWORDS)
    has_scope = any(keyword in normalized for keyword in _ASSISTANT_ANALYSIS_SCOPE_KEYWORDS)
    has_action = any(keyword in normalized for keyword in _ASSISTANT_ANALYSIS_ACTION_KEYWORDS)
    return has_metric and has_scope and has_action
