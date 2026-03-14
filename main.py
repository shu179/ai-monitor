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
from platforms import DoubaoPlatform


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


def run_task(task: dict, notifier: WeComNotifier) -> tuple:
    """
    执行单个监控任务

    返回: (rank, screenshot_path)
    """
    platform_name = task['platform']
    keyword = task['keyword']
    brand = task['brand']

    # 创建平台实例
    user_data_dir = f"./auth/{platform_name}"

    if platform_name == 'doubao':
        platform = DoubaoPlatform(user_data_dir)
    else:
        print(f"[Main] 不支持的平台: {platform_name}")
        return 99, None

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
            'notification': {'cooldown_minutes': 30},
            'tasks': []
        }

    # 3. 创建核心组件
    scheduler_config = config.get('scheduler', {})
    scheduler = SmartScheduler(scheduler_config)

    notify_config = config.get('notification', {})
    webhook_url = notify_config.get('webhook_url', '')

    if not webhook_url or webhook_url == 'YOUR_KEY_HERE':
        print("[Main] 警告: 未配置企业微信Webhook，通知功能不可用")
        notifier = None
    else:
        notifier = WeComNotifier(
            webhook_url=webhook_url,
            cooldown_minutes=notify_config.get('cooldown_minutes', 30),
            send_interval=notify_config.get('send_interval', 2)
        )

    # 4. 创建托盘应用
    app = TrayApp(scheduler, notifier, config)

    # 绑定任务执行函数
    def execute_task(task):
        rank, screenshot = run_task(task, notifier)
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
