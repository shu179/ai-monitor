import copy
import threading

from backend_lib.todo_service import TodoService


class _TodoConfigProvider:
    def __init__(self, config):
        self.config = config
        self.saved = None

    def load(self):
        return self.config

    def save(self, config):
        self.saved = copy.deepcopy(config)


def test_get_todos_normalizes_and_projects_completed_at():
    provider = _TodoConfigProvider({
        "quick_todos": [
            {
                "id": "todo-1",
                "text": "  跟进文章归类  ",
                "done": True,
                "completed_at": "2026-04-30 09:00",
            }
        ]
    })
    calls = []

    def normalize_todos(config, save=False):
        calls.append(save)
        return config.get("quick_todos", [])

    service = TodoService(
        config_provider=provider,
        lock=threading.RLock(),
        normalize_todos=normalize_todos,
    )

    result = service.get_todos()

    assert result == {
        "ok": True,
        "todos": [
            {
                "id": "todo-1",
                "text": "跟进文章归类",
                "done": True,
                "completedAt": "2026-04-30 09:00",
            }
        ],
    }
    assert calls == [True]


def test_sync_todos_cleans_payload_and_saves_normalized_config():
    provider = _TodoConfigProvider({})

    def normalize_todos(config, save=False):
        normalized = []
        for index, todo in enumerate(config.get("quick_todos", []), start=1):
            normalized.append({**todo, "id": todo.get("id") or f"todo-{index}"})
        return normalized

    service = TodoService(
        config_provider=provider,
        lock=threading.RLock(),
        normalize_todos=normalize_todos,
    )

    result = service.sync_todos({
        "todos": [
            {"id": "", "text": "  看 ArticleService 边界  ", "done": False},
            {"id": "done-1", "text": "已完成", "done": True, "completedAt": "2026-04-30"},
            {"text": "   "},
            "not-a-todo",
        ]
    })

    assert result["ok"] is True
    assert result["todos"] == [
        {"id": "todo-1", "text": "看 ArticleService 边界", "done": False},
        {"id": "done-1", "text": "已完成", "done": True, "completedAt": "2026-04-30"},
    ]
    assert provider.saved["quick_todos"] == [
        {"id": "todo-1", "text": "看 ArticleService 边界", "done": False, "completed_at": ""},
        {"id": "done-1", "text": "已完成", "done": True, "completed_at": "2026-04-30"},
    ]


def test_sync_todos_rejects_non_list_payload():
    service = TodoService(
        config_provider=_TodoConfigProvider({}),
        lock=threading.RLock(),
        normalize_todos=lambda config, save=False: [],
    )

    result = service.sync_todos({"todos": "bad"})

    assert result == {"ok": False, "message": "todos 必须是数组"}
