"""
系统托盘应用
- 启动/停止监控
- 查看状态
- 编辑配置
- 退出
"""

import os
import sys
import tkinter as tk
from tkinter import messagebox, scrolledtext
from pathlib import Path

import pystray
from PIL import Image, ImageDraw


class TrayApp:
    """
    系统托盘应用
    集成调度器和通知器
    """

    def __init__(self, scheduler=None, notifier=None, config=None):
        self.scheduler = scheduler
        self.notifier = notifier
        self.config = config or {}

        self.icon = None
        self.running = False
        self.last_results = {}  # (platform, brand) -> rank
        self.status_message = "就绪"

    def create_icon(self):
        """创建托盘图标"""
        # 创建一个简单的蓝色方形图标
        width = 64
        height = 64
        image = Image.new('RGB', (width, height), color='#1890ff')
        dc = ImageDraw.Draw(image)

        # 画一个白色的"AI"字样
        dc.text((18, 20), "AI", fill='white')

        return image

    def create_menu(self):
        """创建托盘菜单"""
        return pystray.Menu(
            pystray.MenuItem(
                lambda text: f"📊 状态: {self.status_message}",
                lambda: None,
                enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "📈 查看排名状态",
                self.show_status
            ),
            pystray.MenuItem(
                lambda text: "⏸️ 停止监控" if self.running else "▶️ 开始监控",
                self.toggle_monitoring
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "⚙️ 编辑配置",
                self.open_config
            ),
            pystray.MenuItem(
                "📝 查看日志",
                self.view_logs
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "❌ 退出",
                self.quit
            )
        )

    def show_status(self):
        """弹出状态窗口"""
        root = tk.Tk()
        root.title("品牌监控状态")
        root.geometry("500x400")
        root.resizable(True, True)

        # 标题
        title = tk.Label(
            root,
            text="📊 各平台排名状态",
            font=("Arial", 14, "bold")
        )
        title.pack(pady=10)

        # 状态文本区域
        text_area = scrolledtext.ScrolledText(
            root,
            wrap=tk.WORD,
            font=("Courier", 11)
        )
        text_area.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

        # 填充内容
        if self.last_results:
            for (platform, brand), rank in sorted(self.last_results.items()):
                status_emoji = "🎉" if rank <= 3 else "📊" if rank < 99 else "❌"
                line = f"{status_emoji} {platform:12s} | {brand:15s} | 第 {rank:2d} 名\n"
                text_area.insert(tk.END, line)
        else:
            text_area.insert(tk.END, "暂无数据，监控未运行或未产生结果\n")

        # 调度器状态
        if self.scheduler:
            text_area.insert(tk.END, "\n" + "="*50 + "\n")
            status = self.scheduler.get_status()
            text_area.insert(tk.END, f"调度器状态: {'运行中' if status['running'] else '已停止'}\n")
            text_area.insert(tk.END, f"夜间模式: {'开启' if status['night_mode'] else '关闭'}\n")
            if status['night_mode']:
                text_area.insert(tk.END, f"当前夜间: {'是' if status['is_night'] else '否'}\n")

        # 通知器状态
        if self.notifier:
            text_area.insert(tk.END, "\n" + "="*50 + "\n")
            notify_status = self.notifier.get_status()
            text_area.insert(tk.END, f"活跃冷却: {len(notify_status['active_cooldowns'])} 个\n")

        text_area.config(state=tk.DISABLED)

        # 关闭按钮
        btn = tk.Button(root, text="关闭", command=root.destroy)
        btn.pack(pady=10)

        root.mainloop()

    def toggle_monitoring(self):
        """切换监控状态"""
        if self.running:
            self.stop_monitoring()
        else:
            self.start_monitoring()

    def start_monitoring(self):
        """开始监控"""
        if not self.scheduler:
            messagebox.showerror("错误", "调度器未初始化")
            return

        self.running = True
        self.status_message = "运行中"

        # 加载任务
        tasks = self.config.get('tasks', [])
        enabled_tasks = [t for t in tasks if t.get('enabled', True)]

        def on_status_change(status, message):
            self.status_message = message[:30]
            print(f"[Tray] {status}: {message}")

        self.scheduler.start(enabled_tasks, self._execute_task, on_status_change)
        print("[Tray] 监控已启动")

    def stop_monitoring(self):
        """停止监控"""
        if self.scheduler:
            self.scheduler.stop()

        self.running = False
        self.status_message = "已停止"
        print("[Tray] 监控已停止")

    def _execute_task(self, task: dict):
        """执行单个任务"""
        # 这里会在 main.py 中被具体实现
        # 临时输出日志
        print(f"[Tray] 执行任务: {task.get('name', task.get('platform'))}")

    def open_config(self):
        """打开配置文件"""
        config_path = Path("config.yaml").absolute()

        if not config_path.exists():
            messagebox.showerror("错误", f"配置文件不存在: {config_path}")
            return

        # 尝试用系统默认编辑器打开
        try:
            if sys.platform == 'win32':
                os.startfile(config_path)
            elif sys.platform == 'darwin':
                os.system(f'open "{config_path}"')
            else:
                os.system(f'xdg-open "{config_path}"')
        except Exception as e:
            messagebox.showerror("错误", f"打开配置文件失败: {e}")

    def view_logs(self):
        """查看日志"""
        log_path = Path("logs/monitor.log")

        root = tk.Tk()
        root.title("运行日志")
        root.geometry("700x500")

        text_area = scrolledtext.ScrolledText(root, wrap=tk.WORD)
        text_area.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

        if log_path.exists():
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    text_area.insert(tk.END, content)
            except Exception as e:
                text_area.insert(tk.END, f"读取日志失败: {e}")
        else:
            text_area.insert(tk.END, "日志文件不存在\n")

        text_area.config(state=tk.DISABLED)

        btn = tk.Button(root, text="关闭", command=root.destroy)
        btn.pack(pady=10)

        root.mainloop()

    def quit(self):
        """退出程序"""
        print("[Tray] 正在退出...")

        if self.running:
            self.stop_monitoring()

        if self.icon:
            self.icon.stop()

        sys.exit(0)

    def update_result(self, platform: str, brand: str, rank: int):
        """更新排名结果"""
        self.last_results[(platform, brand)] = rank

    def run(self):
        """启动托盘"""
        self.icon = pystray.Icon(
            "ai_brand_monitor",
            self.create_icon(),
            "AI品牌监控",
            self.create_menu()
        )

        print("[Tray] 系统托盘已启动")
        self.icon.run()


if __name__ == "__main__":
    # 测试运行
    app = TrayApp()
    app.run()
