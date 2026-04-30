"""Quick todo service for the local web backend."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend_lib.config_provider import RuntimeConfigProvider


def _todo_to_api(todo: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": todo.get("id", ""),
        "text": str(todo.get("text", "")).strip(),
        "done": bool(todo.get("done", False)),
    }
    completed_at = str(todo.get("completed_at", "") or "").strip()
    if completed_at:
        payload["completedAt"] = completed_at
    return payload


class TodoService:
    """Small service for quick todo config persistence."""

    def __init__(
        self,
        *,
        config_provider: RuntimeConfigProvider,
        lock: Any,
        normalize_todos: Callable[..., list[dict[str, Any]]],
    ) -> None:
        self._config_provider = config_provider
        self._lock = lock
        self._normalize_todos = normalize_todos

    def get_todos(self) -> dict:
        with self._lock:
            config = self._config_provider.load()
            todos = self._normalize_todos(config, save=True)
            return {"ok": True, "todos": [_todo_to_api(todo) for todo in todos]}

    def sync_todos(self, payload: dict) -> dict:
        with self._lock:
            try:
                config = self._config_provider.load()
                todos = payload.get("todos", [])
                if not isinstance(todos, list):
                    return {"ok": False, "message": "todos 必须是数组"}
                config["quick_todos"] = [
                    {
                        "id": todo.get("id", ""),
                        "text": str(todo.get("text", "")).strip(),
                        "done": bool(todo.get("done", False)),
                        "completed_at": str(todo.get("completed_at", todo.get("completedAt", "")) or "").strip(),
                    }
                    for todo in todos
                    if isinstance(todo, dict) and str(todo.get("text", "")).strip()
                ]
                normalized = self._normalize_todos(config)
                config["quick_todos"] = normalized
                self._config_provider.save(config)
                return {"ok": True, "todos": [_todo_to_api(todo) for todo in normalized]}
            except Exception as exc:
                return {"ok": False, "message": f"待办保存失败: {exc}"}
