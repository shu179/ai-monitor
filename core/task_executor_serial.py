"""Serial platform lifecycle management for task execution."""

from __future__ import annotations

from typing import Callable

from core.task_executor_browser import _create_browser_platform


class SerialPlatformRuntime:
    """Wrap the mutable serial-platform state shared across task rounds."""

    def __init__(
        self,
        state: dict | None,
        *,
        task: dict,
        config: dict | None,
        stop_checker,
        create_browser_platform: Callable[..., object] = _create_browser_platform,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.state = state if isinstance(state, dict) else {"name": "", "platform": None}
        self.owns_state = self.state is not state
        self._task = task
        self._config = config or {}
        self._stop_checker = stop_checker
        self._create_browser_platform = create_browser_platform
        self._logger = logger

    @property
    def active_name(self) -> str:
        return str(self.state.get("name") or "").strip()

    def close(self, reason: str = "") -> None:
        active_platform = self.state.get("platform")
        active_name = self.active_name
        if active_platform is None:
            self.state["name"] = ""
            return
        if reason:
            self._logger(f"[Main] 按平台分组串行策略关闭平台: {active_name or '未知平台'} ({reason})")
        try:
            active_platform.close()
        except Exception as e:
            self._logger(f"[Main] 关闭串行平台失败 ({active_name or '未知平台'}): {e}")
        finally:
            self.state["platform"] = None
            self.state["name"] = ""

    def acquire(self, platform_name: str, platform_class):
        active_platform = self.state.get("platform")
        active_name = self.active_name
        needs_new_platform = (
            active_platform is None
            or active_name != platform_name
            or getattr(active_platform, "page", None) is None
        )
        if not needs_new_platform:
            return active_platform
        if active_platform is not None and active_name != platform_name:
            self.close(reason="切换到下一个平台")
        elif active_platform is not None and getattr(active_platform, "page", None) is None:
            self.close(reason="当前平台实例失效，准备重建")
        next_platform = self._create_browser_platform(
            platform_name,
            platform_class,
            config=self._config,
            inspect=bool(self._task.get("inspect", False)),
            stop_checker=self._stop_checker,
        ).start()
        self.state["platform"] = next_platform
        self.state["name"] = platform_name
        self._logger(f"[Main] 按平台分组串行策略切换平台: {platform_name}")
        return next_platform


__all__ = ["SerialPlatformRuntime"]
