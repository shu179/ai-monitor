"""Public task execution entrypoints.

`run_task_group` used to live in `main.py`, which made Web UI code reach back
into the process entry module. Keeping the public import here gives desktop,
Web, and tests a stable module boundary while `main.py` remains a compatibility
importer.
"""

from __future__ import annotations

from .task_executor_browser import (
    _create_browser_platform,
    _should_use_session_pool_for_query,
    _sync_reused_platform_runtime_state,
)
from .task_executor_impl import run_task_group

__all__ = [
    "_create_browser_platform",
    "_should_use_session_pool_for_query",
    "_sync_reused_platform_runtime_state",
    "run_task_group",
]
