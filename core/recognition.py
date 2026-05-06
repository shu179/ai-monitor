"""
识别模式：低频剪切板截图监听 + AI 品牌识别 + 按任务组批次归类
"""

import copy
import hashlib
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageGrab

from core.ai_runtime import detect_brands_from_image
from core.app_paths import resolve_app_path
from core.daily_task_state import (
    apply_task_keyword_updates,
    build_task_state_extra,
    derive_task_id,
    get_task_day_status,
    write_task_status,
)
from core.history import (
    record as history_record,
)
from core.local_ocr import (
    build_match_summary,
    dump_provider_status,
    extract_text_from_image,
    is_ai_fallback_enabled,
    match_candidate_brands,
)
from core.notifier import WeComNotifier
from core.notification_retry import (
    enqueue_wecom_notification,
    start_notification_retry_worker,
)
from core.time_utils import local_now, local_today


PLATFORM_INFERENCE_TOKENS = {
    "doubao": (
        ("豆包", 8),
        ("doubao", 8),
        ("发送消息或输入/选择技能", 28),
        ('发送消息或输入"/"选择技能', 28),
        ("发送消息或输入 / 选择技能", 28),
        ("选择技能", 12),
        ("帮我写作", 12),
        ("图像生成", 12),
        ("视频生成", 12),
        ("编程", 6),
        ("翻译", 6),
        ("发送消息或输入", 10),
    ),
    "deepseek": (
        ("给deepseek发送消息", 24),
        ("给 deepseek 发送消息", 24),
        ("deepseek", 4),
        ("智能搜索", 4),
        ("深度思考", 3),
    ),
    "ark_deepseek": (
        ("方舟", 16),
        ("火山方舟", 20),
        ("ark", 10),
        ("volces", 10),
    ),
    "kimi": (
        ("kimi", 24),
        ("moonshot", 16),
        ("问点难的，让我多想一步", 10),
        ("k2.5思考", 3),
        ("k2.5 思考", 3),
    ),
    "tongyi": (
        ("千问", 22),
        ("向千问提问", 26),
        ("任务助理", 22),
        ("深度研究", 4),
        ("通义", 16),
        ("qwen", 12),
    ),
    "wenxin": (
        ("文心", 18),
        ("一言", 12),
        ("自动适配需求", 4),
        ("复杂问题自动深析", 4),
        ("思考·自动", 2),
        ("思考-自动", 2),
    ),
    "yuanbao": (
        ("元宝", 18),
        ("腾讯元宝", 24),
        ("hunyuan", 12),
        ("有问题，尽管问", 26),
        ("shift+enter换行", 28),
        ("shift+enter 换行", 28),
        ("工具", 12),
        ("联网搜索", 2),
    ),
    "chatgpt": (("chatgpt", 24), ("openai", 20), ("gpt", 12)),
    "claude": (("claude", 24), ("anthropic", 20)),
    "gemini": (("gemini", 24), ("google ai", 20), ("谷歌", 14)),
    "perplexity": (("perplexity", 24),),
}

PLATFORM_STRONG_HINTS = {
    "doubao": (
        "发送消息或输入/选择技能",
        '发送消息或输入"/"选择技能',
        "发送消息或输入 / 选择技能",
        "选择技能",
        "帮我写作",
        "图像生成",
        "视频生成",
    ),
    "deepseek": (
        "给deepseek发送消息",
        "给 deepseek 发送消息",
    ),
    "yuanbao": (
        "有问题，尽管问",
        "shift+enter换行",
        "shift+enter 换行",
    ),
    "tongyi": (
        "向千问提问",
        "任务助理",
    ),
    "wenxin": (
        "文心",
        "自动适配需求",
    ),
    "kimi": (
        "kimi",
        "moonshot",
    ),
}

INPUT_BUBBLE_HINTS = (
    "发送", "输入", "问", "追问", "深度思考", "联网搜索", "上传", "附件",
    "chatgpt", "claude", "gemini", "deepseek", "豆包", "通义", "文心", "元宝", "kimi",
)

PLATFORM_ID_ALIASES = {
    "豆包": "doubao",
    "doubao": "doubao",
    "deepseek": "deepseek",
    "DeepSeek": "deepseek",
    "方舟 DeepSeek": "ark_deepseek",
    "ark_deepseek": "ark_deepseek",
    "ark deepseek": "ark_deepseek",
    "Kimi": "kimi",
    "kimi": "kimi",
    "元宝": "yuanbao",
    "yuanbao": "yuanbao",
    "通义千问": "tongyi",
    "通义": "tongyi",
    "tongyi": "tongyi",
    "qwen": "tongyi",
    "文心一言": "wenxin",
    "文心": "wenxin",
    "wenxin": "wenxin",
    "ernie": "wenxin",
    "ChatGPT": "chatgpt",
    "chatgpt": "chatgpt",
    "Claude": "claude",
    "claude": "claude",
    "Gemini": "gemini",
    "gemini": "gemini",
    "Perplexity": "perplexity",
    "perplexity": "perplexity",
}

PLATFORM_DISPLAY = {
    "local_model": "本地模型",
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "ark_deepseek": "方舟 DeepSeek",
    "kimi": "Kimi",
    "yuanbao": "元宝",
    "tongyi": "通义千问",
    "wenxin": "文心一言",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
}


class ClipboardRecognitionManager:
    """识别模式后台管理器。"""

    _instance = None
    _active_listener = None
    _active_listener_lock = threading.Lock()

    def __init__(self, config_getter, on_batch_ready=None, on_send_complete=None,
                 on_mode_change=None, on_manual_switch_required=None,
                 on_manual_state_change=None, on_round_complete=None):
        self._config_getter = config_getter
        self._on_batch_ready = on_batch_ready
        self._on_send_complete = on_send_complete
        self._on_mode_change = on_mode_change
        self._on_manual_switch_required = on_manual_switch_required
        self._on_manual_state_change = on_manual_state_change
        self._on_round_complete = on_round_complete

        self._stop_event = threading.Event()
        self._poll_thread = None
        self._recognize_thread = None
        self._send_thread = None
        self._running = False

        self._seen_hashes = deque(maxlen=200)
        self._seen_set = set()
        self._buffers = {}
        self._pending_batches = {}
        self._recognition_queue = queue.Queue(maxsize=self._queue_capacity())
        self._send_queue = queue.Queue()
        self._active_send_batch = None
        self._lock = threading.RLock()
        self._recognizing = False
        self._sending = False
        self._clipboard_armed = True
        self._startup_clipboard_hash = None
        self._reference_clipboard_armed = True
        self._startup_reference_text_hash = None
        self._seen_reference_hashes = deque(maxlen=200)
        self._seen_reference_set = set()
        self._last_image_seen_at = None
        self._idle_reminded = False
        self._work_started = False
        self._matched_in_cycle = False
        self._manual_index = 0
        self._guide_status_text = ""
        self._suppressed_tasks = set()
        self._active_capture_platform = ""

        self._save_dir = resolve_app_path("screenshots/recognition")
        self._save_dir.mkdir(parents=True, exist_ok=True)
        ClipboardRecognitionManager._instance = self

    @classmethod
    def get_instance(cls):
        return cls._instance

    def start(self):
        if self._running:
            return
        self._claim_active_listener()
        enabled_tasks = self._get_enabled_tasks()
        self._join_worker_threads()
        self._running = True
        self._stop_event.clear()
        with self._lock:
            next_capacity = self._queue_capacity()
            current_capacity = getattr(self._recognition_queue, "maxsize", 0) or 0
            if current_capacity != next_capacity and self._recognition_queue.empty():
                self._recognition_queue = queue.Queue(maxsize=next_capacity)
            self._seen_hashes = deque(maxlen=200)
            self._seen_set = set()
            self._buffers = {}
            self._pending_batches = {}
            self._active_send_batch = None
            # 清空队列而非替换对象——旧线程持有同一引用，替换会导致竞态
            self._drain_queue(self._recognition_queue)
            self._drain_queue(self._send_queue)
            self._recognizing = False
            self._sending = False
            self._idle_reminded = False
            self._work_started = False
            self._matched_in_cycle = False
            self._reference_clipboard_armed = True
            self._startup_reference_text_hash = None
            self._seen_reference_hashes = deque(maxlen=200)
            self._seen_reference_set = set()
            self._last_image_seen_at = None
            self._manual_index = 0
            self._guide_status_text = ""
            self._suppressed_tasks = set()
            self._active_capture_platform = ""
        self._prime_clipboard_baseline()
        self._prime_reference_clipboard_baseline()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._recognize_thread = threading.Thread(target=self._recognize_loop, daemon=True)
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._poll_thread.start()
        self._recognize_thread.start()
        self._send_thread.start()
        start_notification_retry_worker(
            pause_callback=self._should_pause_notification_retry,
        )
        if self._dom_render_mode_enabled():
            self._set_guide_status("DOM 文本识别中，复制回答文本后命中品牌会自动发送")
        elif self._safe_mode_ocr_enabled():
            self._set_guide_status("本地 OCR 自动识别中，识别成功后会自动切到下一个关键词")
        else:
            self._set_guide_status("等待新的剪切板截图…")
        self._notify_mode_change("recognition", "识别模式已启动，仅监听启动后的新截图")
        self._notify_manual_state_change()
        task_preview = "、".join(task.get("name", "") for task in enabled_tasks[:3]) or "无任务"
        if len(enabled_tasks) > 3:
            task_preview += f" 等 {len(enabled_tasks)} 个任务"
        print(f"[Recognition] 识别模式已启动，当前监听任务: {task_preview}")
        if enabled_tasks:
            debug_preview = ", ".join(
                f"{task.get('name', '')}(batch={int(task.get('recognition_batch_size', 1) or 1)})"
                for task in enabled_tasks[:5]
            )
            print(f"[Recognition] 当前监听任务批次: {debug_preview}")
        print(f"[Recognition] 本地 OCR 配置: {dump_provider_status(self._config_getter() or {})}")

    def _should_pause_notification_retry(self) -> bool:
        return bool(
            self._sending
            or (self._send_queue is not None and not self._send_queue.empty())
        )

    def stop(self):
        if not self._running and not self._has_live_workers():
            self._release_active_listener()
            return
        self._running = False
        self._stop_event.set()
        try:
            self._recognition_queue.put_nowait(None)
        except Exception:
            pass
        try:
            self._send_queue.put_nowait(None)
        except Exception:
            pass
        self._join_worker_threads()
        with self._lock:
            self._buffers.clear()
            self._pending_batches.clear()
            self._suppressed_tasks.clear()
        self._release_active_listener()
        print("[Recognition] 识别模式已停止")

    def _claim_active_listener(self) -> None:
        previous = None
        with self.__class__._active_listener_lock:
            if self.__class__._active_listener is self:
                return
            previous = self.__class__._active_listener
            self.__class__._active_listener = self
        if previous is not None and previous is not self:
            try:
                print("[Recognition] 检测到另一识别监听实例正在运行，已自动停止旧实例")
                previous.stop()
            except Exception as exc:
                print(f"[Recognition] 停止旧识别监听实例失败: {exc}")

    def _release_active_listener(self) -> None:
        with self.__class__._active_listener_lock:
            if self.__class__._active_listener is self:
                self.__class__._active_listener = None

    def export_runtime_state(self) -> dict:
        """导出当前运行态，供临时测试结束后恢复。"""
        with self._lock:
            return {
                "seen_hashes": list(self._seen_hashes),
                "buffers": copy.deepcopy(self._buffers),
                "pending_batches": copy.deepcopy(self._pending_batches),
                "active_send_batch": copy.deepcopy(self._active_send_batch),
                "manual_index": int(self._manual_index or 0),
                "guide_status_text": str(self._guide_status_text or ""),
                "last_image_seen_at": self._last_image_seen_at,
                "idle_reminded": bool(self._idle_reminded),
                "work_started": bool(self._work_started),
                "matched_in_cycle": bool(self._matched_in_cycle),
                "seen_reference_hashes": list(self._seen_reference_hashes),
                "reference_clipboard_armed": bool(self._reference_clipboard_armed),
                "startup_reference_text_hash": self._startup_reference_text_hash,
                "active_capture_platform": self._active_capture_platform,
            }

    def import_runtime_state(self, state: dict | None) -> None:
        """恢复先前导出的运行态。"""
        payload = state if isinstance(state, dict) else {}
        with self._lock:
            self._seen_hashes = deque(payload.get("seen_hashes", []), maxlen=200)
            self._seen_set = set(self._seen_hashes)
            self._buffers = copy.deepcopy(payload.get("buffers", {}))
            self._pending_batches = copy.deepcopy(payload.get("pending_batches", {}))
            self._active_send_batch = copy.deepcopy(payload.get("active_send_batch"))
            self._manual_index = max(0, int(payload.get("manual_index", 0) or 0))
            self._guide_status_text = str(payload.get("guide_status_text", "") or "")
            self._last_image_seen_at = payload.get("last_image_seen_at")
            self._idle_reminded = bool(payload.get("idle_reminded", False))
            self._work_started = bool(payload.get("work_started", False))
            self._matched_in_cycle = bool(payload.get("matched_in_cycle", False))
            self._seen_reference_hashes = deque(payload.get("seen_reference_hashes", []), maxlen=200)
            self._seen_reference_set = set(self._seen_reference_hashes)
            self._reference_clipboard_armed = bool(payload.get("reference_clipboard_armed", True))
            self._startup_reference_text_hash = payload.get("startup_reference_text_hash")
            self._active_capture_platform = self._normalize_platform_id(payload.get("active_capture_platform", ""))
        self._notify_manual_state_change()

    def _safe_mode_ocr_enabled(self) -> bool:
        config = self._config_getter() or {}
        recognition_cfg = config.get("recognition", {})
        return bool(recognition_cfg.get("safe_mode_ocr_enabled", True))

    def _dom_render_mode_enabled(self) -> bool:
        config = self._config_getter() or {}
        recognition_cfg = config.get("recognition", {})
        return bool(recognition_cfg.get("dom_render_mode", False))

    def _auto_send_recognition_batches_enabled(self) -> bool:
        return self._safe_mode_ocr_enabled() or self._dom_render_mode_enabled()

    def set_active_capture_platform(self, platform_name: str) -> None:
        normalized = self._normalize_platform_id(platform_name)
        with self._lock:
            self._active_capture_platform = normalized

    def _get_active_capture_platform(self) -> str:
        with self._lock:
            return self._normalize_platform_id(self._active_capture_platform)

    def _is_manual_test_task(self, task_payload: dict | None) -> bool:
        return str((task_payload or {}).get("_daily_state_source") or "").strip() == "manual_test"

    def _make_suppressed_task_key(self, task_name: str) -> str:
        return f"{str(task_name or '').strip()}#{local_today().isoformat()}"

    def _is_task_suppressed_for_today(self, task_name: str) -> bool:
        key = self._make_suppressed_task_key(task_name)
        with self._lock:
            return key in self._suppressed_tasks

    def _get_relevant_daily_status_payload(self, task_payload: dict | None) -> dict:
        task_name = str((task_payload or {}).get("name") or "").strip()
        task_ref = {
            "task_id": str((task_payload or {}).get("task_id") or "").strip(),
            "name": task_name,
        }
        status = get_task_day_status(task_ref)
        if self._is_manual_test_task(task_payload):
            return {
                "status": str(status.get("test_status") or "").strip(),
                "source": str(status.get("test_source") or "").strip(),
                "message": str(status.get("test_message") or "").strip(),
                "extra": dict(status.get("test_extra") or {}) if isinstance(status.get("test_extra"), dict) else {},
                "scope": "test",
            }
        return {
            "status": str(status.get("status") or "").strip(),
            "source": str(status.get("source") or "").strip(),
            "message": str(status.get("message") or "").strip(),
            "extra": dict(status.get("official_extra") or {}) if isinstance(status.get("official_extra"), dict) else {},
            "scope": "official",
        }

    def _is_task_completed_for_today(self, task_payload: dict | None) -> bool:
        task_name = str((task_payload or {}).get("name") or "").strip()
        if task_name and self._is_task_suppressed_for_today(task_name):
            return True
        status_payload = self._get_relevant_daily_status_payload(task_payload)
        current_status = str(status_payload.get("status") or "").strip()
        scope = str(status_payload.get("scope") or "official").strip() or "official"
        if scope == "test":
            return current_status == "success"
        return current_status in {"success", "sent"}

    def _persist_task_progress_status(
        self,
        task_payload: dict | None,
        *,
        current_count: int,
        batch_size: int,
        historical_count: int,
        status_message: str,
    ) -> None:
        if not task_payload or self._is_task_completed_for_today(task_payload):
            return

        status_payload = self._get_relevant_daily_status_payload(task_payload)
        current_status = str(status_payload.get("status") or "").strip()
        scope = str(status_payload.get("scope") or "official").strip() or "official"
        completed_statuses = {"success"} if scope == "test" else {"success", "sent"}
        if current_status in completed_statuses:
            return
        if int(current_count or 0) > 0 or int(historical_count or 0) > 0:
            current_status = "running"
        elif current_status not in {"pending", "running"}:
            current_status = "running" if int(current_count or 0) > 0 or int(historical_count or 0) > 0 else "pending"

        extra = dict(status_payload.get("extra") or {})
        write_task_status(
            task_payload,
            status=current_status,
            source=str((task_payload or {}).get("_daily_state_source") or "recognition").strip() or "recognition",
            scope=str(status_payload.get("scope") or "official").strip() or "official",
            message=str(status_message or "").strip(),
            extra=build_task_state_extra(
                brands=list((task_payload or {}).get("brands") or []),
                image_count=max(0, int(current_count or 0)) + max(0, int(historical_count or 0)),
                fixed_screenshot_target=max(1, int(batch_size or 1)),
                completed_by_quota=(max(0, int(current_count or 0)) + max(0, int(historical_count or 0))) >= max(1, int(batch_size or 1)),
                task_failure_kind="",
            ),
        )

    def _add_runtime_completed_slots_from_batch(
        self,
        completed: dict[tuple[str, str, str], set[str]],
        batch: dict | None,
        *,
        task_name_hint: str = "",
    ) -> None:
        batch_payload = dict(batch or {})
        task_name = str(batch_payload.get("task_name") or task_name_hint or "").strip()
        if not task_name:
            return
        image_count = max(0, int(self._batch_image_count(batch_payload) or 0))
        if image_count <= 0:
            return
        slots = self._expand_matched_pair_slots(batch_payload.get("matched_pairs") or [])
        if not slots:
            return
        for slot in slots[:image_count]:
            keyword = str((slot or {}).get("keyword") or "").strip()
            brand = str((slot or {}).get("brand") or "").strip()
            platforms = [
                self._normalize_platform_id(platform)
                for platform in ((slot or {}).get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            if not keyword or not platforms:
                continue
            keyword_key, brand_key = self._normalize_keyword_brand_pair(keyword, brand)
            if not keyword_key:
                continue
            completed.setdefault((task_name, keyword_key, brand_key), set()).update(platforms)

    def _collect_runtime_completed_slot_map(self) -> dict[tuple[str, str, str], set[str]]:
        completed: dict[tuple[str, str, str], set[str]] = {}
        with self._lock:
            buffers_snapshot = copy.deepcopy(self._buffers)
            pending_snapshot = copy.deepcopy(list(self._pending_batches.values()))
            active_send_batch = copy.deepcopy(self._active_send_batch)

        for task_name, items in dict(buffers_snapshot or {}).items():
            for item in list(items or []):
                item_payload = dict(item or {})
                path_text = str(item_payload.get("path") or "").strip()
                source_text = str(item_payload.get("source_text") or "").strip()
                ocr_text = str(item_payload.get("ocr_text") or "").strip()
                image_items = []
                if path_text or source_text:
                    image_items.append({
                        "path": path_text,
                        "ocr_text": ocr_text,
                        "source_text": source_text,
                    })
                batch_like = {
                    "task_name": str(task_name or "").strip(),
                    "matched_pairs": item_payload.get("matched_pairs") or [],
                    "image_paths": [path_text] if path_text else [],
                }
                if image_items:
                    batch_like["image_items"] = image_items
                self._add_runtime_completed_slots_from_batch(completed, batch_like)

        for batch in pending_snapshot:
            self._add_runtime_completed_slots_from_batch(completed, dict(batch or {}))

        queued_batches: list[dict] = []
        try:
            with self._send_queue.mutex:
                queued_batches = copy.deepcopy([
                    item for item in list(self._send_queue.queue) if isinstance(item, dict)
                ])
        except Exception:
            queued_batches = []
        for batch in queued_batches:
            self._add_runtime_completed_slots_from_batch(completed, dict(batch or {}))

        if isinstance(active_send_batch, dict):
            self._add_runtime_completed_slots_from_batch(completed, active_send_batch)
        return completed

    def _runtime_completed_platforms_for_entry(
        self,
        entry: dict,
        task: dict,
        runtime_completed: dict[tuple[str, str, str], set[str]],
    ) -> set[str]:
        task_name = str((task or {}).get("name") or "").strip()
        keyword = str((entry or {}).get("keyword") or "").strip()
        if not task_name or not keyword:
            return set()
        keyword_key, _ = self._normalize_keyword_brand_pair(keyword, "")
        if not keyword_key:
            return set()
        brands = [
            str(brand or "").strip()
            for brand in ((entry or {}).get("brands", task.get("brands", [])) or [])
            if str(brand or "").strip()
        ]
        if not brands:
            brands = [str((task or {}).get("primary_brand") or "").strip()]

        completed_platforms: set[str] = set()
        for brand in brands:
            _, brand_key = self._normalize_keyword_brand_pair(keyword, brand)
            completed_platforms.update(runtime_completed.get((task_name, keyword_key, brand_key), set()))
        completed_platforms.update(runtime_completed.get((task_name, keyword_key, ""), set()))
        return {
            self._normalize_platform_id(platform)
            for platform in completed_platforms
            if self._normalize_platform_id(platform)
        }

    def _build_keyword_guide_items(self, tasks: list[dict]) -> list[dict]:
        items = []
        runtime_completed = self._collect_runtime_completed_slot_map()
        for task in tasks:
            force_show_all_entries = self._is_manual_test_task(task)
            if not force_show_all_entries and self._is_task_completed_for_today(task):
                continue
            daily_state = self._get_task_daily_shared_state(task)
            progress = dict(daily_state.get("progress") or {})
            sent_count_by_brand = dict(progress.get("historical_image_count_by_brand", {}) or {})
            for brand, count in (progress.get("recognition_image_count_by_brand", {}) or {}).items():
                sent_count_by_brand[brand] = int(sent_count_by_brand.get(brand, 0) or 0) + int(count or 0)
            keyword_states = dict((daily_state.get("status") or {}).get("keyword_states") or {})
            all_entries = list(task.get("guide_keywords", []) or [])

            visible_entries = []
            waiting_entries = []
            for entry in all_entries:
                runtime_completed_platforms = (
                    set()
                    if force_show_all_entries
                    else self._runtime_completed_platforms_for_entry(entry, task, runtime_completed)
                )
                official_pending_statuses = self._get_pending_entry_statuses(
                    entry,
                    task,
                )
                pending_statuses = self._get_pending_entry_statuses(
                    entry,
                    task,
                    runtime_completed_platforms=runtime_completed_platforms,
                )
                if not pending_statuses and not force_show_all_entries:
                    if official_pending_statuses and runtime_completed_platforms:
                        waiting_entries.append({
                            **entry,
                            "pending_platforms": [],
                            "failed_platforms": [],
                            "pending_only_platforms": [],
                            "pending_statuses": [],
                            "runtime_completed_platforms": sorted(runtime_completed_platforms),
                            "runtime_waiting": True,
                        })
                    continue
                pending_platforms = [item.get("platform", "") for item in pending_statuses if str(item.get("platform") or "").strip()]
                failed_platforms = [item.get("platform", "") for item in pending_statuses if item.get("failed")]
                pending_only_platforms = [item.get("platform", "") for item in pending_statuses if not item.get("failed")]
                visible_entries.append({
                    **entry,
                    "pending_platforms": pending_platforms,
                    "failed_platforms": failed_platforms,
                    "pending_only_platforms": pending_only_platforms,
                    "pending_statuses": pending_statuses,
                    "runtime_completed_platforms": sorted(runtime_completed_platforms),
                    "runtime_waiting": False,
                })

            if not visible_entries and waiting_entries:
                visible_entries = waiting_entries

            visible_entries.sort(
                key=lambda item: (
                    0 if item.get("failed_platforms") else 1,
                    str(item.get("keyword") or "").strip(),
                )
            )

            total = len(visible_entries)
            for idx, entry in enumerate(visible_entries):
                brands = list(entry.get("brands", task.get("brands", [])))
                primary_brand = next((brand for brand in brands if str(brand or "").strip()), task.get("primary_brand", ""))
                pending_platforms = list(entry.get("pending_platforms", []))
                detail_parts = []
                failed_platforms = list(entry.get("failed_platforms", []))
                pending_only_platforms = list(entry.get("pending_only_platforms", []))
                runtime_completed_platforms = list(entry.get("runtime_completed_platforms", []))
                if failed_platforms:
                    detail_parts.append(
                        "失败平台：" + "、".join(
                            self._display_platform_name(platform)
                            for platform in failed_platforms
                            if str(platform or "").strip()
                        )
                    )
                if pending_only_platforms:
                    detail_parts.append(
                        "未完成平台：" + "、".join(
                            self._display_platform_name(platform)
                            for platform in pending_only_platforms
                            if str(platform or "").strip()
                        )
                    )
                if bool(entry.get("runtime_waiting")) and runtime_completed_platforms:
                    detail_parts.append(
                        "已捕获平台：" + "、".join(
                            self._display_platform_name(platform)
                            for platform in runtime_completed_platforms
                            if str(platform or "").strip()
                        ) + "，等待发送"
                    )
                progress_platforms = [
                    self._normalize_platform_id(platform)
                    for platform in (pending_platforms or entry.get("platforms") or [])
                    if self._normalize_platform_id(platform)
                ]
                if not progress_platforms:
                    progress_platforms = [
                        self._normalize_platform_id(platform)
                        for platform in (task.get("platform_candidates") or [])
                        if self._normalize_platform_id(platform)
                    ]
                scope_platform = progress_platforms[0] if progress_platforms else ""
                completed_keyword_count = 0
                total_keyword_count = 0
                for progress_entry in all_entries:
                    progress_keyword = str((progress_entry or {}).get("keyword") or "").strip()
                    entry_platforms = [
                        self._normalize_platform_id(platform)
                        for platform in ((progress_entry or {}).get("platforms") or task.get("platform_candidates") or [])
                        if self._normalize_platform_id(platform)
                    ]
                    entry_platforms = list(dict.fromkeys(entry_platforms))
                    if scope_platform and scope_platform not in entry_platforms:
                        continue
                    if not scope_platform and entry_platforms:
                        continue
                    total_keyword_count += 1
                    progress_state = dict(keyword_states.get(progress_keyword) or {})
                    completion_platforms = entry_platforms or ([scope_platform] if scope_platform else [])
                    if self._keyword_state_complete_for_platforms(progress_state, completion_platforms):
                        completed_keyword_count += 1

                items.append({
                    "task_name": task.get("name", ""),
                    "keyword": entry.get("keyword", ""),
                    "brands": brands,
                    "platforms": pending_platforms,
                    "detail_text": "；".join(part for part in detail_parts if part),
                    "batch_size": int(task.get("recognition_batch_size", 1) or 1),
                    "is_last_in_task": idx == total - 1,
                    "sent_count": int(sent_count_by_brand.get(primary_brand, 0) or 0),
                    "historical_count": int(progress.get("historical_screenshot_count", 0) or 0),
                    "completed_keyword_count": int(completed_keyword_count),
                    "total_keyword_count": int(total_keyword_count),
                    "progress_platform": scope_platform,
                })
        return items

    def _build_daily_progress_from_status(self, status: dict) -> dict:
        keyword_states = dict(status.get("keyword_states") or {})
        historical_screenshot_paths = []
        historical_query_platforms = []
        historical_image_count_by_brand: dict[str, int] = {}
        completed_keywords = []
        seen_paths = set()
        seen_platforms = set()

        for keyword, state in keyword_states.items():
            state_payload = dict(state or {})
            platform_states = dict(state_payload.get("platform_states") or {})
            if platform_states:
                completed_any = False
                for platform_name, platform_state in platform_states.items():
                    platform_payload = dict(platform_state or {})
                    if not bool(platform_payload.get("run_success")) or not bool(platform_payload.get("screenshot_saved")):
                        continue
                    completed_any = True
                    brand_text = str(platform_payload.get("brand") or state_payload.get("brand") or "").strip()
                    if brand_text:
                        historical_image_count_by_brand[brand_text] = int(historical_image_count_by_brand.get(brand_text, 0) or 0) + 1
                    image_path = str(platform_payload.get("image_path") or "").strip()
                    if image_path and image_path not in seen_paths and Path(image_path).exists():
                        seen_paths.add(image_path)
                        historical_screenshot_paths.append(image_path)
                    platform_text = self._normalize_platform_id(platform_name or platform_payload.get("platform", ""))
                    if platform_text and platform_text not in seen_platforms:
                        seen_platforms.add(platform_text)
                        historical_query_platforms.append(platform_text)
                keyword_text = str(keyword or state_payload.get("keyword") or "").strip()
                if completed_any and keyword_text and keyword_text not in completed_keywords:
                    completed_keywords.append(keyword_text)
                continue
            if not bool(state_payload.get("run_success")) or not bool(state_payload.get("screenshot_saved")):
                continue
            keyword_text = str(keyword or state_payload.get("keyword") or "").strip()
            if keyword_text and keyword_text not in completed_keywords:
                completed_keywords.append(keyword_text)

            brand_text = str(state_payload.get("brand") or "").strip()
            if brand_text:
                historical_image_count_by_brand[brand_text] = int(historical_image_count_by_brand.get(brand_text, 0) or 0) + 1

            image_path = str(state_payload.get("image_path") or "").strip()
            if image_path and image_path not in seen_paths and Path(image_path).exists():
                seen_paths.add(image_path)
                historical_screenshot_paths.append(image_path)

            platform_text = self._normalize_platform_id(state_payload.get("platform", ""))
            if platform_text and platform_text not in seen_platforms:
                seen_platforms.add(platform_text)
                historical_query_platforms.append(platform_text)

        return {
            "completed_keywords": completed_keywords,
            "historical_screenshot_paths": historical_screenshot_paths,
            "historical_query_platforms": historical_query_platforms,
            "historical_screenshot_count": len(historical_screenshot_paths),
            "historical_image_count_by_brand": historical_image_count_by_brand,
            "recognition_image_count_by_brand": {},
        }

    def _extract_completed_send_state_from_pool(self, pool: dict) -> tuple[list[str], list[str]]:
        keyword_states = dict((pool or {}).get("keywords") or {})
        screenshot_paths: list[str] = []
        query_platforms: list[str] = []
        seen_paths: set[str] = set()
        seen_platforms: set[str] = set()

        def collect(state: dict, platform_hint: str = "") -> None:
            state_payload = dict(state or {})
            if not bool(state_payload.get("run_success")) or not bool(state_payload.get("screenshot_saved")):
                return
            image_path = str(state_payload.get("image_path") or "").strip()
            if image_path and image_path not in seen_paths and Path(image_path).exists():
                seen_paths.add(image_path)
                screenshot_paths.append(image_path)
            platform_text = self._normalize_platform_id(platform_hint or state_payload.get("platform", ""))
            if platform_text and platform_text not in seen_platforms:
                seen_platforms.add(platform_text)
                query_platforms.append(platform_text)

        for state in keyword_states.values():
            state_payload = dict(state or {})
            platform_states = dict(state_payload.get("platform_states") or {})
            if platform_states:
                for platform_name, platform_state in platform_states.items():
                    collect(dict(platform_state or {}), str(platform_name or ""))
                continue
            collect(state_payload)

        return screenshot_paths, query_platforms

    def _get_task_daily_shared_state(self, task_payload: dict) -> dict:
        task_ref = {
            "task_id": str((task_payload or {}).get("task_id") or "").strip(),
            "name": str((task_payload or {}).get("name") or "").strip(),
        }
        status = get_task_day_status(task_ref) if task_ref["task_id"] or task_ref["name"] else {}
        progress = self._build_daily_progress_from_status(status)
        daily_state = {
            "success_bundle": {},
            "status": status,
            "progress": progress,
        }
        task_payload["_daily_shared_state"] = daily_state
        return daily_state

    def _get_task_daily_progress(self, task_payload: dict) -> dict:
        if self._is_manual_test_task(task_payload):
            return {
                "completed_keywords": [],
                "historical_screenshot_paths": [],
                "historical_query_platforms": [],
                "historical_screenshot_count": 0,
                "historical_image_count_by_brand": {},
                "recognition_image_count_by_brand": {},
            }
        daily_state = self._get_task_daily_shared_state(task_payload)
        return dict(daily_state.get("progress") or {})

    def _get_pending_entry_statuses(
        self,
        entry: dict,
        task: dict,
        *,
        runtime_completed_platforms: set[str] | None = None,
    ) -> list[dict]:
        is_manual_test = self._is_manual_test_task(task)
        if not is_manual_test and self._is_task_completed_for_today(task):
            return []
        keyword = str((entry or {}).get("keyword") or "").strip()
        if not keyword:
            return []
        brands = list((entry or {}).get("brands", task.get("brands", [])) or [])
        if not brands:
            brands = [task.get("primary_brand", "")]
        configured_platforms = [
            self._normalize_platform_id(platform)
            for platform in ((entry or {}).get("platforms") or [])
            if self._normalize_platform_id(platform)
        ]
        if not configured_platforms:
            configured_platforms = [
                self._normalize_platform_id(platform)
                for platform in (task.get("platform_candidates") or [])
                if self._normalize_platform_id(platform)
        ]
        if not configured_platforms:
            return []

        status = self._get_task_daily_shared_state(task).get("status") or {}
        keyword_states = dict(status.get("keyword_states") or {})
        matched_state = None
        for brand in brands:
            state = dict(keyword_states.get(keyword) or {})
            state_brand = str(state.get("brand") or "").strip()
            if not state:
                continue
            if not state_brand or state_brand == brand:
                matched_state = state
                break
        if not is_manual_test and matched_state and self._keyword_state_complete_for_platforms(matched_state, configured_platforms):
            return []

        runtime_completed = {
            self._normalize_platform_id(platform)
            for platform in (runtime_completed_platforms or set())
            if self._normalize_platform_id(platform)
        }
        pending_statuses = []
        seen = set()
        state_platform = self._normalize_platform_id((matched_state or {}).get("platform", ""))
        failure_reason = str((matched_state or {}).get("failure_reason") or "").strip()
        for platform_name in configured_platforms:
            normalized_platform = str(platform_name or "").strip().lower()
            if not normalized_platform or normalized_platform in seen:
                continue
            seen.add(normalized_platform)
            platform_state = dict((matched_state or {}).get("platform_states", {}).get(normalized_platform) or {})
            platform_complete = bool(platform_state.get("run_success")) and bool(platform_state.get("screenshot_saved"))
            runtime_complete = normalized_platform in runtime_completed
            if (platform_complete or runtime_complete) and not is_manual_test:
                continue
            platform_failure_reason = str(platform_state.get("failure_reason") or failure_reason).strip()
            platform_state_platform = self._normalize_platform_id(platform_state.get("platform", "")) or state_platform
            failed = bool(
                bool(matched_state)
                and platform_failure_reason
                and platform_failure_reason != "not_run"
                and (not platform_state_platform or platform_state_platform == normalized_platform)
            )
            pending_statuses.append({
                "platform": platform_name,
                "failed": failed,
            })
        pending_statuses.sort(
            key=lambda item: (
                0 if item.get("failed") else 1,
                self._display_platform_name(item.get("platform", "")),
            )
        )
        return pending_statuses

    def _task_has_pending_recognition_work(self, task_payload: dict) -> bool:
        """只要任务组仍有未补齐的关键词/平台，就允许识别模式继续接手。"""
        if self._is_manual_test_task(task_payload):
            return (not self._is_task_completed_for_today(task_payload)) and bool(task_payload.get("guide_keywords"))
        if self._is_task_completed_for_today(task_payload):
            return False
        for entry in task_payload.get("guide_keywords", []) or []:
            if self._get_pending_entry_statuses(entry, task_payload):
                return True
        return False

    def suppress_task_for_today(self, task_name: str, status_text: str | None = None) -> None:
        task_text = str(task_name or "").strip()
        if not task_text:
            return

        suppressed_key = self._make_suppressed_task_key(task_text)
        with self._lock:
            self._suppressed_tasks.add(suppressed_key)
            self._buffers.pop(task_text, None)
            for batch_id, _ in self._get_pending_batches_for_task_locked(task_text):
                self._pending_batches.pop(batch_id, None)
        self._set_guide_status(status_text or f"{task_text} 已按无视失败发送处理，今日不再进入识别补齐")
        self._notify_manual_state_change()

    def _normalize_keyword_brand_pair(self, keyword: str, brand: str) -> tuple[str, str]:
        return (str(keyword or "").strip().lower(), str(brand or "").strip().lower())

    def _display_platform_name(self, platform_name: str) -> str:
        normalized = self._normalize_platform_id(platform_name)
        if normalized:
            return PLATFORM_DISPLAY.get(normalized, normalized)
        text = str(platform_name or "").strip()
        return PLATFORM_DISPLAY.get(text, text)

    def _keyword_state_complete_for_platforms(self, state: dict, platforms: list[str]) -> bool:
        payload = dict(state or {})
        normalized_platforms = [
            self._normalize_platform_id(platform)
            for platform in (platforms or [])
            if self._normalize_platform_id(platform)
        ]
        if not normalized_platforms:
            return bool(payload.get("run_success")) and bool(payload.get("screenshot_saved"))
        platform_states = dict(payload.get("platform_states") or {})
        if platform_states:
            return all(
                bool((platform_states.get(platform) or {}).get("run_success"))
                and bool((platform_states.get(platform) or {}).get("screenshot_saved"))
                for platform in normalized_platforms
            )
        state_platform = self._normalize_platform_id(payload.get("platform", ""))
        return (
            len(normalized_platforms) == 1
            and state_platform == normalized_platforms[0]
            and bool(payload.get("run_success"))
            and bool(payload.get("screenshot_saved"))
        )

    def _format_threshold_progress_text(self, current_count: int, batch_size: int, historical_count: int) -> str:
        current_count = max(0, int(current_count or 0))
        batch_size = max(1, int(batch_size or 1))
        historical_count = max(0, int(historical_count or 0))
        total_count = current_count + historical_count
        return f"累计 {total_count}/{batch_size} 张（本次 {current_count} 张，历史 {historical_count} 张）"

    def _batch_image_count(self, batch: dict | None) -> int:
        payload = batch if isinstance(batch, dict) else {}
        image_items = payload.get("image_items")
        if isinstance(image_items, list):
            return len(image_items)
        image_paths = payload.get("image_paths")
        if isinstance(image_paths, list):
            return len(image_paths)
        return 0

    def _get_pending_batches_for_task_locked(self, task_name: str) -> list[tuple[str, dict]]:
        task_text = str(task_name or "").strip()
        if not task_text:
            return []
        return [
            (batch_id, batch)
            for batch_id, batch in self._pending_batches.items()
            if str((batch or {}).get("task_name") or "").strip() == task_text
        ]

    def _get_pending_image_count_locked(self, task_name: str) -> int:
        return sum(
            self._batch_image_count(batch)
            for _, batch in self._get_pending_batches_for_task_locked(task_name)
        )

    def _merge_batch_item_into_pending_locked(self, task_name: str, task: dict, batch_item: dict) -> dict | None:
        pending_batches = self._get_pending_batches_for_task_locked(task_name)
        if not pending_batches:
            return None

        batch_id, pending_batch = pending_batches[0]
        pending_batch["task"] = task
        pending_batch.setdefault("brands", [])
        for brand in batch_item.get("brands", []) or []:
            brand_text = str(brand or "").strip()
            if brand_text and brand_text not in pending_batch["brands"]:
                pending_batch["brands"].append(brand_text)

        path_text = str(batch_item.get("path", "") or "").strip()
        if path_text:
            pending_batch.setdefault("image_paths", [])
            pending_batch["image_paths"].append(path_text)

        pending_batch.setdefault("image_items", [])
        pending_batch["image_items"].append({
            "path": path_text,
            "ocr_text": str(batch_item.get("ocr_text", "") or "").strip(),
            "source_text": str(batch_item.get("source_text", "") or "").strip(),
        })

        merged_pairs = list(pending_batch.get("matched_pairs") or [])
        merged_pairs.extend(batch_item.get("matched_pairs") or [])
        pending_batch["matched_pairs"] = self._serialize_matched_pairs(merged_pairs)

        summary_text = str(batch_item.get("summary", "") or "").strip()
        if summary_text:
            existing_summary = str(pending_batch.get("summary", "") or "").strip()
            pending_batch["summary"] = f"{existing_summary}；{summary_text}" if existing_summary else summary_text

        self._attach_historical_progress(pending_batch)
        pending_batch["id"] = batch_id
        return pending_batch

    def get_keyword_guide_state(self) -> dict:
        tasks = self._get_enabled_tasks()
        items = self._build_keyword_guide_items(tasks)
        manual_mode = not self._auto_send_recognition_batches_enabled()

        # 判断识别模式类型
        config = self._config_getter() or {}
        dom_render_mode = config.get("recognition", {}).get("dom_render_mode", False)

        if manual_mode:
            mode_label = "手动确认模式"
        elif dom_render_mode:
            mode_label = "DOM 文本识别"
        else:
            mode_label = "OCR 截图识别"

        with self._lock:
            if items:
                self._manual_index = max(0, min(self._manual_index, len(items) - 1))
                current_index = self._manual_index
            else:
                self._manual_index = 0
                current_index = 0
            buffered_counts = {
                task_name: len(buffers)
                for task_name, buffers in self._buffers.items()
            }
            pending_counts = {
                task_name: self._get_pending_image_count_locked(task_name)
                for task_name in {
                    *buffered_counts.keys(),
                    *(str((batch or {}).get("task_name") or "").strip() for batch in self._pending_batches.values()),
                }
                if task_name
            }
            guide_status = self._guide_status_text

        enriched_items = []
        for item in items:
            task_name = str((item or {}).get("task_name") or "").strip()
            active_count = int(buffered_counts.get(task_name, 0) or 0) + int(pending_counts.get(task_name, 0) or 0)
            historical_count = int((item or {}).get("historical_count", 0) or 0)
            batch_size = max(1, int((item or {}).get("batch_size", 1) or 1))
            enriched_items.append({
                **item,
                "active_screenshot_count": active_count,
                "screenshot_count": active_count + historical_count,
                "screenshot_total": batch_size,
            })
        items = enriched_items

        state = {
            "items": items,
            "index": current_index,
            "manual_mode": manual_mode,
            "mode_label": mode_label,
            "action_label": None,
            "action_enabled": None,
            "detail_text": guide_status or "",
            "controls_visible": True,
        }
        if items:
            current = items[current_index]
            current_detail = str(current.get("detail_text") or "").strip()
            if current_detail:
                state["detail_text"] = f"{current_detail}；{state['detail_text']}" if state["detail_text"] else current_detail
            sent_count = int(current.get("sent_count", 0) or 0)
            if sent_count > 0:
                sent_hint = f"已成功 {sent_count} 张"
                state["detail_text"] = f"{state['detail_text']}；{sent_hint}" if state["detail_text"] else sent_hint

        if not manual_mode or not items:
            return state

        current = items[current_index]
        buffered_count = int(buffered_counts.get(current["task_name"], 0) or 0)
        pending_count = int(pending_counts.get(current["task_name"], 0) or 0)
        active_count = buffered_count + pending_count
        batch_size = max(1, int(current.get("batch_size", 1) or 1))
        sent_count = int(current.get("sent_count", 0) or 0)
        historical_count = int(current.get("historical_count", 0) or 0)
        total_count = active_count + historical_count
        progress_text = self._format_threshold_progress_text(active_count, batch_size, historical_count)
        if current.get("is_last_in_task"):
            if total_count >= batch_size:
                state["action_label"] = "确认发布"
                state["action_enabled"] = "normal"
                progress_detail = f"当前任务组截图 {progress_text}，可确认发布"
            else:
                state["action_label"] = f"继续截图 {total_count}/{batch_size}"
                state["action_enabled"] = "disabled"
                progress_detail = f"当前任务组截图 {progress_text}，未达到发布数量"
        else:
            state["action_label"] = "下一个 →"
            state["action_enabled"] = "normal"
            progress_detail = f"当前任务组截图 {progress_text}"
        if state["detail_text"]:
            state["detail_text"] = f"{state['detail_text']}；{progress_detail}"
        else:
            state["detail_text"] = progress_detail
        return state

    def _notify_manual_state_change(self):
        if self._on_manual_state_change:
            self._on_manual_state_change(self.get_keyword_guide_state())

    def _find_next_task_start_index(self, items: list[dict], current_index: int):
        if not items or current_index >= len(items):
            return None
        current_task = items[current_index].get("task_name", "")
        for idx in range(current_index + 1, len(items)):
            if items[idx].get("task_name", "") != current_task:
                return idx
        return None

    def handle_keyword_guide_action(self, action: str | None = None):
        state = self.get_keyword_guide_state()
        action = str(action or "").strip().lower()
        if action not in {"", "complete", "next", "prev", "skip"}:
            return state

        if not state.get("manual_mode"):
            items = state.get("items") or []
            if not items:
                return state
            with self._lock:
                max_index = max(0, len(items) - 1)
                if action == "prev":
                    self._manual_index = max(0, self._manual_index - 1)
                elif action in {"next", "skip", "complete"}:
                    self._manual_index = min(max_index, self._manual_index + 1)
            self._notify_manual_state_change()
            return state

        tasks = self._get_enabled_tasks()
        items = state.get("items") or []
        if not items:
            return state

        task_map = {task["name"]: task for task in tasks}
        should_refresh_only = False
        with self._lock:
            self._manual_index = max(0, min(self._manual_index, len(items) - 1))
            item = items[self._manual_index]
            task_name = item.get("task_name", "")
            bucket = self._buffers.get(task_name, [])
            pending_batches = self._get_pending_batches_for_task_locked(task_name)
            batch_size = max(1, int(item.get("batch_size", 1) or 1))
            historical_count = int(item.get("historical_count", 0) or 0)
            pending_count = sum(self._batch_image_count(batch) for _, batch in pending_batches)
            total_count = len(bucket) + pending_count + historical_count

            if action == "prev":
                self._manual_index = max(0, self._manual_index - 1)
                should_refresh_only = True
            elif action == "skip":
                if task_name in self._buffers:
                    self._buffers[task_name] = []
                for batch_id, _ in pending_batches:
                    self._pending_batches.pop(batch_id, None)
                next_index = self._find_next_task_start_index(items, self._manual_index)
                if next_index is not None:
                    self._manual_index = next_index
                else:
                    self._manual_index = min(self._manual_index + 1, len(items) - 1)
                should_refresh_only = True
            elif action == "next":
                if item.get("is_last_in_task"):
                    next_index = self._find_next_task_start_index(items, self._manual_index)
                    if next_index is not None:
                        self._manual_index = next_index
                else:
                    self._manual_index = min(self._manual_index + 1, len(items) - 1)
                should_refresh_only = True
            elif action in {"", "complete"}:
                if item.get("is_last_in_task"):
                    if total_count < batch_size:
                        print(
                            f"[Recognition] {task_name} 截图数量不足，"
                            f"{self._format_threshold_progress_text(len(bucket) + pending_count, batch_size, historical_count)}，继续等待截图"
                        )
                        should_refresh_only = True
                    else:
                        batch_items = list(bucket)
                        pending_image_paths: list[str] = []
                        pending_image_items: list[dict] = []
                        pending_matched_pairs: list[dict] = []
                        pending_summaries: list[str] = []
                        pending_brands: list[str] = []
                        for _, pending_batch in pending_batches:
                            pending_image_paths.extend(
                                str(path or "").strip()
                                for path in (pending_batch.get("image_paths") or [])
                                if str(path or "").strip()
                            )
                            pending_image_items.extend(
                                dict(image_item)
                                for image_item in (pending_batch.get("image_items") or [])
                                if isinstance(image_item, dict)
                            )
                            pending_matched_pairs.extend(list(pending_batch.get("matched_pairs") or []))
                            pending_summary = str(pending_batch.get("summary", "") or "").strip()
                            if pending_summary:
                                pending_summaries.append(pending_summary)
                            for brand in (pending_batch.get("brands") or []):
                                brand_text = str(brand or "").strip()
                                if brand_text and brand_text not in pending_brands:
                                    pending_brands.append(brand_text)
                        for batch_id, _ in pending_batches:
                            self._pending_batches.pop(batch_id, None)
                        del bucket[:]
                        task = task_map.get(task_name)
                        if task:
                            batch = {
                                "id": f"{task_name}_manual_{int(time.time() * 1000)}",
                                "task_name": task_name,
                                "brands": list(dict.fromkeys(
                                    pending_brands + [
                                        brand
                                        for batch_item in batch_items
                                        for brand in batch_item["brands"]
                                    ]
                                )),
                                "image_paths": pending_image_paths + [batch_item["path"] for batch_item in batch_items],
                                "image_items": pending_image_items + [
                                    {
                                        "path": batch_item["path"],
                                        "ocr_text": batch_item.get("ocr_text", ""),
                                        "source_text": batch_item.get("source_text", ""),
                                    }
                                    for batch_item in batch_items
                                ],
                                "matched_pairs": self._serialize_matched_pairs(
                                    pending_matched_pairs + [
                                        pair
                                        for batch_item in batch_items
                                        for pair in (batch_item.get("matched_pairs") or [])
                                    ]
                                ),
                                "summary": "；".join(
                                    pending_summaries + [
                                        batch_item["summary"]
                                        for batch_item in batch_items
                                        if batch_item.get("summary")
                                    ]
                                ) or "人工确认发布",
                                "task": task,
                            }
                            self._attach_historical_progress(batch)
                            self._send_queue.put(batch)
                            print(
                                f"[Recognition] {task_name} 已人工确认发布，"
                                f"{self._format_batch_image_progress(batch)}"
                            )

                        next_index = self._find_next_task_start_index(items, self._manual_index)
                        if next_index is not None:
                            self._manual_index = next_index
                else:
                    self._manual_index = min(self._manual_index + 1, len(items) - 1)

        if should_refresh_only:
            return self.get_keyword_guide_state()
        self._notify_manual_state_change()
        return self.get_keyword_guide_state()

    def _set_guide_status(self, text: str):
        with self._lock:
            self._guide_status_text = (text or "").strip()

    def _find_next_index_in_task(self, items: list[dict], current_index: int):
        if not items or current_index >= len(items):
            return None
        current_task = items[current_index].get("task_name", "")
        for idx in range(current_index + 1, len(items)):
            if items[idx].get("task_name", "") == current_task:
                return idx
            break
        return None

    def _set_guide_index(self, items: list[dict], next_index: int | None):
        if not items:
            return
        with self._lock:
            if next_index is None:
                self._manual_index = min(self._manual_index, len(items) - 1)
            else:
                self._manual_index = max(0, min(int(next_index), len(items) - 1))

    def _should_advance_for_match(self, focus_task_name: str, task_name: str, advanced: bool) -> bool:
        if advanced:
            return False
        focus_task_name = str(focus_task_name or "").strip()
        task_name = str(task_name or "").strip()
        if not task_name:
            return False
        if focus_task_name:
            return task_name == focus_task_name
        return True

    def _remaining_current_platforms_after_match(self, current_item: dict | None, matched_pairs: list[dict]) -> list[str]:
        item = dict(current_item or {})
        current_platforms = [
            self._normalize_platform_id(platform)
            for platform in (item.get("platforms") or [])
            if self._normalize_platform_id(platform)
        ]
        current_platforms = list(dict.fromkeys(current_platforms))
        if not current_platforms:
            return []

        current_keyword = str(item.get("keyword") or "").strip()
        current_keyword_key, _ = self._normalize_keyword_brand_pair(current_keyword, "")
        if not current_keyword_key:
            return current_platforms
        current_brand_keys = {
            self._normalize_keyword_brand_pair("", brand)[1]
            for brand in (item.get("brands") or [])
            if str(brand or "").strip()
        }

        matched_platforms: set[str] = set()
        for pair in self._expand_matched_pair_slots(matched_pairs or []):
            pair_keyword_key, pair_brand_key = self._normalize_keyword_brand_pair(
                str((pair or {}).get("keyword") or "").strip(),
                str((pair or {}).get("brand") or "").strip(),
            )
            if pair_keyword_key != current_keyword_key:
                continue
            if current_brand_keys and pair_brand_key and pair_brand_key not in current_brand_keys:
                continue
            for platform in (pair or {}).get("platforms") or []:
                normalized_platform = self._normalize_platform_id(platform)
                if normalized_platform:
                    matched_platforms.add(normalized_platform)

        if not matched_platforms:
            return current_platforms
        return [platform for platform in current_platforms if platform not in matched_platforms]

    def _guide_item_key(self, item: dict | None) -> tuple[str, str, tuple[str, ...]]:
        payload = dict(item or {})
        task_name = str(payload.get("task_name") or "").strip()
        keyword_key, _ = self._normalize_keyword_brand_pair(str(payload.get("keyword") or "").strip(), "")
        brand_keys = tuple(sorted({
            self._normalize_keyword_brand_pair("", brand)[1]
            for brand in (payload.get("brands") or [])
            if str(brand or "").strip()
        }))
        return task_name, keyword_key, brand_keys

    def _find_guide_item_index_by_key(self, items: list[dict], target_key: tuple[str, str, tuple[str, ...]]) -> int | None:
        if not target_key[0] or not target_key[1]:
            return None
        for index, item in enumerate(items or []):
            if self._guide_item_key(item) == target_key:
                return index
        return None

    def _find_first_task_index(self, items: list[dict], task_name: str, *, exclude_key: tuple[str, str, tuple[str, ...]] | None = None) -> int | None:
        task_text = str(task_name or "").strip()
        if not task_text:
            return None
        for index, item in enumerate(items or []):
            if str((item or {}).get("task_name") or "").strip() != task_text:
                continue
            if exclude_key is not None and self._guide_item_key(item) == exclude_key:
                continue
            return index
        return None

    def _set_post_match_guide_index(
        self,
        tasks: list[dict],
        guide_items: list[dict],
        current_index: int,
        current_item: dict | None,
        *,
        remaining_current_platforms: list[str],
    ) -> str:
        refreshed_items = self._build_keyword_guide_items(tasks)
        if not refreshed_items:
            return "none"

        current_key = self._guide_item_key(current_item)
        current_task = current_key[0] or str((current_item or {}).get("task_name") or "").strip()
        if remaining_current_platforms:
            target_index = self._find_guide_item_index_by_key(refreshed_items, current_key)
            if target_index is None:
                target_index = self._find_first_task_index(refreshed_items, current_task)
            self._set_guide_index(refreshed_items, target_index)
            return "current"

        next_index = self._find_next_index_in_task(guide_items, current_index)
        if next_index is not None:
            target_key = self._guide_item_key(guide_items[next_index])
            target_index = self._find_guide_item_index_by_key(refreshed_items, target_key)
            if target_index is None:
                target_index = self._find_first_task_index(refreshed_items, current_task, exclude_key=current_key)
            self._set_guide_index(refreshed_items, target_index)
            return "same_task" if target_index is not None else "none"

        next_index = self._find_next_task_start_index(guide_items, current_index)
        if next_index is not None:
            target_key = self._guide_item_key(guide_items[next_index])
            target_index = self._find_guide_item_index_by_key(refreshed_items, target_key)
            self._set_guide_index(refreshed_items, target_index)
            return "next_task" if target_index is not None else "none"

        self._set_guide_index(refreshed_items, None)
        return "none"

    def _has_live_workers(self):
        return any(
            thread and thread.is_alive()
            for thread in (self._poll_thread, self._recognize_thread, self._send_thread)
        )

    def _join_worker_threads(self, timeout: float = 2.0):
        current = threading.current_thread()
        for attr in ("_poll_thread", "_recognize_thread", "_send_thread"):
            thread = getattr(self, attr)
            if thread and thread.is_alive() and thread is not current:
                thread.join(timeout=timeout)
            if thread and not thread.is_alive():
                setattr(self, attr, None)

    def _drain_queue(self, q: queue.Queue) -> None:
        """清空队列中的残留条目，不替换对象本身，避免旧线程引用失效导致竞态。"""
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def confirm_batch(self, batch_id: str, send: bool):
        with self._lock:
            batch = self._pending_batches.pop(batch_id, None)
        if not batch:
            return
        if send:
            self._send_queue.put(batch)
        else:
            print(f"[Recognition] 已忽略批次: {batch['task_name']} / {batch_id}")
            self._check_cycle_complete()
        self._notify_manual_state_change()

    def has_recognition_tasks(self) -> bool:
        return bool(self._get_enabled_tasks())

    def _task_has_recognition_queries(self, task: dict) -> bool:
        keywords = task.get("keywords", [])
        if not keywords and task.get("keyword"):
            keywords = [{
                "keyword": task.get("keyword", ""),
                "platforms": [task.get("platform", "")],
            }]

        fallback_platform = self._normalize_platform_id(task.get("platform", ""))
        for kw in keywords:
            if not isinstance(kw, dict):
                continue
            keyword = str(kw.get("keyword", "") or "").strip()
            if not keyword:
                continue
            platforms = [
                self._normalize_platform_id(platform)
                for platform in (kw.get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            if platforms or fallback_platform:
                return True
        return False

    def get_task_overview(self) -> dict:
        config = self._config_getter() or {}
        global_mode = str(config.get("detection_mode", "browser") or "browser").strip()
        overview = {
            "mode": global_mode,
            "configured_task_count": 0,
            "watchable_today_count": 0,
            "completed_today_count": 0,
            "available_task_count": 0,
        }
        if global_mode != "recognition":
            return overview

        current = local_now()
        for index, task in enumerate(config.get("tasks", []) or []):
            if not bool(task.get("enabled", True)):
                continue
            overview["configured_task_count"] += 1

            if not self._should_watch_task_today(task, current):
                continue

            overview["watchable_today_count"] += 1
            task_payload = self._build_recognition_task_payload(task, index)
            if not task_payload:
                continue

            if self._task_has_pending_recognition_work(task_payload):
                overview["available_task_count"] += 1
                continue

            if self._task_has_recognition_queries(task):
                overview["completed_today_count"] += 1

        return overview

    def get_runtime_status(self, *, include_overview: bool = True) -> dict:
        """返回识别模式运行态快照，供主页文案和后续 UI 使用。"""
        with self._lock:
            pending_batches = len(self._pending_batches)
            buffered_images = sum(len(items) for items in self._buffers.values())

        idle_seconds = None
        idle_minutes = None
        if self._last_image_seen_at:
            idle_seconds = max(0.0, (datetime.now() - self._last_image_seen_at).total_seconds())
            idle_minutes = round(idle_seconds / 60, 1)

        status = {
            "running": self._running,
            "pending_batches": pending_batches,
            "buffered_images": buffered_images,
            "queued_images": self._recognition_queue.qsize() if self._recognition_queue else 0,
            "send_queue_size": self._send_queue.qsize() if self._send_queue else 0,
            "recognizing": self._recognizing,
            "sending": self._sending,
            "idle_seconds": idle_seconds,
            "idle_minutes": idle_minutes,
            "work_started": self._work_started,
        }
        if include_overview:
            status["task_overview"] = self.get_task_overview()
        return status

    def _should_watch_task_today(self, task: dict, now: datetime | None = None) -> bool:
        if not task.get("enabled", True):
            return False

        current = now or local_now()
        today = current.date()

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

        raw_weekdays = task.get("weekdays", [0, 1, 2, 3, 4])
        weekdays: list[int] = []
        for item in raw_weekdays:
            try:
                weekdays.append(int(item))
            except Exception:
                continue
        if not weekdays:
            weekdays = [0, 1, 2, 3, 4]
        return current.weekday() in weekdays

    def _build_recognition_task_payload(self, task: dict, index: int) -> dict | None:
        keywords = task.get("keywords", [])
        if not keywords and task.get("keyword"):
            keywords = [{
                "keyword": task.get("keyword", ""),
                "brand": task.get("brand", ""),
                "platforms": [task.get("platform", "")],
                "mode": "recognition",
            }]

        if not keywords:
            return None

        first_brand = keywords[0].get("brand", "").strip()
        recognition_rows = []
        for kw in keywords:
            if not isinstance(kw, dict):
                continue
            recognition_rows.append({
                **kw,
                "mode": "recognition",
            })
        if not recognition_rows:
            return None

        brands = []
        alias_map = {}
        for kw in recognition_rows:
            brand = kw.get("brand", "").strip() or first_brand
            if brand and brand not in brands:
                brands.append(brand)
            self._register_brand_alias(alias_map, brand, brand)

        if not brands:
            return None

        extra_aliases = self._parse_task_aliases(task, brands[0])
        for alias in extra_aliases:
            self._register_brand_alias(alias_map, brands[0], alias)

        task_platform_candidates = []
        for kw in recognition_rows:
            for platform in (kw.get("platforms") or []):
                platform_name = self._normalize_platform_id(platform)
                if platform_name and platform_name not in task_platform_candidates:
                    task_platform_candidates.append(platform_name)
        if not task_platform_candidates:
            fallback_platform = self._normalize_platform_id(task.get("platform", ""))
            if fallback_platform:
                task_platform_candidates.append(fallback_platform)

        guide_keywords = []
        for kw in recognition_rows:
            keyword = kw.get("keyword", "").strip()
            brand = kw.get("brand", "").strip() or first_brand or brands[0]
            guide_platforms = [
                self._normalize_platform_id(platform)
                for platform in (kw.get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            if not guide_platforms:
                guide_platforms = list(task_platform_candidates)
            guide_keywords.append({
                "keyword": keyword or task.get("name", brands[0]),
                "brands": [brand] if brand else list(brands),
                "platforms": list(dict.fromkeys(guide_platforms)),
            })

        daily_keywords = []
        for entry in guide_keywords:
            keyword_text = str(entry.get("keyword") or "").strip()
            entry_brands = [
                str(item or "").strip()
                for item in (entry.get("brands") or [])
                if str(item or "").strip()
            ]
            entry_platforms = [
                self._normalize_platform_id(platform)
                for platform in (entry.get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            if not keyword_text or not entry_platforms:
                continue
            daily_keywords.append({
                "keyword": keyword_text,
                "brand": entry_brands[0] if entry_brands else brands[0],
                "platforms": list(dict.fromkeys(entry_platforms)),
                "mode": "recognition",
            })

        task_payload = {
            "order": index,
            "name": task.get("name", brands[0]),
            "task_id": str(task.get("task_id") or derive_task_id(task)).strip(),
            "_daily_state_source": str(task.get("_daily_state_source") or "").strip(),
            "brands": brands,
            "primary_brand": brands[0],
            "candidate_brands": list(dict.fromkeys(brands + extra_aliases)),
            "brand_alias_map": alias_map,
            "task_name_keys": self._extract_name_keys(task.get("name", brands[0])),
            "webhook_url": task.get("webhook_url", "").strip(),
            "recognition_batch_size": max(1, int(task.get("recognition_batch_size", 3) or 3)),
            "platform_candidates": list(task_platform_candidates),
            "guide_keywords": guide_keywords,
            "keywords": daily_keywords,
        }
        task_payload["_daily_shared_state"] = self._get_task_daily_shared_state(task_payload)
        return task_payload

    def _get_enabled_tasks(self):
        config = self._config_getter() or {}
        global_mode = str(config.get("detection_mode", "browser") or "browser").strip()
        if global_mode != "recognition":
            return []
        current = local_now()
        tasks = []
        for index, task in enumerate(config.get("tasks", [])):
            is_manual_test = str(task.get("_daily_state_source") or "").strip() == "manual_test"
            if not is_manual_test and not self._should_watch_task_today(task, current):
                continue
            task_payload = self._build_recognition_task_payload(task, index)
            if not task_payload:
                continue

            if not self._task_has_pending_recognition_work(task_payload):
                continue

            tasks.append(task_payload)
        return tasks

    def _get_current_item_candidates(self, current_item: dict | None, focus_task: dict | None) -> list[str]:
        candidates: list[str] = []
        if current_item:
            for brand in (current_item.get("brands") or []):
                text = str(brand or "").strip()
                if text and text not in candidates:
                    candidates.append(text)

        if candidates:
            return candidates

        if focus_task:
            for brand in (focus_task.get("brands") or []):
                text = str(brand or "").strip()
                if text and text not in candidates:
                    candidates.append(text)
        return candidates

    def _get_current_guide_item(self, tasks: list[dict]) -> dict | None:
        items = self._build_keyword_guide_items(tasks)
        if not items:
            return None
        with self._lock:
            self._manual_index = max(0, min(self._manual_index, len(items) - 1))
            return dict(items[self._manual_index])

    def _collect_global_candidates(self, tasks: list[dict]) -> list[str]:
        candidates = []
        for task in tasks:
            for brand in task.get("candidate_brands", task.get("brands", [])):
                if brand and brand not in candidates:
                    candidates.append(brand)
        return candidates

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                if self.has_recognition_tasks():
                    # 根据配置选择监听模式
                    config = self._config_getter() or {}
                    if config.get("recognition", {}).get("dom_render_mode", False):
                        self._poll_once_text_mode()  # 文本模式
                    else:
                        self._poll_once_reference_url_mode()  # 截图模式下同步监听复制的网址引用
                        self._poll_once()  # 截图模式（原有逻辑）
                self._check_idle_timeout()
                self._check_flush_timeout()
            except Exception as e:
                print(f"[Recognition] 轮询失败: {e}")
            self._stop_event.wait(self._poll_interval())

    def _poll_interval(self):
        config = self._config_getter() or {}
        recognition_cfg = config.get("recognition", {})
        return max(0.8, float(recognition_cfg.get("poll_interval_seconds", 1.2) or 1.2))

    def _queue_capacity(self):
        config = self._config_getter() or {}
        recognition_cfg = config.get("recognition", {})
        try:
            configured = int(recognition_cfg.get("max_pending_images", 16) or 16)
        except Exception:
            configured = 16
        return min(256, max(4, configured))

    def _poll_once(self):
        clip = ImageGrab.grabclipboard()
        clip = self._extract_clipboard_image(clip)
        if clip is None:
            if not self._clipboard_armed:
                self._clipboard_armed = True
                self._startup_clipboard_hash = None
            return

        image_hash, image_bytes = self._prepare_clipboard_image(clip)
        if not image_hash or not image_bytes:
            return

        if not self._clipboard_armed:
            if image_hash == self._startup_clipboard_hash:
                return
            self._clipboard_armed = True
            self._startup_clipboard_hash = None

        if self._is_duplicate(image_hash):
            return

        image_path = self._persist_clipboard_image(image_hash, image_bytes)
        if not image_path:
            return

        self._work_started = True
        self._last_image_seen_at = datetime.now()
        self._idle_reminded = False
        payload = {
            "hash": image_hash,
            "path": str(image_path),
            "platform_hint": self._get_active_capture_platform(),
        }
        try:
            self._recognition_queue.put_nowait(payload)
            print(f"[Recognition] 已缓存截图，等待识别: {image_path.name}")
        except queue.Full:
            print("[Recognition] 待识别队列已满，本次截图已跳过")
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _poll_clipboard_text(self) -> Optional[str]:
        """
        获取剪贴板文本内容（跨平台）

        Returns:
            剪贴板文本，失败返回None
        """
        try:
            if sys.platform == "darwin":
                # macOS: 使用 pbpaste
                result = subprocess.run(['pbpaste'], capture_output=True, text=True, timeout=1)
                return result.stdout if result.returncode == 0 else None
            elif sys.platform == "win32":
                # Windows: 使用 PowerShell Get-Clipboard
                result = subprocess.run(
                    ['powershell', '-command', 'Get-Clipboard'],
                    capture_output=True, text=True, timeout=1
                )
                return result.stdout if result.returncode == 0 else None
            else:
                # Linux: 使用 xclip 或 xsel
                for cmd in [['xclip', '-selection', 'clipboard', '-o'], ['xsel', '--clipboard']]:
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
                        if result.returncode == 0:
                            return result.stdout
                    except FileNotFoundError:
                        continue
                return None
        except Exception as e:
            print(f"[Recognition] 获取剪贴板文本失败: {e}")
            return None

    def _prime_reference_clipboard_baseline(self) -> None:
        """记录启动时剪贴板文本，避免刚开启识别模式就误标旧链接。"""
        self._reference_clipboard_armed = True
        self._startup_reference_text_hash = None
        try:
            text = self._poll_clipboard_text()
            if text:
                self._startup_reference_text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
                self._reference_clipboard_armed = False
        except Exception as exc:
            print(f"[Recognition] 初始化引用链接剪切板基线失败: {exc}")

    def _is_duplicate_reference_text(self, text_hash: str) -> bool:
        normalized_hash = str(text_hash or "").strip()
        if not normalized_hash:
            return True
        with self._lock:
            if normalized_hash in self._seen_reference_set:
                return True
            self._seen_reference_hashes.append(normalized_hash)
            self._seen_reference_set.add(normalized_hash)
            while len(self._seen_reference_set) > self._seen_reference_hashes.maxlen:
                removed = self._seen_reference_hashes.popleft()
                self._seen_reference_set.discard(removed)
            return False

    def _extract_reference_urls_from_text(self, text: str) -> list[str]:
        try:
            from core.reference_urls import extract_reference_urls_from_text
        except Exception:
            return []
        return extract_reference_urls_from_text(text)

    def _get_reference_task_names(self, tasks: list[dict]) -> list[str]:
        guide_items = self._build_keyword_guide_items(tasks)
        if guide_items:
            with self._lock:
                self._manual_index = max(0, min(self._manual_index, len(guide_items) - 1))
                current_item = dict(guide_items[self._manual_index])
            current_task_name = str(current_item.get("task_name") or "").strip()
            if current_task_name:
                return [current_task_name]

        task_names = []
        for task in tasks or []:
            task_name = str((task or {}).get("name") or "").strip()
            if task_name and task_name not in task_names:
                task_names.append(task_name)
        return task_names

    def _mark_referenced_articles_from_text(
        self,
        text: str,
        *,
        tasks: list[dict] | None = None,
        source: str = "recognition",
        platform: str = "",
    ) -> dict:
        urls = self._extract_reference_urls_from_text(text)
        if not urls:
            return {"matched_count": 0, "updated_count": 0, "urls": []}

        active_tasks = tasks if tasks is not None else self._get_enabled_tasks()
        task_names = self._get_reference_task_names(active_tasks)
        if not task_names:
            return {"matched_count": 0, "updated_count": 0, "urls": urls}

        try:
            from core.article_store import mark_articles_referenced_by_urls

            result = mark_articles_referenced_by_urls(
                task_names,
                urls,
                source=source,
                platform=platform or self._get_active_capture_platform(),
            )
        except Exception as exc:
            print(f"[Recognition] 引用链接匹配失败: {exc}")
            return {"matched_count": 0, "updated_count": 0, "urls": urls, "error": str(exc)}

        matched_count = int(result.get("matched_count", 0) or 0)
        if matched_count > 0:
            task_text = "、".join(task_names[:2])
            suffix = "等" if len(task_names) > 2 else ""
            self._set_guide_status(f"已标记引用链接：{matched_count} 条（{task_text}{suffix}）")
            self._notify_manual_state_change()
            print(
                f"[Recognition] 已标记引用链接: matched={matched_count}, "
                f"tasks={task_names}, urls={result.get('urls', [])}"
            )
        return result

    def _poll_once_reference_url_mode(self) -> None:
        """截图识别模式下，额外监听剪贴板文本 URL，用于标记已录入链接为已引用。"""
        text = self._poll_clipboard_text()
        if not text or not text.strip():
            if not self._reference_clipboard_armed:
                self._reference_clipboard_armed = True
                self._startup_reference_text_hash = None
            return

        text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        if not self._reference_clipboard_armed:
            if text_hash == self._startup_reference_text_hash:
                return
            self._reference_clipboard_armed = True
            self._startup_reference_text_hash = None

        if self._is_duplicate_reference_text(text_hash):
            return

        self._mark_referenced_articles_from_text(
            text,
            tasks=self._get_enabled_tasks(),
            source="recognition_clipboard_url",
            platform=self._get_active_capture_platform(),
        )

    def _collect_text_mode_candidate_brands(self) -> list[str]:
        """收集 DOM 文本模式可用于直接匹配的品牌候选。"""
        candidates: list[str] = []
        seen: set[str] = set()

        def add_candidate(value: str) -> None:
            brand = str(value or "").strip()
            if not brand or brand in seen:
                return
            seen.add(brand)
            candidates.append(brand)

        # 优先使用当前真正处于监听中的识别任务，避免监听范围与命中范围不一致。
        active_tasks = self._get_enabled_tasks()
        for task in active_tasks:
            for brand in (task.get("candidate_brands") or task.get("brands") or []):
                add_candidate(brand)
            add_candidate(task.get("primary_brand", ""))

        if candidates:
            return candidates

        # 回退到配置级收集，兼容单测和未完整构建运行态的场景。
        config = self._config_getter() or {}
        global_mode = str(config.get("detection_mode", "browser") or "browser").strip()
        tasks = config.get("tasks", [])

        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                continue
            if not bool(task.get("enabled", True)):
                continue
            if global_mode != "recognition" and not task.get("recognition_enabled"):
                continue

            payload = self._build_recognition_task_payload(task, index)
            if payload:
                for brand in (payload.get("candidate_brands") or payload.get("brands") or []):
                    add_candidate(brand)
                add_candidate(payload.get("primary_brand", ""))
                continue

            primary_brand = str(task.get("brand") or "").strip()
            add_candidate(primary_brand)
            for alias in self._parse_task_aliases(task, primary_brand):
                add_candidate(alias)

            for kw_item in task.get("guide_keywords", []):
                if not isinstance(kw_item, dict):
                    continue
                add_candidate(kw_item.get("brand", ""))

        return candidates

    def _match_brands_from_text(self, text: str) -> List[str]:
        """
        从文本中直接匹配品牌（无需AI识别）

        Args:
            text: 原始文本

        Returns:
            匹配到的品牌列表
        """
        all_candidate_brands = self._collect_text_mode_candidate_brands()
        if not all_candidate_brands:
            return []

        # 使用现有的品牌匹配逻辑
        matched = match_candidate_brands(text, all_candidate_brands)
        return matched

    def _poll_once_text_mode(self):
        """
        DOM文本渲染模式的轮询逻辑（监听剪贴板文本）
        """
        text = self._poll_clipboard_text()
        if not text or not text.strip():
            if not self._clipboard_armed:
                self._clipboard_armed = True
                self._startup_clipboard_hash = None
            return

        # 文本长度过滤
        config = self._config_getter() or {}
        text_length = len(text)
        min_length = config.get("recognition", {}).get("dom_render_min_length", 10)
        max_length = config.get("recognition", {}).get("dom_render_max_length", 50000)
        if text_length < min_length or text_length > max_length:
            return

        # 计算文本哈希用于去重
        text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()

        # 启动时忽略已存在的剪贴板内容
        if not self._clipboard_armed:
            if text_hash == self._startup_clipboard_hash:
                return
            self._clipboard_armed = True
            self._startup_clipboard_hash = None

        # 去重检查
        if self._is_duplicate(text_hash):
            return

        active_tasks = self._get_enabled_tasks()
        self._mark_referenced_articles_from_text(
            text,
            tasks=active_tasks,
            source="recognition_dom_text",
            platform=self._get_active_capture_platform(),
        )

        # 【关键优化】先进行文本品牌识别，再决定是否渲染
        matched_brands = self._match_brands_from_text(text)
        if not matched_brands:
            # 没有匹配到品牌，跳过本次文本
            print(f"[Recognition] 文本中未检测到品牌，已跳过")
            return

        # DOM 文本模式直接保留原始正文入队，最终发送前再统一渲染最终卡片，
        # 避免生成一张不会直接使用的中间 JPG。
        self._work_started = True
        self._last_image_seen_at = datetime.now()
        self._idle_reminded = False
        payload = {
            "hash": text_hash,
            "path": "",
            "source_text": text,  # 保留原始文本
            "matched_brands": matched_brands,  # 已识别的品牌
            "skip_ai_recognition": True,  # 标记跳过AI识别
            "platform_hint": self._get_active_capture_platform(),
        }
        try:
            self._recognition_queue.put_nowait(payload)
            print(f"[Recognition] 文本已识别品牌 {matched_brands}，已直接入队等待最终渲染")
        except queue.Full:
            print("[Recognition] 待识别队列已满，本次文本已跳过")

    def _recognize_loop(self):
        while True:
            try:
                item = self._recognition_queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set() and self._recognition_queue.empty():
                    return
                continue

            if item is None:
                if self._stop_event.is_set() and self._recognition_queue.empty():
                    return
                continue

            try:
                self._recognizing = True
                self._recognize_one(item)
            except Exception as e:
                print(f"[Recognition] 后台识别失败: {e}")
            finally:
                self._recognizing = False
                self._check_cycle_complete()

    def _recognize_one(self, item: dict):
        image_path = item.get("path", "")
        source_text = item.get("source_text")  # DOM文本模式下的原始文本
        skip_ai = item.get("skip_ai_recognition", False)  # 是否跳过AI识别
        matched_brands = item.get("matched_brands", [])  # 已匹配的品牌
        platform_hint = self._normalize_platform_id(item.get("platform_hint", "")) or self._get_active_capture_platform()

        tasks = self._get_enabled_tasks()

        # 【关键优化】如果已经在文本态识别完成，直接使用结果
        if skip_ai and matched_brands:
            print(f"[Recognition] 使用文本匹配结果: {matched_brands}")
            self._set_guide_status(f"文本识别成功：{', '.join(matched_brands)}")
            self._notify_manual_state_change()
            # 直接进入路由逻辑
            self._route_to_batches(
                image_path=image_path,
                brands=matched_brands,
                summary=f"检测到品牌: {', '.join(matched_brands)}",
                tasks=tasks,
                ocr_text=source_text or "",
                platform_hint=platform_hint,
            )
            return

        if not self._safe_mode_ocr_enabled():
            self._route_manual_capture(item.get("path", ""), tasks, platform_hint=platform_hint)
            return

        if not tasks:
            return

        guide_items = self._build_keyword_guide_items(tasks)
        if not guide_items:
            self._set_guide_status("当前没有待补关键词，等待下一轮任务")
            self._notify_manual_state_change()
            print("[Recognition] 当前没有待补关键词，跳过本次截图")
            return
        config = self._config_getter() or {}
        current_item = self._get_current_guide_item(tasks)
        focus_task = None
        focus_candidates = []
        hint_text = ""
        if current_item:
            focus_task = next((task for task in tasks if task.get("name") == current_item.get("task_name")), None)
            if focus_task:
                focus_candidates = self._get_current_item_candidates(current_item, focus_task)
                hint_text = (
                    f"当前任务组: {focus_task.get('name', '')}\n"
                    f"当前关键词: {current_item.get('keyword', '')}\n"
                    f"优先品牌: {', '.join(current_item.get('brands', []))}"
                )

        candidate_brands = focus_candidates or self._collect_global_candidates(tasks)
        if not candidate_brands:
            return

        ocr_text, ocr_meta = extract_text_from_image(config, image_path, candidate_brands)
        brands = list(ocr_meta.get("matched_brands", [])) or (match_candidate_brands(ocr_text, candidate_brands) if ocr_text else [])
        summary = build_match_summary(ocr_text, brands) if ocr_text else ""

        if brands:
            provider = ocr_meta.get("provider", "") or "local"
            variant = ocr_meta.get("variant", "") or "base"
            self._set_guide_status(f"本地 OCR 识别成功：{', '.join(brands)}")
            self._notify_manual_state_change()
            print(
                f"[Recognition] 本地OCR识别到品牌: {', '.join(brands)}; "
                f"provider={provider}; variant={variant}; text_len={len(ocr_text or '')}"
            )
            self._route_to_batches(image_path, brands, summary, tasks, platform_hint=platform_hint)
            return

        if focus_candidates and focus_task and is_ai_fallback_enabled(config):
            print(
                f"[Recognition] 当前任务组 {focus_task.get('name', '')} 本地OCR未命中，"
                "进入 AI fallback 复核"
            )
            brands, summary = detect_brands_from_image(
                config,
                image_path,
                focus_candidates,
                hint_text=hint_text,
            )

        if not brands and is_ai_fallback_enabled(config):
            global_candidates = self._collect_global_candidates(tasks)
            if global_candidates and global_candidates != candidate_brands:
                print("[Recognition] 本地OCR与任务内 fallback 均未命中，扩大到全局候选复核")
                brands, summary = detect_brands_from_image(config, image_path, global_candidates)

        if not brands:
            if ocr_text:
                provider = ocr_meta.get("provider", "") or "local"
                variant = ocr_meta.get("variant", "") or "base"
                self._set_guide_status("本地 OCR 未识别到目标品牌，继续等待截图")
                print(
                    f"[Recognition] 本地OCR未命中目标品牌; provider={provider}; "
                    f"variant={variant}; text_len={len(ocr_text)}"
                )
            else:
                self._set_guide_status("未检测到可用本地 OCR 结果，继续等待截图")
                print("[Recognition] 本地OCR未产出有效文本，继续等待截图")
            self._notify_manual_state_change()
            print("[Recognition] 剪切板截图未识别到目标品牌")
            return

        self._set_guide_status(f"截图识别成功：{', '.join(brands)}")
        self._notify_manual_state_change()
        print(f"[Recognition] 识别到品牌: {', '.join(brands)}; 总结: {summary or '无'}")
        self._route_to_batches(image_path, brands, summary, tasks, ocr_text=ocr_text, platform_hint=platform_hint)

    def _route_manual_capture(self, image_path: str, tasks: list[dict], platform_hint: str = ""):
        items = self._build_keyword_guide_items(tasks)
        if not items:
            print("[Recognition] 当前没有可用于人工确认的关键词")
            return

        task_map = {task["name"]: task for task in tasks}
        normalized_hint = self._normalize_platform_id(platform_hint)
        with self._lock:
            self._manual_index = max(0, min(self._manual_index, len(items) - 1))
            current = items[self._manual_index]
            task_name = current.get("task_name", "")
            task = task_map.get(task_name)
            if not task:
                return

            bucket = self._buffers.setdefault(task_name, [])
            current_platforms = [
                self._normalize_platform_id(platform)
                for platform in (current.get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            current_platforms = list(dict.fromkeys(current_platforms))
            if normalized_hint and normalized_hint in current_platforms:
                current_platforms = [normalized_hint]
            elif len(current_platforms) > 1:
                current_platforms = []
            matched_pairs = self._build_matched_pairs(
                current.get("keyword", ""),
                list(current.get("brands", task.get("brands", []))),
                current_platforms,
            )
            bucket.append({
                "path": image_path,
                "brands": list(current.get("brands", task.get("brands", []))),
                "summary": f"人工归类：{current.get('keyword', '')}",
                "ocr_text": "",
                "matched_pairs": matched_pairs,
            })
            buffered_count = len(bucket)
            batch_size = max(1, int(task.get("recognition_batch_size", 1) or 1))
            historical_count = int(current.get("historical_count", 0) or 0)

        print(
            f"[Recognition] 手动归类到 {task_name} / {current.get('keyword', '')}，"
            f"{self._format_threshold_progress_text(buffered_count, batch_size, historical_count)}"
        )
        self._set_guide_status(f"已归类到 {task_name}，继续手动确认")
        self._notify_manual_state_change()

    def _prepare_clipboard_image(self, image):
        config = self._config_getter() or {}
        recognition_cfg = config.get("recognition", {})
        max_width = max(640, int(recognition_cfg.get("max_image_width", 1440) or 1440))
        quality = max(55, min(95, int(recognition_cfg.get("jpeg_quality", 82) or 82)))

        if image.mode != "RGB":
            image = image.convert("RGB")

        if image.width > max_width:
            ratio = max_width / image.width
            image = image.resize((max_width, max(1, int(image.height * ratio))))

        from io import BytesIO

        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        image_bytes = output.getvalue()
        image_hash = hashlib.md5(image_bytes).hexdigest()
        return image_hash, image_bytes

    def _extract_clipboard_image(self, clip):
        if clip is None:
            return None
        if isinstance(clip, Image.Image):
            return clip
        if isinstance(clip, list):
            for item in clip:
                try:
                    path = Path(str(item))
                except Exception:
                    continue
                if not path.exists() or not path.is_file():
                    continue
                try:
                    with Image.open(path) as img:
                        return img.copy()
                except Exception:
                    continue
        return None

    def _prime_clipboard_baseline(self):
        self._clipboard_armed = True
        self._startup_clipboard_hash = None
        try:
            config = self._config_getter() or {}
            # 根据模式记录启动时的剪贴板状态
            if config.get("recognition", {}).get("dom_render_mode", False):
                # 文本模式：记录文本哈希
                text = self._poll_clipboard_text()
                if text:
                    self._startup_clipboard_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
                    self._clipboard_armed = False
                    print("[Recognition] 已记录启动时剪切板文本，后续仅监听新文本")
            else:
                # 截图模式：记录图片哈希（原有逻辑）
                clip = ImageGrab.grabclipboard()
                clip = self._extract_clipboard_image(clip)
                if clip is None:
                    return
                image_hash, _ = self._prepare_clipboard_image(clip)
                if image_hash:
                    self._startup_clipboard_hash = image_hash
                    self._clipboard_armed = False
                    print("[Recognition] 已记录启动时剪切板截图，后续仅监听新截图")
        except Exception as e:
            print(f"[Recognition] 初始化剪切板基线失败: {e}")

    def _persist_clipboard_image(self, image_hash: str, image_bytes: bytes):
        ts = datetime.now().strftime("%m%d_%H%M%S_%f")
        path = self._save_dir / f"clip_{ts}_{image_hash[:8]}.jpg"
        try:
            path.write_bytes(image_bytes)
            return path
        except Exception as e:
            print(f"[Recognition] 保存截图失败: {e}")
            return None

    def _parse_task_aliases(self, task: dict, default_brand: str):
        raw = task.get("recognition_brands", [])
        if isinstance(raw, str):
            items = [part.strip() for part in re.split(r"[\n,，、]+", raw) if part.strip()]
        elif isinstance(raw, (list, tuple, set)):
            items = [str(part).strip() for part in raw if str(part).strip()]
        elif isinstance(raw, dict):
            items = []
            for brand, aliases in raw.items():
                if brand:
                    items.append(str(brand).strip())
                if isinstance(aliases, str):
                    items.extend(part.strip() for part in re.split(r"[\n,，、]+", aliases) if part.strip())
                elif isinstance(aliases, (list, tuple, set)):
                    items.extend(str(part).strip() for part in aliases if str(part).strip())
        else:
            items = []

        aliases = []
        for item in items:
            if item and item != default_brand and item not in aliases:
                aliases.append(item)
        return aliases

    def _infer_platform_from_text(self, text: str, candidate_platforms: list[str]) -> str:
        normalized_text = str(text or "").strip().lower()
        if not normalized_text:
            return ""
        compact_text = re.sub(r"\s+", "", normalized_text)
        for platform in candidate_platforms:
            for token in PLATFORM_STRONG_HINTS.get(platform, ()):
                normalized_token = str(token or "").strip().lower()
                compact_token = re.sub(r"\s+", "", normalized_token)
                if normalized_token and (
                    normalized_token in normalized_text or
                    (compact_token and compact_token in compact_text)
                ):
                    return platform
        best_platform = ""
        best_score = 0
        second_score = 0
        for platform in candidate_platforms:
            score = 0
            for token, weight in PLATFORM_INFERENCE_TOKENS.get(platform, ()):
                normalized_token = str(token or "").strip().lower()
                compact_token = re.sub(r"\s+", "", normalized_token)
                if normalized_token and (
                    normalized_token in normalized_text or
                    (compact_token and compact_token in compact_text)
                ):
                    score += int(weight)
            if score > best_score:
                second_score = best_score
                best_platform = platform
                best_score = score
            elif score > second_score:
                second_score = score
        if best_score < 16:
            return ""
        if second_score and (best_score - second_score) < 6:
            return ""
        return best_platform

    def _infer_platform_from_body_text(self, text: str, candidate_platforms: list[str]) -> str:
        normalized_text = str(text or "").strip().lower()
        if not normalized_text:
            return ""
        compact_text = re.sub(r"\s+", "", normalized_text)
        best_platform = ""
        best_score = 0
        second_score = 0
        for platform in candidate_platforms:
            score = 0
            for token, weight in PLATFORM_INFERENCE_TOKENS.get(platform, ()):
                normalized_token = str(token or "").strip().lower()
                compact_token = re.sub(r"\s+", "", normalized_token)
                if not normalized_token:
                    continue
                if normalized_token in normalized_text or (compact_token and compact_token in compact_text):
                    score += int(weight)
            if score > best_score:
                second_score = best_score
                best_platform = platform
                best_score = score
            elif score > second_score:
                second_score = score

        # 正文里经常会提到别的平台名，必须提高阈值并要求明显领先，避免把豆包正文里的
        # “DeepSeek”内容误判成 DeepSeek 界面。
        if best_score < 22:
            return ""
        if second_score and (best_score - second_score) < 10:
            return ""
        return best_platform

    def _extract_bottom_hint_text(self, image_path: str, config: dict) -> str:
        temp_path = None
        try:
            with Image.open(image_path) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                crop_h = min(max(int(img.height * 0.18), 110), 220)
                if crop_h >= img.height:
                    return ""
                bottom = img.crop((0, img.height - crop_h, img.width, img.height))
                temp_path = self._save_dir / f"bottom_hint_{int(time.time() * 1000)}_{Path(image_path).stem}.jpg"
                bottom.save(temp_path, format="JPEG", quality=90, optimize=True)
            text, _ = extract_text_from_image(config, str(temp_path), None)
            return str(text or "").strip()
        except Exception:
            return ""
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _resolve_image_platform_with_bottom_hint(
        self,
        image_path: str,
        task: dict,
        config: dict,
        ocr_text: str = "",
        *,
        need_bottom_hint: bool = False,
    ) -> tuple[str, str]:
        candidates = []
        for platform in (task.get("platform_candidates") or []):
            normalized = self._normalize_platform_id(platform)
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        bottom_text = ""
        image_file = str(image_path or "").strip()
        should_read_bottom = bool(need_bottom_hint or len(candidates) != 1)
        if image_file and Path(image_file).exists() and should_read_bottom:
            bottom_text = self._extract_bottom_hint_text(image_file, config)

        if len(candidates) == 1:
            return candidates[0], bottom_text

        if not candidates:
            return "", bottom_text

        if bottom_text:
            inferred = self._infer_platform_from_text(bottom_text, candidates)
            if inferred:
                print(
                    f"[Recognition] 平台推断命中底部气泡: {inferred}; "
                    f"candidates={candidates}; bottom_text={bottom_text[:120]!r}"
                )
                return inferred, bottom_text

        inferred = self._infer_platform_from_body_text(ocr_text, candidates)
        if inferred:
            print(
                f"[Recognition] 平台推断命中正文兜底: {inferred}; "
                f"candidates={candidates}; body_text={str(ocr_text or '')[:120]!r}"
            )
            return inferred, bottom_text

        print(
            f"[Recognition] 平台推断未命中，回退通用装饰; "
            f"candidates={candidates}; bottom_text={bottom_text[:80]!r}"
        )
        return "", bottom_text

    def _resolve_image_platform(self, image_path: str, task: dict, config: dict, ocr_text: str = "") -> str:
        platform_name, _ = self._resolve_image_platform_with_bottom_hint(
            image_path,
            task,
            config,
            ocr_text,
        )
        return platform_name

    def _should_trim_input_bubble(self, text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        return any(token.lower() in normalized for token in INPUT_BUBBLE_HINTS)

    def _build_decorated_send_image(
        self,
        *,
        image_path: str,
        task: dict,
        platform_name: str,
        brand: str,
        keyword: str,
        bubble_hint_text: str,
    ) -> str:
        from core.screenshot_tools import decorate_screenshot

        src = Path(image_path)
        output_dir = self._save_dir / "decorated"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{src.stem}_{platform_name or 'recognition'}_decorated.jpg"

        with Image.open(src) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            if self._should_trim_input_bubble(bubble_hint_text):
                crop_h = min(max(int(img.height * 0.16), 110), 240)
                if img.height - crop_h >= int(img.height * 0.55):
                    img = img.crop((0, 0, img.width, img.height - crop_h))
            img.save(output_path, format="JPEG", quality=90, optimize=True)

        decorate_screenshot(
            str(output_path),
            platform_name=platform_name,
            brand=brand,
            keyword=keyword,
        )
        return str(output_path)

    def _build_text_mode_send_image(
        self,
        *,
        image_path: str,
        source_text: str,
        platform_name: str,
        brand: str,
        keyword: str,
    ) -> str:
        from platforms.html_renderer import render_text_to_screenshot

        src = Path(image_path) if str(image_path or "").strip() else None
        output_dir = self._save_dir / "decorated"
        output_dir.mkdir(parents=True, exist_ok=True)
        if src is not None and src.is_file():
            output_stem = src.stem
        else:
            signature = "||".join([
                str(source_text or ""),
                str(platform_name or ""),
                str(brand or ""),
                str(keyword or ""),
            ])
            text_hash = hashlib.md5(signature.encode("utf-8")).hexdigest()[:8]
            output_stem = f"text_{text_hash}"
        output_path = output_dir / f"{output_stem}_{platform_name or 'recognition'}_dom.jpg"

        rendered_path = render_text_to_screenshot(
            text=source_text,
            platform=platform_name,
            keyword=keyword,
            brand=brand,
            output_path=str(output_path),
            include_badges=True,
        )
        rendered_file = Path(str(rendered_path or output_path))
        if rendered_file.exists():
            return str(rendered_file)
        if src is not None and src.is_file():
            print(f"[Recognition] DOM文本模板渲染失败，回退发送原图: {src}")
            return str(src)
        print("[Recognition] DOM文本模板渲染失败，且无可回退原图")
        return ""

    def _prepare_send_images(self, batch: dict, config: dict) -> tuple[list[str], list[str]]:
        prepare_started = time.perf_counter()
        image_items = list(batch.get("image_items") or [])
        if not image_items:
            image_items = [{"path": path, "ocr_text": "", "source_text": ""} for path in (batch.get("image_paths") or [])]

        processed_paths: list[str] = []
        detected_platforms: list[str] = []
        task = batch.get("task") or {}
        brand = str((batch.get("brands") or [task.get("primary_brand", "")])[0] or task.get("primary_brand", "")).strip()
        matched_pairs = self._expand_matched_pair_slots(batch.get("matched_pairs") or [])

        for index, item in enumerate(image_items):
            image_path = str(item.get("path", "") or "").strip()
            image_file_exists = bool(image_path and Path(image_path).exists())
            ocr_text = str(item.get("ocr_text", "") or "").strip()
            source_text = str(item.get("source_text", "") or "").strip()
            if not image_file_exists and not source_text:
                continue
            matched_pair = matched_pairs[index] if index < len(matched_pairs) else (matched_pairs[0] if len(matched_pairs) == 1 else {})
            pair_platforms = [
                self._normalize_platform_id(platform)
                for platform in ((matched_pair or {}).get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            platform_name = ""
            bubble_hint_text = ""
            if source_text and len(pair_platforms) == 1:
                platform_name = pair_platforms[0]
            if not platform_name:
                if source_text:
                    platform_name = self._resolve_image_platform(image_path, task, config, ocr_text)
                else:
                    platform_name, bubble_hint_text = self._resolve_image_platform_with_bottom_hint(
                        image_path,
                        task,
                        config,
                        ocr_text,
                        need_bottom_hint=True,
                    )
                platform_name = platform_name or "recognition"
            keyword = str((matched_pair or {}).get("keyword") or "").strip()
            pair_brand = str((matched_pair or {}).get("brand") or "").strip() or brand
            try:
                if source_text:
                    processed_path = self._build_text_mode_send_image(
                        image_path=image_path,
                        source_text=source_text,
                        platform_name=platform_name,
                        brand=pair_brand,
                        keyword=keyword,
                    )
                else:
                    processed_path = self._build_decorated_send_image(
                        image_path=image_path,
                        task=task,
                        platform_name=platform_name,
                        brand=pair_brand,
                        keyword=keyword,
                        bubble_hint_text=bubble_hint_text,
                    )
            except Exception as exc:
                print(f"[Recognition] 截图装饰失败，回退原图: {exc}")
                processed_path = image_path if image_file_exists else ""
            if processed_path:
                processed_paths.append(processed_path)
            if platform_name and platform_name != "recognition":
                detected_platforms.append(platform_name)

        print(
            f"[Recognition] 图片预处理耗时: task={batch.get('task_name', '')}, "
            f"items={len(image_items)}, output={len(processed_paths)}, "
            f"elapsed={time.perf_counter() - prepare_started:.2f}s"
        )
        return processed_paths, detected_platforms

    def _attach_historical_progress(self, batch: dict) -> dict:
        task = batch.get("task") if isinstance(batch.get("task"), dict) else {}
        if self._is_manual_test_task(task):
            image_items = list(batch.get("image_items") or [])
            current_paths = [
                str(path).strip()
                for path in (batch.get("image_paths") or [])
                if str(path).strip() and Path(str(path).strip()).exists()
            ]
            current_count = len(image_items) if image_items else len(current_paths)
            batch["historical_screenshot_paths"] = []
            batch["historical_screenshot_count"] = 0
            batch["historical_query_platforms"] = []
            batch["historical_completed_keywords"] = []
            batch["current_image_count"] = current_count
            batch["total_image_count"] = current_count
            batch["image_paths"] = current_paths
            return batch

        task_name = str(batch.get("task_name") or "").strip()
        if not task_name:
            image_items = list(batch.get("image_items") or [])
            current_paths = [
                str(path).strip()
                for path in (batch.get("image_paths") or [])
                if str(path).strip() and Path(str(path).strip()).exists()
            ]
            current_count = len(image_items) if image_items else len(current_paths)
            batch["historical_screenshot_paths"] = []
            batch["historical_screenshot_count"] = 0
            batch["historical_query_platforms"] = []
            batch["historical_completed_keywords"] = []
            batch["current_image_count"] = current_count
            batch["total_image_count"] = current_count
            return batch

        historical_progress = self._get_task_daily_progress(batch.get("task") or {"name": task_name})
        historical_screenshot_paths = [
            str(item).strip()
            for item in (historical_progress.get("historical_screenshot_paths") or [])
            if str(item).strip() and Path(str(item).strip()).exists()
        ]
        historical_query_platforms = [
            str(item).strip()
            for item in (historical_progress.get("historical_query_platforms") or [])
            if str(item).strip()
        ]
        historical_completed_keywords = list(historical_progress.get("completed_keywords") or [])

        image_items = list(batch.get("image_items") or [])
        current_paths = [
            str(path).strip()
            for path in (batch.get("image_paths") or [])
            if str(path).strip() and Path(str(path).strip()).exists()
        ]
        current_count = len(image_items) if image_items else len(current_paths)
        merged_image_paths: list[str] = []
        seen_image_paths: set[str] = set()
        for path in historical_screenshot_paths + current_paths:
            if not path or path in seen_image_paths:
                continue
            seen_image_paths.add(path)
            merged_image_paths.append(path)

        batch["historical_screenshot_paths"] = historical_screenshot_paths
        batch["historical_screenshot_count"] = len(historical_screenshot_paths)
        batch["historical_query_platforms"] = historical_query_platforms
        batch["historical_completed_keywords"] = historical_completed_keywords
        batch["current_image_count"] = current_count
        batch["total_image_count"] = len(historical_screenshot_paths) + current_count
        batch["image_paths"] = merged_image_paths
        return batch

    def _format_batch_image_progress(self, batch: dict) -> str:
        current_count = int(batch.get("current_image_count") or len(batch.get("image_paths") or []))
        historical_count = int(batch.get("historical_screenshot_count") or 0)
        total_count = int(batch.get("total_image_count") or len(batch.get("image_paths") or []))
        return f"累计 {total_count} 张（本次 {current_count} 张，历史 {historical_count} 张）"

    def _build_keyword_updates_from_batch(
        self,
        batch: dict,
        *,
        image_paths: list[str],
        detected_platforms: list[str],
    ) -> list[dict]:
        matched_pairs = self._expand_matched_pair_slots(batch.get("matched_pairs") or [])
        normalized_paths = []
        seen_paths = set()
        for path in (image_paths or []):
            path_text = str(path).strip()
            if not path_text or path_text in seen_paths:
                continue
            seen_paths.add(path_text)
            normalized_paths.append(path_text)
        normalized_platforms = [
            self._normalize_platform_id(platform)
            for platform in (detected_platforms or [])
            if self._normalize_platform_id(platform)
        ]
        updates = []
        path_index = 0
        for index, pair in enumerate(matched_pairs):
            keyword = str(pair.get("keyword") or "").strip()
            brand = str(pair.get("brand") or "").strip()
            pair_platforms = [
                self._normalize_platform_id(platform)
                for platform in (pair.get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            platform_name = (
                (pair_platforms[0] if pair_platforms else "")
                or (normalized_platforms[0] if normalized_platforms else "")
            )
            image_path = ""
            if path_index < len(normalized_paths):
                image_path = normalized_paths[path_index]
                path_index += 1
            if not keyword:
                continue
            updates.append({
                "keyword": keyword,
                "brand": brand,
                "run_success": True,
                "screenshot_saved": bool(image_path),
                "failure_reason": "" if image_path else "screenshot_save_failed",
                "platform": platform_name,
                "image_path": image_path,
            })
        if len(matched_pairs) > len(normalized_paths):
            print(
                f"[Recognition] 关键词与截图严格一对一绑定，"
                f"当前仅有 {len(normalized_paths)} 张唯一截图，"
                f"剩余 {len(matched_pairs) - len(normalized_paths)} 个关键词保留缺口"
            )
        return updates

    def _extract_current_send_state_from_pool_updates(
        self,
        keyword_updates: list[dict],
        applied_pool: dict,
    ) -> tuple[list[str], list[str]]:
        """Resolve paths moved by the daily-state canonicalization for this send only."""
        pool_keywords = dict((applied_pool or {}).get("keywords") or {})
        image_paths: list[str] = []
        platforms: list[str] = []
        seen_paths: set[str] = set()

        for update in keyword_updates or []:
            keyword = str((update or {}).get("keyword") or "").strip()
            if not keyword:
                continue
            update_platform = self._normalize_platform_id((update or {}).get("platform", ""))
            state = pool_keywords.get(keyword)
            if not isinstance(state, dict):
                continue

            candidate_state = state
            platform_states = state.get("platform_states") if isinstance(state.get("platform_states"), dict) else {}
            if update_platform and isinstance(platform_states.get(update_platform), dict):
                candidate_state = platform_states[update_platform]

            path = str((candidate_state or {}).get("image_path") or "").strip()
            if path and path not in seen_paths and Path(path).exists():
                seen_paths.add(path)
                image_paths.append(path)

            platform_text = self._normalize_platform_id((candidate_state or {}).get("platform", "")) or update_platform
            if platform_text and platform_text not in platforms:
                platforms.append(platform_text)

        return image_paths, platforms

    def _expand_matched_pair_slots(self, matched_pairs: list[dict]) -> list[dict]:
        slots: list[dict] = []
        for pair in self._serialize_matched_pairs(matched_pairs or []):
            keyword = str(pair.get("keyword") or "").strip()
            brand = str(pair.get("brand") or "").strip()
            platforms = [
                self._normalize_platform_id(platform)
                for platform in (pair.get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            platforms = list(dict.fromkeys(platforms))
            if not platforms:
                slots.append({
                    "keyword": keyword,
                    "brand": brand,
                    "platforms": [],
                })
                continue
            for platform in platforms:
                slots.append({
                    "keyword": keyword,
                    "brand": brand,
                    "platforms": [platform],
                })
        return slots

    def _register_brand_alias(self, alias_map: dict, canonical_brand: str, alias: str):
        key = self._normalize_brand(alias)
        if key and canonical_brand:
            alias_map[key] = canonical_brand

    def _normalize_brand(self, value: str):
        return str(value or "").strip().lower().replace(" ", "")

    def _normalize_platform_id(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return PLATFORM_ID_ALIASES.get(raw, PLATFORM_ID_ALIASES.get(raw.lower(), raw.lower()))

    def _extract_name_keys(self, value: str) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []

        keys = []
        whole = self._normalize_brand(text)
        if whole:
            keys.append(whole)

        parts = [
            self._normalize_brand(part)
            for part in re.split(r"[\s\-_/|｜()（）\[\]【】,:：]+", text)
            if str(part).strip()
        ]
        for part in parts:
            if part and part not in keys:
                keys.append(part)
        return keys

    def _build_matched_pairs(self, keyword: str, brands: list[str], platforms: list[str] | None = None) -> list[dict]:
        keyword_text = str(keyword or "").strip()
        pairs = []
        seen = set()
        normalized_platforms = [
            self._normalize_platform_id(platform)
            for platform in (platforms or [])
            if self._normalize_platform_id(platform)
        ]
        for brand in brands or []:
            brand_text = str(brand or "").strip()
            pair_key = (
                self._normalize_keyword_brand_pair(keyword_text, brand_text),
                tuple(dict.fromkeys(normalized_platforms)),
            )
            if not keyword_text or pair_key in seen:
                continue
            seen.add(pair_key)
            pairs.append({
                "keyword": keyword_text,
                "brand": brand_text,
                "platforms": list(dict.fromkeys(normalized_platforms)),
            })
        return pairs

    def _serialize_matched_pairs(self, matched_pairs: list[dict]) -> list[dict]:
        serialized = []
        seen = set()
        for item in matched_pairs or []:
            if not isinstance(item, dict):
                continue
            keyword = str(item.get("keyword") or "").strip()
            brand = str(item.get("brand") or "").strip()
            platforms = [
                self._normalize_platform_id(platform)
                for platform in (item.get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            pair_key = (
                self._normalize_keyword_brand_pair(keyword, brand),
                tuple(dict.fromkeys(platforms)),
            )
            if not keyword or pair_key in seen:
                continue
            seen.add(pair_key)
            serialized.append({
                "keyword": keyword,
                "brand": brand,
                "platforms": list(dict.fromkeys(platforms)),
            })
        return serialized

    def _task_name_overlap_score(self, task: dict, brand: str, canonical_brand: str) -> int:
        task_keys = task.get("task_name_keys", [])
        if not task_keys:
            return 0

        brand_keys = [
            self._normalize_brand(brand),
            self._normalize_brand(canonical_brand),
        ]
        best = 0
        for brand_key in brand_keys:
            if not brand_key:
                continue
            for task_key in task_keys:
                if not task_key:
                    continue
                if task_key == brand_key:
                    best = max(best, 300)
                elif brand_key in task_key:
                    best = max(best, 220)
                elif task_key in brand_key:
                    best = max(best, 160)
        return best

    def _score_task_brand_match(self, task: dict, brand: str, canonical_brand: str) -> int:
        score = 100
        score += self._task_name_overlap_score(task, brand, canonical_brand)

        primary_brand_key = self._normalize_brand(task.get("primary_brand", ""))
        canonical_key = self._normalize_brand(canonical_brand)
        if primary_brand_key and canonical_key and primary_brand_key == canonical_key:
            score += 20

        return score

    def _select_task_targets(self, brands: list[str], tasks: list[dict]) -> dict[str, list[str]]:
        routed: dict[str, list[str]] = {}

        for brand in brands:
            brand_key = self._normalize_brand(brand)
            if not brand_key:
                continue

            candidates = []
            for task in tasks:
                canonical_brand = task.get("brand_alias_map", {}).get(brand_key)
                if not canonical_brand:
                    continue
                score = self._score_task_brand_match(task, brand, canonical_brand)
                candidates.append((score, -int(task.get("order", 0) or 0), task, canonical_brand))

            if not candidates:
                continue

            candidates.sort(reverse=True, key=lambda item: (item[0], item[1]))
            best_score, _, best_task, canonical_brand = candidates[0]

            if len(candidates) > 1 and candidates[1][0] == best_score:
                print(
                    f"[Recognition] 品牌 {brand} 存在多个候选任务组，"
                    f"已按配置顺序归入 {best_task['name']}"
                )

            matched = routed.setdefault(best_task["name"], [])
            if canonical_brand not in matched:
                matched.append(canonical_brand)

            print(
                f"[Recognition] 品牌 {brand} -> 任务组 {best_task['name']} "
                f"(归一品牌: {canonical_brand})"
            )

        return routed

    def _idle_timeout_seconds(self):
        config = self._config_getter() or {}
        recognition_cfg = config.get("recognition", {})
        return max(300, int(recognition_cfg.get("idle_manual_switch_seconds", 1800) or 1800))

    def _batch_flush_seconds(self):
        config = self._config_getter() or {}
        recognition_cfg = config.get("recognition", {})
        return max(30, float(recognition_cfg.get("batch_flush_seconds", 120) or 120))

    def _check_flush_timeout(self):
        """若 buffer 中有未满批次且超过 flush 超时，自动发送。"""
        if not self._work_started or not self._last_image_seen_at:
            return
        elapsed = (datetime.now() - self._last_image_seen_at).total_seconds()
        if elapsed < self._batch_flush_seconds():
            return
        tasks = self._get_enabled_tasks()
        task_map = {t["name"]: t for t in tasks}
        with self._lock:
            to_flush = [(name, list(items)) for name, items in self._buffers.items() if items]
        for task_name, items in to_flush:
            task = task_map.get(task_name)
            if not task:
                continue
            with self._lock:
                bucket = self._buffers.get(task_name, [])
                if not bucket:
                    continue
                batch_items = list(bucket)
                del bucket[:]
                batch_id = f"{task_name}_flush_{int(time.time() * 1000)}"
                batch = {
                    "id": batch_id,
                    "task_name": task_name,
                    "brands": list(dict.fromkeys(b for item in batch_items for b in item["brands"])),
                    "image_paths": [item["path"] for item in batch_items],
                    "image_items": [
                        {
                            "path": item["path"],
                            "ocr_text": item.get("ocr_text", ""),
                            "source_text": item.get("source_text", ""),
                        }
                        for item in batch_items
                    ],
                    "matched_pairs": self._serialize_matched_pairs(
                        [pair for item in batch_items for pair in (item.get("matched_pairs") or [])]
                    ),
                    "summary": "；".join(item["summary"] for item in batch_items if item.get("summary")),
                    "task": task,
                }
                self._attach_historical_progress(batch)
                auto_send = self._auto_send_recognition_batches_enabled()
                if not auto_send:
                    self._pending_batches[batch_id] = batch
            if auto_send:
                print(
                    f"[Recognition] {task_name} 超时自动 flush，"
                    f"{self._format_batch_image_progress(batch)}，自动发送中"
                )
                self._send_queue.put(batch)
            else:
                print(
                    f"[Recognition] {task_name} 超时自动 flush，"
                    f"{self._format_batch_image_progress(batch)}，等待确认"
                )
                if self._on_batch_ready:
                    self._on_batch_ready(batch)

    def _has_unfinished_work(self):
        with self._lock:
            has_buffer = any(bool(items) for items in self._buffers.values())
            has_pending = bool(self._pending_batches)
        return any([
            has_buffer,
            has_pending,
            not self._recognition_queue.empty(),
            not self._send_queue.empty(),
            self._recognizing,
            self._sending,
        ])

    def _check_idle_timeout(self):
        if not self._work_started or self._idle_reminded or not self._has_unfinished_work():
            return
        if not self._last_image_seen_at:
            return
        elapsed = datetime.now() - self._last_image_seen_at
        if elapsed < timedelta(seconds=self._idle_timeout_seconds()):
            return

        self._idle_reminded = True
        minutes = int(elapsed.total_seconds() // 60)
        message = f"识别模式已有 {minutes} 分钟未收到新截图，且仍有未完成批次，请手动切换到抓取模式。"
        print(f"[Recognition] {message}")
        if self._on_manual_switch_required:
            self._on_manual_switch_required({
                "mode": "recognition",
                "target_mode": "capture",
                "message": message,
            })

    def _notify_mode_change(self, mode: str, reason: str = ""):
        if self._on_mode_change:
            self._on_mode_change({
                "mode": mode,
                "reason": reason,
            })

    def _notify_round_complete(self, payload: dict | None = None):
        if not self._on_round_complete:
            return
        try:
            self._on_round_complete(payload or {})
        except Exception as exc:
            print(f"[Recognition] 识别轮次完成回调失败: {exc}")

    def _check_cycle_complete(self):
        if not self._work_started or self._has_unfinished_work():
            return
        if not self._matched_in_cycle:
            return
        tasks = self._get_enabled_tasks()
        items = self._build_keyword_guide_items(tasks)
        if items:
            if self._auto_send_recognition_batches_enabled():
                return
            if self._manual_index < len(items) - 1:
                return
        self._work_started = False
        self._idle_reminded = False
        self._notify_round_complete({
            "mode": "recognition",
            "reason": "识别批次已全部处理完成",
        })
        self._notify_mode_change("capture", "识别批次已全部处理完成，已自动切换为抓取模式")
        self.stop()

    def _build_task_matched_pairs(
        self,
        *,
        task_name: str,
        matched_brands: list[str],
        current_item: dict | None,
        guide_items: list[dict],
        platform_hint: str = "",
    ) -> list[dict]:
        normalized_hint = self._normalize_platform_id(platform_hint)

        def platforms_for_item(item: dict) -> list[str]:
            item_platforms = [
                self._normalize_platform_id(platform)
                for platform in ((item or {}).get("platforms") or [])
                if self._normalize_platform_id(platform)
            ]
            item_platforms = list(dict.fromkeys(item_platforms))
            if normalized_hint and (not item_platforms or normalized_hint in item_platforms):
                return [normalized_hint]
            if len(item_platforms) == 1:
                return item_platforms
            if len(item_platforms) > 1:
                return []
            return item_platforms

        if current_item and str(current_item.get("task_name") or "").strip() == str(task_name or "").strip():
            return self._build_matched_pairs(
                current_item.get("keyword", ""),
                list(current_item.get("brands", matched_brands)),
                platforms_for_item(current_item),
            )

        for item in guide_items or []:
            if str(item.get("task_name") or "").strip() != str(task_name or "").strip():
                continue
            return self._build_matched_pairs(
                item.get("keyword", ""),
                list(item.get("brands", matched_brands)),
                platforms_for_item(item),
            )
        return []

    def _is_duplicate(self, image_hash: str) -> bool:
        with self._lock:
            if image_hash in self._seen_set:
                return True
            self._seen_hashes.append(image_hash)
            self._seen_set.add(image_hash)
            while len(self._seen_set) > self._seen_hashes.maxlen:
                removed = self._seen_hashes.popleft()
                self._seen_set.discard(removed)
            return False

    def _route_to_batches(
        self,
        image_path: str,
        brands: list[str],
        summary: str,
        tasks: list[dict],
        ocr_text: str = "",
        platform_hint: str = "",
    ):
        routed = self._select_task_targets(brands, tasks)
        if not routed:
            self._set_guide_status("截图未匹配到具体任务组，继续等待截图")
            self._notify_manual_state_change()
            print("[Recognition] 当前截图未匹配到具体任务组")
            return
        self._matched_in_cycle = True

        guide_items = self._build_keyword_guide_items(tasks)
        current_index = 0
        current_item = None
        if guide_items:
            with self._lock:
                self._manual_index = max(0, min(self._manual_index, len(guide_items) - 1))
                current_index = self._manual_index
            current_item = dict(guide_items[current_index])
        focus_task_name = (current_item or {}).get("task_name", "")
        guide_advanced = False
        task_map = {task["name"]: task for task in tasks}
        for task_name, matched_brands in routed.items():
            task = task_map.get(task_name)
            if not task:
                continue
            progress = self._get_task_daily_progress(task)
            historical_count = int(progress.get("historical_screenshot_count", 0) or 0)
            progress_status_message = ""
            matched_pairs = self._build_task_matched_pairs(
                task_name=task_name,
                matched_brands=matched_brands,
                current_item=current_item,
                guide_items=guide_items,
                platform_hint=platform_hint,
            )

            with self._lock:
                bucket = self._buffers.setdefault(task_name, [])
                batch_item = {
                    "path": image_path,
                    "brands": matched_brands,
                    "summary": summary,
                    "ocr_text": ocr_text,
                    "source_text": ocr_text,
                    "matched_pairs": matched_pairs,
                }
                merged_pending_batch = self._merge_batch_item_into_pending_locked(task_name, task, batch_item)
                if merged_pending_batch is not None:
                    current_count = int(merged_pending_batch.get("current_image_count") or self._batch_image_count(merged_pending_batch))
                    total_count = int(merged_pending_batch.get("total_image_count") or current_count)
                    batch_size = task["recognition_batch_size"]
                    remaining_count = max(0, batch_size - total_count)
                    progress_text = self._format_threshold_progress_text(current_count, batch_size, historical_count)
                    if total_count >= batch_size and self._auto_send_recognition_batches_enabled():
                        batch_id = str(merged_pending_batch.get("id") or "").strip()
                        if batch_id:
                            self._pending_batches.pop(batch_id, None)
                        self._send_queue.put(merged_pending_batch)
                        guide_position = "none"
                        if self._should_advance_for_match(focus_task_name, task_name, guide_advanced):
                            remaining_current_platforms = self._remaining_current_platforms_after_match(
                                current_item,
                                merged_pending_batch.get("matched_pairs") or [],
                            )
                            guide_position = self._set_post_match_guide_index(
                                tasks,
                                guide_items,
                                current_index,
                                current_item,
                                remaining_current_platforms=remaining_current_platforms,
                            )
                            if guide_position in {"same_task", "next_task"}:
                                guide_advanced = True
                        if guide_position == "current":
                            self._set_guide_status(
                                f"截图识别成功，已补齐 {task_name} "
                                f"{progress_text}，正在自动发送并继续当前关键词剩余平台"
                            )
                        elif guide_position == "same_task":
                            self._set_guide_status(
                                f"截图识别成功，已补齐 {task_name} "
                                f"{progress_text}，正在自动发送并切到同品牌下一个关键词"
                            )
                        elif guide_position == "next_task":
                            self._set_guide_status(
                                f"截图识别成功，已补齐 {task_name} "
                                f"{progress_text}，正在自动发送并切到下一个品牌"
                            )
                        else:
                            self._set_guide_status(
                                f"截图识别成功，已补齐 {task_name} "
                                f"{progress_text}，正在自动发送"
                            )
                        print(
                            f"[Recognition] {task_name} 已补齐待确认批次，"
                            f"{progress_text}，自动发送中"
                        )
                        self._persist_task_progress_status(
                            task,
                            current_count=current_count,
                            batch_size=batch_size,
                            historical_count=historical_count,
                            status_message=f"识别模式已补齐：{progress_text}，正在自动发送",
                        )
                        self._notify_manual_state_change()
                        continue
                    if total_count >= batch_size:
                        status_text = (
                            f"截图识别成功，已补充到 {task_name} 待确认批次 "
                            f"{progress_text}，"
                            "可直接确认发布"
                        )
                        progress_status_message = f"识别模式已补齐：{progress_text}，等待确认发布"
                    else:
                        status_text = (
                            f"截图识别成功，已补充到 {task_name} 待确认批次 "
                            f"{progress_text}，"
                            "继续当前品牌截图"
                        )
                        progress_status_message = f"识别模式补图中：{progress_text}，还差 {remaining_count} 张"
                    self._set_guide_status(status_text)
                    self._notify_manual_state_change()
                    print(
                        f"[Recognition] {task_name} 已补充到待确认批次，"
                        f"{progress_text}"
                    )
                    self._persist_task_progress_status(
                        task,
                        current_count=current_count,
                        batch_size=batch_size,
                        historical_count=historical_count,
                        status_message=progress_status_message,
                    )
                    if self._on_batch_ready:
                        self._on_batch_ready(merged_pending_batch)
                    continue

                bucket.append(batch_item)
                batch_size = task["recognition_batch_size"]
                current_count = len(bucket)
                total_count = historical_count + current_count
                if total_count < batch_size:
                    remaining_count = max(0, batch_size - total_count)
                    progress_text = self._format_threshold_progress_text(current_count, batch_size, historical_count)
                    progress_status_message = f"识别模式补图中：{progress_text}，还差 {remaining_count} 张"
                    if self._auto_send_recognition_batches_enabled():
                        advanced_in_task = False
                        remaining_current_platforms = self._remaining_current_platforms_after_match(
                            current_item,
                            matched_pairs,
                        )
                        guide_position = "none"
                        if self._should_advance_for_match(focus_task_name, task_name, guide_advanced):
                            guide_position = self._set_post_match_guide_index(
                                tasks,
                                guide_items,
                                current_index,
                                current_item,
                                remaining_current_platforms=remaining_current_platforms,
                            )
                            if guide_position in {"same_task", "next_task"}:
                                guide_advanced = True
                                advanced_in_task = guide_position == "same_task"
                        if advanced_in_task:
                            self._set_guide_status(
                                f"截图识别成功，已加入 {task_name} "
                                f"{progress_text}，"
                                "切到同品牌下一个关键词"
                            )
                        elif guide_position == "next_task":
                            self._set_guide_status(
                                f"截图识别成功，已加入 {task_name} "
                                f"{progress_text}，"
                                "切到下一个品牌"
                            )
                        elif remaining_current_platforms:
                            self._set_guide_status(
                                f"截图识别成功，已加入 {task_name} "
                                f"{progress_text}，"
                                "继续当前关键词剩余平台"
                            )
                        else:
                            self._set_guide_status(
                                f"截图识别成功，已加入 {task_name} "
                                f"{progress_text}，"
                                "继续当前品牌截图"
                            )
                        self._notify_manual_state_change()
                    print(
                        f"[Recognition] {task_name} "
                        f"{progress_text}"
                    )
                    self._persist_task_progress_status(
                        task,
                        current_count=current_count,
                        batch_size=batch_size,
                        historical_count=historical_count,
                        status_message=progress_status_message,
                    )
                    continue

                batch_items = bucket[:batch_size]
                del bucket[:batch_size]
                batch_id = f"{task_name}_{int(time.time() * 1000)}"
                batch = {
                    "id": batch_id,
                    "task_name": task_name,
                    "brands": list(dict.fromkeys(brand for item in batch_items for brand in item["brands"])),
                    "image_paths": [item["path"] for item in batch_items],
                    "image_items": [
                        {
                            "path": item["path"],
                            "ocr_text": item.get("ocr_text", ""),
                            "source_text": item.get("source_text", ""),
                        }
                        for item in batch_items
                    ],
                    "matched_pairs": self._serialize_matched_pairs(
                        [pair for item in batch_items for pair in (item.get("matched_pairs") or [])]
                    ),
                    "summary": "；".join(item["summary"] for item in batch_items if item.get("summary")),
                    "task": task,
                }
                self._attach_historical_progress(batch)
                auto_send = self._auto_send_recognition_batches_enabled()
                if not auto_send:
                    self._pending_batches[batch_id] = batch

            if auto_send:
                progress_status_message = (
                    f"识别模式已补齐：{self._format_batch_image_progress(batch)}，正在自动发送"
                )
                self._send_queue.put(batch)
                if self._should_advance_for_match(focus_task_name, task_name, guide_advanced):
                    remaining_current_platforms = self._remaining_current_platforms_after_match(
                        current_item,
                        batch.get("matched_pairs") or [],
                    )
                    guide_position = self._set_post_match_guide_index(
                        tasks,
                        guide_items,
                        current_index,
                        current_item,
                        remaining_current_platforms=remaining_current_platforms,
                    )
                    if guide_position == "current":
                        self._set_guide_status(f"{task_name} 截图识别成功，正在自动发送并继续当前关键词剩余平台")
                    elif guide_position == "same_task":
                        guide_advanced = True
                        self._set_guide_status(f"{task_name} 截图识别成功，正在自动发送并切到同品牌下一个关键词")
                    elif guide_position == "next_task":
                        guide_advanced = True
                        self._set_guide_status(f"{task_name} 截图识别成功，正在自动发送并切到下一个品牌")
                    else:
                        self._set_guide_status(f"{task_name} 截图识别成功，正在自动发送")
                print(
                    f"[Recognition] {task_name} 已达到发送批次，"
                    f"{self._format_batch_image_progress(batch)}，自动发送中"
                )
            else:
                progress_status_message = (
                    f"识别模式已补齐：{self._format_batch_image_progress(batch)}，等待确认发布"
                )
                print(
                    f"[Recognition] {task_name} 已达到发送批次，"
                    f"{self._format_batch_image_progress(batch)}，等待确认"
                )
                if self._on_batch_ready:
                    self._on_batch_ready(batch)
            self._persist_task_progress_status(
                task,
                current_count=int(batch.get("current_image_count") or self._batch_image_count(batch)),
                batch_size=int(task.get("recognition_batch_size") or 1),
                historical_count=historical_count,
                status_message=progress_status_message,
            )
            self._notify_manual_state_change()

    def _send_loop(self):
        while True:
            try:
                batch = self._send_queue.get(timeout=1.0)
            except queue.Empty:
                if self._stop_event.is_set() and self._send_queue.empty():
                    return
                continue  # 每秒自检 _stop_event，确保 stop() 后能及时退出

            if batch is None:
                if self._stop_event.is_set() and self._send_queue.empty():
                    return
                continue
            self._sending = True
            with self._lock:
                self._active_send_batch = copy.deepcopy(batch)
            try:
                self._send_batch(batch)
            except Exception as exc:
                task_name = str((batch or {}).get("task_name") or "").strip()
                print(f"[Recognition] 发送批次异常: task={task_name or 'unknown'}, error={exc}")
                traceback.print_exc()
                self._set_guide_status(f"{task_name or '当前任务'} 发送失败：{exc}")
                self._notify_manual_state_change()
                if self._on_send_complete:
                    self._on_send_complete(batch, False, str(exc))
            finally:
                self._sending = False
                with self._lock:
                    self._active_send_batch = None
                self._check_cycle_complete()

    def _resolve_webhook(self, task_info: dict):
        return str(task_info.get("webhook_url", "")).strip()

    def _enqueue_wecom_retry(
        self,
        *,
        batch: dict,
        webhook_url: str,
        default_notify: dict,
        completed_keywords: list,
        supplemented_keywords: list,
        detected_platforms: list,
        daily_state_source: str,
        last_error: str,
    ) -> dict:
        try:
            return enqueue_wecom_notification(
                {
                    "task": dict(batch.get("task") or {}),
                    "daily_state_source": daily_state_source,
                    "daily_state_scope": "test" if daily_state_source == "manual_test" else "official",
                    "last_error": last_error,
                    "notifier": {
                        "webhook_url": webhook_url,
                        "cooldown_minutes": default_notify.get("cooldown_minutes", 30),
                        "send_interval": default_notify.get("send_interval", 2),
                    },
                    "send_args": {
                        "task_name": batch.get("task_name", ""),
                        "brands": list(batch.get("brands") or []),
                        "screenshot_paths": list(batch.get("image_paths") or []),
                        "detected_platforms": list(detected_platforms or []),
                        "source": "识别模式",
                        "completed_keywords": list(completed_keywords or []),
                        "supplemented_keywords": list(supplemented_keywords or []),
                        "total_screenshot_count": len(batch.get("image_paths") or []),
                    },
                }
            )
        except Exception as exc:
            print(f"[Recognition] 写入企业微信补发队列失败: task={batch.get('task_name', '')}, error={exc}")
            return {}

    def _enqueue_recognition_cloud_records(self, task: dict, record: dict | None) -> None:
        if not isinstance(record, dict):
            return
        cloud_task_id = (task or {}).get("cloud_task_id") or (task or {}).get("cloudTaskId")
        try:
            from core.cloud_run_sync import enqueue_run_record_from_history
        except Exception:
            return
        try:
            enqueue_run_record_from_history(record, cloud_task_id=cloud_task_id)
        except Exception:
            pass

    def _send_batch(self, batch: dict):
        from core.diagnostics import record_event

        send_batch_started = time.perf_counter()
        task = batch["task"]
        if self._is_task_suppressed_for_today(batch.get("task_name", "")):
            print(f"[Recognition] {batch.get('task_name', '')} 已被标记为今日跳过补齐，忽略待发送批次")
            self._check_cycle_complete()
            self._notify_manual_state_change()
            return
        daily_state_source = str(task.get("_daily_state_source") or "recognition")
        webhook_url = self._resolve_webhook(task)
        if not webhook_url or "YOUR_KEY_HERE" in webhook_url:
            print(f"[Recognition] {batch['task_name']} 未配置有效 webhook，跳过发送")
            self._set_guide_status(f"{batch['task_name']} 未配置有效 webhook，未发送")
            self._notify_manual_state_change()
            record_event(
                category="notification",
                message="识别模式未配置有效 webhook",
                task_name=batch["task_name"],
                platform="recognition",
                keyword="clipboard",
                brand=",".join(batch.get("brands", [])),
                details={"image_paths": batch.get("image_paths", [])},
            )
            status_extra = build_task_state_extra(
                brands=list(batch.get("brands", [])),
                image_count=len(batch.get("image_paths", [])),
                task_failure_kind="notification",
                notification_success=False,
            )
            if daily_state_source == "manual_test":
                write_task_status(
                    task,
                    status="send_failed",
                    source=daily_state_source,
                    scope="test",
                    message="识别模式未配置有效 webhook",
                    extra=status_extra,
                )
            else:
                write_task_status(
                    task,
                    status="send_failed",
                    source=daily_state_source,
                    message="识别模式未配置有效 webhook",
                    extra=status_extra,
                )
            if self._on_send_complete:
                self._on_send_complete(batch, False, "未配置有效 webhook")
            return

        config = self._config_getter() or {}
        default_notify = config.get("default_notification", {})
        notifier = WeComNotifier(
            webhook_url=webhook_url,
            cooldown_minutes=default_notify.get("cooldown_minutes", 30),
            send_interval=default_notify.get("send_interval", 2),
        )
        matched_pairs = self._serialize_matched_pairs(batch.get("matched_pairs") or [])
        self._attach_historical_progress(batch)
        completed_keywords = list(batch.get("historical_completed_keywords") or [])
        historical_completed_keywords = set(completed_keywords)
        historical_screenshot_paths = list(batch.get("historical_screenshot_paths") or [])
        historical_query_platforms = list(batch.get("historical_query_platforms") or [])
        supplemented_keywords = []
        for pair in matched_pairs:
            keyword = str(pair.get("keyword") or "").strip()
            if not keyword:
                continue
            if keyword not in historical_completed_keywords and keyword not in supplemented_keywords:
                supplemented_keywords.append(keyword)
            if keyword not in completed_keywords:
                completed_keywords.append(keyword)
        batch["supplemented_keywords"] = list(supplemented_keywords)
        batch["completed_keywords"] = list(completed_keywords)

        prepare_started = time.perf_counter()
        send_paths, detected_platforms = self._prepare_send_images(batch, config)
        prepare_elapsed = time.perf_counter() - prepare_started
        if send_paths:
            batch["image_paths"] = send_paths
        merged_image_paths = []
        seen_image_paths = set()
        for path in historical_screenshot_paths + list(batch.get("image_paths") or []):
            normalized = str(path or "").strip()
            if not normalized or normalized in seen_image_paths or not Path(normalized).exists():
                continue
            seen_image_paths.add(normalized)
            merged_image_paths.append(normalized)
        batch["image_paths"] = merged_image_paths
        merged_detected_platforms = []
        for platform_name in historical_query_platforms + list(detected_platforms or []):
            platform_text = str(platform_name or "").strip()
            if platform_text and platform_text not in merged_detected_platforms:
                merged_detected_platforms.append(platform_text)
        batch["detected_platforms"] = merged_detected_platforms
        print(
            f"[Recognition] 发送批次图片清单: task={batch['task_name']}, "
            f"count={len(merged_image_paths)}, paths={merged_image_paths}"
        )

        keyword_updates = self._build_keyword_updates_from_batch(
            batch,
            image_paths=list(send_paths or []),
            detected_platforms=list(merged_detected_platforms),
        )
        applied_pool: dict = {}
        state_started = time.perf_counter()
        if keyword_updates:
            applied_pool = apply_task_keyword_updates(
                task,
                keyword_updates,
                source_mode="test" if daily_state_source == "manual_test" else "formal",
            )
            if daily_state_source == "manual_test":
                missing_current_paths = any(
                    not Path(str((update or {}).get("image_path") or "").strip()).exists()
                    for update in keyword_updates
                    if str((update or {}).get("image_path") or "").strip()
                )
                if missing_current_paths:
                    refreshed_current_paths, refreshed_current_platforms = self._extract_current_send_state_from_pool_updates(
                        keyword_updates,
                        applied_pool,
                    )
                    if refreshed_current_paths:
                        if refreshed_current_paths != merged_image_paths:
                            print(
                                f"[Recognition] 当前测试图片路径已移动，发送路径刷新: task={batch['task_name']}, "
                                f"paths={refreshed_current_paths}"
                            )
                        merged_image_paths = list(refreshed_current_paths)
                        batch["image_paths"] = list(refreshed_current_paths)
                    if refreshed_current_platforms:
                        merged_detected_platforms = list(dict.fromkeys(refreshed_current_platforms))
                        batch["detected_platforms"] = list(merged_detected_platforms)

        updated_status = get_task_day_status(task)
        updated_progress = self._get_task_daily_progress(task)
        refreshed_image_paths: list[str] = []
        refreshed_platforms: list[str] = []
        if daily_state_source != "manual_test":
            refreshed_image_paths = [
                str(path).strip()
                for path in (updated_progress.get("historical_screenshot_paths") or [])
                if str(path).strip() and Path(str(path).strip()).exists()
            ]
            refreshed_platforms = [
                str(platform).strip()
                for platform in (updated_progress.get("historical_query_platforms") or [])
                if str(platform).strip()
            ]
            if not refreshed_image_paths and applied_pool:
                refreshed_image_paths, refreshed_platforms = self._extract_completed_send_state_from_pool(applied_pool)
        if refreshed_image_paths:
            if refreshed_image_paths != merged_image_paths:
                print(
                    f"[Recognition] 发送图片路径已刷新: task={batch['task_name']}, "
                    f"paths={refreshed_image_paths}"
                )
            merged_image_paths = list(refreshed_image_paths)
            batch["image_paths"] = list(refreshed_image_paths)
        if refreshed_platforms:
            merged_detected_platforms = list(dict.fromkeys(refreshed_platforms))
            batch["detected_platforms"] = list(merged_detected_platforms)
        completed_keywords = [
            str(item).strip()
            for item in (updated_status.get("completed_keywords") or [])
            if str(item).strip()
        ]
        supplemented_keywords = [
            keyword
            for keyword in completed_keywords
            if keyword not in historical_completed_keywords
        ]
        batch["supplemented_keywords"] = list(supplemented_keywords)
        batch["completed_keywords"] = list(completed_keywords)
        state_elapsed = time.perf_counter() - state_started

        if bool(updated_status.get("has_gap")):
            gap_text = "、".join(str(item).strip() for item in (updated_status.get("gap_reasons") or []) if str(item).strip())
            status_message = (
                f"识别模式已写入共享池，但仍有关键词缺口待补齐"
                + (f"：{gap_text}" if gap_text else "")
            )
            self._set_guide_status(status_message)
            self._persist_task_progress_status(
                task,
                current_count=0,
                batch_size=int(task.get("recognition_batch_size") or 1),
                historical_count=int(updated_progress.get("historical_screenshot_count", 0) or 0),
                status_message=status_message,
            )
            self._notify_manual_state_change()
            print(f"[Recognition] {batch['task_name']} 仍有关键词缺口，继续等待补齐后再发送")
            print(
                f"[Recognition] 发送批次耗时: task={batch['task_name']}, "
                f"prepare={prepare_elapsed:.2f}s, state={state_elapsed:.2f}s, "
                f"wecom=0.00s, total={time.perf_counter() - send_batch_started:.2f}s, "
                "status=gap"
            )
            return

        wecom_started = time.perf_counter()
        ok = notifier.send_detected_images(
            task_name=batch["task_name"],
            brands=batch["brands"],
            screenshot_paths=batch["image_paths"],
            detected_platforms=merged_detected_platforms,
            source="识别模式",
            completed_keywords=completed_keywords,
            supplemented_keywords=supplemented_keywords,
            total_screenshot_count=len(merged_image_paths),
        )
        wecom_elapsed = time.perf_counter() - wecom_started

        diagnostic_id = ""
        retry_entry = {}
        if not ok:
            diagnostic = record_event(
                category="notification",
                message=notifier.last_error or "识别模式企业微信发送失败",
                task_name=batch["task_name"],
                platform="recognition",
                keyword="clipboard",
                brand=",".join(batch.get("brands", [])),
                details={"image_paths": batch.get("image_paths", [])},
            )
            diagnostic_id = str(diagnostic.get("id", "")).strip()
            retry_entry = self._enqueue_wecom_retry(
                batch=batch,
                webhook_url=webhook_url,
                default_notify=default_notify,
                completed_keywords=completed_keywords,
                supplemented_keywords=supplemented_keywords,
                detected_platforms=merged_detected_platforms,
                daily_state_source=daily_state_source,
                last_error=notifier.last_error or "识别模式企业微信发送失败",
            )

        if ok or daily_state_source != "manual_test":
            for brand in batch["brands"]:
                written_entry = history_record(
                    batch["task_name"],
                    "recognition",
                    "clipboard",
                    brand,
                    1 if ok else 99,
                    ok,
                    task_id=str((task.get("task_id") or "").strip()),
                    details={
                        "screenshot": batch["image_paths"][0] if batch.get("image_paths") else "",
                        "evidence": batch.get("summary", ""),
                        "diagnostic_id": diagnostic_id,
                        "mode": "recognition",
                        "execution_source": daily_state_source,
                        "extra": {
                            "batch_id": str(batch.get("id") or "").strip(),
                            "image_count": len(batch.get("image_paths", [])),
                            "screenshot_paths": list(batch.get("image_paths") or []),
                            "detected_platforms": list(merged_detected_platforms),
                            "completed_keywords": list(completed_keywords),
                            "supplemented_keywords": list(supplemented_keywords),
                            "matched_pairs": matched_pairs,
                        },
                    },
                )
                self._enqueue_recognition_cloud_records(task, written_entry)

        self._set_guide_status(
            f"{batch['task_name']} 已发送，准备继续下一个关键词"
            if ok else
            (
                f"{batch['task_name']} 发送失败，已加入补发队列：{notifier.last_error or '未知错误'}"
                if retry_entry else
                f"{batch['task_name']} 发送失败：{notifier.last_error or '未知错误'}"
            )
        )
        if ok:
            status_extra = build_task_state_extra(
                brands=list(batch.get("brands", [])),
                image_count=len(batch.get("image_paths", [])),
                completed_keywords=list(completed_keywords),
                supplemented_keywords=list(supplemented_keywords),
                task_failure_kind="",
                notification_success=True,
            )
            if daily_state_source == "manual_test":
                write_task_status(
                    task,
                    status="success",
                    source=daily_state_source,
                    scope="test",
                    message="识别模式测试发送成功",
                    extra=status_extra,
                )
            else:
                write_task_status(
                    task,
                    status="success",
                    source=daily_state_source,
                    message="识别模式发送成功",
                    extra=status_extra,
                )
        elif not ok:
            retry_extra = {
                "diagnostic_id": diagnostic_id,
            }
            if retry_entry:
                retry_extra.update(
                    {
                        "retry_queue_id": str(retry_entry.get("id") or ""),
                        "retry_next_attempt_at": str(retry_entry.get("next_attempt_at") or ""),
                        "retry_attempt_count": int(retry_entry.get("attempt_count") or 0),
                    }
                )
            failure_extra = build_task_state_extra(
                brands=list(batch.get("brands", [])),
                image_count=len(batch.get("image_paths", [])),
                completed_keywords=list(completed_keywords),
                supplemented_keywords=list(supplemented_keywords),
                task_failure_kind="notification",
                notification_success=False,
                extra=retry_extra,
            )
            if daily_state_source == "manual_test":
                write_task_status(
                    task,
                    status="send_failed",
                    source=daily_state_source,
                    scope="test",
                    message=notifier.last_error or "识别模式企业微信发送失败",
                    extra=failure_extra,
                )
            else:
                write_task_status(
                    task,
                    status="send_failed",
                    source=daily_state_source,
                    message=notifier.last_error or "识别模式企业微信发送失败",
                    extra=failure_extra,
                )
        self._notify_manual_state_change()
        if self._on_send_complete:
            self._on_send_complete(batch, ok, "" if ok else (notifier.last_error or "企业微信发送失败"))
        print(
            f"[Recognition] 发送批次耗时: task={batch['task_name']}, "
            f"prepare={prepare_elapsed:.2f}s, state={state_elapsed:.2f}s, "
            f"wecom={wecom_elapsed:.2f}s, total={time.perf_counter() - send_batch_started:.2f}s, "
            f"status={'ok' if ok else 'failed'}"
        )
