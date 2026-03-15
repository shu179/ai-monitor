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


def run_task(task: dict, default_notify_config: dict) -> tuple:
    """
    执行单个监控任务
    每个任务有自己的webhook_url

    参数:
        task: 任务配置
        default_notify_config: 默认通知配置

    返回: (rank, screenshot_path)
    """
    platform_name = task['platform']
    keyword = task['keyword']
    brand = task['brand']

    # 获取任务的webhook
    webhook_url = task.get('webhook_url', '')
    notifier = None

    if webhook_url and webhook_url != 'YOUR_KEY_HERE':
        notifier = WeComNotifier(
            webhook_url=webhook_url,
            cooldown_minutes=default_notify_config.get('cooldown_minutes', 30),
            send_interval=default_notify_config.get('send_interval', 2)
        )
    else:
        print(f"[Main] 警告: 任务 '{task.get('name')}' 未配置有效的Webhook")

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


    # 获取默认通知配置
    default_notify_config = config.get('default_notification', {})

    # 4. 创建托盘应用
    app = TrayApp(scheduler, None, config)

    # 绑定任务执行函数
    def execute_task(task):
        rank, screenshot = run_task(task, default_notify_config)
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
