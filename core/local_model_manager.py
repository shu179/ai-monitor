"""
本地模型运行时管理。

当前先以 Ollama 为默认本地推理后端，负责：
- 探测本地服务是否可用
- 在需要时自动拉起服务
- 按需预拉取模型
- 暴露可读状态，便于 UI 提示
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from core.app_paths import get_app_root, get_data_root


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:e2b"


def _get_local_platform_config(config: dict[str, Any] | None) -> dict[str, Any]:
    platforms_cfg = ((config or {}).get("platforms", {}) or {})
    entry = platforms_cfg.get("local_model")
    if isinstance(entry, dict):
        return entry
    legacy_entry = platforms_cfg.get("local_qwen")
    return legacy_entry if isinstance(legacy_entry, dict) else {}


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = str(parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def current_bundle_platform() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if os.name == "nt":
        if machine in {"amd64", "x86_64", "x64"}:
            arch = "amd64"
        elif machine in {"arm64", "aarch64"}:
            arch = "arm64"
        else:
            arch = machine or "unknown"
        return f"windows-{arch}"
    if os.sys.platform == "darwin":
        if machine in {"arm64", "aarch64"}:
            arch = "arm64"
        elif machine in {"x86_64", "amd64"}:
            arch = "amd64"
        else:
            arch = machine or "unknown"
        return f"darwin-{arch}"
    if machine in {"x86_64", "amd64", "x64"}:
        arch = "amd64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        arch = machine or "unknown"
    return f"linux-{arch}"


class LocalModelManager:
    _instance: "LocalModelManager | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._service_process: subprocess.Popen | None = None
        self._pull_process: subprocess.Popen | None = None
        self._pull_thread: threading.Thread | None = None
        self._prepare_thread: threading.Thread | None = None
        self._last_prepare_attempt_at = 0.0
        self._pulling_model = ""
        self._resolved_binary = ""
        self._resolved_bundled_binary = ""
        self._last_error = ""
        self._status = "idle"
        self._status_message = "本地模型未启动"
        self._last_healthcheck_at = 0.0
        self._last_tags: dict[str, Any] | None = None
        self._last_tags_error = ""
        self._last_config: dict[str, Any] = {}
        self._bundled_models_seeded = False
        self._runtime_binary_prepared = False
        self._resolved_bundled_models = ""
        self._resolved_runtime_models = ""
        self._resolved_runtime_binary = ""
        self._binary_source = ""
        self._direct_http = requests.Session()
        self._direct_http.trust_env = False

    @classmethod
    def get_instance(cls) -> "LocalModelManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def sync_config(self, config: dict | None) -> None:
        cfg = dict((config or {}).get("local_model", {}) or {})
        with self._lock:
            self._last_config = cfg
            self._resolved_binary = ""
            self._resolved_bundled_binary = ""
            self._resolved_bundled_models = ""
            self._resolved_runtime_models = ""
            self._resolved_runtime_binary = ""
            self._bundled_models_seeded = False
            self._runtime_binary_prepared = False
            self._binary_source = ""
        if cfg.get("auto_prepare_on_launch", True):
            model = str(
                cfg.get("default_model")
                or _get_local_platform_config(config).get("api_model", "")
                or DEFAULT_MODEL
            ).strip()
            self.prepare_in_background(model=model or DEFAULT_MODEL, reason="app_launch")

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_process_state_locked()
            if not self._resolved_binary:
                detected_binary, detected_source = self._detect_binary_path_locked(prepare_runtime=False)
                if detected_binary:
                    self._resolved_binary = detected_binary
                    self._binary_source = detected_source
            healthy = self._check_health(force=False)
            model_present = self._model_exists_locked(self._pulling_model) if self._pulling_model else None
            bundled_binary = self._resolve_bundled_binary_source()
            bundled_models_dir = self._resolve_bundled_models_dir()
            runtime_binary = self._runtime_binary_path()
            runtime_models_dir = self._runtime_models_dir()
            return {
                "provider": "ollama",
                "base_url": self._base_url(),
                "healthy": healthy,
                "status": self._status,
                "status_message": self._status_message,
                "last_error": self._last_error,
                "binary_path": self._resolved_binary,
                "binary_source": self._binary_source,
                "bundled_binary_path": str(bundled_binary) if bundled_binary else "",
                "bundled_binary_available": bool(bundled_binary),
                "runtime_binary_path": str(runtime_binary),
                "runtime_binary_prepared": self._runtime_binary_prepared,
                "service_process_running": bool(self._service_process and self._service_process.poll() is None),
                "pull_process_running": bool(self._pull_process and self._pull_process.poll() is None),
                "pulling_model": self._pulling_model,
                "pulling_model_ready": model_present,
                "models": self._extract_model_names(self._last_tags),
                "runtime_models_path": str(runtime_models_dir),
                "bundled_models_path": str(bundled_models_dir) if bundled_models_dir else "",
                "bundled_models_available": bool(bundled_models_dir),
                "bundled_models_seeded": self._bundled_models_seeded,
            }

    def prepare_in_background(self, model: str, *, reason: str = "") -> None:
        model = str(model or "").strip() or DEFAULT_MODEL
        with self._lock:
            if self._prepare_thread and self._prepare_thread.is_alive():
                return
            now = time.time()
            if now - self._last_prepare_attempt_at < 10:
                return
            self._last_prepare_attempt_at = now

        def _runner() -> None:
            try:
                self.ensure_ready(model, reason=reason)
            except Exception:
                pass

        thread = threading.Thread(target=_runner, daemon=True, name="LocalModelPrepare")
        with self._lock:
            self._prepare_thread = thread
        thread.start()

    def ensure_ready(self, model: str, *, reason: str = "") -> tuple[bool, str]:
        model = str(model or "").strip() or DEFAULT_MODEL
        with self._lock:
            self._refresh_process_state_locked()
            self._ensure_bundled_models_seeded_locked()
            healthy = self._check_health(force=True)
            if not healthy:
                ok, message = self._ensure_service_locked(reason=reason or "request")
                if not ok:
                    return False, message
                healthy = self._check_health(force=True)
            if not healthy:
                message = self._last_error or "本地模型服务未就绪"
                self._set_status_locked("error", message, error=message)
                return False, message

            if self._model_exists_locked(model):
                self._set_status_locked("ready", f"本地模型已就绪：{model}")
                return True, ""

            if self._auto_pull_enabled():
                started, message = self._ensure_pull_locked(model)
                if started:
                    return False, message

            message = f"本地模型 {model} 尚未准备好"
            self._set_status_locked("error", message, error=message)
            return False, message

    def shutdown_owned_process(self) -> None:
        with self._lock:
            process = self._service_process
            self._service_process = None
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def _local_cfg(self) -> dict[str, Any]:
        return dict(self._last_config or {})

    def _base_url(self) -> str:
        return str(self._local_cfg().get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")

    def _auto_start_enabled(self) -> bool:
        return bool(self._local_cfg().get("auto_start", True))

    def _auto_pull_enabled(self) -> bool:
        return bool(self._local_cfg().get("auto_pull", True))

    def _seed_bundled_models_enabled(self) -> bool:
        return bool(self._local_cfg().get("seed_bundled_models_on_launch", True))

    def _healthcheck_timeout(self) -> float:
        try:
            return max(1.0, float(self._local_cfg().get("healthcheck_timeout_seconds", 2.5) or 2.5))
        except Exception:
            return 2.5

    def _startup_timeout(self) -> float:
        try:
            return max(2.0, float(self._local_cfg().get("startup_timeout_seconds", 15) or 15))
        except Exception:
            return 15.0

    def _poll_interval(self) -> float:
        try:
            return max(0.2, float(self._local_cfg().get("startup_poll_interval_seconds", 0.5) or 0.5))
        except Exception:
            return 0.5

    def _runtime_models_dir(self) -> Path:
        if self._resolved_runtime_models:
            return Path(self._resolved_runtime_models)

        cfg = self._local_cfg()
        configured = str(cfg.get("runtime_models_dir") or os.environ.get("AIBRANDMONITOR_OLLAMA_MODELS_DIR", "")).strip()
        if configured:
            candidate = Path(configured).expanduser()
            resolved = candidate if candidate.is_absolute() else (get_data_root() / candidate)
        else:
            resolved = get_data_root() / "ollama" / "models"

        self._resolved_runtime_models = str(resolved)
        return resolved

    def _runtime_binary_path(self) -> Path:
        if self._resolved_runtime_binary:
            return Path(self._resolved_runtime_binary)

        cfg = self._local_cfg()
        configured = str(cfg.get("runtime_binary_path") or os.environ.get("AIBRANDMONITOR_OLLAMA_RUNTIME_BIN", "")).strip()
        if configured:
            candidate = Path(configured).expanduser()
            resolved = candidate if candidate.is_absolute() else (get_data_root() / candidate)
        else:
            resolved = get_data_root() / "ollama" / "runtime" / ("ollama.exe" if os.name == "nt" else "ollama")

        self._resolved_runtime_binary = str(resolved)
        return resolved

    def _bundle_sync_marker_path(self) -> Path:
        return self._runtime_models_dir().parent / ".bundle_sync_state.json"

    def _runtime_models_seed_present(self) -> bool:
        runtime_dir = self._runtime_models_dir()
        return runtime_dir.exists() and (runtime_dir / "manifests").exists() and (runtime_dir / "blobs").exists()

    def _build_bundle_sync_signature(self, bundled_dir: Path) -> dict[str, Any]:
        manifests_dir = bundled_dir / "manifests"
        blobs_dir = bundled_dir / "blobs"

        def _safe_stat(path: Path) -> dict[str, int]:
            try:
                stat_result = path.stat()
            except Exception:
                return {"mtime_ns": 0, "size": 0}
            return {
                "mtime_ns": int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1_000_000_000))),
                "size": int(stat_result.st_size),
            }

        return {
            "source_path": str(bundled_dir.resolve()),
            "source_root": _safe_stat(bundled_dir),
            "manifests": _safe_stat(manifests_dir),
            "blobs": _safe_stat(blobs_dir),
        }

    def _load_bundle_sync_marker(self) -> dict[str, Any]:
        marker_path = self._bundle_sync_marker_path()
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_bundle_sync_marker(self, payload: dict[str, Any]) -> None:
        marker_path = self._bundle_sync_marker_path()
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _can_skip_bundled_models_sync_locked(self, bundled_dir: Path) -> bool:
        if not self._runtime_models_seed_present():
            return False
        marker = self._load_bundle_sync_marker()
        signature = self._build_bundle_sync_signature(bundled_dir)
        return marker == signature

    def _normalize_models_store_dir(self, candidate: str | Path | None) -> Path | None:
        raw = str(candidate or "").strip()
        if not raw:
            return None

        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (get_app_root() / path).resolve()
        if not path.exists():
            return None
        if (path / "manifests").exists() or (path / "blobs").exists():
            return path

        nested = path / "models"
        if nested.exists() and ((nested / "manifests").exists() or (nested / "blobs").exists()):
            return nested
        return None

    def _resolve_bundled_models_dir(self) -> Path | None:
        if self._resolved_bundled_models:
            return Path(self._resolved_bundled_models)

        cfg = self._local_cfg()
        platform_dir = current_bundle_platform()
        candidates = [
            cfg.get("bundled_models_path"),
            os.environ.get("AIBRANDMONITOR_OLLAMA_MODELS_BUNDLE", ""),
            cfg.get("bundled_models_relative_path"),
            f"third_party/ollama-models/{platform_dir}",
            "third_party/ollama-models",
            "third_party/ollama/models",
            "ollama-models",
        ]
        for candidate in candidates:
            resolved = self._normalize_models_store_dir(candidate)
            if resolved:
                self._resolved_bundled_models = str(resolved)
                return resolved
        return None

    def _resolve_bundled_binary_source(self) -> Path | None:
        if self._resolved_bundled_binary:
            return Path(self._resolved_bundled_binary)

        cfg = self._local_cfg()
        app_root = get_app_root()
        platform_dir = current_bundle_platform()
        binary_name = "ollama.exe" if os.name == "nt" else "ollama"
        candidates = [
            cfg.get("bundled_binary_path"),
            os.environ.get("AIBRANDMONITOR_OLLAMA_BUNDLED_BIN", ""),
            cfg.get("bundled_binary_relative_path"),
            str(app_root / "third_party" / "ollama" / platform_dir / binary_name),
            str(app_root / "third_party" / "ollama" / binary_name),
            str(app_root / "ollama" / binary_name),
            str(app_root / "bin" / binary_name),
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if not value:
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = (app_root / path).resolve()
            if path.exists() and path.is_file():
                self._resolved_bundled_binary = str(path)
                return path
        return None

    def _copy_missing_tree(self, src: Path, dst: Path) -> None:
        if not src.exists():
            return
        if src.is_file():
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            return

        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            self._copy_missing_tree(child, dst / child.name)

    def _ensure_runtime_binary_from_bundle_locked(self) -> str:
        runtime_binary = self._runtime_binary_path()
        bundled_binary = self._resolve_bundled_binary_source()
        if runtime_binary.exists() and bundled_binary is None:
            self._runtime_binary_prepared = True
            return str(runtime_binary)
        if bundled_binary is None:
            self._runtime_binary_prepared = False
            return ""

        try:
            runtime_binary.parent.mkdir(parents=True, exist_ok=True)
            needs_copy = not runtime_binary.exists()
            if runtime_binary.exists():
                src_stat = bundled_binary.stat()
                dst_stat = runtime_binary.stat()
                needs_copy = (
                    src_stat.st_size != dst_stat.st_size
                    or int(src_stat.st_mtime) > int(dst_stat.st_mtime)
                )
            if needs_copy:
                shutil.copy2(bundled_binary, runtime_binary)
            try:
                current_mode = runtime_binary.stat().st_mode
                os.chmod(
                    runtime_binary,
                    current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
                )
            except Exception:
                pass
            self._runtime_binary_prepared = True
            return str(runtime_binary)
        except Exception as exc:
            self._runtime_binary_prepared = False
            self._last_error = f"自动部署内置 Ollama 引擎失败：{exc}"
            return ""

    def _ensure_bundled_models_seeded_locked(self) -> tuple[bool, str]:
        if not self._seed_bundled_models_enabled():
            return True, ""

        bundled_dir = self._resolve_bundled_models_dir()
        if bundled_dir is None:
            self._bundled_models_seeded = False
            return True, ""

        runtime_dir = self._runtime_models_dir()
        try:
            if self._can_skip_bundled_models_sync_locked(bundled_dir):
                self._bundled_models_seeded = True
                return True, f"随包模型资源已存在，跳过同步：{runtime_dir}"
            self._set_status_locked("preparing", "正在同步随安装包分发的本地模型资源…")
            self._copy_missing_tree(bundled_dir, runtime_dir)
            try:
                self._save_bundle_sync_marker(self._build_bundle_sync_signature(bundled_dir))
            except Exception:
                # 标记写入失败不影响本次资源可用性，只是下次无法快速跳过。
                pass
            self._bundled_models_seeded = True
            return True, f"已同步随包模型资源到：{runtime_dir}"
        except Exception as exc:
            self._bundled_models_seeded = False
            message = f"同步随包模型资源失败：{exc}"
            self._last_error = message
            return False, message

    def _build_ollama_env(self) -> dict[str, str]:
        env = os.environ.copy()
        runtime_dir = self._runtime_models_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        env["OLLAMA_MODELS"] = str(runtime_dir)

        base_url = self._base_url()
        parsed = urlparse(base_url if "://" in base_url else f"http://{base_url}")
        host_value = (parsed.netloc or parsed.path or "").strip()
        if host_value:
            env["OLLAMA_HOST"] = host_value
        env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        env["no_proxy"] = "127.0.0.1,localhost,::1"
        return env

    def _detect_binary_path_locked(self, *, prepare_runtime: bool) -> tuple[str, str]:
        cfg = self._local_cfg()
        explicit = str(cfg.get("binary_path") or "").strip()
        env_bin = str(os.environ.get("AIBRANDMONITOR_OLLAMA_BIN", "") or "").strip()
        runtime_binary = self._runtime_binary_path()

        candidates = [
            (explicit, "configured"),
            (env_bin, "environment"),
            (str(runtime_binary), "bundled_runtime"),
        ]

        for candidate, source in candidates:
            value = str(candidate or "").strip()
            if not value:
                continue
            path = Path(value)
            if path.exists():
                if source == "bundled_runtime":
                    self._runtime_binary_prepared = True
                return str(path), source

        if prepare_runtime:
            prepared_runtime = self._ensure_runtime_binary_from_bundle_locked()
            if prepared_runtime:
                return prepared_runtime, "bundled_runtime"

        discovered = shutil.which("ollama")
        if discovered:
            return discovered, "system"
        fallback_candidates = [
            "/usr/local/bin/ollama",
            "/opt/homebrew/bin/ollama",
            str(Path.home() / ".ollama" / "bin" / "ollama"),
            str(Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"),
        ]
        for candidate in fallback_candidates:
            path = Path(candidate).expanduser()
            if path.exists() and path.is_file():
                return str(path), "system"
        return "", "missing"

    def _resolve_binary_path(self) -> str:
        if self._resolved_binary:
            return self._resolved_binary

        detected_binary, detected_source = self._detect_binary_path_locked(prepare_runtime=True)
        if detected_binary:
            self._resolved_binary = detected_binary
            self._binary_source = detected_source
            return self._resolved_binary
        self._binary_source = "missing"
        return ""

    def _build_missing_binary_message(self) -> str:
        if self._resolve_bundled_binary_source():
            if self._last_error:
                return self._last_error
            return "已发现随包 Ollama 引擎，但自动部署失败，暂时无法启动本地模型服务"
        return "当前版本未内置 Ollama 引擎，也没有发现系统已安装的 Ollama"

    def _check_health(self, *, force: bool) -> bool:
        now = time.time()
        if not force and now - self._last_healthcheck_at < 1.5:
            return bool(self._last_tags is not None and not self._last_tags_error)

        self._last_healthcheck_at = now
        try:
            session = self._direct_http if _is_loopback_url(self._base_url()) else requests
            resp = session.get(
                f"{self._base_url()}/api/tags",
                timeout=self._healthcheck_timeout(),
            )
            resp.raise_for_status()
            self._last_tags = resp.json()
            self._last_tags_error = ""
            return True
        except Exception as exc:
            self._last_tags = None
            self._last_tags_error = str(exc).strip() or exc.__class__.__name__
            return False

    def _extract_model_names(self, payload: dict[str, Any] | None) -> list[str]:
        models = []
        for item in (payload or {}).get("models") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "").strip()
            if name and name not in models:
                models.append(name)
        return models

    def _model_exists_locked(self, model: str) -> bool:
        if not model:
            return False
        if not self._check_health(force=False):
            return False
        names = self._extract_model_names(self._last_tags)
        return model in names

    def _ensure_service_locked(self, *, reason: str = "") -> tuple[bool, str]:
        if self._check_health(force=False):
            self._set_status_locked("ready", "本地模型服务已连接")
            return True, ""

        if not self._auto_start_enabled():
            message = "本地模型服务未启动，且已关闭自动拉起"
            self._set_status_locked("error", message, error=message)
            return False, message

        binary = self._resolve_binary_path()
        if not binary:
            message = self._build_missing_binary_message()
            self._set_status_locked("error", message, error=message)
            return False, message

        if self._service_process and self._service_process.poll() is None:
            return self._wait_for_service_locked(reason=reason or "wait_running")

        try:
            self._service_process = subprocess.Popen(
                [binary, "serve"],
                cwd=str(get_app_root()),
                env=self._build_ollama_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_status_locked("starting", "正在启动本地模型服务…")
        except Exception as exc:
            message = f"自动启动本地模型服务失败：{exc}"
            self._set_status_locked("error", message, error=message)
            return False, message

        return self._wait_for_service_locked(reason=reason or "start")

    def _wait_for_service_locked(self, *, reason: str = "") -> tuple[bool, str]:
        deadline = time.time() + self._startup_timeout()
        while time.time() < deadline:
            self._refresh_process_state_locked()
            if self._check_health(force=True):
                self._set_status_locked("ready", "本地模型服务已就绪")
                return True, ""
            time.sleep(self._poll_interval())

        message = (
            self._last_tags_error
            or f"本地模型服务启动超时{f'（{reason}）' if reason else ''}"
        )
        self._set_status_locked("error", message, error=message)
        return False, message

    def _ensure_pull_locked(self, model: str) -> tuple[bool, str]:
        if self._pull_process and self._pull_process.poll() is None:
            if self._pulling_model == model:
                message = f"本地模型首次下载中：{model}"
                self._set_status_locked("preparing", message)
                return True, message
            message = f"正在下载另一个本地模型：{self._pulling_model}"
            self._set_status_locked("preparing", message)
            return True, message

        binary = self._resolve_binary_path()
        if not binary:
            message = self._build_missing_binary_message()
            self._set_status_locked("error", message, error=message)
            return False, message

        self._pulling_model = model

        def _worker() -> None:
            process: subprocess.Popen | None = None
            try:
                process = subprocess.Popen(
                    [binary, "pull", model],
                    cwd=str(get_app_root()),
                    env=self._build_ollama_env(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with self._lock:
                    self._pull_process = process
                    self._set_status_locked("preparing", f"正在下载本地模型：{model}")
                code = process.wait()
                with self._lock:
                    self._pull_process = None
                    self._refresh_process_state_locked()
                    if code == 0:
                        self._check_health(force=True)
                        self._set_status_locked("ready", f"本地模型已准备完成：{model}")
                    else:
                        message = f"下载本地模型失败：{model}"
                        self._set_status_locked("error", message, error=message)
                    self._pulling_model = ""
            except Exception as exc:
                with self._lock:
                    self._pull_process = None
                    self._pulling_model = ""
                    message = f"下载本地模型失败：{exc}"
                    self._set_status_locked("error", message, error=message)
            finally:
                if process and process.poll() is None:
                    try:
                        process.kill()
                    except Exception:
                        pass

        self._pull_thread = threading.Thread(target=_worker, daemon=True, name="LocalModelPull")
        self._pull_thread.start()
        message = f"本地模型首次下载中：{model}"
        self._set_status_locked("preparing", message)
        return True, message

    def _refresh_process_state_locked(self) -> None:
        if self._service_process and self._service_process.poll() is not None:
            self._service_process = None
        if self._pull_process and self._pull_process.poll() is not None:
            self._pull_process = None

    def _set_status_locked(self, status: str, message: str, *, error: str = "") -> None:
        self._status = str(status or "idle").strip() or "idle"
        self._status_message = str(message or "").strip() or "本地模型未启动"
        if error:
            self._last_error = str(error or "").strip()
        elif self._status == "ready":
            self._last_error = ""


def get_local_model_manager() -> LocalModelManager:
    return LocalModelManager.get_instance()
