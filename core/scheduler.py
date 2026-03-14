"""
智能调度器
- 夜间休眠模式
- 随机偏移（防封号）
- 星期控制
- 任务管理
"""

import time
import random
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Callable, Optional


class SmartScheduler:
    """
    智能任务调度器
    支持夜间休眠、随机间隔、星期筛选
    """

    def __init__(self, config: dict):
        self.config = config
        self.night_mode = config.get('night_mode', True)
        self.night_start = config.get('night_start', '23:00')
        self.night_end = config.get('night_end', '07:00')
        self.base_interval = config.get('interval', 300)  # 默认5分钟
        self.random_jitter = config.get('random_jitter', 0.2)  # ±20%

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def is_night(self) -> bool:
        """判断是否处于夜间休眠时间"""
        if not self.night_mode:
            return False

        now = datetime.now()
        current_time = now.hour + now.minute / 60

        # 解析夜间时间
        start_parts = self.night_start.split(':')
        end_parts = self.night_end.split(':')

        night_start_hour = int(start_parts[0])
        night_start_min = int(start_parts[1]) if len(start_parts) > 1 else 0
        night_end_hour = int(end_parts[0])
        night_end_min = int(end_parts[1]) if len(end_parts) > 1 else 0

        night_start = night_start_hour + night_start_min / 60
        night_end = night_end_hour + night_end_min / 60

        # 处理跨天的情况（如 23:00 - 07:00）
        if night_start > night_end:
            # 跨天
            return current_time >= night_start or current_time < night_end
        else:
            # 不跨天
            return night_start <= current_time < night_end

    def get_next_interval(self) -> float:
        """
        获取下一次执行的间隔时间
        基础间隔 + 随机偏移 ±random_jitter
        """
        jitter = self.base_interval * self.random_jitter
        interval = self.base_interval + random.uniform(-jitter, jitter)
        return max(interval, 60)  # 最少60秒

    def should_run_task(self, task: dict) -> bool:
        """
        检查任务是否应该执行
        - 是否启用
        - 今天是否在允许的运行日期内
        """
        if not task.get('enabled', True):
            return False

        # 检查星期
        weekdays = task.get('weekdays', [0, 1, 2, 3, 4, 5, 6])
        today = datetime.now().weekday()  # 0=周一, 6=周日

        if today not in weekdays:
            return False

        return True

    def format_sleep_time(self, seconds: float) -> str:
        """格式化休眠时间显示"""
        if seconds < 60:
            return f"{int(seconds)}秒"
        elif seconds < 3600:
            return f"{int(seconds/60)}分钟"
        else:
            hours = int(seconds / 3600)
            minutes = int((seconds % 3600) / 60)
            return f"{hours}小时{minutes}分钟"

    def run(
        self,
        tasks: List[dict],
        executor: Callable,
        on_status_change: Optional[Callable] = None
    ):
        """
        主调度循环

        参数:
            tasks: 任务配置列表
            executor: 任务执行函数 (task) -> result
            on_status_change: 状态变更回调 (status: str, message: str)
        """
        self._running = True
        self._stop_event.clear()

        print(f"[Scheduler] 调度器启动，共 {len(tasks)} 个任务")

        while self._running and not self._stop_event.is_set():
            # 检查夜间模式
            if self.is_night():
                # 计算到早晨的剩余时间
                now = datetime.now()
                end_parts = self.night_end.split(':')
                end_hour = int(end_parts[0])
                end_min = int(end_parts[1]) if len(end_parts) > 1 else 0

                # 构造今天的结束时间
                end_time = now.replace(hour=end_hour, minute=end_min, second=0)

                # 如果已经过了今天结束时间，设为明天
                if now >= end_time:
                    end_time += timedelta(days=1)

                sleep_seconds = (end_time - now).total_seconds()

                msg = f"夜间模式，休眠至 {self.night_end}"
                print(f"[Scheduler] {msg}（{self.format_sleep_time(sleep_seconds)}）")

                if on_status_change:
                    on_status_change("night_mode", msg)

                # 分段休眠，便于响应停止信号
                self._stop_event.wait(min(sleep_seconds, 60))
                continue

            # 执行启用的任务
            active_tasks = [t for t in tasks if self.should_run_task(t)]

            if active_tasks:
                if on_status_change:
                    on_status_change("running", f"执行 {len(active_tasks)} 个任务")

                for task in active_tasks:
                    if not self._running or self._stop_event.is_set():
                        break

                    try:
                        print(f"[Scheduler] 执行任务: {task.get('name', task.get('platform'))}")
                        executor(task)
                    except Exception as e:
                        error_msg = f"任务执行失败: {e}"
                        print(f"[Scheduler] {error_msg}")
                        if on_status_change:
                            on_status_change("error", error_msg)

            # 等待下一次执行
            interval = self.get_next_interval()
            next_run = datetime.now() + timedelta(seconds=interval)

            msg = f"下次执行: {next_run.strftime('%H:%M:%S')}（{self.format_sleep_time(interval)}后）"
            print(f"[Scheduler] {msg}")

            if on_status_change:
                on_status_change("waiting", msg)

            # 分段等待，便于响应停止
            self._stop_event.wait(interval)

        print("[Scheduler] 调度器已停止")
        if on_status_change:
            on_status_change("stopped", "调度器已停止")

    def start(
        self,
        tasks: List[dict],
        executor: Callable,
        on_status_change: Optional[Callable] = None
    ):
        """在后台线程中启动调度器"""
        if self._running:
            print("[Scheduler] 调度器已在运行")
            return

        self._thread = threading.Thread(
            target=self.run,
            args=(tasks, executor, on_status_change),
            daemon=True
        )
        self._thread.start()

    def stop(self):
        """停止调度器"""
        if not self._running:
            return

        print("[Scheduler] 正在停止调度器...")
        self._running = False
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def get_status(self) -> dict:
        """获取调度器状态"""
        return {
            "running": self._running,
            "night_mode": self.night_mode,
            "is_night": self.is_night(),
            "night_start": self.night_start,
            "night_end": self.night_end,
            "base_interval": self.base_interval,
            "next_interval": self.get_next_interval() if self._running else None
        }
