"""
AI品牌监控 - 主入口
系统托盘应用
"""

import os
import sys
import atexit
import subprocess
from pathlib import Path

# 添加项目目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from core import load_config, SmartScheduler, WeComNotifier
from ui.tray import TrayApp
from platforms import (
    DoubaoPlatform, DeepSeekPlatform, KimiPlatform,
    YuanbaoPlatform, TongyiPlatform, WenxinPlatform
)


def cleanup_chrome_processes():
    """清理残留的Chrome进程"""
    try:
        if sys.platform == 'win32':
            subprocess.run(
                ['taskkill', '/F', '/IM', 'chrome.exe', '/FI', 'STATUS eq RUNNING'],
                capture_output=True,
                check=False
            )
        elif sys.platform == 'darwin':
            subprocess.run(
                ['pkill', '-f', 'Chromium'],
                capture_output=True,
                check=False
            )
        print("[Cleanup] 已清理残留进程")
    except Exception as e:
        print(f"[Cleanup] 清理进程时出错: {e}")


def run_task(task: dict, notifiers: dict, default_notify_config: dict) -> tuple:
    """
    执行单个监控任务
    每个任务可以指定使用哪个机器人

    参数:
        task: 任务配置
        notifiers: 机器人字典 {name: WeComNotifier}
        default_notify_config: 默认通知配置

    返回: (rank, screenshot_path)
    """
    platform_name = task['platform']
    keyword = task['keyword']
    brand = task['brand']

    # 获取任务指定的机器人
    bot_name = task.get('wechat_bot', 'default')
    notifier = notifiers.get(bot_name)

    if not notifier:
        print(f"[Main] 警告: 任务 '{task.get('name')}' 指定的机器人 '{bot_name}' 不存在")

    # 创建平台实例
    user_data_dir = f"./auth/{platform_name}"

    # 平台映射
    platform_map = {
        'doubao': DoubaoPlatform,
        'deepseek': DeepSeekPlatform,
        'kimi': KimiPlatform,
        'yuanbao': YuanbaoPlatform,
        'tongyi': TongyiPlatform,
        'wenxin': WenxinPlatform,
    }

    platform_class = platform_map.get(platform_name)
    if not platform_class:
        print(f"[Main] 不支持的平台: {platform_name}")
        return 99, None

    platform = platform_class(user_data_dir)

    try:
        with platform:
            rank, screenshot = platform.search(keyword, brand)

            # 发送通知（如果排名较好）
            if rank <= 3 and notifier:
                notifier.send(
                    platform=platform_name,
                    keyword=keyword,
                    brand=brand,
                    rank=rank,
                    screenshot_path=screenshot
                )

            return rank, screenshot

    except Exception as e:
        print(f"[Main] 任务执行失败: {e}")
        return 99, None


def create_notifiers(config: dict) -> dict:
    """
    根据配置创建多个企业微信通知器

    返回: {bot_name: WeComNotifier}
    """
    notifiers = {}

    # 获取默认通知配置
    default_notify = config.get('default_notification', {})

    # 获取机器人配置
    bots_config = config.get('wechat_bots', {})

    if not bots_config:
        print("[Main] 警告: 未配置任何企业微信机器人")
        return notifiers

    for bot_name, bot_config in bots_config.items():
        webhook_url = bot_config.get('webhook_url', '')

        if not webhook_url or webhook_url == 'YOUR_KEY_HERE':
            print(f"[Main] 警告: 机器人 '{bot_name}' 未配置有效的Webhook")
            continue

        notifier = WeComNotifier(
            webhook_url=webhook_url,
            cooldown_minutes=default_notify.get('cooldown_minutes', 30),
            send_interval=default_notify.get('send_interval', 2)
        )
        notifiers[bot_name] = notifier
        print(f"[Main] 已加载机器人: {bot_name} ({bot_config.get('name', '')})")

    return notifiers


def main():
    """主函数"""
    print("=" * 50)
    print("AI品牌监控系统启动")
    print("=" * 50)

    # 1. 清理残留进程
    cleanup_chrome_processes()
    atexit.register(cleanup_chrome_processes)

    # 2. 加载配置
    config = load_config("config.yaml")
    if not config:
        print("[Main] 配置文件加载失败，使用默认配置")
        config = {
            'scheduler': {'interval': 300, 'night_mode': True},
            'default_notification': {'cooldown_minutes': 30},
            'wechat_bots': {},
            'tasks': []
        }

    # 3. 创建核心组件
    scheduler_config = config.get('scheduler', {})
    scheduler = SmartScheduler(scheduler_config)

    # 创建多个企业微信通知器（支持多群）
    notifiers = create_notifiers(config)

    # 获取默认通知配置
    default_notify_config = config.get('default_notification', {})

    # 4. 创建托盘应用
    app = TrayApp(scheduler, notifiers.get('default'), config)

    # 绑定任务执行函数
    def execute_task(task):
        rank, screenshot = run_task(task, notifiers, default_notify_config)
        # 更新托盘显示
        app.update_result(task['platform'], task['brand'], rank)

    app._execute_task = execute_task

    # 5. 启动托盘
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n[Main] 收到中断信号，正在退出...")
        app.quit()


if __name__ == "__main__":
    main()
