"""
配置文件热重载
使用 watchdog 监听文件变化
"""

import copy
import os
import tempfile
import time
import yaml
from typing import Callable, Optional
from pathlib import Path

from core.app_paths import resolve_app_path


_TASK_SECRET_SECTION = "task_secrets"
_CONFIG_WARNING_CACHE: set[str] = set()


def _read_yaml_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _normalize_secret_text(value) -> str:
    return str(value or "").strip()


def _set_nested_value(target: dict, path: list[str], value) -> None:
    cursor = target
    for key in path[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            child = {}
            cursor[key] = child
        cursor = child
    cursor[path[-1]] = value


def _prune_empty_containers(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            cleaned = _prune_empty_containers(item)
            if cleaned in ({}, [], None):
                continue
            result[key] = cleaned
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            cleaned = _prune_empty_containers(item)
            if cleaned in ({}, [], None):
                continue
            result.append(cleaned)
        return result
    return value


def _strip_managed_local_secrets(local_config: dict) -> dict:
    cleaned = copy.deepcopy(local_config or {})

    scheduler_cfg = cleaned.get("scheduler")
    if isinstance(scheduler_cfg, dict):
        scheduler_cfg.pop("notification_webhook_url", None)
        if not scheduler_cfg:
            cleaned.pop("scheduler", None)

    search_cfg = cleaned.get("search")
    if isinstance(search_cfg, dict):
        search_cfg.pop("tavily_api_key", None)
        if not search_cfg:
            cleaned.pop("search", None)

    cloud_sync_cfg = cleaned.get("cloud_sync")
    if isinstance(cloud_sync_cfg, dict):
        cloud_sync_cfg.pop("api_token", None)
        if not cloud_sync_cfg:
            cleaned.pop("cloud_sync", None)

    platforms_cfg = cleaned.get("platforms")
    if isinstance(platforms_cfg, dict):
        empty_platforms = []
        for platform_id, platform_cfg in platforms_cfg.items():
            if isinstance(platform_cfg, dict):
                platform_cfg.pop("api_key", None)
                if not platform_cfg:
                    empty_platforms.append(platform_id)
        for platform_id in empty_platforms:
            platforms_cfg.pop(platform_id, None)
        if not platforms_cfg:
            cleaned.pop("platforms", None)

    cleaned.pop(_TASK_SECRET_SECTION, None)
    return _prune_empty_containers(cleaned)


def _split_sensitive_config(config: dict) -> tuple[dict, dict]:
    public_config = copy.deepcopy(config or {})
    local_patch: dict = {}

    scheduler_cfg = public_config.get("scheduler")
    if isinstance(scheduler_cfg, dict):
        webhook = _normalize_secret_text(scheduler_cfg.get("notification_webhook_url", ""))
        if webhook:
            _set_nested_value(local_patch, ["scheduler", "notification_webhook_url"], webhook)
        scheduler_cfg["notification_webhook_url"] = ""

    search_cfg = public_config.get("search")
    if isinstance(search_cfg, dict):
        tavily_api_key = _normalize_secret_text(search_cfg.get("tavily_api_key", ""))
        if tavily_api_key:
            _set_nested_value(local_patch, ["search", "tavily_api_key"], tavily_api_key)
        search_cfg["tavily_api_key"] = ""

    cloud_sync_cfg = public_config.get("cloud_sync")
    if isinstance(cloud_sync_cfg, dict):
        api_token = _normalize_secret_text(cloud_sync_cfg.get("api_token", ""))
        if api_token:
            _set_nested_value(local_patch, ["cloud_sync", "api_token"], api_token)
        cloud_sync_cfg["api_token"] = ""

    platforms_cfg = public_config.get("platforms")
    if isinstance(platforms_cfg, dict):
        for platform_id, platform_cfg in platforms_cfg.items():
            if not isinstance(platform_cfg, dict):
                continue
            api_key = _normalize_secret_text(platform_cfg.get("api_key", ""))
            if api_key:
                _set_nested_value(local_patch, ["platforms", str(platform_id), "api_key"], api_key)
            platform_cfg["api_key"] = ""

    tasks = public_config.get("tasks")
    task_secret_entries: dict[str, dict[str, str]] = {}
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            webhook_url = _normalize_secret_text(task.get("webhook_url", ""))
            task["webhook_url"] = ""
            if not webhook_url:
                continue
            task_id = _normalize_secret_text(task.get("task_id", ""))
            task_name = _normalize_secret_text(task.get("name", ""))
            cloud_task_id = _normalize_secret_text(task.get("cloud_task_id") or task.get("cloudTaskId"))
            cloud_task_key = _normalize_secret_text(task.get("cloud_task_key") or task.get("cloudTaskKey"))
            key = task_id or f"name::{task_name}"
            if not key:
                continue
            task_secret_entries[key] = {
                "task_id": task_id,
                "task_name": task_name,
                "cloud_task_id": cloud_task_id,
                "cloud_task_key": cloud_task_key,
                "webhook_url": webhook_url,
            }
    if task_secret_entries:
        local_patch[_TASK_SECRET_SECTION] = task_secret_entries

    return public_config, _prune_empty_containers(local_patch)


def _pick_unique_secret_entry(entries: list[dict] | None) -> dict | None:
    unique_entries: list[dict] = []
    seen_keys: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        secret_key = _normalize_secret_text(entry.get("_secret_key", ""))
        if not secret_key or secret_key in seen_keys:
            continue
        seen_keys.add(secret_key)
        unique_entries.append(entry)
    if len(unique_entries) != 1:
        return None
    return unique_entries[0]


def _apply_task_secret_overrides(config: dict, local_config: dict) -> dict:
    task_secrets = local_config.get(_TASK_SECRET_SECTION)
    tasks = config.get("tasks")
    if not isinstance(task_secrets, dict) or not isinstance(tasks, list):
        return config

    entries_by_key: dict[str, dict] = {}
    by_name: dict[str, list[dict]] = {}
    by_task_id: dict[str, list[dict]] = {}
    by_cloud_task_id: dict[str, list[dict]] = {}
    by_cloud_task_key: dict[str, list[dict]] = {}
    unique_webhooks = {
        _normalize_secret_text((entry or {}).get("webhook_url", ""))
        for entry in task_secrets.values()
        if isinstance(entry, dict) and _normalize_secret_text((entry or {}).get("webhook_url", ""))
    }
    common_cloud_webhook = next(iter(unique_webhooks)) if len(unique_webhooks) == 1 else ""

    for secret_key, raw_entry in task_secrets.items():
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        normalized_secret_key = _normalize_secret_text(secret_key)
        entry["_secret_key"] = normalized_secret_key
        if not entry.get("task_id") and normalized_secret_key and not normalized_secret_key.startswith("name::"):
            entry["task_id"] = normalized_secret_key
        entries_by_key[normalized_secret_key] = entry

        task_name = _normalize_secret_text(entry.get("task_name", ""))
        if task_name:
            by_name.setdefault(task_name, []).append(entry)
        task_id = _normalize_secret_text(entry.get("task_id", ""))
        if task_id:
            by_task_id.setdefault(task_id, []).append(entry)
        cloud_task_id = _normalize_secret_text(entry.get("cloud_task_id", ""))
        if cloud_task_id:
            by_cloud_task_id.setdefault(cloud_task_id, []).append(entry)
        cloud_task_key = _normalize_secret_text(entry.get("cloud_task_key", ""))
        if cloud_task_key:
            by_cloud_task_key.setdefault(cloud_task_key, []).append(entry)

    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = _normalize_secret_text(task.get("task_id", ""))
        task_name = _normalize_secret_text(task.get("name", ""))
        cloud_task_id = _normalize_secret_text(task.get("cloud_task_id") or task.get("cloudTaskId"))
        cloud_task_key = _normalize_secret_text(task.get("cloud_task_key") or task.get("cloudTaskKey"))

        override = entries_by_key.get(task_id) if task_id else None
        if not isinstance(override, dict) and cloud_task_key:
            override = _pick_unique_secret_entry(by_cloud_task_key.get(cloud_task_key))
        if not isinstance(override, dict) and cloud_task_id:
            override = _pick_unique_secret_entry(by_cloud_task_id.get(cloud_task_id))
        if not isinstance(override, dict) and task_id:
            override = _pick_unique_secret_entry(by_task_id.get(task_id))
        if not isinstance(override, dict) and task_name:
            override = entries_by_key.get(f"name::{task_name}") or _pick_unique_secret_entry(by_name.get(task_name))

        if isinstance(override, dict) and "webhook_url" in override:
            task["webhook_url"] = _normalize_secret_text(override.get("webhook_url", ""))
            continue

        if not _normalize_secret_text(task.get("webhook_url", "")) and common_cloud_webhook and (cloud_task_id or cloud_task_key):
            task["webhook_url"] = common_cloud_webhook
    return config


def _warn_config_once(message: str) -> None:
    text = str(message or "").strip()
    if not text or text in _CONFIG_WARNING_CACHE:
        return
    _CONFIG_WARNING_CACHE.add(text)
    print(f"[Config] 配置校验提示: {text}")


def _validate_config_shape(config: dict) -> None:
    """Lightweight, non-blocking config validation for common typo/shape issues."""
    if not isinstance(config, dict):
        _warn_config_once("配置根节点应为对象")
        return

    scheduler_cfg = config.get("scheduler")
    if scheduler_cfg is not None and not isinstance(scheduler_cfg, dict):
        _warn_config_once("scheduler 应为对象")
    if isinstance(scheduler_cfg, dict):
        if "notificaton_webhook_url" in scheduler_cfg and "notification_webhook_url" not in scheduler_cfg:
            _warn_config_once("scheduler.notificaton_webhook_url 可能拼写错误，应为 notification_webhook_url")
        weekly_times = scheduler_cfg.get("weekly_times")
        if weekly_times is not None and not isinstance(weekly_times, dict):
            _warn_config_once("scheduler.weekly_times 应为对象")

    tasks = config.get("tasks")
    if tasks is not None and not isinstance(tasks, list):
        _warn_config_once("tasks 应为列表")
    if isinstance(tasks, list):
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                _warn_config_once(f"tasks[{index}] 应为对象")
                continue
            if not str(task.get("name") or task.get("brand") or "").strip():
                _warn_config_once(f"tasks[{index}] 缺少 name/brand")
            keywords = task.get("keywords")
            if keywords is not None and not isinstance(keywords, list):
                _warn_config_once(f"tasks[{index}].keywords 应为列表")

    platforms_cfg = config.get("platforms")
    if platforms_cfg is not None and not isinstance(platforms_cfg, dict):
        _warn_config_once("platforms 应为对象")
    if isinstance(platforms_cfg, dict):
        for platform_id, platform_cfg in platforms_cfg.items():
            if not isinstance(platform_cfg, dict):
                _warn_config_once(f"platforms.{platform_id} 应为对象")
                continue
            if "apikey" in platform_cfg and "api_key" not in platform_cfg:
                _warn_config_once(f"platforms.{platform_id}.apikey 可能拼写错误，应为 api_key")

def _load_merged_config(resolved_path: Path) -> dict:
    try:
        config = _read_yaml_file(resolved_path)
    except FileNotFoundError:
        print(f"[Config] 配置文件不存在: {resolved_path}")
        return {}
    except Exception as e:
        print(f"[Config] 加载配置失败: {e}")
        return {}

    local_path = resolved_path.with_name("config.local.yaml")
    if not local_path.exists():
        _validate_config_shape(config)
        return config

    try:
        local_config = _read_yaml_file(local_path)
        merge_local = {k: v for k, v in local_config.items() if k != _TASK_SECRET_SECTION}
        config = _deep_merge(config, merge_local)
        config = _apply_task_secret_overrides(config, local_config)
    except Exception as e:
        print(f"[Config] 加载本地配置失败: {e}")

    _validate_config_shape(config)
    return config


def _write_yaml_atomic(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(data or {}, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if path.name == "config.local.yaml":
        try:
            path.chmod(0o600)
        except OSError as exc:
            print(f"[Config] 设置本地敏感配置权限失败: {exc}")
    return path


def _write_bytes_atomic(path: Path, data: bytes, *, private: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if private:
        try:
            path.chmod(0o600)
        except OSError as exc:
            print(f"[Config] 设置本地敏感配置权限失败: {exc}")
    return path


class ConfigWatcher:
    """
    配置文件监听器
    支持热重载配置
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        on_change: Optional[Callable] = None,
        debounce_seconds: float = 1.0
    ):
        self.config_path = resolve_app_path(config_path)
        self.local_path = self.config_path.with_name("config.local.yaml")
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self._last_modified = 0
        self._last_content = None
        self._running = False
        self._watcher = None

    def load_config(self) -> dict:
        """加载配置文件，自动合并 config.local.yaml"""
        try:
            return _load_merged_config(self.config_path)
        except Exception as e:
            print(f"[ConfigWatcher] 加载配置失败: {e}")
            return {}

    def check_and_reload(self) -> bool:
        """
        检查文件是否变化，如有变化则重新加载
        返回: 是否发生变化
        """
        try:
            watched_paths = [path for path in (self.config_path, self.local_path) if path.exists()]
            if not watched_paths:
                return False

            current_modified = max(path.stat().st_mtime for path in watched_paths)

            # 防抖：确保文件稳定后才读取
            if current_modified == self._last_modified:
                return False

            if time.time() - current_modified < self.debounce_seconds:
                return False

            content_parts = []
            for path in watched_paths:
                with open(path, 'r', encoding='utf-8') as f:
                    text = f.read()
                try:
                    yaml.safe_load(text)
                except yaml.YAMLError as e:
                    print(f"[ConfigWatcher] YAML解析错误 ({path.name}): {e}")
                    return False
                content_parts.append((path.name, text))
            current_content = tuple(content_parts)

            # 对比内容
            if current_content == self._last_content:
                self._last_modified = current_modified
                return False

            new_config = self.load_config()
            if new_config is None:
                return False

            # 更新状态
            self._last_modified = current_modified
            self._last_content = current_content

            print("[ConfigWatcher] 配置已更新")

            # 触发回调
            if self.on_change:
                self.on_change(new_config)

            return True

        except Exception as e:
            print(f"[ConfigWatcher] 检查更新失败: {e}")
            return False

    def start_polling(self, interval: float = 2.0):
        """
        启动轮询模式监听（简单可靠）
        参数:
            interval: 检查间隔（秒）
        """
        import threading

        self._running = True

        def poll():
            while self._running:
                self.check_and_reload()
                time.sleep(interval)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        print(f"[ConfigWatcher] 轮询监听已启动: {self.config_path}")

    def start_watchdog(self):
        """
        启动 watchdog 监听（更高效，依赖watchdog库）
        """
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class ConfigHandler(FileSystemEventHandler):
                def __init__(self, watcher):
                    self.watcher = watcher

                def on_modified(self, event):
                    if not event.is_directory:
                        if Path(event.src_path).name in {
                            self.watcher.config_path.name,
                            self.watcher.local_path.name,
                        }:
                            time.sleep(self.watcher.debounce_seconds)
                            self.watcher.check_and_reload()

            self._watcher = Observer()
            handler = ConfigHandler(self)
            self._watcher.schedule(
                handler,
                str(self.config_path.parent),
                recursive=False
            )
            self._watcher.start()
            print(f"[ConfigWatcher] Watchdog监听已启动: {self.config_path}")

        except ImportError:
            print("[ConfigWatcher] watchdog未安装，使用轮询模式")
            self.start_polling()

    def stop(self):
        """停止监听"""
        self._running = False
        if self._watcher:
            self._watcher.stop()
            self._watcher.join()


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 的值覆盖 base"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# 简单的配置加载函数
def load_config(config_path: str = "config.yaml") -> dict:
    """加载YAML配置文件，自动合并同目录下的 config.local.yaml"""
    resolved_path = resolve_app_path(config_path)
    return _load_merged_config(resolved_path)


def save_config(config: dict, config_path: str = "config.yaml") -> Path:
    """保存 YAML 配置文件，并把敏感字段拆到 config.local.yaml。"""
    resolved_path = resolve_app_path(config_path)
    local_path = resolved_path.with_name("config.local.yaml")
    public_config, local_secret_patch = _split_sensitive_config(config or {})

    existing_local = {}
    if local_path.exists():
        try:
            existing_local = _read_yaml_file(local_path)
        except Exception as e:
            print(f"[Config] 读取本地配置失败，改为覆盖写入: {e}")
            existing_local = {}

    next_local = _strip_managed_local_secrets(existing_local)
    next_local = _deep_merge(next_local, local_secret_patch) if local_secret_patch else next_local

    existing_public_bytes = resolved_path.read_bytes() if resolved_path.exists() else None
    existing_local_bytes = local_path.read_bytes() if local_path.exists() else None
    try:
        if next_local:
            _write_yaml_atomic(local_path, next_local)
        elif local_path.exists():
            local_path.unlink()
        _write_yaml_atomic(resolved_path, public_config)
    except Exception:
        try:
            if existing_public_bytes is not None:
                _write_bytes_atomic(resolved_path, existing_public_bytes)
            elif resolved_path.exists():
                resolved_path.unlink()
            if existing_local_bytes is not None:
                _write_bytes_atomic(local_path, existing_local_bytes, private=True)
            elif local_path.exists():
                local_path.unlink()
        except Exception as rollback_error:
            print(f"[Config] 配置保存失败后回滚也失败: {rollback_error}")
        raise
    return resolved_path
