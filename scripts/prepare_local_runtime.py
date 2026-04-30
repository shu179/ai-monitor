"""源码模式下准备本地模型运行环境。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.app_paths import resolve_app_path
from core.local_runtime_prep import prepare_local_runtime, shutdown_owned_local_runtime


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="准备 Surfaced 本地模型运行环境")
    parser.add_argument(
        "--model",
        default="",
        help="强制使用的模型名，默认读取 config.yaml",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="准备过程超时时间（秒），默认 1800",
    )
    parser.add_argument(
        "--no-model-pull",
        action="store_true",
        help="只准备运行时，不自动拉取缺失模型",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = resolve_app_path("config.yaml")
    try:
        result = prepare_local_runtime(
            config_path,
            model_override=args.model,
            timeout_seconds=args.timeout,
            allow_model_pull=not args.no_model_pull,
        )
        message = str(result.get("message") or "").strip() or "本地运行环境准备完成"
        print(f"[LocalRuntimePrep] {message}")
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(f"[LocalRuntimePrep] 本地运行环境准备失败: {exc}")
        return 1
    finally:
        shutdown_owned_local_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
