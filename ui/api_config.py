"""
API Key 配置对话框
从托盘菜单"API Key 配置"打开
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import requests
from core.app_paths import resolve_app_path
from core.config_watcher import load_config as load_yaml_config
from platforms.api_client import (
    PLATFORM_API_CONFIG,
    get_platform_last_error,
    query_platform_api,
    get_platform_api_key,
    platform_requires_api_key,
    send_platform_chat_messages,
)
from ui.config_runtime import persist_config_with_feedback
from ui.tk_compat import (
    bind_mousewheel_recursive,
    install_global_tk_behaviors,
    scroll_canvas_on_mousewheel,
)


PLATFORM_GROUPS = [
    (
        "国内大模型",
        [
            ("local_model", "本地模型", "本地接口令牌（可留空，默认走 Ollama）", True),
            ("doubao", "豆包", "ARK API Key", True),
            ("deepseek", "DeepSeek", "API Key", True),
            ("ark_deepseek", "方舟 DeepSeek", "ARK API Key（可直接复用豆包 Key）", True),
            ("kimi", "Kimi", "API Key", True),
            ("tongyi", "通义千问", "API Key", True),
            ("wenxin", "文心一言", "Bearer Token 或 ACCESS_KEY|SECRET_KEY", True),
            ("yuanbao", "腾讯元宝", "API Key", True),
        ],
    ),
    (
        "国外大模型",
        [
            ("chatgpt", "ChatGPT", "OpenAI API Key", True),
            ("claude", "Claude", "Anthropic API Key", True),
            ("gemini", "Gemini", "Gemini API Key", True),
            ("perplexity", "Perplexity", "Perplexity API Key", True),
        ],
    ),
]

PLATFORM_INFO = [item for _, items in PLATFORM_GROUPS for item in items]
PLATFORM_LABELS = {code: name for code, name, _, _ in PLATFORM_INFO}


def iter_platform_groups():
    return PLATFORM_GROUPS

def get_default_model(code):
    return str(PLATFORM_API_CONFIG.get(code, {}).get("default_model") or "").strip()


def normalize_model_options(*values):
    options = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                model = str(item or "").strip()
                if model and model not in options:
                    options.append(model)
            continue

        model = str(value or "").strip()
        if model and model not in options:
            options.append(model)
    return options


def get_config_model_options(config, code, *extra_models):
    config = config or {}
    platforms_cfg = config.get("platforms", {}) or {}
    platform_cfg = platforms_cfg.get(code, {}) or {}
    if code == "local_model" and not platform_cfg:
        platform_cfg = platforms_cfg.get("local_qwen", {}) or {}
    return normalize_model_options(
        platform_cfg.get("model_options", []),
        platform_cfg.get("api_model", ""),
        get_default_model(code),
        *extra_models,
    )


def run_platform_api_test(code, api_key, model, config=None):
    code = str(code or "").strip()
    api_key = str(api_key or "").strip()
    model = str(model or "").strip()
    config = config or {}

    effective_key = api_key or get_platform_api_key(config, code)

    if platform_requires_api_key(code) and not effective_key:
        raise ValueError("请先填写 API Key")
    if not model:
        if code == "ark_deepseek":
            raise ValueError("请先填写 Endpoint ID / 模型标识")
        raise ValueError("请先填写模型名")

    prompt = "这是一条接口连通性测试。请只回复“测试成功”。"
    try:
        text = _run_platform_api_test_request(code, effective_key, model, prompt)
    except Exception as e:
        raise RuntimeError(_classify_test_exception(code, e)) from e
    if not text:
        raise RuntimeError("接口已连通，但没有返回内容")
    return str(text).strip()


def _run_platform_api_test_request(code, api_key, model, prompt):
    if code == "wenxin" and "|" in api_key:
        text = query_platform_api(
            code,
            prompt,
            api_key,
            model,
            enable_search=False,
            deep_think=False,
        )
        if not text:
            raise RuntimeError("文心旧版凭证调用失败，请检查 AK/SK、模型名或权限")
        return text

    cfg = PLATFORM_API_CONFIG.get(code, {}) or {}
    base_url = str(cfg.get("base_url") or "").strip()
    if not base_url:
        raise RuntimeError("当前平台没有可测试的官方 API 地址")

    text = send_platform_chat_messages(
        code,
        api_key,
        model,
        [
            {"role": "system", "content": "你只需要回复“测试成功”。"},
            {"role": "user", "content": prompt},
        ],
    )
    if not text:
        detail = get_platform_last_error(code)
        raise RuntimeError(detail or "接口已连接，但没有返回内容")
    return text


def _classify_test_exception(code, exc):
    raw = str(exc).strip() or exc.__class__.__name__
    lower = raw.lower()
    class_name = exc.__class__.__name__.lower()

    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)) or "timeout" in lower:
        return f"网络超时，请检查当前网络、代理设置，或稍后重试。原始错误：{raw}"

    if isinstance(exc, requests.exceptions.ConnectionError) or "connection error" in lower or "apiconnectionerror" in class_name:
        if code == "local_model":
            return f"本地模型服务连接失败。若你已经装了 Ollama，优先到“设置 -> 本地模型”点一次“准备本地模型”；再不行再检查本机 `http://127.0.0.1:11434` 是否可访问。原始错误：{raw}"
        return f"网络连接失败，请检查网络、DNS、代理或目标接口地址是否可达。原始错误：{raw}"

    if "authenticationerror" in class_name or "invalid api key" in lower or "unauthorized" in lower or "401" in lower:
        return f"API Key 无效或未开通该服务。原始错误：{raw}"

    if "permissiondeniederror" in class_name or "permission" in lower or "forbidden" in lower or "403" in lower:
        return f"当前账号没有这个模型或接口的调用权限。原始错误：{raw}"

    if "notfounderror" in class_name or "notfound" in lower or "does not exist" in lower or "invalidendpointormodel.notfound" in lower or "404" in lower:
        if code == "ark_deepseek":
            return f"模型或 Endpoint ID 不存在，或你当前账号无权访问。方舟通常应填写 `ep-...` 的接入点 ID。原始错误：{raw}"
        return f"模型名不存在，或当前账号无权访问这个模型。原始错误：{raw}"

    if "badrequesterror" in class_name or "invalid_request_error" in lower or "400" in lower:
        if code == "ark_deepseek":
            return f"请求参数不正确，请重点检查 Endpoint ID / 模型标识是否填对。原始错误：{raw}"
        return f"请求参数不正确，请检查模型名、Key 类型或接口参数。原始错误：{raw}"

    if "ratelimiterror" in class_name or "rate limit" in lower or "429" in lower:
        return f"请求过于频繁，或当前额度/并发受限。原始错误：{raw}"

    return f"调用失败，请检查 Key、模型名/Endpoint ID、权限和网络。原始错误：{raw}"


class ApiConfigDialog:
    """API Key 管理对话框"""

    def __init__(self, parent, config_path="config.yaml", on_config_change=None):
        self.config_path = resolve_app_path(config_path)
        self.config = self._load()
        self.on_config_change = on_config_change

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("API Key 配置")
        self.dialog.geometry("620x480")
        self.dialog.grab_set()
        self.dialog.resizable(False, True)
        self.dialog.lift()
        self.dialog.focus_force()
        install_global_tk_behaviors(self.dialog)

        self._vars = {}  # platform -> {'api_key', 'api_model', 'model_options', ...}
        self._build_ui()

    def _load(self):
        try:
            return load_yaml_config(self.config_path)
        except Exception:
            return {}

    def _build_ui(self):
        # 滚动区域
        canvas = tk.Canvas(self.dialog, borderwidth=0)
        scrollbar = ttk.Scrollbar(self.dialog, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=20)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        def on_scroll(event):
            return scroll_canvas_on_mousewheel(canvas, event)
        canvas.bind("<MouseWheel>", on_scroll)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(inner, text="API Key 配置",
                  font=("Arial", 13, "bold")).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(inner,
                  text="这里只负责各平台 API Key 和模型。联网搜索、OCR 与调度时间请到“设置”里配置。",
                  foreground="gray").pack(anchor=tk.W, pady=(0, 14))

        platforms_cfg = self.config.get('platforms', {})

        for section_title, section_items in iter_platform_groups():
            section = ttk.Frame(inner)
            section.pack(fill=tk.X, pady=(0, 10))

            ttk.Label(
                section,
                text=section_title,
                font=("Arial", 11, "bold"),
            ).pack(anchor=tk.W, pady=(6, 6))

            for code, name, hint, has_api in section_items:
                row = ttk.LabelFrame(section, text=name, padding=10)
                row.pack(fill=tk.X, pady=4)
                row.bind("<MouseWheel>", on_scroll)

                if not has_api:
                    ttk.Label(row, text=hint, foreground="gray").pack(anchor=tk.W)
                    self._vars[code] = {
                        'api_key': tk.StringVar(value=''),
                        'api_model': tk.StringVar(value=''),
                        'model_options': [],
                        'test_status': tk.StringVar(value=''),
                    }
                    continue

                # API Key
                key_label = f"API Key（{hint}）:"
                if code == "local_model":
                    key_label = f"接口令牌（{hint}）:"
                ttk.Label(row, text=key_label).pack(anchor=tk.W)
                key_var = tk.StringVar(value=platforms_cfg.get(code, {}).get('api_key', ''))
                key_entry = ttk.Entry(row, textvariable=key_var, width=60, show="*")
                key_entry.pack(fill=tk.X, pady=(2, 6))
                key_entry.bind("<MouseWheel>", on_scroll)

                def _ctx(event, e=key_entry):
                    m = tk.Menu(self.dialog, tearoff=0)
                    m.add_command(label="复制", command=lambda: e.event_generate("<<CopyCompat>>"))
                    m.add_command(label="剪切", command=lambda: e.event_generate("<<CutCompat>>"))
                    m.add_command(label="粘贴", command=lambda: e.event_generate("<<PasteCompat>>"))
                    m.add_command(label="全选", command=lambda: e.event_generate("<<SelectAllCompat>>"))
                    m.post(event.x_root, event.y_root)
                key_entry.bind("<Button-3>", _ctx)
                key_entry.bind("<Button-2>", _ctx)

                # 模型
                saved_model = platforms_cfg.get(code, {}).get('api_model', '')
                model_options = get_config_model_options(self.config, code, saved_model)
                model_var = tk.StringVar(value=saved_model)
                model_row = ttk.Frame(row)
                model_row.pack(fill=tk.X)
                if code == 'ark_deepseek':
                    model_text = "推理接入点 ID / 模型标识（建议填 Endpoint ID，如 ep-xxxx，不要填页面展示名）:"
                elif code == 'local_model':
                    model_text = "本地模型名（默认 gemma4:e2b，按 Ollama 模型名填写）:"
                else:
                    model_text = "模型（手填或从自定义列表中选，建议显式填写）:"
                ttk.Label(model_row, text=model_text).pack(side=tk.LEFT)
                model_combo = ttk.Combobox(
                    model_row,
                    textvariable=model_var,
                    values=model_options,
                    width=30,
                    state='normal',
                )
                model_combo.pack(side=tk.LEFT, padx=(6, 0))

                def _model_ctx(event, e=model_combo):
                    m = tk.Menu(self.dialog, tearoff=0)
                    m.add_command(label="复制", command=lambda: e.event_generate("<<CopyCompat>>"))
                    m.add_command(label="剪切", command=lambda: e.event_generate("<<CutCompat>>"))
                    m.add_command(label="粘贴", command=lambda: e.event_generate("<<PasteCompat>>"))
                    m.add_command(label="全选", command=lambda: e.event_generate("<<SelectAllCompat>>"))
                    m.post(event.x_root, event.y_root)
                model_combo.bind("<Button-3>", _model_ctx)
                model_combo.bind("<Button-2>", _model_ctx)

                model_btn_row = ttk.Frame(row)
                model_btn_row.pack(fill=tk.X, pady=(6, 0))
                ttk.Button(
                    model_btn_row,
                    text="新增模型名",
                    command=lambda c=code: self._add_model_option(c),
                ).pack(side=tk.LEFT)
                ttk.Button(
                    model_btn_row,
                    text="删除当前模型名",
                    command=lambda c=code: self._remove_model_option(c),
                ).pack(side=tk.LEFT, padx=(8, 0))
                test_btn = ttk.Button(
                    model_btn_row,
                    text="测试",
                    command=lambda c=code: self._test_platform(c),
                )
                test_btn.pack(side=tk.LEFT, padx=(8, 0))
                test_status = tk.StringVar(value="")
                ttk.Label(
                    model_btn_row,
                    text="列表由你维护，程序不再内置固定候选。",
                    foreground="gray",
                ).pack(side=tk.LEFT, padx=(10, 0))
                ttk.Label(
                    model_btn_row,
                    textvariable=test_status,
                    foreground="gray",
                ).pack(side=tk.RIGHT)

                self._vars[code] = {
                    'api_key': key_var,
                    'api_model': model_var,
                    'model_options': model_options,
                    'model_combo': model_combo,
                    'test_button': test_btn,
                    'test_status': test_status,
                }

                ttk.Label(
                    row,
                    text="联网搜索默认启用：优先走各平台原生搜索，失败时自动兜底。",
                    foreground="gray",
                    wraplength=520,
                ).pack(anchor=tk.W, pady=(8, 0))

                if code == 'wenxin':
                    ttk.Label(
                        row,
                        text="提示：文心使用 Bearer Token 时可优先走原生搜索；旧 ACCESS_KEY|SECRET_KEY 会自动退回兼容模式。",
                        foreground="gray",
                        wraplength=520,
                    ).pack(anchor=tk.W, pady=(4, 0))
                elif code == 'ark_deepseek':
                    ttk.Label(
                        row,
                        text="提示：方舟 DeepSeek 可以直接复用豆包的 API Key，模型单独填 Endpoint ID（通常形如 ep-xxxx）即可；联网搜索会优先走火山方舟原生 web_search。",
                        foreground="gray",
                        wraplength=520,
                    ).pack(anchor=tk.W, pady=(4, 0))

        # 按钮
        btn_frame = ttk.Frame(inner)
        btn_frame.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(btn_frame, text="保存", command=self._save).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.dialog.destroy).pack(side=tk.RIGHT, padx=5)
        bind_mousewheel_recursive(inner, on_scroll)

    def _save(self):
        if 'platforms' not in self.config:
            self.config['platforms'] = {}

        for code, d in self._vars.items():
            if code not in self.config['platforms']:
                self.config['platforms'][code] = {}
            self.config['platforms'][code]['api_key'] = d['api_key'].get().strip()
            model = d['api_model'].get().strip()
            if model:
                self.config['platforms'][code]['api_model'] = model
            elif 'api_model' in self.config['platforms'].get(code, {}):
                del self.config['platforms'][code]['api_model']

            model_options = normalize_model_options(d.get('model_options', []))
            if model_options:
                self.config['platforms'][code]['model_options'] = model_options
            elif 'model_options' in self.config['platforms'].get(code, {}):
                del self.config['platforms'][code]['model_options']

        persist_config_with_feedback(
            self.config,
            self.config_path,
            success_message="API Key 已保存",
            on_config_change=self.on_config_change,
            event_root=self.dialog._root(),
            on_success=self.dialog.destroy,
        )

    def _refresh_model_combo(self, code):
        data = self._vars.get(code)
        if not data:
            return
        combo = data.get('model_combo')
        if combo is not None:
            combo['values'] = normalize_model_options(data.get('model_options', []), data['api_model'].get().strip())

    def _add_model_option(self, code):
        data = self._vars.get(code)
        if not data:
            return
        model = data['api_model'].get().strip()
        if not model:
            messagebox.showwarning("提示", "请先输入模型名", parent=self.dialog)
            return
        options = normalize_model_options(data.get('model_options', []), model)
        data['model_options'] = options
        self._refresh_model_combo(code)

    def _remove_model_option(self, code):
        data = self._vars.get(code)
        if not data:
            return
        model = data['api_model'].get().strip()
        if not model:
            messagebox.showwarning("提示", "请先输入或选中要删除的模型名", parent=self.dialog)
            return
        options = [item for item in data.get('model_options', []) if item != model]
        if len(options) == len(data.get('model_options', [])):
            messagebox.showinfo("提示", "当前模型名不在自定义列表里", parent=self.dialog)
            return
        data['model_options'] = options
        self._refresh_model_combo(code)

    def _set_test_ui_state(self, code, status_text="", busy=False):
        data = self._vars.get(code)
        if not data:
            return
        status_var = data.get('test_status')
        if status_var is not None:
            status_var.set(status_text)
        button = data.get('test_button')
        if button is not None:
            button.config(state=(tk.DISABLED if busy else tk.NORMAL))

    def _test_platform(self, code):
        data = self._vars.get(code)
        if not data:
            return

        api_key = data['api_key'].get().strip()
        model = data['api_model'].get().strip()
        platform_label = PLATFORM_LABELS.get(code, code)
        self._set_test_ui_state(code, "测试中...", busy=True)

        def worker():
            try:
                result = run_platform_api_test(code, api_key, model, config=self.config)
                preview = result if len(result) <= 120 else result[:120] + "..."
                self.dialog.after(0, lambda: (
                    self._set_test_ui_state(code, "测试成功", busy=False),
                    messagebox.showinfo(
                        "测试成功",
                        f"{platform_label} 接口可用。\n\n返回内容：\n{preview}",
                        parent=self.dialog,
                    ),
                ))
            except Exception as e:
                error_text = str(e).strip() or e.__class__.__name__
                self.dialog.after(0, lambda: (
                    self._set_test_ui_state(code, "测试失败", busy=False),
                    messagebox.showerror(
                        "测试失败",
                        f"{platform_label} 测试失败：{error_text}",
                        parent=self.dialog,
                    ),
                ))

        threading.Thread(target=worker, daemon=True).start()
