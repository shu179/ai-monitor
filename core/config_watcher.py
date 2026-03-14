"""
配置文件热重载
使用 watchdog 监听文件变化
"""

import os
import time
import yaml
from typing import Callable, Optional
from pathlib import Path


class ConfigWatcher:
    """
    配置文件监听器
    支持热重载配置
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        on_change: Optional[Callable] = None,
        debounce_seconds: float = 1.0
    ):
        self.config_path = Path(config_path)
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self._last_modified = 0
        self._last_content = None
        self._running = False
        self._watcher = None

    def load_config(self) -> dict:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[ConfigWatcher] 加载配置失败: {e}")
            return {}

    def check_and_reload(self) -> bool:
        """
        检查文件是否变化，如有变化则重新加载
        返回: 是否发生变化
        """
        try:
            if not self.config_path.exists():
                return False

            current_modified = self.config_path.stat().st_mtime

            # 防抖：确保文件稳定后才读取
            if current_modified == self._last_modified:
                return False

            if time.time() - current_modified < self.debounce_seconds:
                return False

            # 读取新内容
            with open(self.config_path, 'r', encoding='utf-8') as f:
                current_content = f.read()

            # 对比内容
            if current_content == self._last_content:
                self._last_modified = current_modified
                return False

            # 尝试解析YAML
            try:
                new_config = yaml.safe_load(current_content)
                if new_config is None:
                    return False
            except yaml.YAMLError as e:
                print(f"[ConfigWatcher] YAML解析错误: {e}")
                return False

            # 更新状态
            self._last_modified = current_modified
            self._last_content = current_content

            print("[ConfigWatcher] 配置已更新")

            # 触发回调
            if self.on_change:
                self.on_change(new_config)

            return True

        except Exception as e:
            print(f"[ConfigWatcher] 检查更新失败: {e}")
            return False

    def start_polling(self, interval: float = 2.0):
        """
        启动轮询模式监听（简单可靠）
        参数:
            interval: 检查间隔（秒）
        """
        import threading

        self._running = True

        def poll():
            while self._running:
                self.check_and_reload()
                time.sleep(interval)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        print(f"[ConfigWatcher] 轮询监听已启动: {self.config_path}")

    def start_watchdog(self):
        """
        启动 watchdog 监听（更高效，依赖watchdog库）
        """
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class ConfigHandler(FileSystemEventHandler):
                def __init__(self, watcher):
                    self.watcher = watcher

                def on_modified(self, event):
                    if not event.is_directory:
                        if Path(event.src_path).name == self.watcher.config_path.name:
                            time.sleep(self.watcher.debounce_seconds)
                            self.watcher.check_and_reload()

            self._watcher = Observer()
            handler = ConfigHandler(self)
            self._watcher.schedule(
                handler,
                str(self.config_path.parent),
                recursive=False
            )
            self._watcher.start()
            print(f"[ConfigWatcher] Watchdog监听已启动: {self.config_path}")

        except ImportError:
            print("[ConfigWatcher] watchdog未安装，使用轮询模式")
            self.start_polling()

    def stop(self):
        """停止监听"""
        self._running = False
        if self._watcher:
            self._watcher.stop()
            self._watcher.join()


# 简单的配置加载函数
def load_config(config_path: str = "config.yaml") -> dict:
    """加载YAML配置文件"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"[Config] 配置文件不存在: {config_path}")
        return {}
    except Exception as e:
        print(f"[Config] 加载配置失败: {e}")
        return {}
