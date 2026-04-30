"""Config access boundary for the local web backend."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.config_watcher import load_config as read_config
from core.config_watcher import save_config as write_config
from core.daily_task_state import ensure_config_task_ids


class RuntimeConfigProvider:
    """Centralizes runtime config reads and writes.

    AppRuntime historically exposed ``load_config`` directly. Keeping the
    side effects here lets services depend on one small object instead of the
    whole runtime as future slices move out of ``web_backend.py``.
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        on_load: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self._on_load = on_load

    def load(self) -> dict[str, Any]:
        config = read_config(self.config_path)
        ensure_config_task_ids(config)
        if self._on_load is not None:
            self._on_load(config)
        return config

    def save(self, config: dict[str, Any]) -> Path:
        return write_config(config, str(self.config_path))
