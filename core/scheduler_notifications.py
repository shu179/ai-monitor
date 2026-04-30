from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from core.cycle_state import (
    get_cycle_report,
    mark_cycle_success_notified,
    save_cycle_report,
    update_cycle_report_with_recognition,
)
from core.daily_task_state import SOURCE_MODE_TEST
from core.notifier import WeComNotifier
from core.time_utils import local_now


_PLATFORM_LABELS = {
    'local_model': '本地模型',
    'doubao': '豆包',
    'deepseek': 'DeepSeek',
    'ark_deepseek': '方舟 DeepSeek',
    'kimi': 'Kimi',
    'yuanbao': '元宝',
    'tongyi': '通义',
    'wenxin': '文心',
    'recognition': '识别模式',
}


def normalize_webhook_url(value: str) -> str:
    text = str(value or '').strip()
    if not text or 'YOUR_KEY_HERE' in text:
        return ''
    return text


def display_platform_name(platform_name: str) -> str:
    text = str(platform_name or '').strip()
    return _PLATFORM_LABELS.get(text, text or '未知平台')


def dedupe_non_empty(items: Iterable[Any]) -> list[str]:
    values = []
    for item in items:
        text = str(item or '').strip()
        if text and text not in values:
            values.append(text)
    return values


def get_scheduler_notification_webhook(config: dict | None) -> str:
    scheduler_cfg = (config or {}).get('scheduler', {}) or {}
    return normalize_webhook_url(scheduler_cfg.get('notification_webhook_url', ''))


def build_scheduler_notifier(config: dict | None):
    webhook_url = get_scheduler_notification_webhook(config)
    if not webhook_url:
        return None
    notify_cfg = (config or {}).get('default_notification', {}) or {}
    try:
        send_interval = max(1, int(notify_cfg.get('send_interval', 2) or 2))
    except Exception:
        send_interval = 2
    return WeComNotifier(
        webhook_url=webhook_url,
        cooldown_minutes=0,
        send_interval=send_interval,
    )


_normalize_webhook_url = normalize_webhook_url
_display_platform_name = display_platform_name
_dedupe_non_empty = dedupe_non_empty
_get_scheduler_notification_webhook = get_scheduler_notification_webhook
_build_scheduler_notifier = build_scheduler_notifier


class SchedulerWebhookReporter:
    """向全局调度 webhook 推送模式汇总、整轮汇总和实时异常。"""

    def __init__(self, config_getter: Callable[[], dict | None]):
        self._config_getter = config_getter
        self._issue_lock = threading.Lock()
        self._recent_issue_keys: dict[str, float] = {}
        self._issue_streaks: dict[str, dict[str, float | int | str]] = {}
        self._recognition_lock = threading.Lock()
        self._recognition_round_events: list[dict] = []
        self._recognition_round_event_keys: set[str] = set()

    def _get_config(self) -> dict:
        try:
            return self._config_getter() or {}
        except Exception:
            return {}

    def _send_text(self, content: str) -> bool:
        notifier = _build_scheduler_notifier(self._get_config())
        if not notifier:
            return False
        ok = notifier.send_text_message(content)
        if not ok:
            print(f"[Main] 调度通知发送失败: {notifier.last_error or '未知错误'}")
        return ok

    def _notification_policy(self) -> tuple[int, int]:
        notify_cfg = (self._get_config() or {}).get('default_notification', {}) or {}
        try:
            threshold = max(1, int(notify_cfg.get('failure_alert_threshold', 7) or 7))
        except Exception:
            threshold = 7
        try:
            cooldown_minutes = max(0, int(notify_cfg.get('failure_alert_cooldown_minutes', 5) or 5))
        except Exception:
            cooldown_minutes = 5
        return threshold, cooldown_minutes * 60

    def _issue_group_key(
        self,
        title: str,
        *,
        task_name: str = '',
        mode: str = '',
        keyword: str = '',
        platform: str = '',
        brand: str = '',
    ) -> str:
        return "|".join([
            str(title or '').strip(),
            str(task_name or '').strip(),
            str(mode or '').strip(),
            str(keyword or '').strip(),
            str(platform or '').strip(),
            str(brand or '').strip(),
        ])

    def _should_send_issue(
        self,
        issue_key: str,
        *,
        group_key: str,
        force: bool = False,
    ) -> tuple[bool, int]:
        now = time.time()
        threshold, ttl_seconds = self._notification_policy()
        with self._issue_lock:
            last_sent = self._recent_issue_keys.get(issue_key, 0)
            streak = dict(self._issue_streaks.get(group_key, {}) or {})
            if force:
                if now - last_sent < ttl_seconds:
                    return False, int(streak.get('count') or 0)
                self._recent_issue_keys[issue_key] = now
                streak['count'] = 0
                streak['last_seen'] = now
                self._issue_streaks[group_key] = streak
                return True, 1

            last_seen = float(streak.get('last_seen') or 0.0)
            if last_seen and now - last_seen > max(ttl_seconds * 3, 1800):
                streak = {}
            streak_count = int(streak.get('count') or 0) + 1
            streak['count'] = streak_count
            streak['last_seen'] = now
            self._issue_streaks[group_key] = streak

            if streak_count < threshold or now - last_sent < ttl_seconds:
                expired_streaks = [
                    key for key, item in self._issue_streaks.items()
                    if now - float((item or {}).get('last_seen') or 0.0) > max(ttl_seconds * 6, 3600)
                ]
                for key in expired_streaks:
                    del self._issue_streaks[key]
                return False, streak_count

            self._recent_issue_keys[issue_key] = now
            streak['count'] = 0
            self._issue_streaks[group_key] = streak
            expired_keys = [
                key for key, sent_at in self._recent_issue_keys.items()
                if now - sent_at > max(ttl_seconds * 2, 600)
            ]
            for key in expired_keys:
                del self._recent_issue_keys[key]
        return True, streak_count

    def _format_failed_tasks(self, reports: list[dict]) -> list[str]:
        lines = []
        for report in reports:
            task_name = str(report.get('task_name') or '未命名任务').strip()
            failed_details = list(report.get('failed_query_details') or [])
            if failed_details:
                detail_parts = []
                for item in failed_details[:5]:
                    keyword = str(item.get('keyword') or '').strip() or '未填关键词'
                    platform = _display_platform_name(item.get('platform', ''))
                    reason = str(item.get('error_message') or '').strip() or '执行失败'
                    detail_parts.append(f"{keyword}/{platform}: {reason}")
                lines.append(f"- {task_name}: " + "；".join(detail_parts))
                continue
            failure_message = str(report.get('task_failure_message') or '').strip() or '任务执行失败'
            lines.append(f"- {task_name}: {failure_message}")
        return lines

    def _collect_success_brands(self, reports: list[dict]) -> list[str]:
        brands = []
        for report in reports:
            brands.extend(report.get('successful_brands') or [])
        return _dedupe_non_empty(brands)

    def _build_cycle_summary_lines(
        self,
        cycle_payload: dict,
        *,
        header: str,
        extra_lines: list[str] | None = None,
    ) -> list[str]:
        summary = cycle_payload.get('summary', {}) or {}
        task_outcomes = list(cycle_payload.get('task_outcomes') or [])
        executed_rounds = list(cycle_payload.get('executed_rounds') or [])
        executed_mode_labels = [
            str((item.get('mode_label') or item.get('mode') or '')).strip()
            for item in executed_rounds
            if str((item.get('mode_label') or item.get('mode') or '')).strip()
        ]
        success_brands = []
        failed_lines = []
        for item in task_outcomes:
            if str(item.get('final_status') or '').strip() == 'success':
                success_brands.extend(item.get('successful_brands') or [])
                continue
            if str(item.get('final_status') or '').strip() == 'failed':
                failed_lines.extend(self._format_failed_tasks([item]))
        success_brands = _dedupe_non_empty(success_brands)

        lines = [
            header,
            f"时间：{local_now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"已执行模式：{' -> '.join(executed_mode_labels) if executed_mode_labels else '无'}",
            f"任务统计：成功 {summary.get('success', 0)} / 失败 {summary.get('failed', 0)} / 跳过 {summary.get('skipped', 0)}",
            f"完成品牌：{', '.join(success_brands) if success_brands else '无'}",
        ]
        if extra_lines:
            lines.extend([str(item).strip() for item in extra_lines if str(item).strip()])
        if failed_lines:
            lines.append("仍失败任务：")
            lines.extend(failed_lines)
        else:
            lines.append("仍失败任务：无")
        return lines

    def _build_all_success_lines(self, cycle_payload: dict) -> list[str]:
        summary = dict(cycle_payload.get('summary') or {})
        task_outcomes = list(cycle_payload.get('task_outcomes') or [])
        success_brands = []
        for item in task_outcomes:
            if str(item.get('final_status') or '').strip() == 'success':
                success_brands.extend(item.get('successful_brands') or [])
        success_brands = _dedupe_non_empty(success_brands)
        return [
            "🎉 当日任务已全部完成",
            "",
            f"时间：{local_now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"完成任务：{summary.get('success', 0)} 个",
            f"完成品牌：{', '.join(success_brands) if success_brands else '无'}",
            "✅ 今日需执行的监控任务已全部成功完成",
        ]

    def _maybe_send_all_success_notification(self, cycle_payload: dict) -> bool:
        payload = dict(cycle_payload or {})
        summary = dict(payload.get('summary') or {})
        if int(summary.get('total_tasks', 0) or 0) <= 0:
            return False
        if int(summary.get('failed', 0) or 0) > 0:
            return False
        if str(payload.get('all_success_notified_at') or '').strip():
            return False

        ok = self._send_text("\n".join(self._build_all_success_lines(payload)))
        if ok:
            marked = mark_cycle_success_notified()
            if isinstance(marked, dict):
                payload.update(marked)
        return ok

    def send_mode_summary(self, round_payload: dict) -> bool:
        mode_label = str(round_payload.get('mode_label') or round_payload.get('mode') or '当前模式').strip()
        summary = round_payload.get('summary', {}) or {}
        reports = list(round_payload.get('reports') or [])
        success_reports = [
            report for report in reports
            if str(report.get('task_status') or report.get('round_status') or '').strip() == 'success'
        ]
        failed_reports = [
            report for report in reports
            if str(report.get('task_status') or report.get('round_status') or '').strip() not in {'', 'success', 'skipped'}
        ]
        success_brands = self._collect_success_brands(success_reports)
        failed_lines = self._format_failed_tasks(failed_reports)

        lines = [
            f"【自动监控模式汇总】{mode_label}",
            f"时间：{local_now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"任务统计：成功 {summary.get('success', 0)} / 失败 {summary.get('failed', 0)} / 跳过 {summary.get('skipped', 0)}",
            f"完成品牌：{', '.join(success_brands) if success_brands else '无'}",
        ]
        if failed_lines:
            lines.append("失败任务：")
            lines.extend(failed_lines)
        else:
            lines.append("失败任务：无")
        lines.append("请留意是否需要继续后续模式或人工介入处理。")
        return self._send_text("\n".join(lines))

    def send_cycle_summary(self, cycle_payload: dict) -> bool:
        saved_payload = save_cycle_report(cycle_payload)
        lines = self._build_cycle_summary_lines(
            saved_payload,
            header="【自动监控整轮汇总】",
        )
        lines.append("本轮自动调度已结束，请按结果安排后续处理。")
        summary_sent = self._send_text("\n".join(lines))
        self._maybe_send_all_success_notification(saved_payload)
        return summary_sent

    def _find_cycle_task_outcome(self, cycle_payload: dict | None, task_id: str) -> dict:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id or not isinstance(cycle_payload, dict):
            return {}
        for item in list(cycle_payload.get('task_outcomes') or []):
            if str((item or {}).get('task_id') or '').strip() == normalized_task_id:
                return dict(item or {})
        return {}

    def _build_recognition_event(self, batch: dict, ok: bool, reason: str = "") -> dict:
        task_name = str(batch.get('task_name') or '未命名任务').strip()
        brands = _dedupe_non_empty(batch.get('brands') or [])
        matched_pairs = list(batch.get('matched_pairs') or [])
        supplemented_keywords = _dedupe_non_empty(batch.get('supplemented_keywords') or [])
        completed_keywords = _dedupe_non_empty(batch.get('completed_keywords') or [])
        if not supplemented_keywords and matched_pairs:
            supplemented_keywords = _dedupe_non_empty(
                str((pair or {}).get('keyword') or '').strip()
                for pair in matched_pairs
            )
        detected_platforms = _dedupe_non_empty(batch.get('detected_platforms') or [])
        image_count = int(batch.get('total_image_count') or len(batch.get('image_paths') or []))
        task = batch.get('task') if isinstance(batch.get('task'), dict) else {}
        original_task_id = str(task.get('task_id') or '').strip()
        daily_state_source = str(task.get('_daily_state_source') or '').strip()
        cycle_before = get_cycle_report()
        previous_outcome = self._find_cycle_task_outcome(cycle_before, original_task_id)
        previous_status = str(previous_outcome.get('final_status') or '').strip()

        return {
            "event_key": "|".join([
                original_task_id,
                str(batch.get('id') or '').strip(),
                task_name,
                ",".join(completed_keywords),
                "1" if ok else "0",
            ]),
            "task_id": original_task_id,
            "task_name": task_name,
            "brands": brands,
            "ok": bool(ok),
            "reason": str(reason or '').strip(),
            "supplemented_keywords": supplemented_keywords,
            "completed_keywords": completed_keywords,
            "detected_platforms": detected_platforms,
            "image_count": image_count,
            "previous_status": previous_status,
            "was_recovery": previous_status in {"failed", "skipped"},
            "manual_test": daily_state_source == SOURCE_MODE_TEST or daily_state_source == "manual_test",
        }

    def record_recognition_result(self, batch: dict, ok: bool, reason: str = "") -> bool:
        event = self._build_recognition_event(batch or {}, ok, reason)
        if event.get("manual_test"):
            return False

        cycle_payload = None
        if event.get("task_id"):
            cycle_payload = update_cycle_report_with_recognition(
                task_id=str(event.get("task_id") or ""),
                task_name=str(event.get("task_name") or ""),
                brands=list(event.get("brands") or []),
                ok=bool(event.get("ok")),
                reason=str(event.get("reason") or ""),
                supplemented_keywords=list(event.get("supplemented_keywords") or []),
                completed_keywords=list(event.get("completed_keywords") or []),
                detected_platforms=list(event.get("detected_platforms") or []),
                image_count=int(event.get("image_count") or 0),
            )
        event["cycle_updated"] = cycle_payload is not None

        event_key = str(event.get("event_key") or "").strip()
        with self._recognition_lock:
            if event_key and event_key in self._recognition_round_event_keys:
                return False
            if event_key:
                self._recognition_round_event_keys.add(event_key)
            self._recognition_round_events.append(event)
        return False

    def send_recognition_summary(self, batch: dict, ok: bool, reason: str = "") -> bool:
        """Compatibility wrapper: record a single recognition result without sending a summary."""
        return self.record_recognition_result(batch, ok, reason)

    def _format_recognition_event_label(self, event: dict) -> str:
        task_name = str(event.get('task_name') or '未命名任务').strip()
        brands = _dedupe_non_empty(event.get('brands') or [])
        keyword_count = len(_dedupe_non_empty(event.get('supplemented_keywords') or []))
        platform_count = len(_dedupe_non_empty(event.get('detected_platforms') or []))
        details = []
        if brands:
            details.append(f"品牌 {', '.join(brands)}")
        if keyword_count:
            details.append(f"{keyword_count} 个关键词")
        if platform_count:
            details.append(f"{platform_count} 个平台")
        return f"{task_name}（{'，'.join(details)}）" if details else task_name

    def send_recognition_round_summary(self, payload: dict | None = None) -> bool:
        with self._recognition_lock:
            events = list(self._recognition_round_events)
            self._recognition_round_events = []
            self._recognition_round_event_keys = set()

        if not events:
            return False

        cycle_payload = get_cycle_report()
        completed_events = [item for item in events if item.get('ok') and not item.get('was_recovery')]
        recovered_events = [item for item in events if item.get('ok') and item.get('was_recovery')]
        failed_events = [item for item in events if not item.get('ok')]

        failed_lines = []
        if cycle_payload:
            task_outcomes = list(cycle_payload.get('task_outcomes') or [])
            failed_lines = self._format_failed_tasks([
                item for item in task_outcomes
                if str((item or {}).get('final_status') or '').strip() == 'failed'
            ])
        if not failed_lines and failed_events:
            for item in failed_events:
                reason_text = str(item.get('reason') or '企业微信发送失败').strip()
                failed_lines.append(f"- {str(item.get('task_name') or '未命名任务').strip()}: {reason_text}")

        current_summary = dict((cycle_payload or {}).get('summary') or {})
        lines = [
            "【自动监控识别补齐汇总】",
            f"时间：{local_now().strftime('%Y-%m-%d %H:%M:%S')}",
            (
                f"本轮统计：完成 {len(completed_events)} / 补齐 {len(recovered_events)} / "
                f"仍未成功 {len(failed_lines)}"
            ),
        ]
        if current_summary:
            lines.append(
                f"整轮当前：成功 {current_summary.get('success', 0)} / "
                f"失败 {current_summary.get('failed', 0)} / 跳过 {current_summary.get('skipped', 0)}"
            )
        lines.append(
            "本轮完成：" + (
                "、".join(self._format_recognition_event_label(item) for item in completed_events)
                if completed_events else "无"
            )
        )
        lines.append(
            "本轮补齐：" + (
                "、".join(self._format_recognition_event_label(item) for item in recovered_events)
                if recovered_events else "无"
            )
        )
        if failed_lines:
            lines.append("仍未成功：")
            lines.extend(failed_lines)
        else:
            lines.append("仍未成功：无")
        if current_summary and int(current_summary.get('failed', 0) or 0) <= 0:
            lines.append("当前整轮已全部成功。")
        return self._send_text("\n".join(lines))

    def handle_status_event(self, status: str, message: str) -> bool:
        if str(status or '').strip() != 'error':
            return False
        return self.send_issue("调度器异常", message, force=True)

    def send_timeout(self, task: dict, elapsed_seconds: float) -> bool:
        task_name = str(task.get('name', '') or '').strip() or '未命名任务'
        keywords = [
            str((kw or {}).get('keyword') or '').strip()
            for kw in (task.get('keywords', []) or [])[:3]
            if str((kw or {}).get('keyword') or '').strip()
        ]
        return self.send_issue(
            "任务超时提醒",
            f"任务组「{task_name}」已运行 {int(elapsed_seconds // 60)} 分钟仍未结束",
            task_name=task_name,
            keyword=', '.join(keywords),
            mode=str(task.get('_scheduler_mode') or '').strip(),
            force=True,
        )

    def send_issue(
        self,
        title: str,
        message: str,
        *,
        task_name: str = '',
        mode: str = '',
        keyword: str = '',
        platform: str = '',
        brand: str = '',
        force: bool = False,
    ) -> bool:
        normalized_message = str(message or '').strip()
        if not normalized_message:
            return False
        issue_key = "|".join([
            str(title or '').strip(),
            str(task_name or '').strip(),
            str(mode or '').strip(),
            str(keyword or '').strip(),
            str(platform or '').strip(),
            normalized_message,
        ])
        group_key = self._issue_group_key(
            title,
            task_name=task_name,
            mode=mode,
            keyword=keyword,
            platform=platform,
            brand=brand,
        )
        should_send, streak_count = self._should_send_issue(
            issue_key,
            group_key=group_key,
            force=force,
        )
        if not should_send:
            return False

        lines = [
            f"【自动监控实时告警】{str(title or '运行异常').strip()}",
            f"时间：{local_now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if task_name:
            lines.append(f"任务：{task_name}")
        if mode:
            lines.append(f"模式：{mode}")
        if brand:
            lines.append(f"品牌：{brand}")
        if keyword:
            lines.append(f"关键词：{keyword}")
        if platform:
            lines.append(f"平台：{_display_platform_name(platform)}")
        if not force:
            lines.append(f"连续失败次数：{max(1, int(streak_count or 0))}")
        lines.append(f"问题：{normalized_message}")
        lines.append("请尽快查看后台运行状态并决定是否人工处理。")
        return self._send_text("\n".join(lines))

    def handle_issue_payload(self, payload: dict) -> bool:
        return self.send_issue(
            str(payload.get('title') or '运行异常').strip(),
            str(payload.get('message') or '').strip(),
            task_name=str(payload.get('task_name') or '').strip(),
            mode=str(payload.get('mode') or '').strip(),
            keyword=str(payload.get('keyword') or '').strip(),
            platform=str(payload.get('platform') or '').strip(),
            brand=str(payload.get('brand') or '').strip(),
        )


__all__ = [
    "SchedulerWebhookReporter",
    "build_scheduler_notifier",
    "dedupe_non_empty",
    "display_platform_name",
    "get_scheduler_notification_webhook",
    "normalize_webhook_url",
]
