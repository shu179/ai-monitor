#!/usr/bin/env python3
"""Run a read-only selector diagnosis from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend_lib.selector_heal_service import SelectorHealService
from core.browser_platform_factory import create_browser_platform
from core.config_watcher import load_config
from core.app_paths import resolve_app_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读诊断抓取模式 selector 候选")
    parser.add_argument("--platform", default="deepseek", help="平台 ID，当前优先支持 deepseek")
    parser.add_argument(
        "--field",
        action="append",
        dest="fields",
        default=[],
        help="要诊断的 selector 字段，可传多次；默认 new_chat_selector",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="在临时有头浏览器中执行行为验证；当前仅支持 deepseek.new_chat_selector",
    )
    parser.add_argument(
        "--vision",
        action="store_true",
        help="预留参数：视觉兜底尚未开放，传入会得到明确拒绝",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    service = SelectorHealService(
        config_loader=lambda: load_config(resolve_app_path("config.yaml")),
        platform_factory=create_browser_platform,
    )
    result = service.diagnose(
        {
            "platform": args.platform,
            "fields": args.fields or ["new_chat_selector"],
            "verify": bool(args.verify),
            "vision": bool(args.vision),
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
