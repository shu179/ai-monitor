"""
任务配置管理器
图形界面管理任务组：平台 + 关键词 + 品牌 + 企业微信
"""

import tkinter as tk
from tkinter import ttk, messagebox
import yaml
from pathlib import Path


class TaskConfigDialog:
    """添加/编辑任务对话框"""

    def __init__(self, parent, task=None, bots=None):
        self.result = None
        self.bots = bots or {}

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("添加任务组" if task is None else "编辑任务组")
        self.dialog.geometry("550x600")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self.create_form(task)

    def create_form(self, task):
        """创建表单（带滚动条）"""
        # 创建Canvas和滚动条
        canvas = tk.Canvas(self.dialog)
        scrollbar = ttk.Scrollbar(self.dialog, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas, padding="20")

        frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 鼠标滚轮支持
        def on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind("<MouseWheel>", on_mousewheel)
        frame.bind("<MouseWheel>", on_mousewheel)

        # 任务名称
        ttk.Label(frame, text="任务名称:").pack(anchor=tk.W, pady=(0, 5))
        self.name_var = tk.StringVar(value=task.get('name', '') if task else '')
        ttk.Entry(frame, textvariable=self.name_var, width=50).pack(fill=tk.X, pady=(0, 15))

        # 选择平台
        ttk.Label(frame, text="选择平台:").pack(anchor=tk.W, pady=(0, 5))
        self.platform_var = tk.StringVar(value=task.get('platform', 'doubao') if task else 'doubao')

        platforms_frame = ttk.Frame(frame)
        platforms_frame.pack(fill=tk.X, pady=(0, 15))

        platforms = [
            ('doubao', '豆包'),
            ('deepseek', 'DeepSeek'),
            ('kimi', 'Kimi'),
            ('yuanbao', '腾讯元宝'),
            ('tongyi', '通义千问'),
            ('wenxin', '文心一言')
        ]

        for code, name in platforms:
            ttk.Radiobutton(
                platforms_frame,
                text=name,
                variable=self.platform_var,
                value=code
            ).pack(anchor=tk.W)

        # 关键词
        ttk.Label(frame, text="搜索关键词:").pack(anchor=tk.W, pady=(0, 5))
        self.keyword_var = tk.StringVar(value=task.get('keyword', '') if task else '')
        ttk.Entry(frame, textvariable=self.keyword_var, width=50).pack(fill=tk.X, pady=(0, 15))

        # 品牌名
        ttk.Label(frame, text="监控品牌:").pack(anchor=tk.W, pady=(0, 5))
        self.brand_var = tk.StringVar(value=task.get('brand', '') if task else '')
        ttk.Entry(frame, textvariable=self.brand_var, width=50).pack(fill=tk.X, pady=(0, 15))

        # 企业微信Webhook（带右键菜单）
        ttk.Label(frame, text="企业微信机器人Webhook:").pack(anchor=tk.W, pady=(0, 5))
        self.webhook_var = tk.StringVar(value=task.get('webhook_url', '') if task else '')
        self.webhook_entry = ttk.Entry(frame, textvariable=self.webhook_var, width=50)
        self.webhook_entry.pack(fill=tk.X, pady=(0, 15))

        # 右键菜单
        def show_context_menu(event):
            menu = tk.Menu(self.dialog, tearoff=0)
            menu.add_command(label="粘贴", command=lambda: self.webhook_entry.event_generate("<<Paste>>"))
            menu.post(event.x_root, event.y_root)

        # 绑定右键 (Windows/Linux: Button-3, Mac: Button-2)
        self.webhook_entry.bind("<Button-3>", show_context_menu)
        self.webhook_entry.bind("<Button-2>", show_context_menu)

        # 运行星期
        ttk.Label(frame, text="运行时间:").pack(anchor=tk.W, pady=(0, 5))
        self.weekdays_frame = ttk.Frame(frame)
        self.weekdays_frame.pack(fill=tk.X, pady=(0, 15))

        self.weekday_vars = []
        weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        current_weekdays = task.get('weekdays', [0, 1, 2, 3, 4]) if task else [0, 1, 2, 3, 4]

        for i, name in enumerate(weekday_names):
            var = tk.BooleanVar(value=i in current_weekdays)
            self.weekday_vars.append(var)
            ttk.Checkbutton(
                self.weekdays_frame,
                text=name,
                variable=var
            ).pack(side=tk.LEFT, padx=5)

        # 调度方式选择
        ttk.Label(frame, text="调度方式:").pack(anchor=tk.W, pady=(0, 5))

        self.schedule_mode = tk.StringVar(value='schedule' if task and task.get('schedule') else 'interval')

        mode_frame = ttk.Frame(frame)
        mode_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Radiobutton(
            mode_frame,
            text="定点时间运行",
            variable=self.schedule_mode,
            value='schedule',
            command=self.toggle_schedule_mode
        ).pack(side=tk.LEFT, padx=5)

        ttk.Radiobutton(
            mode_frame,
            text="间隔运行",
            variable=self.schedule_mode,
            value='interval',
            command=self.toggle_schedule_mode
        ).pack(side=tk.LEFT, padx=5)

        # 定点时间设置
        self.schedule_frame = ttk.LabelFrame(frame, text="每天运行时间（可多选）", padding="10")
        self.schedule_frame.pack(fill=tk.X, pady=(0, 15))

        self.time_vars = []
        current_schedule = task.get('schedule', ['09:00', '14:00']) if task else ['09:00', '14:00']

        # 预设时间点
        preset_times = ['08:00', '09:00', '10:00', '11:00', '12:00',
                       '13:00', '14:00', '15:00', '16:00', '17:00', '18:00']

        time_grid = ttk.Frame(self.schedule_frame)
        time_grid.pack(fill=tk.X)

        for i, time_str in enumerate(preset_times):
            var = tk.BooleanVar(value=time_str in current_schedule)
            self.time_vars.append((time_str, var))
            ttk.Checkbutton(
                time_grid,
                text=time_str,
                variable=var
            ).grid(row=i // 4, column=i % 4, sticky=tk.W, padx=10, pady=2)

        # 间隔设置
        self.interval_frame = ttk.LabelFrame(frame, text="运行间隔", padding="10")
        self.interval_frame.pack(fill=tk.X, pady=(0, 15))

        interval_value = task.get('interval', 300) if task else 300
        self.interval_var = tk.IntVar(value=interval_value)

        interval_options = [
            (300, '5分钟'),
            (600, '10分钟'),
            (900, '15分钟'),
            (1800, '30分钟'),
            (3600, '1小时'),
        ]

        for seconds, label in interval_options:
            ttk.Radiobutton(
                self.interval_frame,
                text=label,
                variable=self.interval_var,
                value=seconds
            ).pack(side=tk.LEFT, padx=10)

        # 默认显示定点时间模式
        self.toggle_schedule_mode()

        # 启用状态
        self.enabled_var = tk.BooleanVar(value=task.get('enabled', True) if task else True)
        ttk.Checkbutton(
            frame,
            text="启用此任务",
            variable=self.enabled_var
        ).pack(anchor=tk.W, pady=(10, 20))

        # 按钮
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(
            btn_frame,
            text="保存",
            command=self.save
        ).pack(side=tk.RIGHT, padx=5)

        ttk.Button(
            btn_frame,
            text="取消",
            command=self.dialog.destroy
        ).pack(side=tk.RIGHT, padx=5)

    def toggle_schedule_mode(self):
        """切换调度模式显示"""
        if self.schedule_mode.get() == 'schedule':
            self.schedule_frame.pack(fill=tk.X, pady=(0, 15))
            self.interval_frame.pack_forget()
        else:
            self.schedule_frame.pack_forget()
            self.interval_frame.pack(fill=tk.X, pady=(0, 15))

    def save(self):
        """保存任务配置"""
        name = self.name_var.get().strip()
        keyword = self.keyword_var.get().strip()
        brand = self.brand_var.get().strip()

        if not name:
            messagebox.showerror("错误", "请输入任务名称")
            return

        if not keyword:
            messagebox.showerror("错误", "请输入搜索关键词")
            return

        if not brand:
            messagebox.showerror("错误", "请输入监控品牌")
            return

        weekdays = [i for i, var in enumerate(self.weekday_vars) if var.get()]
        if not weekdays:
            messagebox.showerror("错误", "请至少选择一天运行")
            return

        # 构建调度配置
        if self.schedule_mode.get() == 'schedule':
            schedule_times = [time_str for time_str, var in self.time_vars if var.get()]
            if not schedule_times:
                messagebox.showerror("错误", "请至少选择一个运行时间")
                return
            schedule_config = {'schedule': schedule_times}
        else:
            schedule_config = {'interval': self.interval_var.get()}

        self.result = {
            'name': name,
            'platform': self.platform_var.get(),
            'keyword': keyword,
            'brand': brand,
            'webhook_url': self.webhook_var.get().strip(),
            'weekdays': weekdays,
            'enabled': self.enabled_var.get(),
            **schedule_config
        }

        self.dialog.destroy()


class ConfigManagerWindow:
    """
    配置管理窗口
    管理所有任务组：添加、编辑、删除
    """

    def __init__(self, config_path="config.yaml"):
        self.config_path = Path(config_path)
        self.config = self.load_config()

        self.root = tk.Tk()
        self.root.title("AI品牌监控 - 任务配置管理")
        self.root.geometry("900x600")

        self.create_ui()
        self.refresh_task_list()

    def load_config(self) -> dict:
        """加载配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except:
            return {
                'wechat_bots': {'default': {'name': '默认群', 'webhook_url': ''}},
                'tasks': []
            }

    def save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.config, f, allow_unicode=True, sort_keys=False)
            messagebox.showinfo("成功", "配置已保存！")
            return True
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")
            return False

    def create_ui(self):
        """创建界面"""
        # 标题
        title = ttk.Label(
            self.root,
            text="任务组配置管理",
            font=("Arial", 16, "bold")
        )
        title.pack(pady=10)

        # 说明
        desc = ttk.Label(
            self.root,
            text="每个任务组 = 平台 + 关键词 + 品牌 + 企业微信群",
            font=("Arial", 10)
        )
        desc.pack()

        # 任务列表
        list_frame = ttk.Frame(self.root, padding="20")
        list_frame.pack(fill=tk.BOTH, expand=True)

        # 列表标题
        headers = ttk.Frame(list_frame)
        headers.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(headers, text="状态", width=6).pack(side=tk.LEFT)
        ttk.Label(headers, text="任务名称", width=20).pack(side=tk.LEFT)
        ttk.Label(headers, text="平台", width=10).pack(side=tk.LEFT)
        ttk.Label(headers, text="关键词", width=15).pack(side=tk.LEFT)
        ttk.Label(headers, text="品牌", width=12).pack(side=tk.LEFT)
        ttk.Label(headers, text="Webhook", width=18).pack(side=tk.LEFT)
        ttk.Label(headers, text="运行时间", width=15).pack(side=tk.LEFT)

        ttk.Separator(list_frame, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # 任务列表（带滚动条）
        canvas_frame = ttk.Frame(list_frame)
        canvas_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        scrollbar = ttk.Scrollbar(canvas_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas = tk.Canvas(canvas_frame, yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar.config(command=self.canvas.yview)

        self.tasks_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.tasks_frame, anchor=tk.NW)

        # 按钮区域
        btn_frame = ttk.Frame(self.root, padding="20")
        btn_frame.pack(fill=tk.X)

        ttk.Button(
            btn_frame,
            text="➕ 添加任务组",
            command=self.add_task
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame,
            text="💾 保存配置",
            command=self.save_config
        ).pack(side=tk.RIGHT, padx=5)

    def refresh_task_list(self):
        """刷新任务列表显示"""
        # 清空现有列表
        for widget in self.tasks_frame.winfo_children():
            widget.destroy()

        tasks = self.config.get('tasks', [])
        bots = self.config.get('wechat_bots', {})

        weekday_names = ['一', '二', '三', '四', '五', '六', '日']

        for i, task in enumerate(tasks):
            row = ttk.Frame(self.tasks_frame)
            row.pack(fill=tk.X, pady=2)

            # 状态
            status = "✅" if task.get('enabled', True) else "⏸️"
            ttk.Label(row, text=status, width=6).pack(side=tk.LEFT)

            # 任务名称
            ttk.Label(row, text=task.get('name', ''), width=20).pack(side=tk.LEFT)

            # 平台
            platform_names = {
                'doubao': '豆包',
                'deepseek': 'DeepSeek',
                'kimi': 'Kimi',
                'yuanbao': '元宝',
                'tongyi': '通义',
                'wenxin': '文心'
            }
            platform = platform_names.get(task.get('platform', ''), task.get('platform', ''))
            ttk.Label(row, text=platform, width=10).pack(side=tk.LEFT)

            # 关键词
            ttk.Label(row, text=task.get('keyword', ''), width=15).pack(side=tk.LEFT)

            # 品牌
            ttk.Label(row, text=task.get('brand', ''), width=12).pack(side=tk.LEFT)

            # Webhook（只显示前10个字符）
            webhook = task.get('webhook_url', '')
            webhook_short = webhook[:15] + '...' if len(webhook) > 15 else webhook
            ttk.Label(row, text=webhook_short, width=18).pack(side=tk.LEFT)

            # 运行时间（星期 + 时间点）
            weekdays = task.get('weekdays', [])
            days_str = ''.join([weekday_names[d] for d in weekdays if d < 7])

            schedule_times = task.get('schedule', [])
            if schedule_times:
                time_str = ','.join(schedule_times[:2])  # 最多显示2个时间点
                if len(schedule_times) > 2:
                    time_str += '...'
                run_info = f"{days_str} {time_str}"
            else:
                interval = task.get('interval', 300)
                interval_str = f"{interval // 60}分" if interval < 3600 else f"{interval // 3600}小时"
                run_info = f"{days_str} 每{interval_str}"

            ttk.Label(row, text=run_info, width=20).pack(side=tk.LEFT)

            # 操作按钮
            ttk.Button(
                row,
                text="编辑",
                command=lambda idx=i: self.edit_task(idx)
            ).pack(side=tk.RIGHT, padx=2)

            ttk.Button(
                row,
                text="删除",
                command=lambda idx=i: self.delete_task(idx)
            ).pack(side=tk.RIGHT, padx=2)

        self.tasks_frame.update_idletasks()
        self.canvas.config(scrollregion=self.canvas.bbox("all"))

    def add_task(self):
        """添加新任务"""
        dialog = TaskConfigDialog(self.root)
        self.root.wait_window(dialog.dialog)

        if dialog.result:
            if 'tasks' not in self.config:
                self.config['tasks'] = []
            self.config['tasks'].append(dialog.result)
            self.refresh_task_list()

    def edit_task(self, index):
        """编辑任务"""
        tasks = self.config.get('tasks', [])
        if index >= len(tasks):
            return

        dialog = TaskConfigDialog(self.root, task=tasks[index])
        self.root.wait_window(dialog.dialog)

        if dialog.result:
            tasks[index] = dialog.result
            self.refresh_task_list()

    def delete_task(self, index):
        """删除任务"""
        if messagebox.askyesno("确认", "确定要删除这个任务组吗？"):
            tasks = self.config.get('tasks', [])
            if index < len(tasks):
                tasks.pop(index)
                self.refresh_task_list()

    def run(self):
        """运行窗口"""
        self.root.mainloop()


def open_config_manager():
    """打开配置管理器（供托盘调用）"""
    app = ConfigManagerWindow("config.yaml")
    app.run()


if __name__ == "__main__":
    open_config_manager()
