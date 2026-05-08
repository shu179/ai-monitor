"""Public task execution entrypoints.

`run_task_group` used to live in `main.py`, which made Web UI code reach back
into the process entry module. Keeping the public import here gives desktop,
Web, and tests a stable module boundary while `main.py` remains a compatibility
importer.
"""

from __future__ import annotations

# Compatibility exports: `main.py` historically exposed these private helpers.
# Keep them here while downstream imports migrate to the concrete modules.
from .task_executor_api import _run_api_task
from .task_executor_browser import (
    _apply_browser_runtime_config,
    _create_browser_platform,
    _should_use_platform_serial_for_query,
    _should_use_session_pool_for_query,
    _sync_reused_platform_runtime_state,
)
from .task_executor_impl import run_task_group
from .task_executor_smart import _run_smart_browser_task

__all__ = [
    "_apply_browser_runtime_config",
    "_create_browser_platform",
    "_run_api_task",
    "_run_smart_browser_task",
    "_should_use_platform_serial_for_query",
    "_should_use_session_pool_for_query",
    "_sync_reused_platform_runtime_state",
    "run_task_group",
]
