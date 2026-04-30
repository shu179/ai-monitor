"""
自动调度器

设计目标：
- 按全局每周时间表唤醒
- 按模式分轮执行：保险(api) -> 抓取(browser) -> 智能(smart)
- 每轮内部按任务规模、平均耗时和历史失败惩罚排序
- 展示层状态和调度层状态解耦
- 识别模式不进入主动调度队列
"""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import date, datetime
from typing import Callable, Optional

from .cycle_state import build_task_outcome, merge_task_outcome_from_report
from .daily_task_state import derive_task_id, get_task_day_status, is_task_sent_today
from .platform_sessions import build_query_execution_policy
from .scheduler_state import get_entry as get_scheduler_entry
from .scheduler_state import mark_processed, update_after_run


WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MODE_ORDER = ["api", "browser", "smart"]
MODE_LABELS = {
    "api": "保险模式",
    "browser": "抓取模式",
    "smart": "智能模式",
}
PROCESSED_ROUND_STATUSES = {"success", "skipped", "partial", "failed", "pending", "query_failed", "send_failed"}
DEFAULT_WEEKLY_TIMES = {
    "0": "09:30",
    "1": "09:30",
    "2": "09:30",
    "3": "09:30",
    "4": "09:30",
    "5": None,
    "6": None,
}


def _call_executor_hook(executor: Callable, hook_name: str, *args, **kwargs):
    hook = getattr(executor, hook_name, None)
    if callable(hook):
        return hook(*args, **kwargs)
    owner = getattr(executor, "__self__", None)
    if owner is not None:
        hook = getattr(owner, hook_name, None)
        if callable(hook):
            return hook(*args, **kwargs)
    return None


def normalize_weekly_times(config: dict | None) -> dict[str, str | None]:
    scheduler_cfg = config or {}
    weekly_times = scheduler_cfg.get("weekly_times") or DEFAULT_WEEKLY_TIMES
    normalized: dict[str, str | None] = {}
    for weekday in range(7):
        raw = weekly_times.get(str(weekday), weekly_times.get(weekday))
        if raw in (None, "", False):
            normalized[str(weekday)] = None
            continue
        text = str(raw).strip()
        try:
            hour, minute = parse_time(text)
            normalized[str(weekday)] = f"{hour:02d}:{minute:02d}"
        except Exception:
            normalized[str(weekday)] = None
    return normalized


def parse_time(time_text: str) -> tuple[int, int]:
    parts = str(time_text or "").split(":")
    if len(parts) != 2:
        raise ValueError(f"非法时间格式: {time_text}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"非法时间值: {time_text}")
    return hour, minute


def get_weekday_run_time(config: dict | None, weekday: int) -> str | None:
    return normalize_weekly_times(config).get(str(weekday))


def describe_task_schedule(task: dict, scheduler_config: dict | None) -> str:
    weekdays = [int(day) for day in (task.get("weekdays") or []) if isinstance(day, int)]
    if not weekdays:
        return "未设置运行日"

    weekly_times = normalize_weekly_times(scheduler_config)
    parts = []
    for weekday in weekdays:
        if weekday < 0 or weekday > 6:
            continue
        run_time = weekly_times.get(str(weekday))
        if run_time:
            parts.append(f"{WEEKDAY_LABELS[weekday]} {run_time}")
        else:
            parts.append(f"{WEEKDAY_LABELS[weekday]} 不运行")
    return " / ".join(parts[:3]) + (" ..." if len(parts) > 3 else "")


def _normalize_keywords(task: dict) -> list[dict]:
    if "keyword" in task and "keywords" not in task:
        return [{
            "keyword": task.get("keyword", ""),
            "brand": task.get("brand", ""),
            "platforms": [task.get("platform", "")],
            "mode": task.get("mode", "browser"),
        }]
    return list(task.get("keywords", []) or [])


def _configured_query_mode(config: dict | None) -> str:
    mode = str((config or {}).get("detection_mode") or "").strip()
    if mode in MODE_ORDER or mode == "recognition":
        return mode
    return ""


def _extract_modes(task: dict, configured_mode: str = "") -> list[str]:
    if configured_mode == "recognition":
        return []
    if configured_mode in MODE_ORDER:
        return [configured_mode]
    modes = []
    for kw in _normalize_keywords(task):
        mode = str(kw.get("mode") or "browser").strip() or "browser"
        if mode == "recognition":
            continue
        if mode not in MODE_ORDER:
            mode = "browser"
        if mode not in modes:
            modes.append(mode)
    return [mode for mode in MODE_ORDER if mode in modes]


def _build_mode_unit(task: dict, mode: str) -> dict | None:
    filtered_keywords = []
    for kw in _normalize_keywords(task):
        keyword = str(kw.get("keyword") or "").strip()
        platforms = [str(p).strip() for p in (kw.get("platforms") or []) if str(p).strip()]
        if not keyword or not platforms:
            continue
        filtered_keywords.append({**kw, "platforms": platforms, "mode": mode})

    if not filtered_keywords:
        return None

    unit = deepcopy(task)
    original_task_id = derive_task_id(task)
    unit_id = f"{original_task_id}::{mode}"
    unit["keywords"] = filtered_keywords
    unit["task_id"] = unit_id
    unit["_scheduler_mode"] = mode
    unit["_scheduler_unit_id"] = unit_id
    unit["_scheduler_original_task_id"] = original_task_id
    return unit


def _count_queries(task: dict) -> int:
    total = 0
    for kw in _normalize_keywords(task):
        keyword = str(kw.get("keyword") or "").strip()
        platforms = [str(p).strip() for p in (kw.get("platforms") or []) if str(p).strip()]
        if keyword and platforms:
            total += len(platforms)
    return total


def _mode_weight(mode: str) -> int:
    if mode == "smart":
        return 5
    if mode == "browser":
        return 3
    return 1


def _score_unit(task_unit: dict) -> float:
    unit_id = str(task_unit.get("_scheduler_unit_id") or derive_task_id(task_unit)).strip()
    mode = str(task_unit.get("_scheduler_mode") or "browser").strip()
    state = get_scheduler_entry(unit_id)
    query_size = _count_queries(task_unit)
    base_cost = query_size * _mode_weight(mode)
    duration_cost = float(state.get("avg_duration_seconds") or 0.0) / 30.0
    fail_cost = int(state.get("recent_auto_failures") or 0) * 2
    fail_cost += int(state.get("consecutive_auto_failures") or 0) * 2
    structural_cost = int(state.get("structural_failures") or 0) * 5
    penalty_cost = int(state.get("scheduler_penalty") or 0)
    return base_cost + duration_cost + fail_cost + structural_cost + penalty_cost


def _is_fixed_screenshot_unit(task_unit: dict) -> bool:
    return bool(task_unit.get("fixed_screenshot_enabled", False))


def _unit_platform_sequence(task_unit: dict) -> list[str]:
    platforms: list[str] = []
    for kw in _normalize_keywords(task_unit):
        for platform_name in (kw.get("platforms") or []):
            normalized = str(platform_name or "").strip()
            if normalized and normalized not in platforms:
                platforms.append(normalized)
    return platforms


def _unit_platform_execution_order(task_unit: dict, preferred_first: str = "") -> list[str]:
    ordered = _unit_platform_sequence(task_unit)
    normalized_preferred = str(preferred_first or "").strip()
    if normalized_preferred and normalized_preferred in ordered:
        ordered = [normalized_preferred] + [item for item in ordered if item != normalized_preferred]
    return ordered


def _reorder_units_for_platform_continuity(units: list[dict]) -> list[dict]:
    if len(units) <= 1:
        return list(units)
    remaining = list(units)
    ordered: list[dict] = []
    active_platform = ""
    lookahead = 3
    while remaining:
        selected_index = 0
        if active_platform:
            window = remaining[:min(len(remaining), lookahead)]
            for idx, unit in enumerate(window):
                if active_platform in _unit_platform_sequence(unit):
                    selected_index = idx
                    break
        chosen = remaining.pop(selected_index)
        ordered.append(chosen)
        execution_order = _unit_platform_execution_order(chosen, active_platform)
        active_platform = execution_order[-1] if execution_order else ""
    return ordered


def _is_processed_today(unit_id: str, today_text: str, scheduled_time: str = "") -> bool:
    entry = get_scheduler_entry(unit_id)
    if str(entry.get("last_auto_run_date") or "").strip() != today_text:
        return False
    round_status = str(entry.get("last_round_status") or "").strip()
    if round_status == "success":
        return True
    current_slot = str(scheduled_time or "").strip()
    recorded_slot = str(entry.get("last_scheduled_time") or "").strip()
    if current_slot:
        # 兼容旧状态或异常状态：若当天失败记录没有写入时间点，允许按当前时间点重新补跑。
        if not recorded_slot:
            return False
        if current_slot != recorded_slot:
            return False
    # 同一时间点已被标记为 skipped，说明该调度单元今天已经明确处理过
    # （例如已由其他模式成功完成），不应在后续轮询中反复重新入队并重复打印跳过日志。
    return round_status in PROCESSED_ROUND_STATUSES


class SmartScheduler:
    """按模式轮次排序执行自动任务。"""

    def __init__(self, config: dict):
        self.config = config or {}
        self.weekly_times = normalize_weekly_times(self.config)

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()  # 保护跨线程访问的共享状态
        self._task_start_times: dict[str, tuple[datetime, dict]] = {}  # name -> (started_at, unit)
        self._timeout_fired: set[str] = set()  # 已触发超时回调的任务名，防重复
        self._watcher_thread: Optional[threading.Thread] = None
        self._on_task_timeout: Optional[Callable] = None

        self._current_mode = ""
        self._current_mode_label = ""
        self._current_round_summary = self._empty_round_summary("")
        self._last_completed_mode = ""
        self._last_completed_summary = self._empty_round_summary("")
        self._followup_event = threading.Event()
        self._pending_followup_units: dict[str, list[dict]] = {}
        self._pending_followup_message = ""
        self._pending_followup_default_mode = ""
        self._pending_followup_selection: list[str] | None = None

    def _refresh_config(self) -> None:
        self.weekly_times = normalize_weekly_times(self.config)

    def should_run_task_today(self, task: dict, now: datetime | None = None) -> bool:
        if not task.get("enabled", True):
            return False
        current = now or datetime.now()
        today = current.date()

        # 检查优化时间周期
        start_str = task.get("optimization_start_date")
        end_str = task.get("optimization_end_date")
        if start_str:
            try:
                if today < date.fromisoformat(str(start_str)):
                    return False
            except ValueError:
                pass
        if end_str:
            try:
                if today > date.fromisoformat(str(end_str)):
                    return False
            except ValueError:
                pass

        weekdays = task.get("weekdays", [0, 1, 2, 3, 4])
        return current.weekday() in weekdays

    def get_task_run_datetime(self, task: dict, current_date: datetime | None = None) -> Optional[datetime]:
        current = current_date or datetime.now()
        if not self.should_run_task_today(task, current):
            return None

        run_time = self.weekly_times.get(str(current.weekday()))
        if not run_time:
            return None

        hour, minute = parse_time(run_time)
        return current.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _build_units(self, tasks: list[dict]) -> list[dict]:
        units = []
        configured_mode = _configured_query_mode(self.config)
        for task in tasks:
            for mode in _extract_modes(task, configured_mode):
                unit = _build_mode_unit(task, mode)
                if unit:
                    units.append(unit)
        return units

    def _task_success_exists(self, task_unit: dict, now: datetime) -> bool:
        original_task = dict(task_unit)
        original_task_id = task_unit.get("_scheduler_original_task_id") or derive_task_id(task_unit)
        original_task["task_id"] = original_task_id
        return is_task_sent_today(original_task, now.date())

    def _due_units(self, tasks: list[dict], now: datetime) -> list[dict]:
        units = self._build_units(tasks)
        today_text = now.date().isoformat()
        due = []

        for unit in units:
            unit_id = unit["_scheduler_unit_id"]
            run_at = self.get_task_run_datetime(unit, now)
            if not run_at or now < run_at:
                continue
            scheduled_time = run_at.strftime("%H:%M")
            if _is_processed_today(unit_id, today_text, scheduled_time):
                continue
            if self._task_success_exists(unit, now):
                mark_processed(
                    unit_id,
                    task_name=unit.get("name", unit_id),
                    mode=unit.get("_scheduler_mode", ""),
                    round_status="skipped",
                    scheduled_time=scheduled_time,
                    target_date=now.date(),
                )
                print(f"[Scheduler] 跳过任务单元 {unit.get('name', unit_id)}，今日已由其他模式成功完成")
                continue
            due.append(unit)

        return due

    def _next_wakeup_seconds(self, tasks: list[dict], now: datetime) -> float:
        candidates: list[float] = []
        today_text = now.date().isoformat()
        for unit in self._build_units(tasks):
            unit_id = unit["_scheduler_unit_id"]
            run_at = self.get_task_run_datetime(unit, now)
            if not run_at:
                continue
            scheduled_time = run_at.strftime("%H:%M")
            if _is_processed_today(unit_id, today_text, scheduled_time):
                continue
            delta = (run_at - now).total_seconds()
            if delta > 0:
                candidates.append(delta)
        if candidates:
            return max(1.0, min(min(candidates), 60.0))
        return 30.0

    def _start_timeout_watcher(self) -> None:
        """启动统一超时监控线程（全局唯一，调度器生命周期内共享）。"""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._watcher_thread = threading.Thread(
            target=self._timeout_watcher_loop, daemon=True, name="scheduler-timeout-watcher"
        )
        self._watcher_thread.start()

    def _timeout_watcher_loop(self) -> None:
        """每 60 秒检查一次所有正在运行的任务，超过 1800 秒则触发超时回调。"""
        while self._running and not self._stop_event.is_set():
            self._stop_event.wait(60)
            if not self._running:
                break
            now = datetime.now()
            with self._state_lock:
                snapshot = list(self._task_start_times.items())
                fired = set(self._timeout_fired)
            for name, (started_at, unit) in snapshot:
                if name in fired:
                    continue
                elapsed = (now - started_at).total_seconds()
                if elapsed >= 1800 and self._on_task_timeout:
                    with self._state_lock:
                        self._timeout_fired.add(name)
                    try:
                        self._on_task_timeout(unit, elapsed)
                    except Exception as e:
                        print(f"[Scheduler] 超时回调执行失败: {e}")

    def _empty_round_summary(self, mode: str) -> dict:
        return {
            "mode": mode,
            "mode_label": MODE_LABELS.get(mode, ""),
            "total": 0,
            "completed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
        }

    def _get_default_mode(self) -> str:
        mode = str(self.config.get("default_mode") or "").strip()
        return mode if mode in MODE_ORDER else "browser"

    def _should_auto_continue_after_failure(self) -> bool:
        return bool(self.config.get("auto_continue_after_default_failure", False))

    def _clear_followup_state(self) -> None:
        self._pending_followup_units = {}
        self._pending_followup_message = ""
        self._pending_followup_default_mode = ""
        self._pending_followup_selection = None
        self._followup_event.clear()

    def _mark_units_skipped(self, units: list[dict], current_date: datetime) -> None:
        scheduled_time = get_weekday_run_time(self.config, current_date.weekday()) or ""
        for unit in units:
            mark_processed(
                unit["_scheduler_unit_id"],
                task_name=unit.get("name", unit["_scheduler_unit_id"]),
                mode=unit.get("_scheduler_mode", ""),
                round_status="skipped",
                scheduled_time=scheduled_time,
                target_date=current_date.date(),
            )

    def _wait_for_followup_selection(
        self,
        default_mode: str,
        mode_groups: dict[str, list[dict]],
        on_status_change: Optional[Callable],
        current_date: datetime,
    ) -> list[str]:
        pending_modes = [mode for mode in MODE_ORDER if mode_groups.get(mode)]
        if not pending_modes:
            return []

        self._pending_followup_units = {mode: list(mode_groups.get(mode) or []) for mode in pending_modes}
        self._pending_followup_default_mode = default_mode
        mode_names = "、".join(MODE_LABELS.get(mode, mode) for mode in pending_modes)
        self._pending_followup_message = f"{MODE_LABELS.get(default_mode, default_mode)}执行后仍有失败，可继续后续模式：{mode_names}"
        self._pending_followup_selection = None
        self._followup_event.clear()

        if on_status_change:
            on_status_change("followup_needed", self._pending_followup_message)

        while self._running and not self._stop_event.is_set():
            if self._followup_event.wait(0.5):
                break

        selected_modes = [mode for mode in (self._pending_followup_selection or []) if mode in pending_modes]
        skipped_modes = [mode for mode in pending_modes if mode not in selected_modes]
        skipped_units = []
        for mode in skipped_modes:
            skipped_units.extend(self._pending_followup_units.get(mode) or [])
        if skipped_units:
            self._mark_units_skipped(skipped_units, current_date)

        self._clear_followup_state()
        return selected_modes

    def _format_summary_message(self, summary: dict, *, finished: bool = False) -> str:
        mode_label = summary.get("mode_label") or "当前模式"
        if finished:
            return (
                f"{mode_label}完成：成功{summary.get('success', 0)} "
                f"失败{summary.get('failed', 0)} 跳过{summary.get('skipped', 0)}"
            )
        return (
            f"{mode_label} {summary.get('completed', 0)}/{summary.get('total', 0)} "
            f"成功{summary.get('success', 0)} 失败{summary.get('failed', 0)} 跳过{summary.get('skipped', 0)}"
        )

    def _run_mode_round(
        self,
        mode: str,
        units: list[dict],
        executor: Callable,
        on_status_change: Optional[Callable],
        current_date: datetime,
        on_round_complete: Optional[Callable] = None,
    ) -> dict:
        ordered_units = sorted(units, key=lambda unit: (1 if _is_fixed_screenshot_unit(unit) else 0, _score_unit(unit)))
        policy = build_query_execution_policy(self.config, mode)
        if policy.use_platform_serial:
            ordered_units = _reorder_units_for_platform_continuity(ordered_units)
        mode_label = MODE_LABELS.get(mode, mode)
        scheduled_time = get_weekday_run_time(self.config, current_date.weekday()) or ""
        self._current_mode = mode
        self._current_mode_label = mode_label
        self._current_round_summary = self._empty_round_summary(mode)
        self._current_round_summary["total"] = len(ordered_units)
        mode_reports: list[dict] = []
        cancelled = False

        start_message = f"{mode_label}开始，本轮 {len(ordered_units)} 个任务"
        print(f"[Scheduler] {start_message}")
        if on_status_change:
            on_status_change("mode_start", start_message)

        try:
            _call_executor_hook(
                executor,
                "begin_mode_round",
                mode,
                ordered_units,
                current_date,
                self.config,
            )
        except Exception as e:
            print(f"[Scheduler] 任务执行器模式开始钩子失败: {e}")

        try:
            for unit in ordered_units:
                if not self._running or self._stop_event.is_set():
                    break

                unit_id = unit["_scheduler_unit_id"]
                task_name = unit.get("name", unit_id)
                display_name = f"{task_name} [{mode_label}]"

                if self._task_success_exists(unit, current_date):
                    mark_processed(
                        unit_id,
                        task_name=task_name,
                        mode=mode,
                        round_status="skipped",
                        scheduled_time=scheduled_time,
                        target_date=current_date.date(),
                    )
                    self._current_round_summary["completed"] += 1
                    self._current_round_summary["skipped"] += 1
                    mode_reports.append({
                        "task_id": unit.get("task_id", ""),
                        "original_task_id": unit.get("_scheduler_original_task_id") or derive_task_id(unit),
                        "scheduler_unit_id": unit_id,
                        "task_name": task_name,
                        "mode": mode,
                        "mode_label": mode_label,
                        "round_status": "skipped",
                        "task_status": "skipped",
                        "task_failure_kind": "",
                        "task_failure_message": "",
                        "duration_seconds": 0.0,
                        "successful_brands": [],
                        "failed_query_details": [],
                    })
                    if on_status_change:
                        on_status_change("running", self._format_summary_message(self._current_round_summary))
                    continue

                with self._state_lock:
                    self._task_start_times[display_name] = (datetime.now(), unit)
                try:
                    print(f"[Scheduler] 执行任务单元: {display_name}")
                    report = executor(unit) or {}
                    report = dict(report)
                    if bool(report.get("_scheduler_cancelled")):
                        cancelled = True
                        print(f"[Scheduler] 任务单元已暂停: {display_name}")
                        if on_status_change:
                            on_status_change("running", "定时任务已关闭，后续任务已暂停")
                        break
                    round_status = str(report.get("task_status") or report.get("round_status") or "success").strip()
                    failure_kind = str(report.get("task_failure_kind") or report.get("failure_kind") or "").strip()
                    duration_seconds = float(report.get("duration_seconds") or 0.0)
                    recovered_manually = bool(report.get("recovered_manually"))
                    report.setdefault("task_id", unit.get("task_id", ""))
                    report.setdefault("task_name", task_name)
                    report["original_task_id"] = unit.get("_scheduler_original_task_id") or derive_task_id(unit)
                    report["scheduler_unit_id"] = unit_id
                    report["mode"] = str(report.get("mode") or mode).strip() or mode
                    report["mode_label"] = mode_label
                    report.setdefault("successful_brands", [])
                    report.setdefault("failed_query_details", [])
                    update_after_run(
                        unit_id,
                        task_name=task_name,
                        mode=mode,
                        round_status=round_status,
                        scheduled_time=scheduled_time,
                        failure_kind=failure_kind,
                        duration_seconds=duration_seconds,
                        recovered_manually=recovered_manually,
                        target_date=current_date.date(),
                    )
                    mode_reports.append(report)
                    self._current_round_summary["completed"] += 1
                    if round_status == "skipped":
                        self._current_round_summary["skipped"] += 1
                    elif round_status == "success":
                        self._current_round_summary["success"] += 1
                    else:
                        self._current_round_summary["failed"] += 1
                    if on_status_change:
                        on_status_change("running", self._format_summary_message(self._current_round_summary))
                except Exception as e:
                    print(f"[Scheduler] 任务执行失败: {e}")
                    update_after_run(
                        unit_id,
                        task_name=task_name,
                        mode=mode,
                        round_status="failed",
                        scheduled_time=scheduled_time,
                        failure_kind="temporary",
                        duration_seconds=0.0,
                        recovered_manually=False,
                        target_date=current_date.date(),
                    )
                    mode_reports.append({
                        "task_id": unit.get("task_id", ""),
                        "original_task_id": unit.get("_scheduler_original_task_id") or derive_task_id(unit),
                        "scheduler_unit_id": unit_id,
                        "task_name": task_name,
                        "mode": mode,
                        "mode_label": mode_label,
                        "round_status": "failed",
                        "task_status": "failed",
                        "task_failure_kind": "temporary",
                        "task_failure_message": str(e),
                        "duration_seconds": 0.0,
                        "successful_brands": [],
                        "failed_query_details": [],
                    })
                    self._current_round_summary["completed"] += 1
                    self._current_round_summary["failed"] += 1
                    if on_status_change:
                        on_status_change("error", f"{display_name} 执行失败: {e}")
                finally:
                    with self._state_lock:
                        self._task_start_times.pop(display_name, None)
                        self._timeout_fired.discard(display_name)
        finally:
            try:
                _call_executor_hook(
                    executor,
                    "end_mode_round",
                    mode,
                    ordered_units,
                    current_date,
                    mode_reports,
                    cancelled,
                )
            except Exception as e:
                print(f"[Scheduler] 任务执行器模式结束钩子失败: {e}")

        if cancelled:
            return {}

        self._last_completed_mode = mode
        self._last_completed_summary = dict(self._current_round_summary)
        completed_message = self._format_summary_message(self._last_completed_summary, finished=True)
        print(f"[Scheduler] {completed_message}")
        if on_status_change:
            on_status_change("mode_complete", completed_message)
        round_payload = {
            "mode": mode,
            "mode_label": mode_label,
            "summary": dict(self._last_completed_summary),
            "reports": mode_reports,
            "scheduled_time": scheduled_time,
            "date": current_date.date().isoformat(),
        }
        if on_round_complete:
            try:
                on_round_complete(round_payload)
            except Exception as e:
                print(f"[Scheduler] 模式完成回调失败: {e}")
        return round_payload

    def _build_cycle_report(
        self,
        due_units: list[dict],
        executed_rounds: list[dict],
        current_date: datetime,
        default_mode: str,
    ) -> dict:
        task_outcomes: dict[str, dict] = {}
        for unit in due_units:
            original_task_id = str(unit.get("_scheduler_original_task_id") or derive_task_id(unit)).strip()
            task_name = str(unit.get("name") or original_task_id).strip() or original_task_id
            task_outcomes.setdefault(original_task_id, build_task_outcome(original_task_id, task_name))

        for round_payload in executed_rounds:
            for report in round_payload.get("reports", []) or []:
                original_task_id = str(report.get("original_task_id") or report.get("task_id") or "").strip()
                if not original_task_id:
                    continue
                task_name = str(report.get("task_name") or original_task_id).strip() or original_task_id
                entry = task_outcomes.setdefault(original_task_id, build_task_outcome(original_task_id, task_name))
                merge_task_outcome_from_report(
                    [entry],
                    report,
                    task_id=original_task_id,
                    task_name=task_name,
                    mode=str(report.get("mode") or round_payload.get("mode") or "").strip(),
                )

        success_count = 0
        failed_count = 0
        skipped_count = 0
        for item in task_outcomes.values():
            status = str(item.get("final_status") or "skipped").strip()
            if status == "success":
                success_count += 1
            elif status == "failed":
                failed_count += 1
            else:
                skipped_count += 1

        return {
            "date": current_date.date().isoformat(),
            "scheduled_time": get_weekday_run_time(self.config, current_date.weekday()) or "",
            "default_mode": default_mode,
            "executed_rounds": executed_rounds,
            "task_outcomes": list(task_outcomes.values()),
            "summary": {
                "total_tasks": len(task_outcomes),
                "success": success_count,
                "failed": failed_count,
                "skipped": skipped_count,
            },
        }

    def _emit_cycle_complete(
        self,
        due_units: list[dict],
        executed_rounds: list[dict],
        current_date: datetime,
        default_mode: str,
        on_cycle_complete: Optional[Callable],
    ) -> None:
        if not on_cycle_complete or not self._running or self._stop_event.is_set():
            return
        try:
            on_cycle_complete(self._build_cycle_report(due_units, executed_rounds, current_date, default_mode))
        except Exception as e:
            print(f"[Scheduler] 调度轮次完成回调失败: {e}")

    def _run_due_modes(
        self,
        due_units: list[dict],
        executor: Callable,
        on_status_change: Optional[Callable],
        current_date: datetime,
        on_round_complete: Optional[Callable] = None,
        on_cycle_complete: Optional[Callable] = None,
    ) -> None:
        mode_groups = {mode: [] for mode in MODE_ORDER}
        for unit in due_units:
            mode_groups.setdefault(unit["_scheduler_mode"], []).append(unit)

        available_modes = [mode for mode in MODE_ORDER if mode_groups.get(mode)]
        if not available_modes:
            return

        default_mode = self._get_default_mode()
        if default_mode not in available_modes:
            default_mode = available_modes[0]
        executed_rounds: list[dict] = []

        round_payload = self._run_mode_round(
            default_mode,
            mode_groups.get(default_mode) or [],
            executor,
            on_status_change,
            current_date,
            on_round_complete=on_round_complete,
        )
        if round_payload:
            executed_rounds.append(round_payload)
        if not self._running or self._stop_event.is_set():
            self._emit_cycle_complete(due_units, executed_rounds, current_date, default_mode, on_cycle_complete)
            return

        later_modes = [
            mode for mode in MODE_ORDER[MODE_ORDER.index(default_mode) + 1:]
            if mode_groups.get(mode)
        ]
        if not later_modes:
            self._emit_cycle_complete(due_units, executed_rounds, current_date, default_mode, on_cycle_complete)
            return

        summary = dict(self._last_completed_summary)
        if int(summary.get("failed", 0) or 0) <= 0:
            skip_units = []
            for mode in later_modes:
                skip_units.extend(mode_groups.get(mode) or [])
            if skip_units:
                self._mark_units_skipped(skip_units, current_date)
            self._emit_cycle_complete(due_units, executed_rounds, current_date, default_mode, on_cycle_complete)
            return

        if self._should_auto_continue_after_failure():
            for mode in later_modes:
                round_payload = self._run_mode_round(
                    mode,
                    mode_groups.get(mode) or [],
                    executor,
                    on_status_change,
                    current_date,
                    on_round_complete=on_round_complete,
                )
                if round_payload:
                    executed_rounds.append(round_payload)
                if not self._running or self._stop_event.is_set():
                    break
            self._emit_cycle_complete(due_units, executed_rounds, current_date, default_mode, on_cycle_complete)
            return

        selected_modes = self._wait_for_followup_selection(
            default_mode,
            {mode: mode_groups.get(mode) or [] for mode in later_modes},
            on_status_change,
            current_date,
        )
        for mode in later_modes:
            if mode not in selected_modes:
                continue
            round_payload = self._run_mode_round(
                mode,
                mode_groups.get(mode) or [],
                executor,
                on_status_change,
                current_date,
                on_round_complete=on_round_complete,
            )
            if round_payload:
                executed_rounds.append(round_payload)
            if not self._running or self._stop_event.is_set():
                break
        self._emit_cycle_complete(due_units, executed_rounds, current_date, default_mode, on_cycle_complete)

    def run(
        self,
        tasks: list[dict],
        executor: Callable,
        on_status_change: Optional[Callable] = None,
        on_round_complete: Optional[Callable] = None,
        on_cycle_complete: Optional[Callable] = None,
    ):
        self._running = True
        self._stop_event.clear()
        self._refresh_config()
        self._start_timeout_watcher()

        started_at = datetime.now()
        units = self._build_units(tasks)
        print(f"[Scheduler] 调度器启动，共 {len(units)} 个自动任务单元")

        while self._running and not self._stop_event.is_set():
            self._refresh_config()
            now = datetime.now()
            due_units = self._due_units(tasks, now)
            if due_units:
                self._run_due_modes(
                    due_units,
                    executor,
                    on_status_change,
                    now,
                    on_round_complete=on_round_complete,
                    on_cycle_complete=on_cycle_complete,
                )
                self._current_mode = ""
                self._current_mode_label = ""
                self._current_round_summary = self._empty_round_summary("")

            sleep_seconds = self._next_wakeup_seconds(tasks, datetime.now())
            self._stop_event.wait(sleep_seconds)

        self._clear_followup_state()
        self._current_mode = ""
        self._current_mode_label = ""
        print("[Scheduler] 调度器已停止")
        if on_status_change:
            on_status_change("stopped", "调度器已停止")

    def start(
        self,
        tasks: list[dict],
        executor: Callable,
        on_status_change: Optional[Callable] = None,
        on_task_timeout: Optional[Callable] = None,
        on_round_complete: Optional[Callable] = None,
        on_cycle_complete: Optional[Callable] = None,
    ):
        if self._running:
            print("[Scheduler] 调度器已在运行")
            return

        self._on_task_timeout = on_task_timeout
        self._thread = threading.Thread(
            target=self.run,
            args=(tasks, executor, on_status_change, on_round_complete, on_cycle_complete),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        if not self._running:
            return

        print("[Scheduler] 正在停止调度器...")
        self._running = False
        self._pending_followup_selection = []
        self._followup_event.set()
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def should_stop(self) -> bool:
        return (not self._running) or self._stop_event.is_set()

    def continue_with_modes(self, modes: list[str] | str):
        if isinstance(modes, str):
            selected_modes = [modes]
        else:
            selected_modes = list(modes or [])
        self._pending_followup_selection = [mode for mode in selected_modes if mode in MODE_ORDER]
        self._followup_event.set()

    def skip_followup_modes(self):
        self._pending_followup_selection = []
        self._followup_event.set()

    def get_running_tasks(self) -> dict:
        now = datetime.now()
        with self._state_lock:
            return {
                name: (now - started_at).total_seconds()
                for name, (started_at, _) in self._task_start_times.items()
            }

    def get_status(self) -> dict:
        self._refresh_config()
        now = datetime.now()
        next_slots = {}
        for weekday in range(7):
            run_time = self.weekly_times.get(str(weekday))
            if run_time:
                next_slots[str(weekday)] = run_time

        with self._state_lock:
            current_mode = self._current_mode
            current_mode_label = self._current_mode_label
            current_round_summary = dict(self._current_round_summary)
            last_completed_mode = self._last_completed_mode
            last_completed_summary = dict(self._last_completed_summary)

        return {
            "running": self._running,
            "weekly_times": next_slots,
            "current_weekday": now.weekday(),
            "current_date": now.date().isoformat(),
            "current_mode": current_mode,
            "current_mode_label": current_mode_label,
            "current_round_summary": current_round_summary,
            "last_completed_mode": last_completed_mode,
            "last_completed_summary": last_completed_summary,
            "default_mode": self._get_default_mode(),
            "awaiting_followup": bool(self._pending_followup_units),
            "pending_followup_message": self._pending_followup_message,
            "pending_followup_modes": [
                mode for mode in MODE_ORDER
                if self._pending_followup_units.get(mode)
            ],
        }
