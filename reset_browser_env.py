#!/usr/bin/env python3
"""
浏览器环境重置工具

用法：
    python reset_browser_env.py                    # 交互式选择平台
    python reset_browser_env.py doubao             # 重置豆包
    python reset_browser_env.py --all              # 重置所有平台
    python reset_browser_env.py --list             # 列出当前环境状态
"""

import sys
import argparse
from pathlib import Path

# 添加项目目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from core.browser_auth import (
    BROWSER_AUTH_PLATFORM_IDS,
    reset_browser_auth_environment,
    reset_all_browser_auth_environments,
    get_browser_auth_snapshot,
)


def print_platform_status(snapshot: dict):
    """打印平台状态"""
    platform = snapshot.get("platform", "unknown")
    active_profile = snapshot.get("active_profile")

    print(f"\n平台: {platform}")
    if active_profile:
        print(f"  当前环境: {active_profile.get('label')}")
        print(f"  路径: {active_profile.get('absolute_path')}")
        print(f"  认证状态: {active_profile.get('auth_state')}")
        print(f"  有文件: {'是' if active_profile.get('has_files') else '否'}")
    else:
        print("  无活动环境")


def list_environments():
    """列出所有平台的环境状态"""
    print("=" * 60)
    print("浏览器环境状态")
    print("=" * 60)

    snapshots = get_browser_auth_snapshot()
    for platform_name in BROWSER_AUTH_PLATFORM_IDS:
        snapshot = snapshots.get(platform_name)
        if snapshot:
            print_platform_status(snapshot)

    print("\n" + "=" * 60)


def reset_single_platform(platform_name: str):
    """重置单个平台"""
    if platform_name not in BROWSER_AUTH_PLATFORM_IDS:
        print(f"错误: 不支持的平台 '{platform_name}'")
        print(f"支持的平台: {', '.join(BROWSER_AUTH_PLATFORM_IDS)}")
        return False

    print(f"\n准备重置 {platform_name} 的浏览器环境...")
    print("警告: 这将删除所有登录信息和浏览器数据！")

    confirm = input(f"确认重置 {platform_name}? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("已取消")
        return False

    print(f"\n正在重置 {platform_name}...")
    result = reset_browser_auth_environment(platform_name)

    reset_info = result.get("reset_info", {})
    deleted_paths = reset_info.get("deleted_paths", [])
    new_path = reset_info.get("new_profile_path", "")

    print(f"\n✓ 重置完成！")
    print(f"\n已删除的目录 ({len(deleted_paths)}):")
    for path in deleted_paths:
        print(f"  - {path}")

    print(f"\n新环境路径:")
    print(f"  {new_path}")

    print(f"\n下一步:")
    print(f"  1. 重启程序")
    print(f"  2. 在「平台账号」中重新登录 {platform_name}")

    return True


def reset_all_platforms():
    """重置所有平台"""
    print("\n准备重置所有平台的浏览器环境...")
    print("警告: 这将删除所有平台的登录信息和浏览器数据！")
    print(f"影响的平台: {', '.join(BROWSER_AUTH_PLATFORM_IDS)}")

    confirm = input("\n确认重置所有平台? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("已取消")
        return False

    print("\n正在重置所有平台...")
    result = reset_all_browser_auth_environments()

    deleted_paths = result.get("deleted_paths", [])
    platforms = result.get("platforms", {})

    print(f"\n✓ 重置完成！")
    print(f"\n已删除的目录 ({len(deleted_paths)}):")
    for path in deleted_paths:
        print(f"  - {path}")

    print(f"\n新环境:")
    for platform_name, info in platforms.items():
        print(f"  {platform_name}: {info.get('profile_path')}")

    print(f"\n下一步:")
    print(f"  1. 重启程序")
    print(f"  2. 在「平台账号」中重新登录各个平台")

    return True


def interactive_mode():
    """交互式选择平台"""
    print("\n浏览器环境重置工具")
    print("=" * 60)
    print("\n支持的平台:")
    for i, platform in enumerate(BROWSER_AUTH_PLATFORM_IDS, 1):
        print(f"  {i}. {platform}")
    print(f"  {len(BROWSER_AUTH_PLATFORM_IDS) + 1}. 所有平台")
    print("  0. 退出")

    try:
        choice = input("\n请选择要重置的平台 (输入编号): ").strip()
        choice_num = int(choice)

        if choice_num == 0:
            print("已退出")
            return
        elif choice_num == len(BROWSER_AUTH_PLATFORM_IDS) + 1:
            reset_all_platforms()
        elif 1 <= choice_num <= len(BROWSER_AUTH_PLATFORM_IDS):
            platform_name = BROWSER_AUTH_PLATFORM_IDS[choice_num - 1]
            reset_single_platform(platform_name)
        else:
            print("无效的选择")
    except (ValueError, KeyboardInterrupt):
        print("\n已取消")


def main():
    parser = argparse.ArgumentParser(
        description="浏览器环境重置工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python reset_browser_env.py                    # 交互式选择
  python reset_browser_env.py doubao             # 重置豆包
  python reset_browser_env.py --all              # 重置所有平台
  python reset_browser_env.py --list             # 列出环境状态
        """
    )
    parser.add_argument(
        "platform",
        nargs="?",
        help=f"要重置的平台名称 ({', '.join(BROWSER_AUTH_PLATFORM_IDS)})"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="重置所有平台"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出当前环境状态"
    )

    args = parser.parse_args()

    if args.list:
        list_environments()
    elif args.all:
        reset_all_platforms()
    elif args.platform:
        reset_single_platform(args.platform)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
