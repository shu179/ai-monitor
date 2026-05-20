from __future__ import annotations

import queue
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.browser_processes import browser_profile_owner_pids, terminate_browser_profile_processes


_SESSION_RANDOM = secrets.SystemRandom()


DEFAULT_QUERY_EXECUTION_SETTINGS: dict[str, dict[str, Any]] = {
    "browser": {
        "strategy": "session_pool",
        "session_pool_dispatch": "platform_batch",
        "session_pool_platform_batch_size": 2,
        "session_ttl_minutes_min": 120,
        "session_ttl_minutes_max": 150,
        "session_max_queries_min": 110,
        "session_max_queries_max": 140,
        "min_queries_window_minutes": 30,
        "min_queries_per_window": 10,
        "single_query_timeout_minutes": 12,
        "no_progress_timeout_minutes": 30,
        "min_restart_cooldown_minutes": 10,
        "restart_after_manual_recovery": True,
        "restart_after_structural_failures": 2,
    },
    "smart": {
        "strategy": "session_pool",
        "session_pool_dispatch": "platform_batch",
        "session_pool_platform_batch_size": 2,
        "session_ttl_minutes_min": 120,
        "session_ttl_minutes_max": 150,
        "session_max_queries_min": 110,
        "session_max_queries_max": 140,
        "min_queries_window_minutes": 30,
        "min_queries_per_window": 10,
        "single_query_timeout_minutes": 12,
        "no_progress_timeout_minutes": 30,
        "min_restart_cooldown_minutes": 10,
        "restart_after_manual_recovery": True,
        "restart_after_structural_failures": 2,
    },
}

SUPPORTED_STRATEGIES = {"single_query_isolated", "platform_serial", "session_pool"}
SUPPORTED_SESSION_POOL_DISPATCHES = {"keyword_round_robin", "platform_batch"}
LEGACY_STRATEGY_ALIASES = {
    "single_query_isolated": "session_pool",
    "platform_serial": "session_pool",
}


def _safe_int(value: Any, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except Exception:
        return max(minimum, int(default))


def _safe_bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    return bool(value)


@dataclass(slots=True)
class QueryExecutionPolicy:
    mode: str
    strategy: str
    session_pool_dispatch: str
    session_pool_platform_batch_size: int
    session_ttl_minutes_min: int
    session_ttl_minutes_max: int
    session_max_queries_min: int
    session_max_queries_max: int
    min_queries_window_minutes: int
    min_queries_per_window: int
    single_query_timeout_minutes: int
    no_progress_timeout_minutes: int
    min_restart_cooldown_minutes: int
    restart_after_manual_recovery: bool
    restart_after_structural_failures: int

    @property
    def use_session_pool(self) -> bool:
        return self.strategy == "session_pool"

    @property
    def use_platform_batch_dispatch(self) -> bool:
        return self.use_session_pool and self.session_pool_dispatch == "platform_batch"


def normalize_query_execution_strategy(value: Any, default: str = "session_pool") -> str:
    strategy = str(value or default).strip() or default
    strategy = LEGACY_STRATEGY_ALIASES.get(strategy, strategy)
    if strategy not in SUPPORTED_STRATEGIES:
        return LEGACY_STRATEGY_ALIASES.get(default, default)
    return strategy


def normalize_session_pool_dispatch(value: Any, default: str = "platform_batch") -> str:
    dispatch = str(value or default).strip() or default
    if dispatch not in SUPPORTED_SESSION_POOL_DISPATCHES:
        return str(default or "platform_batch").strip() or "platform_batch"
    return dispatch


def build_query_execution_policy(config: dict | None, mode: str) -> QueryExecutionPolicy:
    normalized_mode = str(mode or "").strip()
    defaults = dict(DEFAULT_QUERY_EXECUTION_SETTINGS.get(normalized_mode, {}) or {})
    raw = (
        ((config or {}).get("query_execution", {}) or {}).get(normalized_mode, {}) or {}
        if normalized_mode
        else {}
    )
    force_mode_defaults = normalized_mode in {"browser", "smart"}
    strategy = normalize_query_execution_strategy(
        defaults.get("strategy", "session_pool") if force_mode_defaults else raw.get("strategy", defaults.get("strategy", "session_pool")),
        default=str(defaults.get("strategy", "session_pool") or "session_pool"),
    )
    session_pool_dispatch = normalize_session_pool_dispatch(
        (
            defaults.get("session_pool_dispatch", "platform_batch")
            if force_mode_defaults
            else raw.get("session_pool_dispatch", defaults.get("session_pool_dispatch", "platform_batch"))
        ),
        default=str(defaults.get("session_pool_dispatch", "platform_batch") or "platform_batch"),
    )
    session_pool_platform_batch_size = _safe_int(
        raw.get("session_pool_platform_batch_size"),
        defaults.get("session_pool_platform_batch_size", 2),
        1,
    )

    ttl_min = _safe_int(raw.get("session_ttl_minutes_min"), defaults.get("session_ttl_minutes_min", 120), 10)
    ttl_max = _safe_int(raw.get("session_ttl_minutes_max"), defaults.get("session_ttl_minutes_max", ttl_min), ttl_min)
    query_min = _safe_int(raw.get("session_max_queries_min"), defaults.get("session_max_queries_min", 110), 1)
    query_max = _safe_int(raw.get("session_max_queries_max"), defaults.get("session_max_queries_max", query_min), query_min)

    return QueryExecutionPolicy(
        mode=normalized_mode,
        strategy=strategy,
        session_pool_dispatch=session_pool_dispatch,
        session_pool_platform_batch_size=session_pool_platform_batch_size,
        session_ttl_minutes_min=ttl_min,
        session_ttl_minutes_max=ttl_max,
        session_max_queries_min=query_min,
        session_max_queries_max=query_max,
        min_queries_window_minutes=_safe_int(
            raw.get("min_queries_window_minutes"),
            defaults.get("min_queries_window_minutes", 30),
            1,
        ),
        min_queries_per_window=_safe_int(
            raw.get("min_queries_per_window"),
            defaults.get("min_queries_per_window", 10),
            1,
        ),
        single_query_timeout_minutes=_safe_int(
            raw.get("single_query_timeout_minutes"),
            defaults.get("single_query_timeout_minutes", 12),
            1,
        ),
        no_progress_timeout_minutes=_safe_int(
            raw.get("no_progress_timeout_minutes"),
            defaults.get("no_progress_timeout_minutes", 30),
            1,
        ),
        min_restart_cooldown_minutes=_safe_int(
            raw.get("min_restart_cooldown_minutes"),
            defaults.get("min_restart_cooldown_minutes", 10),
            0,
        ),
        restart_after_manual_recovery=_safe_bool(
            raw.get("restart_after_manual_recovery"),
            defaults.get("restart_after_manual_recovery", True),
        ),
        restart_after_structural_failures=_safe_int(
            raw.get("restart_after_structural_failures"),
            defaults.get("restart_after_structural_failures", 2),
            1,
        ),
    )


def build_round_query_plan(units: list[dict], mode: str) -> dict[str, int]:
    plan: dict[str, int] = {}
    normalized_mode = str(mode or "").strip()
    for unit in units or []:
        for kw in (unit.get("keywords", []) or []):
            keyword = str((kw or {}).get("keyword") or "").strip()
            platforms = [str(item).strip() for item in ((kw or {}).get("platforms", []) or []) if str(item).strip()]
            kw_mode = str((kw or {}).get("mode") or normalized_mode or "browser").strip()
            if kw_mode != normalized_mode or not keyword or not platforms:
                continue
            for platform_name in platforms:
                plan[platform_name] = int(plan.get(platform_name, 0) or 0) + 1
    return plan


def build_session_pool_dispatch_pairs(
    executable_entries: list[dict],
    ordered_platforms: list[str],
    entries_by_platform: dict[str, list[dict]],
    platform_session_manager: "PlatformSessionManager | None",
    *,
    logger=None,
):
    if not executable_entries:
        return
    policy = platform_session_manager.policy if platform_session_manager is not None else None
    if not getattr(policy, "use_platform_batch_dispatch", False):
        for entry in executable_entries:
            for platform_name in (entry.get("platforms", []) or []):
                yield entry, platform_name
        return

    batch_size = max(1, int(getattr(policy, "session_pool_platform_batch_size", 1) or 1))
    positions = {
        platform_name: 0
        for platform_name in ordered_platforms
    }
    ordered_index = {
        platform_name: index
        for index, platform_name in enumerate(ordered_platforms)
    }
    last_selected_platform = ""

    def _platform_dispatch_score(platform_name: str) -> tuple[float, dict]:
        remaining_entries_count = max(
            0,
            len(entries_by_platform.get(platform_name, [])) - positions.get(platform_name, 0),
        )
        score = float(remaining_entries_count * 100)
        reasons = {
            "remaining_entries": remaining_entries_count,
            "has_live_session": False,
            "completed_queries": 0,
            "consecutive_structural_failures": 0,
            "dirty_after_manual_recovery": False,
            "restart_cooldown_remaining_seconds": 0.0,
            "session_age_seconds": 0.0,
            "continued_from_last_round": bool(last_selected_platform and last_selected_platform == platform_name),
        }
        if platform_session_manager is None:
            return score, reasons
        snapshot = platform_session_manager.get_dispatch_snapshot(platform_name)
        if snapshot.get("has_live_session"):
            score += 18.0
        reasons["has_live_session"] = bool(snapshot.get("has_live_session"))
        if last_selected_platform and last_selected_platform == platform_name:
            score += 12.0
        completed_queries = int(snapshot.get("completed_queries", 0) or 0)
        consecutive_failures = int(snapshot.get("consecutive_structural_failures", 0) or 0)
        score -= float(completed_queries) * 3.0
        score -= float(consecutive_failures) * 45.0
        reasons["completed_queries"] = completed_queries
        reasons["consecutive_structural_failures"] = consecutive_failures
        if bool(snapshot.get("dirty_after_manual_recovery", False)):
            score -= 35.0
        reasons["dirty_after_manual_recovery"] = bool(snapshot.get("dirty_after_manual_recovery", False))
        cooldown_remaining = float(snapshot.get("restart_cooldown_remaining_seconds", 0.0) or 0.0)
        if cooldown_remaining > 0:
            score -= min(20.0, cooldown_remaining / 30.0)
        reasons["restart_cooldown_remaining_seconds"] = cooldown_remaining
        session_age_seconds = float(snapshot.get("session_age_seconds", 0.0) or 0.0)
        if session_age_seconds > 0:
            score += min(10.0, session_age_seconds / 120.0)
        reasons["session_age_seconds"] = session_age_seconds
        return score, reasons

    while True:
        round_candidates = [
            platform_name
            for platform_name in ordered_platforms
            if positions.get(platform_name, 0) < len(entries_by_platform.get(platform_name, []))
        ]
        if not round_candidates:
            break
        scheduled_this_round: set[str] = set()
        while round_candidates:
            scored_candidates = [
                (name, *_platform_dispatch_score(name))
                for name in round_candidates
            ]
            platform_name, selected_score, selected_reasons = max(
                scored_candidates,
                key=lambda item: (
                    item[1],
                    -ordered_index.get(item[0], 0),
                ),
            )
            round_candidates.remove(platform_name)
            if platform_name in scheduled_this_round:
                continue
            scheduled_this_round.add(platform_name)
            platform_entries = entries_by_platform.get(platform_name, [])
            start = positions.get(platform_name, 0)
            if start >= len(platform_entries):
                continue
            end = min(len(platform_entries), start + batch_size)
            if callable(logger):
                try:
                    logger(
                        "[Main] 会话池批次调度选择平台: "
                        f"platform={platform_name}, score={selected_score:.1f}, "
                        f"remaining_entries={selected_reasons['remaining_entries']}, "
                        f"live_session={'yes' if selected_reasons['has_live_session'] else 'no'}, "
                        f"completed={selected_reasons['completed_queries']}, "
                        f"structural_failures={selected_reasons['consecutive_structural_failures']}, "
                        f"manual_recovery={'yes' if selected_reasons['dirty_after_manual_recovery'] else 'no'}, "
                        f"cooldown_remaining={int(selected_reasons['restart_cooldown_remaining_seconds'])}s, "
                        f"session_age={int(selected_reasons['session_age_seconds'])}s, "
                        f"continue_prev={'yes' if selected_reasons['continued_from_last_round'] else 'no'}, "
                        f"batch={end - start}"
                    )
                except Exception:
                    pass
            for entry in platform_entries[start:end]:
                yield entry, platform_name
            positions[platform_name] = end
            last_selected_platform = platform_name


@dataclass(slots=True)
class PlatformSessionState:
    platform_name: str
    platform: Any
    started_at: float
    expire_at: float
    max_queries: int
    planned_queries_total: int
    completed_queries: int = 0
    last_progress_at: float | None = None
    consecutive_structural_failures: int = 0
    dirty_after_manual_recovery: bool = False
    last_query_started_at: float | None = None


@dataclass(slots=True)
class _PlatformWorkerRequest:
    action: str
    attr: str = ""
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] | None = None
    response_queue: queue.Queue | None = None


class _PlatformWorkerMethod:
    def __init__(self, proxy: "_PlatformWorkerProxy", name: str) -> None:
        self._proxy = proxy
        self._name = name

    def __call__(self, *args, **kwargs):
        return self._proxy._invoke_remote(self._name, *args, **kwargs)


class _PlatformWorkerProxy:
    STARTUP_TIMEOUT_SECONDS = 75
    CLOSE_REQUEST_TIMEOUT_SECONDS = 5
    CLOSE_JOIN_TIMEOUT_SECONDS = 5
    CLOSE_RECLAIM_JOIN_TIMEOUT_SECONDS = 3

    _LOCAL_ATTRS = {
        "_platform_name",
        "_factory",
        "_log",
        "_request_queue",
        "_startup_queue",
        "_startup_platform",
        "_thread",
        "_closed",
        "_profile_path",
        "_callable_attrs",
        "_value_attrs",
    }

    def __init__(
        self,
        platform_name: str,
        factory: Callable[[], Any],
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        object.__setattr__(self, "_platform_name", str(platform_name or "").strip())
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_log", logger or (lambda message: None))
        object.__setattr__(self, "_request_queue", queue.Queue())
        object.__setattr__(self, "_startup_queue", queue.Queue(maxsize=1))
        object.__setattr__(self, "_startup_platform", None)
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_profile_path", "")
        object.__setattr__(self, "_callable_attrs", set())
        object.__setattr__(self, "_value_attrs", set())

        worker = threading.Thread(
            target=self._worker_main,
            name=f"session-worker-{self._platform_name or 'platform'}",
            daemon=True,
        )
        object.__setattr__(self, "_thread", worker)
        worker.start()
        self._wait_until_ready()

    def _emit(self, message: str) -> None:
        try:
            self._log(f"[SessionWorker:{self._platform_name}] {message}")
        except Exception:
            pass

    def _wait_until_ready(self) -> None:
        try:
            ok, payload = self._startup_queue.get(timeout=self.STARTUP_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            self._abort_startup_platform()
            self.close()
            message = f"{self._platform_name} 浏览器工作线程启动超时，请检查浏览器窗口是否卡在系统弹窗或登录页"
            self._emit(message)
            raise TimeoutError(message) from exc
        if ok:
            self._emit(f"工作线程已就绪: {self._thread.name}")
            return
        self.close()
        self._emit(f"工作线程启动失败: {payload}")
        raise payload

    def _abort_startup_platform(self) -> None:
        platform = self._startup_platform
        if platform is None:
            return
        try:
            abort = getattr(platform, "abort_startup", None)
            if callable(abort):
                abort()
                self._emit("启动超时，已请求中止浏览器启动")
                return
        except Exception as exc:
            self._emit(f"启动超时中止失败: {exc}")
        try:
            if callable(getattr(platform, "close", None)):
                platform.close()
        except Exception as exc:
            self._emit(f"启动超时关闭平台失败: {exc}")

    def _worker_main(self) -> None:
        platform = None
        try:
            self._emit(f"工作线程启动: {threading.current_thread().name}")
            platform = self._factory()
            object.__setattr__(self, "_profile_path", str(getattr(platform, "user_data_dir", "") or "").strip())
            object.__setattr__(self, "_startup_platform", platform)
            if getattr(platform, "page", None) is None and callable(getattr(platform, "start", None)):
                platform = platform.start()
                object.__setattr__(self, "_profile_path", str(getattr(platform, "user_data_dir", "") or "").strip())
            self._startup_queue.put((True, None))
        except BaseException as exc:
            try:
                if platform is not None and callable(getattr(platform, "close", None)):
                    platform.close()
            except Exception:
                pass
            self._startup_queue.put((False, exc))
            return
        finally:
            object.__setattr__(self, "_startup_platform", None)

        while True:
            request = self._request_queue.get()
            if request.action == "shutdown":
                if request.response_queue is not None:
                    request.response_queue.put((True, None))
                break

            try:
                response = self._handle_request(platform, request)
                if request.response_queue is not None:
                    request.response_queue.put((True, response))
            except BaseException as exc:
                self._emit(
                    f"请求执行失败: action={request.action}, attr={request.attr or '-'}, error={exc}"
                )
                if request.response_queue is not None:
                    request.response_queue.put((False, exc))

        try:
            if platform is not None:
                platform.close()
        except Exception:
            pass
        self._emit("工作线程已退出")

    def _reclaim_profile_processes(self, reason: str) -> None:
        profile_path = str(getattr(self, "_profile_path", "") or "").strip()
        if not profile_path:
            return
        try:
            pids = browser_profile_owner_pids(profile_path)
        except Exception as exc:
            self._emit(f"{reason}，扫描残留浏览器进程失败: {exc}")
            return
        if not pids:
            return
        try:
            self._emit(f"{reason}，准备按 profile 回收残留浏览器进程: pids={pids}")
            terminate_browser_profile_processes(profile_path, graceful_timeout=2.0, force=True)
        except Exception as exc:
            self._emit(f"{reason}，回收残留浏览器进程失败: {exc}")

    @staticmethod
    def _handle_request(platform: Any, request: _PlatformWorkerRequest):
        if request.action == "inspect":
            if not hasattr(platform, request.attr):
                raise AttributeError(request.attr)
            value = getattr(platform, request.attr)
            if callable(value):
                return {"callable": True}
            return {"callable": False, "value": value}
        if request.action == "get":
            return getattr(platform, request.attr)
        if request.action == "set":
            setattr(platform, request.attr, (request.kwargs or {}).get("value"))
            return None
        if request.action == "invoke":
            method = getattr(platform, request.attr)
            return method(*request.args, **(request.kwargs or {}))
        raise RuntimeError(f"未知 worker 请求: {request.action}")

    def _submit(self, action: str, *, attr: str = "", args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None):
        if self._closed:
            raise RuntimeError(f"平台工作器已关闭: {self._platform_name}")
        if not self._thread.is_alive():
            raise RuntimeError(f"平台工作器线程已退出: {self._platform_name}")
        response_queue: queue.Queue = queue.Queue(maxsize=1)
        self._request_queue.put(
            _PlatformWorkerRequest(
                action=action,
                attr=attr,
                args=args,
                kwargs=kwargs or {},
                response_queue=response_queue,
            )
        )
        ok, payload = response_queue.get()
        if ok:
            return payload
        raise payload

    def _invoke_remote(self, name: str, *args, **kwargs):
        return self._submit("invoke", attr=name, args=args, kwargs=kwargs)

    def is_alive(self) -> bool:
        return (not self._closed) and self._thread.is_alive()

    def start(self):
        return self

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        if self._thread.is_alive():
            response_queue: queue.Queue = queue.Queue(maxsize=1)
            self._request_queue.put(
                _PlatformWorkerRequest(
                    action="shutdown",
                    response_queue=response_queue,
                )
            )
            try:
                response_queue.get(timeout=self.CLOSE_REQUEST_TIMEOUT_SECONDS)
            except Exception:
                pass
            self._thread.join(timeout=self.CLOSE_JOIN_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                self._emit("工作线程关闭超时，线程仍存活")
                self._reclaim_profile_processes("工作线程关闭超时")
                self._thread.join(timeout=self.CLOSE_RECLAIM_JOIN_TIMEOUT_SECONDS)
                if self._thread.is_alive():
                    self._emit("浏览器残留回收后工作线程仍存活")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def __getattr__(self, name: str):
        if name in self._callable_attrs:
            return _PlatformWorkerMethod(self, name)
        if name in self._value_attrs:
            return self._submit("get", attr=name)
        meta = self._submit("inspect", attr=name)
        if bool(meta.get("callable", False)):
            self._callable_attrs.add(name)
            return _PlatformWorkerMethod(self, name)
        self._value_attrs.add(name)
        return meta.get("value")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._LOCAL_ATTRS:
            object.__setattr__(self, name, value)
            return
        self._value_attrs.add(name)
        self._submit("set", attr=name, kwargs={"value": value})


class PlatformSessionManager:
    def __init__(
        self,
        mode: str,
        policy: QueryExecutionPolicy,
        planned_queries_by_platform: dict[str, int] | None = None,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.mode = str(mode or "").strip()
        self.policy = policy
        self._planned_queries_by_platform = {
            str(platform).strip(): max(0, int(count or 0))
            for platform, count in (planned_queries_by_platform or {}).items()
            if str(platform).strip()
        }
        self._sessions: dict[str, PlatformSessionState] = {}
        self._last_restart_at: dict[str, float] = {}
        self._log = logger or (lambda message: None)
        self._lock = threading.RLock()

    def _emit(self, message: str) -> None:
        self._log(f"[Session] {message}")

    def _random_ttl_minutes(self) -> int:
        return _SESSION_RANDOM.randint(self.policy.session_ttl_minutes_min, self.policy.session_ttl_minutes_max)

    def _random_max_queries(self) -> int:
        return _SESSION_RANDOM.randint(self.policy.session_max_queries_min, self.policy.session_max_queries_max)

    def get_or_create(self, platform_name: str, factory: Callable[[], Any]):
        key = str(platform_name or "").strip()
        if not key:
            raise ValueError("platform_name 不能为空")
        with self._lock:
            return self._get_or_create_locked(key, factory)

    def _get_or_create_locked(self, key: str, factory: Callable[[], Any]):
        state = self._sessions.get(key)
        if state is not None and getattr(state.platform, "is_alive", lambda: True)():
            return state.platform
        if state is not None:
            self._emit(f"{key} 检测到已有 worker 失活，准备重建")
            self._sessions.pop(key, None)
            try:
                state.platform.close()
            except Exception:
                pass

        now = time.monotonic()
        ttl_minutes = self._random_ttl_minutes()
        max_queries = self._random_max_queries()
        platform = _PlatformWorkerProxy(
            key,
            factory,
            logger=self._log,
        )
        state = PlatformSessionState(
            platform_name=key,
            platform=platform,
            started_at=now,
            expire_at=now + ttl_minutes * 60,
            max_queries=max_queries,
            planned_queries_total=max(0, self.remaining_queries(key)),
            last_progress_at=now,
        )
        self._sessions[key] = state
        self._emit(
            f"{key} 已启动并进入复用池，寿命 {ttl_minutes} 分钟，最大查询 {max_queries} 次，"
            f"当前剩余计划查询 {self.remaining_queries(key)}"
        )
        return platform

    def remaining_queries(self, platform_name: str) -> int:
        key = str(platform_name or "").strip()
        with self._lock:
            return max(0, int(self._planned_queries_by_platform.get(key, 0) or 0))

    def get_dispatch_snapshot(self, platform_name: str) -> dict[str, Any]:
        key = str(platform_name or "").strip()
        with self._lock:
            state = self._sessions.get(key)
            now = time.monotonic()
            snapshot = {
                "platform_name": key,
                "has_live_session": False,
                "remaining_queries": self.remaining_queries(key),
                "completed_queries": 0,
                "consecutive_structural_failures": 0,
                "dirty_after_manual_recovery": False,
                "session_age_seconds": 0.0,
                "seconds_since_progress": 0.0,
                "restart_cooldown_remaining_seconds": 0.0,
            }
            if state is not None and getattr(state.platform, "is_alive", lambda: True)():
                snapshot["has_live_session"] = True
                snapshot["completed_queries"] = int(state.completed_queries or 0)
                snapshot["consecutive_structural_failures"] = int(state.consecutive_structural_failures or 0)
                snapshot["dirty_after_manual_recovery"] = bool(state.dirty_after_manual_recovery)
                snapshot["session_age_seconds"] = max(0.0, float(now - state.started_at))
                if state.last_progress_at is not None:
                    snapshot["seconds_since_progress"] = max(0.0, float(now - state.last_progress_at))
            cooldown = max(0, int(self.policy.min_restart_cooldown_minutes or 0))
            if cooldown > 0:
                last_restart_at = self._last_restart_at.get(key)
                if last_restart_at is not None:
                    elapsed = now - last_restart_at
                    snapshot["restart_cooldown_remaining_seconds"] = max(0.0, cooldown * 60 - float(elapsed))
            return snapshot

    def mark_query_started(self, platform_name: str) -> None:
        with self._lock:
            state = self._sessions.get(str(platform_name or "").strip())
            if state is not None:
                state.last_query_started_at = time.monotonic()

    def _consume_query_slot(self, platform_name: str) -> int:
        key = str(platform_name or "").strip()
        with self._lock:
            remaining = self.remaining_queries(key)
            if remaining <= 0:
                self._planned_queries_by_platform[key] = 0
                return 0
            remaining -= 1
            self._planned_queries_by_platform[key] = remaining
            return remaining

    def record_query_skipped(self, platform_name: str) -> None:
        key = str(platform_name or "").strip()
        with self._lock:
            remaining = self._consume_query_slot(key)
            if remaining <= 0 and key in self._sessions:
                self.close_session(key, reason="该平台后续查询均已跳过，关闭复用会话")

    def _restart_allowed(self, platform_name: str) -> bool:
        cooldown = max(0, int(self.policy.min_restart_cooldown_minutes or 0))
        if cooldown <= 0:
            return True
        key = str(platform_name or "").strip()
        last_restart_at = self._last_restart_at.get(key)
        if last_restart_at is None:
            return True
        return time.monotonic() - last_restart_at >= cooldown * 60

    def _decide_post_query_action(
        self,
        state: PlatformSessionState | None,
        platform_name: str,
        *,
        remaining_queries: int,
        progress_gap_exceeded: bool,
        duration_seconds: float,
        recovered_manually: bool,
    ) -> tuple[str, str]:
        key = str(platform_name or "").strip()
        if remaining_queries <= 0:
            return "close", "该平台今日任务已完成"
        if state is None:
            return "keep", ""

        if recovered_manually and self.policy.restart_after_manual_recovery:
            if self._restart_allowed(key):
                return "restart", "本次查询发生人工恢复，轮换平台会话"
            return "keep", "人工恢复后命中重启冷却，继续复用当前会话"

        if state.consecutive_structural_failures >= self.policy.restart_after_structural_failures:
            if self._restart_allowed(key):
                return "restart", f"连续结构性异常达到 {state.consecutive_structural_failures} 次"
            return "keep", "连续结构性异常但仍在重启冷却期"

        if progress_gap_exceeded:
            if self._restart_allowed(key):
                return "restart", f"最近一次查询完成间隔超过 {self.policy.no_progress_timeout_minutes} 分钟"
            return "keep", "查询完成间隔过长但仍在重启冷却期"

        if duration_seconds >= max(60, self.policy.single_query_timeout_minutes * 60):
            if self._restart_allowed(key):
                return "restart", f"本次查询耗时超过 {self.policy.single_query_timeout_minutes} 分钟"
            return "keep", "单次查询耗时过长但仍在重启冷却期"

        session_age_seconds = time.monotonic() - state.started_at
        if (
            remaining_queries > 0
            and state.planned_queries_total >= self.policy.min_queries_per_window
            and session_age_seconds >= self.policy.min_queries_window_minutes * 60
            and state.completed_queries < self.policy.min_queries_per_window
        ):
            if self._restart_allowed(key):
                return (
                    "restart",
                    f"{self.policy.min_queries_window_minutes} 分钟内仅完成 "
                    f"{state.completed_queries} 次查询，低于最少 {self.policy.min_queries_per_window} 次",
                )
            return "keep", "查询吞吐偏低但仍在重启冷却期"

        if state.completed_queries >= state.max_queries:
            if self._restart_allowed(key):
                return "restart", f"会话已完成 {state.completed_queries} 次查询，达到阈值"
            return "keep", "查询数达到阈值但仍在重启冷却期"

        if time.monotonic() >= state.expire_at:
            if self._restart_allowed(key):
                return "restart", "会话寿命已到期"
            return "keep", "会话寿命已到但仍在重启冷却期"

        return "keep", ""

    def record_query_result(
        self,
        platform_name: str,
        outcome: str,
        *,
        duration_seconds: float,
        recovered_manually: bool = False,
    ) -> str:
        key = str(platform_name or "").strip()
        with self._lock:
            state = self._sessions.get(key)
            previous_progress_at = state.last_progress_at if state is not None else None
            now = time.monotonic()
            remaining = self._consume_query_slot(key)
            progress_gap_exceeded = (
                previous_progress_at is not None
                and remaining > 0
                and now - previous_progress_at >= self.policy.no_progress_timeout_minutes * 60
            )

            if state is not None:
                state.completed_queries += 1
                state.last_progress_at = now
                state.last_query_started_at = None
                state.dirty_after_manual_recovery = bool(state.dirty_after_manual_recovery or recovered_manually)
                if outcome == "structural_error":
                    state.consecutive_structural_failures += 1
                else:
                    state.consecutive_structural_failures = 0

            action, reason = self._decide_post_query_action(
                state,
                key,
                remaining_queries=remaining,
                progress_gap_exceeded=progress_gap_exceeded,
                duration_seconds=float(duration_seconds or 0.0),
                recovered_manually=bool(recovered_manually),
            )
            if action == "restart":
                self.restart_session(key, reason=reason)
            elif action == "close":
                self.close_session(key, reason=reason)
            return action

    def close_exhausted_sessions(self) -> None:
        with self._lock:
            for platform_name in list(self._sessions.keys()):
                if self.remaining_queries(platform_name) <= 0:
                    self.close_session(platform_name, reason="该平台已无剩余任务，结束后自动关闭")

    def restart_session(self, platform_name: str, *, reason: str = "") -> None:
        key = str(platform_name or "").strip()
        with self._lock:
            state = self._sessions.pop(key, None)
            if state is None:
                return
            self._last_restart_at[key] = time.monotonic()
            try:
                state.platform.close()
            except Exception:
                pass
            self._emit(f"{key} 已轮换重启: {reason or '会话策略要求重启'}")

    def close_session(self, platform_name: str, *, reason: str = "") -> None:
        key = str(platform_name or "").strip()
        with self._lock:
            state = self._sessions.pop(key, None)
            if state is None:
                return
            try:
                state.platform.close()
            except Exception:
                pass
            if reason:
                self._emit(f"{key} 已关闭: {reason}")
            else:
                self._emit(f"{key} 已关闭")

    def close_all(self, *, reason: str = "") -> None:
        with self._lock:
            for platform_name in list(self._sessions.keys()):
                self.close_session(platform_name, reason=reason or "当前模式轮次结束")
