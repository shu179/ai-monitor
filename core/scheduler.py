"""
智能调度器
- 夜间休眠模式
- 随机偏移（防封号）
- 星期控制 + 具体时间控制
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
    支持：
    - 夜间休眠
    - 星期控制（周几运行）
    - 时间控制（几点几分运行）
    - 随机偏移（防封号）
    """

    def __init__(self, config: dict):
        self.config = config
        self.night_mode = config.get('night_mode', True)
        self.night_start = config.get('night_start', '23:00')
        self.night_end = config.get('night_end', '07:00')
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

    def should_run_task_today(self, task: dict) -> bool:
        """
        检查任务今天是否应该运行（基于星期）
        """
        if not task.get('enabled', True):
            return False

        # 检查星期
        weekdays = task.get('weekdays', [0, 1, 2, 3, 4, 5, 6])
        today = datetime.now().weekday()  # 0=周一, 6=周日

        if today not in weekdays:
            return False

        return True

    def parse_time(self, time_str: str) -> tuple:
        """解析时间字符串 'HH:MM' 返回 (hour, minute)"""
        parts = time_str.split(':')
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return (hour, minute)

    def get_task_next_run_time(self, task: dict, after: datetime = None) -> Optional[datetime]:
        """
        计算任务下次运行时间

        支持两种模式：
        1. interval: 基于间隔（每隔N秒）
        2. schedule: 定点时间（如 ["09:00", "14:30"])

        返回: 下次运行的datetime，如果今天不运行返回None
        """
        now = after or datetime.now()

        # 检查今天是否应该运行
        if not self.should_run_task_today(task):
            return None

        # 获取调度模式
        schedule_times = task.get('schedule')  # 定点时间列表 ["09:00", "14:00"]
        interval = task.get('interval')  # 间隔秒数

        if schedule_times:
            # 定点时间模式
            for time_str in sorted(schedule_times):
                hour, minute = self.parse_time(time_str)
                run_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # 添加随机偏移 ±random_jitter 分钟
                jitter_minutes = int(10 * self.random_jitter)  # 默认±2分钟
                if jitter_minutes > 0:
                    jitter = random.randint(-jitter_minutes, jitter_minutes)
                    run_time += timedelta(minutes=jitter)

                if run_time > now:
                    return run_time

            # 今天的所有时间点已过，返回明天第一个时间点
            tomorrow = now + timedelta(days=1)
            if self.should_run_task_today({**task, 'weekdays': task.get('weekdays')}):
                hour, minute = self.parse_time(schedule_times[0])
                return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)

            return None

        elif interval:
            # 间隔模式
            jitter = interval * self.random_jitter
            actual_interval = interval + random.uniform(-jitter, jitter)
            return now + timedelta(seconds=actual_interval)

        else:
            # 默认间隔5分钟
            return now + timedelta(seconds=300)

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

        支持：
        - 定点时间调度（schedule: ["09:00", "14:30"]）
        - 间隔调度（interval: 300）

        参数:
            tasks: 任务配置列表
            executor: 任务执行函数 (task) -> result
            on_status_change: 状态变更回调 (status: str, message: str)
        """
        self._running = True
        self._stop_event.clear()

        print(f"[Scheduler] 调度器启动，共 {len(tasks)} 个任务")

        # 记录每个任务上次运行时间
        last_run: Dict[str, datetime] = {}

        while self._running and not self._stop_event.is_set():
            now = datetime.now()

            # 检查夜间模式
            if self.is_night():
                end_parts = self.night_end.split(':')
                end_hour = int(end_parts[0])
                end_min = int(end_parts[1]) if len(end_parts) > 1 else 0

                end_time = now.replace(hour=end_hour, minute=end_min, second=0)
                if now >= end_time:
                    end_time += timedelta(days=1)

                sleep_seconds = (end_time - now).total_seconds()
                msg = f"夜间模式，休眠至 {self.night_end}"
                print(f"[Scheduler] {msg}（{self.format_sleep_time(sleep_seconds)}）")

                if on_status_change:
                    on_status_change("night_mode", msg)

                self._stop_event.wait(min(sleep_seconds, 60))
                continue

            # 检查每个任务是否应该运行
            tasks_to_run = []
            for task in tasks:
                task_id = task.get('name', str(id(task)))

                # 检查今天是否应该运行（星期）
                if not self.should_run_task_today(task):
                    continue

                # 检查是否是定点时间任务
                schedule_times = task.get('schedule')
                if schedule_times:
                    # 定点任务：检查当前时间是否匹配
                    for time_str in schedule_times:
                        hour, minute = self.parse_time(time_str)
                        current_time = now.strftime("%H:%M")
                        target_time = f"{hour:02d}:{minute:02d}"

                        # 当前时间是否在目标时间的2分钟内且今天未运行过
                        time_diff = abs((now.hour * 60 + now.minute) - (hour * 60 + minute))
                        if time_diff <= 2:
                            last_run_time = last_run.get(task_id)
                            if not last_run_time or (now - last_run_time).total_seconds() > 3600:
                                tasks_to_run.append(task)
                                last_run[task_id] = now
                                break

                else:
                    # 间隔任务：检查上次运行时间
                    interval = task.get('interval', 300)
                    last_run_time = last_run.get(task_id)

                    if not last_run_time or (now - last_run_time).total_seconds() >= interval:
                        tasks_to_run.append(task)
                        last_run[task_id] = now

            # 执行任务
            if tasks_to_run:
                if on_status_change:
                    on_status_change("running", f"执行 {len(tasks_to_run)} 个任务")

                for task in tasks_to_run:
                    if not self._running or self._stop_event.is_set():
                        break

                    try:
                        task_name = task.get('name', task.get('platform'))
                        print(f"[Scheduler] 执行任务: {task_name}")
                        executor(task)
                    except Exception as e:
                        error_msg = f"任务执行失败: {e}"
                        print(f"[Scheduler] {error_msg}")
                        if on_status_change:
                            on_status_change("error", error_msg)

            # 等待一小段时间再检查
            self._stop_event.wait(30)  # 每30秒检查一次

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
