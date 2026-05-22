"""
搜搜 —— AI 助手对话框

提供三类能力：
- 自由对话
- 一句话生成任务组草案
- 截图识别品牌并匹配任务，确认后发送企业微信
"""

import base64
import json
import mimetypes
import threading
import tkinter as tk
import requests
from tkinter import filedialog, messagebox, scrolledtext, ttk
from datetime import datetime
from pathlib import Path

import yaml

from core.app_paths import resolve_app_path
from core.config_watcher import load_config as load_yaml_config, save_config as save_yaml_config
from core.daily_task_state import assign_task_id
from core.notifier import WeComNotifier
from platforms.api_client import (
    PLATFORM_API_CONFIG,
    get_platform_last_error,
    get_platform_api_key,
    _build_extra_body,
    _build_search_context,
    model_supports_image_input,
    platform_has_configured_access,
    platform_requires_api_key,
    send_platform_chat_messages,
)
from platforms.base import BasePlatform
from ui.tk_compat import install_global_tk_behaviors


PLATFORM_LABELS = {
    "local_model": "本地模型",
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "ark_deepseek": "方舟 DeepSeek",
    "kimi": "Kimi",
    "tongyi": "通义千问",
    "wenxin": "文心一言",
    "yuanbao": "腾讯元宝",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
}

TASK_PLATFORM_CODES = ["doubao", "deepseek", "ark_deepseek", "kimi", "yuanbao", "tongyi", "wenxin"]
AI_PLATFORM_CODES = [
    code for code, cfg in PLATFORM_API_CONFIG.items()
    if cfg.get("base_url")
]

MODE_ALIASES = {
    "browser": "browser",
    "抓取模式": "browser",
    "抓取": "browser",
    "recognition": "recognition",
    "识别模式": "recognition",
    "识别": "recognition",
}


def _normalize_model_options(*values):
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


def _get_config_model_options(config, code, *extra_models):
    config = config or {}
    platforms_cfg = config.get("platforms", {}) or {}
    platform_cfg = platforms_cfg.get(code, {}) or {}
    if code == "local_model" and not platform_cfg:
        platform_cfg = platforms_cfg.get("local_qwen", {}) or {}
    return _normalize_model_options(
        platform_cfg.get("model_options", []),
        platform_cfg.get("api_model", ""),
        str((PLATFORM_API_CONFIG.get(code) or {}).get("default_model") or "").strip(),
        *extra_models,
    )


def _preview_debug_text(value, limit=120):
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _is_explicit_program_intent(text):
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    explicit_phrases = [
        "这个程序", "这个软件", "这个应用", "本程序", "本软件",
        "surfaced 程序", "surfaced 软件", "surfaced 里",
        "帮我配置", "帮我设置", "查看配置", "修改配置",
        "程序状态", "运行状态", "任务状态", "监控任务",
        "新增任务", "创建任务", "删除任务", "启用任务", "禁用任务",
        "调度器", "企业微信", "webhook",
    ]
    return any(phrase in normalized for phrase in explicit_phrases)


class AIAssistantDialog:
    """搜搜（AI 助手）统一对话框。"""

    def __init__(self, parent, config=None, config_path="config.yaml", on_config_change=None,
                 get_runtime_state=None, on_program_action=None):
        self.parent = parent
        self.config_path = resolve_app_path(config_path)
        self.config = config if config is not None else self._load_config()
        self.on_config_change = on_config_change
        self.get_runtime_state = get_runtime_state  # () -> dict
        self.on_program_action = on_program_action   # (action: str, **kwargs) -> str

        self.dialog = tk.Toplevel(parent)
        self.dialog.withdraw()
        self.dialog.title("搜搜")
        self.dialog.geometry("980x760")
        self.dialog.minsize(860, 640)
        install_global_tk_behaviors(self.dialog)
        self._root_widget = self.dialog._root()
        self._config_event_binding = None
        if not self._is_hidden_parent(parent):
            self.dialog.transient(parent)

        self.chat_history = []
        self.generated_tasks = []
        self.selected_images = []
        self.analysis_result = None
        self.match_vars = {}
        self.matched_task_payloads = []

        ai_cfg = self.config.get("ai_assistant", {})
        available = self._get_available_ai_platforms()
        default_platform = ai_cfg.get("platform")
        if default_platform not in available:
            default_platform = available[0] if available else ""

        default_model = ai_cfg.get("model") or self._get_default_model(default_platform)

        self.platform_var = tk.StringVar(value=default_platform)
        self.model_var = tk.StringVar(value=default_model)
        self.chat_search_var = tk.BooleanVar(
            value=self._resolve_chat_search_default(default_platform, ai_cfg=ai_cfg)
        )
        self.status_var = tk.StringVar(value="就绪")
        self._platform_model_cache = {}
        self._current_platform_code = ""

        for code in AI_PLATFORM_CODES:
            saved_model = self.config.get("platforms", {}).get(code, {}).get("api_model", "").strip()
            if saved_model:
                self._platform_model_cache[code] = saved_model
        if default_platform and default_model:
            self._platform_model_cache[default_platform] = default_model

        self._build_ui()
        self._bind_external_config_updates()
        self._refresh_platforms()
        self.dialog.protocol("WM_DELETE_WINDOW", self._on_close)
        self.dialog.after(0, self._show_window)

    def _is_hidden_parent(self, parent):
        try:
            return parent is None or str(parent.state()) == "withdrawn"
        except Exception:
            return False

    def _show_window(self):
        try:
            self.dialog.deiconify()
            self.dialog.update_idletasks()
            self.dialog.lift()
            self.dialog.focus_force()
            self.dialog.grab_set()
        except Exception:
            pass

    def _build_ui(self):
        top = ttk.Frame(self.dialog, padding=14)
        top.pack(fill=tk.X)

        ttk.Label(top, text="接入平台:").pack(side=tk.LEFT)
        self.platform_menu = ttk.OptionMenu(top, self.platform_var, None)
        self.platform_menu.pack(side=tk.LEFT, padx=(6, 12))

        ttk.Label(top, text="模型:").pack(side=tk.LEFT)
        self.model_combo = ttk.Combobox(top, textvariable=self.model_var, width=28, state="normal")
        self.model_combo.pack(side=tk.LEFT, padx=(6, 12))

        def _model_ctx(event, e=self.model_combo):
            m = tk.Menu(self.dialog, tearoff=0)
            m.add_command(label="复制", command=lambda: e.event_generate("<<CopyCompat>>"))
            m.add_command(label="剪切", command=lambda: e.event_generate("<<CutCompat>>"))
            m.add_command(label="粘贴", command=lambda: e.event_generate("<<PasteCompat>>"))
            m.add_command(label="全选", command=lambda: e.event_generate("<<SelectAllCompat>>"))
            m.post(event.x_root, event.y_root)
        self.model_combo.bind("<Button-3>", _model_ctx)
        self.model_combo.bind("<Button-2>", _model_ctx)

        ttk.Button(top, text="刷新平台", command=self._refresh_platforms).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(top, textvariable=self.status_var, foreground="gray").pack(side=tk.RIGHT)

        note = ttk.Label(
            self.dialog,
            text="同一个对话框里支持自由对话、自然语言建任务，以及隐藏能力「AI识图」。关键动作都会先让你确认。",
            foreground="gray"
        )
        note.pack(anchor=tk.W, padx=16, pady=(0, 8))

        self.notebook = ttk.Notebook(self.dialog)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        self._build_chat_tab()
        self._build_task_tab()
        self._build_image_tab()

        self.platform_var.trace_add("write", self._on_platform_change)

    def _build_chat_tab(self):
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="自由对话")

        self.chat_text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Arial", 11))
        self.chat_text.pack(fill=tk.BOTH, expand=True)
        self.chat_text.insert(tk.END, "搜搜已连接。你可以直接提问，也可以让它帮你梳理任务需求。\n\n")
        self.chat_text.config(state=tk.DISABLED)

        input_frame = ttk.Frame(frame)
        input_frame.pack(fill=tk.X, pady=(10, 0))

        self.chat_input = scrolledtext.ScrolledText(input_frame, height=5, wrap=tk.WORD, font=("Arial", 11))
        self.chat_input.pack(fill=tk.X, expand=True)

        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(
            btns,
            text="联网搜索",
            variable=self.chat_search_var,
        ).pack(side=tk.LEFT)
        ttk.Label(
            btns,
            text="在线模型默认开启；本地模型默认关闭，更接近原生对话。",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(btns, text="发送", command=self._send_chat).pack(side=tk.RIGHT)
        ttk.Button(btns, text="清空对话", command=self._clear_chat).pack(side=tk.RIGHT, padx=(0, 8))

    def _build_task_tab(self):
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="一句话建任务")

        ttk.Label(
            frame,
            text="输入你的自然语言需求，例如：品牌是即搜AI，监控关键词是武汉GEO优化公司和AI品牌推广，平台用豆包和DeepSeek，工作日上午9点运行，发默认企业微信。",
            foreground="gray",
            wraplength=860,
            justify="left"
        ).pack(anchor=tk.W, pady=(0, 8))

        self.task_prompt = scrolledtext.ScrolledText(frame, height=6, wrap=tk.WORD, font=("Arial", 11))
        self.task_prompt.pack(fill=tk.X)

        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(8, 10))
        ttk.Button(btns, text="生成任务草案", command=self._generate_tasks).pack(side=tk.LEFT)
        self.apply_task_btn = ttk.Button(btns, text="确认写入配置", command=self._apply_generated_tasks, state=tk.DISABLED)
        self.apply_task_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.task_preview = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Courier", 10))
        self.task_preview.pack(fill=tk.BOTH, expand=True)
        self.task_preview.insert(tk.END, "这里会显示 AI 解析出的任务草案。\n")
        self.task_preview.config(state=tk.DISABLED)

    def _build_image_tab(self):
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="AI识图")

        top = ttk.Frame(frame)
        top.pack(fill=tk.X)
        ttk.Button(top, text="选择截图", command=self._select_images).pack(side=tk.LEFT)
        ttk.Button(top, text="清空截图", command=self._clear_images).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="识别并匹配任务", command=self._analyze_images).pack(side=tk.LEFT, padx=(8, 0))
        self.send_match_btn = ttk.Button(top, text="确认发送到企业微信", command=self._send_selected_matches, state=tk.DISABLED)
        self.send_match_btn.pack(side=tk.RIGHT)

        self.image_info = tk.StringVar(value="未选择截图")
        ttk.Label(frame, textvariable=self.image_info, foreground="gray").pack(anchor=tk.W, pady=(8, 10))

        ttk.Label(frame, text="识别结果").pack(anchor=tk.W)
        self.image_result = scrolledtext.ScrolledText(frame, height=14, wrap=tk.WORD, font=("Courier", 10))
        self.image_result.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="匹配到的任务组").pack(anchor=tk.W, pady=(10, 6))
        self.match_container = ttk.Frame(frame)
        self.match_container.pack(fill=tk.X)

    def _load_config(self):
        try:
            return load_yaml_config(self.config_path)
        except Exception:
            return {}

    def _save_config(self):
        save_yaml_config(self.config, self.config_path)
        if self.on_config_change:
            self.on_config_change(self.config)

    def _on_close(self):
        if self._config_event_binding:
            try:
                self._root_widget.unbind("<<ConfigUpdated>>", self._config_event_binding)
            except Exception:
                pass
        self._persist_ai_preferences()
        self.dialog.destroy()

    def _bind_external_config_updates(self):
        self._config_event_binding = self._root_widget.bind(
            "<<ConfigUpdated>>",
            self._on_external_config_updated,
            add="+",
        )
        self.dialog.bind("<FocusIn>", self._on_external_config_updated, add="+")

    def _on_external_config_updated(self, _event=None):
        latest = self._load_config()
        if not latest:
            return

        self.config = latest
        ai_cfg = latest.get("ai_assistant", {}) or {}

        self._platform_model_cache = {}
        for code in AI_PLATFORM_CODES:
            saved_model = latest.get("platforms", {}).get(code, {}).get("api_model", "").strip()
            if saved_model:
                self._platform_model_cache[code] = saved_model

        assistant_platform = ai_cfg.get("platform", "").strip()
        assistant_model = ai_cfg.get("model", "").strip()
        if assistant_platform and assistant_model and assistant_platform not in self._platform_model_cache:
            self._platform_model_cache[assistant_platform] = assistant_model

        effective_platform = self.platform_var.get().strip() or assistant_platform
        self.chat_search_var.set(
            self._resolve_chat_search_default(effective_platform, ai_cfg=ai_cfg)
        )

        current_code = self.platform_var.get().strip()
        available = self._get_available_ai_platforms()
        if current_code not in available:
            current_code = assistant_platform if assistant_platform in available else (available[0] if available else "")
            self.platform_var.set(current_code)

        self._refresh_platforms()
        self._refresh_model_options(self.platform_var.get().strip(), force_model=True)
        self._current_platform_code = self.platform_var.get().strip()

    def _persist_ai_preferences(self):
        self.config.setdefault("ai_assistant", {})
        self.config["ai_assistant"]["platform"] = self.platform_var.get().strip()
        self.config["ai_assistant"]["model"] = self.model_var.get().strip()
        self.config["ai_assistant"]["chat_enable_search"] = bool(self.chat_search_var.get())
        try:
            self._save_config()
        except Exception:
            pass

    def _set_status(self, text):
        self.status_var.set(text)

    def _run_async(self, worker, on_error_prefix):
        def wrapped():
            try:
                worker()
            except Exception as e:
                error_text = str(e).strip() or e.__class__.__name__
                self.dialog.after(0, lambda error_text=error_text: (
                    self._set_status("失败"),
                    messagebox.showerror("错误", f"{on_error_prefix}: {error_text}", parent=self.dialog)
                ))
        threading.Thread(target=wrapped, daemon=True).start()

    def _append_chat(self, role, content):
        prefix = "你" if role == "user" else "AI"
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.insert(tk.END, f"{prefix}：{content}\n\n")
        self.chat_text.see(tk.END)
        self.chat_text.config(state=tk.DISABLED)

    def _clear_chat(self):
        self.chat_history = []
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.insert(tk.END, "对话已清空。\n\n")
        self.chat_text.config(state=tk.DISABLED)

    def _resolve_chat_search_default(self, platform_code, *, ai_cfg=None):
        settings = ai_cfg if isinstance(ai_cfg, dict) else (self.config.get("ai_assistant", {}) or {})
        if "chat_enable_search" in settings:
            return bool(settings.get("chat_enable_search"))
        return str(platform_code or "").strip() != "local_model"

    def _on_platform_change(self, *_):
        previous_code = self._current_platform_code
        current_code = self.platform_var.get().strip()

        if previous_code:
            previous_model = self.model_var.get().strip()
            if previous_model:
                self._platform_model_cache[previous_code] = previous_model

        self._refresh_model_options(current_code, force_model=current_code != previous_code)
        self._current_platform_code = current_code
        ai_cfg = self.config.get("ai_assistant", {}) or {}
        if "chat_enable_search" not in ai_cfg:
            self.chat_search_var.set(self._resolve_chat_search_default(current_code, ai_cfg=ai_cfg))

    def _refresh_platforms(self):
        available = self._get_available_ai_platforms()
        previous_code = self.platform_var.get().strip()
        menu = self.platform_menu["menu"]
        menu.delete(0, "end")

        if not available:
            self.platform_var.set("")
            self.model_combo["values"] = ()
            self.model_var.set("")
            self._set_status("请先在 API 配置里启用可用的 AI 平台或本地模型")
            return

        for code in available:
            menu.add_command(
                label=f"{PLATFORM_LABELS.get(code, code)} ({code})",
                command=lambda value=code: self.platform_var.set(value)
            )

        if self.platform_var.get() not in available:
            self.platform_var.set(available[0])
        self._refresh_model_options(
            self.platform_var.get().strip(),
            force_model=self.platform_var.get().strip() != previous_code,
        )
        self._current_platform_code = self.platform_var.get().strip()
        self._set_status("就绪")

    def _get_available_ai_platforms(self):
        return [
            code for code in AI_PLATFORM_CODES
            if platform_has_configured_access(self.config, code)
        ]

    def _get_default_model(self, code):
        if not code:
            return ""
        saved = self.config.get("platforms", {}).get(code, {}).get("api_model", "").strip()
        if saved:
            return saved
        return str((PLATFORM_API_CONFIG.get(code) or {}).get("default_model") or "").strip()

    def _get_preferred_model(self, code):
        return self._platform_model_cache.get(code, "").strip() or self._get_default_model(code)

    def _refresh_model_options(self, code, force_model=False):
        if not code:
            self.model_combo["values"] = ()
            if force_model:
                self.model_var.set("")
            return

        current_model = self.model_var.get().strip()
        values = _get_config_model_options(self.config, code, self._get_preferred_model(code), current_model)
        self.model_combo["values"] = values

        if force_model or not current_model:
            self.model_var.set(self._get_preferred_model(code))

    def _selected_platform_config(self):
        code = self.platform_var.get().strip()
        if not code:
            raise ValueError("请先选择接入平台")
        cfg = self.config.get("platforms", {}).get(code, {})
        api_key = get_platform_api_key(self.config, code)
        if platform_requires_api_key(code) and not api_key:
            raise ValueError(f"{PLATFORM_LABELS.get(code, code)} 尚未配置 API Key")
        return code, cfg, api_key

    def _augment_user_text_with_search(self, user_text, platform_code):
        search_context = _build_search_context(user_text, platform_code)
        if not search_context:
            return user_text

        return (
            f"用户问题：{user_text}\n\n"
            f"{search_context}\n\n"
            "请严格遵守：\n"
            "1. 优先依据上面的联网检索结果回答，不要忽略检索摘要。\n"
            "2. 如不同来源有冲突，优先较新日期和更权威来源。\n"
            "3. 如检索摘要仍不足以支持确定结论，请明确说明不确定。\n"
            "4. 回答先给结论，再补充必要依据。\n"
            "5. 用中文回答。"
        )

    def _supports_native_chat_search(self, platform_code):
        return platform_code in {"doubao", "ark_deepseek", "tongyi", "wenxin"}

    def _call_native_search_chat(self, client, code, model, messages, api_key, base_url):
        if code in {"doubao", "ark_deepseek"}:
            import requests as _req
            from platforms.api_client import _extract_responses_json_text
            payload: dict = {
                "model": model,
                "input": messages,
                "tools": [{"type": "web_search"}],
            }
            extra_body = _build_extra_body(code, enable_search=True, deep_think=False)
            if extra_body:
                payload.update(extra_body)
            url = base_url.rstrip("/") + "/responses"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = _req.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            return _extract_responses_json_text(resp.json()) or ""

        kwargs = {
            "model": model,
            "messages": messages,
            "timeout": 120,
        }
        extra_body = _build_extra_body(code, enable_search=True, deep_think=False)
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if isinstance(content, list):
            return "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            ).strip()
        return (content or "").strip()

    def _call_ai_chat(self, system_prompt, user_text, history=None, image_paths=None, enable_search=False):
        code, cfg, api_key = self._selected_platform_config()
        base_url = PLATFORM_API_CONFIG.get(code, {}).get("base_url")
        if not base_url:
            raise ValueError(f"{PLATFORM_LABELS.get(code, code)} 暂不支持搜搜对话")

        model = self.model_var.get().strip() or self._get_default_model(code)
        if not model:
            raise ValueError("请填写模型名称")
        if image_paths and not model_supports_image_input(code, model):
            raise ValueError(
                f"{PLATFORM_LABELS.get(code, code)} 当前模型 {model} 不支持图片输入，"
                "请切换到支持视觉的模型后再使用 AI识图"
            )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            messages.extend(history)

        if image_paths:
            content = [{"type": "text", "text": user_text}]
            for path in image_paths:
                mime_type, _ = mimetypes.guess_type(path)
                mime_type = mime_type or "image/png"
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64}"}
                })
            messages.append({"role": "user", "content": content})
        else:
            use_bridge_search = enable_search and not self._supports_native_chat_search(code)
            final_user_text = (
                self._augment_user_text_with_search(user_text, code)
                if use_bridge_search else
                user_text
            )
            messages.append({"role": "user", "content": final_user_text})
        try:
            if enable_search and not image_paths and self._supports_native_chat_search(code):
                try:
                    from openai import OpenAI
                    client = OpenAI(api_key=api_key, base_url=base_url)
                    return self._call_native_search_chat(client, code, model, messages, api_key, base_url)
                except Exception as native_exc:
                    print(f"[搜搜] {code} 原生联网搜索失败，回退桥接搜索: {native_exc}")
            result = send_platform_chat_messages(
                code,
                api_key,
                model,
                messages,
                deep_think=(code == "local_model"),
            )
            if result is None:
                detail = get_platform_last_error(code)
                raise RuntimeError(detail or f"{PLATFORM_LABELS.get(code, code)} 接口没有返回内容")
            return result
        except Exception as exc:
            raise RuntimeError(
                f"{PLATFORM_LABELS.get(code, code)} 接口请求失败（模型: {model}）: {exc}"
            ) from exc

    def _call_local_model_plain_chat(self, user_text, history=None):
        """
        本地模型普通聊天尽量贴近直接使用 Ollama 的体验：
        - 不注入程序助手 system prompt
        - 不默认做联网桥接
        - 直接命中 Ollama /api/chat，绕过程序里的通用封装
        - 保留 thinking 过程
        """
        code, _cfg, api_key = self._selected_platform_config()
        if code != "local_model":
            raise ValueError("当前不是本地模型平台")

        model = self.model_var.get().strip() or self._get_default_model(code)
        if not model:
            raise ValueError("请填写模型名称")
        base_url = str((PLATFORM_API_CONFIG.get(code) or {}).get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("本地模型未配置接口地址")

        messages = list(history or [])
        messages.append({"role": "user", "content": user_text})

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        reply_parts = []
        thinking_parts = []
        print(
            f"[AI DIALOG DEBUG] direct ollama request model={model} think=True stream=True "
            f"history_messages={len(messages)}"
        )
        try:
            with requests.post(
                f"{base_url}/api/chat",
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "think": True,
                    "stream": True,
                },
                timeout=120,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                seen_thinking = False
                seen_content = False
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except Exception:
                        continue
                    if payload.get("error"):
                        raise RuntimeError(str(payload.get("error") or "").strip() or "Ollama 返回错误")
                    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
                    thinking = str(
                        (message or {}).get("thinking", "")
                        or (message or {}).get("reasoning_content", "")
                        or (message or {}).get("reasoning", "")
                        or payload.get("thinking", "")
                        or payload.get("reasoning_content", "")
                        or payload.get("reasoning", "")
                        or ""
                    )
                    content = str(
                        (message or {}).get("content", "")
                        or payload.get("response", "")
                        or ""
                    )
                    if thinking:
                        if not seen_thinking:
                            seen_thinking = True
                            print(
                                f"[AI DIALOG DEBUG] first thinking chunk model={model} "
                                f"len={len(thinking)} preview={_preview_debug_text(thinking)}"
                            )
                        thinking_parts.append(thinking)
                    if content:
                        if not seen_content:
                            seen_content = True
                            print(
                                f"[AI DIALOG DEBUG] first content chunk model={model} "
                                f"len={len(content)} preview={_preview_debug_text(content)}"
                            )
                        reply_parts.append(content)
                if not seen_thinking:
                    print(f"[AI DIALOG DEBUG] stream finished without thinking chunk, model={model}")
                if not seen_content:
                    print(f"[AI DIALOG DEBUG] stream finished without content chunk, model={model}")
        except Exception as exc:
            detail = str(exc).strip() or get_platform_last_error(code) or "未知错误"
            partial_reply = "".join(reply_parts).strip()
            partial_thinking = "".join(thinking_parts).strip()
            if partial_reply or partial_thinking:
                chunks = []
                if partial_thinking:
                    chunks.append(f"[思考过程]\n{partial_thinking}")
                if partial_reply:
                    chunks.append(f"[回答]\n{partial_reply}")
                chunks.append(f"[错误]\n{detail}")
                return "\n\n".join(chunks).strip()
            raise RuntimeError(
                f"{PLATFORM_LABELS.get(code, code)} 直连 Ollama 失败（模型: {model}）: {detail}"
            ) from exc

        reply_text = "".join(reply_parts).strip()
        thinking_text = "".join(thinking_parts).strip()
        if thinking_text and reply_text:
            return f"[思考过程]\n{thinking_text}\n\n[回答]\n{reply_text}"
        return reply_text or thinking_text or ""

    def _extract_json(self, text):
        text = (text or "").strip()
        if not text:
            raise ValueError("AI 没有返回内容")

        for candidate in (text, self._strip_code_fence(text)):
            try:
                return json.loads(candidate)
            except Exception:
                pass

        start = min([idx for idx in [text.find("{"), text.find("[")] if idx != -1], default=-1)
        if start == -1:
            raise ValueError("AI 返回内容里没有 JSON")

        snippet = text[start:]
        for end in range(len(snippet), 0, -1):
            try:
                return json.loads(snippet[:end])
            except Exception:
                continue
        raise ValueError("AI 返回的 JSON 无法解析")

    def _strip_code_fence(self, text):
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return text

    def _normalize_task_payload(self, payload):
        tasks = payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("AI 没有生成有效任务")

        normalized = []

        for item in tasks:
            keywords = []
            for kw in item.get("keywords", []):
                keyword = str(kw.get("keyword", "")).strip()
                brand = str(kw.get("brand", "")).strip()
                platforms = [
                    code for code in kw.get("platforms", [])
                    if code in TASK_PLATFORM_CODES
                ]
                mode = MODE_ALIASES.get(str(kw.get("mode", "browser")).strip(), "browser")
                if keyword and platforms:
                    keywords.append({
                        "keyword": keyword,
                        "brand": brand,
                        "platforms": platforms,
                        "mode": mode,
                    })

            if not keywords:
                continue

            if not keywords[0].get("brand"):
                raise ValueError("第一个关键词必须包含品牌名")

            task = {
                "name": str(item.get("name", "")).strip() or keywords[0]["brand"],
                "keywords": keywords,
                "weekdays": item.get("weekdays") or [0, 1, 2, 3, 4],
                "enabled": bool(item.get("enabled", True)),
                "inspect": bool(item.get("inspect", False)),
            }
            assign_task_id(task)

            webhook_url = str(item.get("webhook_url", "")).strip()
            if webhook_url:
                task["webhook_url"] = webhook_url

            normalized.append(task)

        if not normalized:
            raise ValueError("AI 生成的任务经过校验后为空")
        return normalized

    def _render_task_preview(self, tasks):
        self.task_preview.config(state=tk.NORMAL)
        self.task_preview.delete("1.0", tk.END)
        self.task_preview.insert(tk.END, yaml.dump({"tasks": tasks}, allow_unicode=True, sort_keys=False))
        self.task_preview.config(state=tk.DISABLED)

    def _looks_like_task_creation_request(self, text):
        text = (text or "").strip().lower()
        if not text:
            return False

        task_keywords = [
            "建任务", "创建任务", "添加任务", "新增任务", "配任务", "生成任务",
            "监控", "关键词", "品牌", "平台", "企业微信", "webhook", "工作日",
            "每天", "几点", "定时", "schedule"
        ]
        return sum(1 for kw in task_keywords if kw in text) >= 2

    def _looks_like_program_query(self, text):
        normalized = (text or "").strip().lower()
        if not normalized:
            return False

        # 长文本更容易碰巧命中“平台/系统/品牌”等弱语义词，导致误切到程序助手模式。
        # 对本地模型聊天，这类输入默认更像通用问答或长文分析，除非用户明确在谈程序本身。
        if len(normalized) >= 180 and not _is_explicit_program_intent(normalized):
            return False

        strong_keywords = [
            "监控", "任务", "关键词", "配置", "调度", "定时", "webhook",
            "平台", "程序", "系统", "截图", "识别", "待办", "报表",
            "运行状态", "运行情况", "命中率", "surfaced",
        ]
        weak_keywords = [
            "品牌", "发送", "失败", "异常", "日报", "周报", "记录",
        ]
        if any(keyword in normalized for keyword in strong_keywords):
            return True
        return sum(1 for keyword in weak_keywords if keyword in normalized) >= 2

    def _should_use_raw_local_model_chat(self, text):
        code = self.platform_var.get().strip()
        if code != "local_model":
            return False
        if self._looks_like_task_creation_request(text):
            print("[AI DIALOG DEBUG] route=program-task reason=task_creation_request")
            return False
        is_program_query = self._looks_like_program_query(text)
        route = "program-assistant" if is_program_query else "raw-local-ollama"
        print(
            f"[AI DIALOG DEBUG] route={route} text_len={len((text or '').strip())} "
            f"explicit_program_intent={_is_explicit_program_intent(text)}"
        )
        return not is_program_query

    def _try_handle_local_utility_query(self, text):
        normalized = (text or "").strip().lower()
        if not normalized:
            return None

        now = datetime.now()
        time_keywords = ("几点", "几时", "现在时间", "当前时间", "time now", "what time")
        date_keywords = ("几号", "几月几号", "星期几", "周几", "今天日期", "today date", "what date")

        if any(keyword in normalized for keyword in time_keywords):
            return f"现在时间是 {now.strftime('%H:%M:%S')}。"

        if any(keyword in normalized for keyword in date_keywords):
            weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            weekday = weekday_names[now.weekday()]
            return f"今天是 {now.strftime('%Y-%m-%d')}，{weekday}。"

        return None

    def _build_task_generation_prompt(self, prompt):
        system_prompt = (
            "你负责把自然语言需求解析成品牌监控任务配置。"
            "只返回 JSON，不要输出解释。"
            "字段结构必须是 {\"tasks\": [...]}。"
            "支持的平台代码只有 doubao, deepseek, ark_deepseek, kimi, yuanbao, tongyi, wenxin。"
            "每个关键词项的 mode 只允许 browser, recognition 两种。"
            "它们分别对应抓取模式、识别模式。"
            "weekdays 用 0-6 表示周一到周日。"
            "任务里不要包含 schedule 或 interval。"
            "每天的自动执行时刻由全局 weekly_times 单独配置。"
            "每个任务至少有一个 keywords 项；第一个 keywords 项必须带 brand。"
        )
        user_text = (
            "请把下面这段需求转换成 JSON 任务配置：\n"
            f"{prompt}"
        )
        return system_prompt, user_text

    def _generate_tasks_from_prompt(self, prompt, from_chat=False):
        system_prompt, user_text = self._build_task_generation_prompt(prompt)
        raw = self._call_ai_chat(system_prompt, user_text)
        payload = self._extract_json(raw)
        tasks = self._normalize_task_payload(payload)
        self.generated_tasks = tasks

        def update_ui():
            self.task_prompt.delete("1.0", tk.END)
            self.task_prompt.insert(tk.END, prompt)
            self._render_task_preview(tasks)
            self.apply_task_btn.config(state=tk.NORMAL)
            self.notebook.select(1)
            self._set_status("任务草案已生成")
            if from_chat:
                summary = f"我已经根据这句话生成了 {len(tasks)} 个任务草案，你确认后就可以写入配置。"
                self._append_chat("assistant", summary)

        self.dialog.after(0, update_ui)

    def _build_program_context(self) -> str:
        """构建当前程序状态上下文，供 AI 理解和管理本地程序。"""
        lines = []
        try:
            if self.get_runtime_state:
                state = self.get_runtime_state()
            else:
                state = {}

            running = state.get("running", None)
            if running is not None:
                lines.append(f"监控状态: {'运行中' if running else '已停止'}")

            status_msg = state.get("status_message", "")
            if status_msg:
                lines.append(f"状态消息: {status_msg}")

            # 调度器信息
            scheduler = state.get("scheduler")
            if scheduler:
                weekly_times = scheduler.get("weekly_times") or {}
                if weekly_times:
                    lines.append("每周自动查询时间:")
                    for weekday in range(7):
                        label = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]
                        lines.append(f"  {label}: {weekly_times.get(str(weekday)) or '不自动运行'}")

            # 任务列表
            tasks = self.config.get("tasks", [])
            if tasks:
                enabled = [t for t in tasks if t.get("enabled", True)]
                lines.append(f"\n任务列表 (共 {len(tasks)} 个，{len(enabled)} 个启用):")
                for t in tasks:
                    enabled_mark = "✓" if t.get("enabled", True) else "✗"
                    kws = t.get("keywords", [])
                    brands = ", ".join(kw.get("brand") or kw.get("keyword", "") for kw in kws if kw.get("brand") or kw.get("keyword"))
                    platforms_used = set()
                    for kw in kws:
                        platforms_used.update(kw.get("platforms", []))
                    lines.append(f"  [{enabled_mark}] {t.get('name', '未命名')} | 关键词/品牌: {brands} | 平台: {', '.join(platforms_used)}")

            # 平台状态
            platforms_cfg = self.config.get("platforms", {})
            if platforms_cfg:
                enabled_platforms = [k for k, v in platforms_cfg.items() if v.get("enabled")]
                disabled_platforms = [k for k, v in platforms_cfg.items() if not v.get("enabled")]
                lines.append(f"\n已启用平台: {', '.join(enabled_platforms) or '无'}")
                if disabled_platforms:
                    lines.append(f"已禁用平台: {', '.join(disabled_platforms)}")

            # 最近结果
            last_results = state.get("last_results", {})
            if last_results:
                lines.append(f"\n最近检测结果 (最多5条):")
                for i, ((task_name, platform, brand), rank) in enumerate(list(last_results.items())[:5]):
                    lines.append(f"  {task_name} | {platform} | {brand}: 排名 {rank}")

        except Exception as e:
            lines.append(f"(获取程序状态时出错: {e})")

        if not lines:
            return ""
        return "当前程序状态:\n" + "\n".join(lines)

    def _extract_action_from_reply(self, reply: str):
        """
        从 AI 回复中提取操作标记。
        格式: [ACTION:tool_name] 或 [ACTION:tool_name:{"key":"val"}]
        返回 (tool_name, params_dict, cleaned_reply)。
        """
        import re
        match = re.search(r'\[ACTION:(\w+)(?::({.+?}))?\]', reply, re.DOTALL)
        if match:
            tool_name = match.group(1)
            params = {}
            if match.group(2):
                try:
                    params = json.loads(match.group(2))
                except Exception:
                    pass
            cleaned = re.sub(r'\s*\[ACTION:\w+(?::{.+?})?\]\s*', ' ', reply, flags=re.DOTALL).strip()
            return tool_name, params, cleaned
        return None, {}, reply

    def _send_chat(self):
        user_text = self.chat_input.get("1.0", tk.END).strip()
        if not user_text:
            return

        self.chat_input.delete("1.0", tk.END)
        self._append_chat("user", user_text)
        self._set_status("对话中...")

        def worker():
            local_reply = self._try_handle_local_utility_query(user_text)
            if local_reply:
                self.chat_history.append({"role": "user", "content": user_text})
                self.chat_history.append({"role": "assistant", "content": local_reply})
                self.dialog.after(0, lambda: (
                    self._append_chat("assistant", local_reply),
                    self._set_status("就绪")
                ))
                return

            if self._looks_like_task_creation_request(user_text):
                self._generate_tasks_from_prompt(user_text, from_chat=True)
                self.chat_history.append({"role": "user", "content": user_text})
                self.chat_history.append({
                    "role": "assistant",
                    "content": "我已经自动识别为建任务请求，并生成了任务草案，请确认后写入配置。"
                })
                return

            use_raw_local_chat = self._should_use_raw_local_model_chat(user_text)
            if use_raw_local_chat:
                reply = self._call_local_model_plain_chat(user_text, history=[])
            else:
                program_context = self._build_program_context()
                action_instructions = ""
                if self.on_program_action:
                    action_instructions = (
                        "\n\n## 可用工具\n"
                        "当用户要求你执行操作时，在回复末尾加上标记（不要有多余说明）：\n"
                        "  [ACTION:工具名] 或 [ACTION:工具名:{\"参数\":\"值\"}]\n"
                        "\n### 监控控制\n"
                        "  [ACTION:start_monitoring] — 开始运行监控\n"
                        "  [ACTION:stop_monitoring] — 停止监控\n"
                        "\n### 任务管理（需要任务名 task_name）\n"
                        '  [ACTION:enable_task:{"task_name":"任务名"}] — 启用任务\n'
                        '  [ACTION:disable_task:{"task_name":"任务名"}] — 禁用任务\n'
                        '  [ACTION:delete_task:{"task_name":"任务名"}] — 删除任务（不可恢复，执行前告知用户）\n'
                        '  [ACTION:update_task_keyword:{"task_name":"任务名","keyword_index":0,"fields":{"keyword":"新词"}}] — 修改关键词字段（keyword/brand/platforms/mode）\n'
                        "\n### 调度器设置\n"
                        '  [ACTION:set_scheduler:{"weekly_times":{"0":"09:30","1":"09:30","2":"14:00","3":"09:30","4":"09:30","5":null,"6":null}}] — 设置每周自动查询时间\n'
                        "\n### 规则：\n"
                        "  - 只在用户明确要求执行操作时才输出标记；查询状态只回答不执行\n"
                        "  - 删除操作前必须在回复文字中告知用户将要删除的任务名，让用户在确认弹窗中确认\n"
                        "  - 一次只输出一个 [ACTION:...] 标记\n"
                        "  - 如需要的参数用户未提供（如任务名不明确），先询问用户再执行\n"
                    )
                system_prompt = (
                    "你是这个 Surfaced 桌面程序的智能助手，同时也能回答通用问题。\n"
                    "当用户询问程序状态、任务、平台、配置、调度等信息时，结合下面的程序状态来回答。\n"
                    "当用户问题与程序无关时，作为通用中文助手正常回答。\n"
                    "回答要简洁、清楚、实用。\n"
                ) + action_instructions
                if program_context:
                    system_prompt += f"\n{program_context}"
                enable_search = bool(self.chat_search_var.get())
                reply = self._call_ai_chat(
                    system_prompt,
                    user_text,
                    history=self.chat_history,
                    enable_search=enable_search,
                )
            # 提取并执行操作标记
            tool_name, tool_params, reply = self._extract_action_from_reply(reply)
            if tool_name and self.on_program_action:
                action_result = self.on_program_action(tool_name, tool_params)
                if action_result:
                    reply = f"{reply}\n{action_result}".strip()
            self.chat_history.append({"role": "user", "content": user_text})
            self.chat_history.append({"role": "assistant", "content": reply})
            self.dialog.after(0, lambda: (
                self._append_chat("assistant", reply),
                self._set_status("就绪")
            ))

        self._run_async(worker, "自由对话失败")

    def _generate_tasks(self):
        prompt = self.task_prompt.get("1.0", tk.END).strip()
        if not prompt:
            messagebox.showwarning("提示", "请先输入你的任务需求", parent=self.dialog)
            return

        self._set_status("生成任务草案中...")

        def worker():
            self._generate_tasks_from_prompt(prompt)

        self._run_async(worker, "生成任务草案失败")

    def _apply_generated_tasks(self):
        if not self.generated_tasks:
            return
        if not messagebox.askyesno("确认", f"确认把 {len(self.generated_tasks)} 个任务组写入配置吗？", parent=self.dialog):
            return

        self.config.setdefault("tasks", [])
        self.config["tasks"].extend(self.generated_tasks)
        try:
            self._save_config()
            self._set_status("任务已写入配置")
            messagebox.showinfo("成功", f"已写入 {len(self.generated_tasks)} 个任务组", parent=self.dialog)
        except Exception as e:
            messagebox.showerror("错误", f"写入配置失败: {e}", parent=self.dialog)

    def _select_images(self):
        paths = filedialog.askopenfilenames(
            parent=self.dialog,
            title="选择截图",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp")]
        )
        if not paths:
            return
        self.selected_images = list(paths)
        self.image_info.set(f"已选择 {len(self.selected_images)} 张截图")

    def _clear_images(self):
        self.selected_images = []
        self.analysis_result = None
        self.matched_task_payloads = []
        self.match_vars = {}
        self.image_info.set("未选择截图")
        self.image_result.delete("1.0", tk.END)
        for child in self.match_container.winfo_children():
            child.destroy()
        self.send_match_btn.config(state=tk.DISABLED)

    def _analyze_images(self):
        if not self.selected_images:
            messagebox.showwarning("提示", "请先选择截图", parent=self.dialog)
            return

        self._set_status("识别截图中...")

        def worker():
            system_prompt = (
                "你负责阅读多张截图，识别其中出现的品牌名、平台名和简短结论。"
                "只返回 JSON，不要输出解释。"
                "格式必须是 {\"images\": [{\"path\": \"文件名\", \"brands\": [\"品牌1\"], "
                "\"platform\": \"平台代码或unknown\", \"summary\": \"一句话总结\"}]}。"
            )
            desc = "请分析这些截图里出现的品牌和平台，并按要求返回 JSON。"
            raw = self._call_ai_chat(system_prompt, desc, image_paths=self.selected_images)
            payload = self._extract_json(raw)
            matches = self._match_tasks_from_analysis(payload)
            self.analysis_result = payload
            self.matched_task_payloads = matches
            self.dialog.after(0, lambda: self._render_analysis_result(payload, matches))

        self._run_async(worker, "截图识别失败")

    def _render_analysis_result(self, payload, matches):
        self.image_result.delete("1.0", tk.END)
        self.image_result.insert(tk.END, yaml.dump(payload, allow_unicode=True, sort_keys=False))

        for child in self.match_container.winfo_children():
            child.destroy()
        self.match_vars = {}

        if not matches:
            ttk.Label(self.match_container, text="没有匹配到可发送的任务组", foreground="gray").pack(anchor=tk.W)
            self.send_match_btn.config(state=tk.DISABLED)
            self._set_status("截图识别完成，但未匹配到任务")
            return

        for idx, match in enumerate(matches):
            var = tk.BooleanVar(value=True)
            self.match_vars[idx] = var
            brands = ", ".join(match["brands"])
            images = ", ".join(Path(p).name for p in match["image_paths"])
            text = f"{match['task_name']} -> 品牌: {brands} -> 截图: {images}"
            ttk.Checkbutton(self.match_container, text=text, variable=var).pack(anchor=tk.W, pady=2)

        self.send_match_btn.config(state=tk.NORMAL)
        self._set_status("截图识别完成，请确认发送对象")

    def _match_tasks_from_analysis(self, payload):
        image_items = payload.get("images", [])
        if not isinstance(image_items, list):
            raise ValueError("AI 返回的图片结果格式不正确")

        tasks = self.config.get("tasks", [])
        matches = []

        def normalize_brand(s):
            return (s or "").strip().lower().replace(" ", "")

        for task in tasks:
            keywords = task.get("keywords", [])
            if not keywords and task.get("keyword"):
                keywords = [{
                    "keyword": task.get("keyword", ""),
                    "brand": task.get("brand", ""),
                    "platforms": [task.get("platform", "")]
                }]

            task_brands = []
            if keywords:
                default_brand = keywords[0].get("brand", "")
                for kw in keywords:
                    brand = kw.get("brand", "") or default_brand
                    if brand:
                        task_brands.append(brand)

            if not task_brands:
                continue

            matched_images = []
            matched_brands = []
            detected_platforms = []

            for item in image_items:
                brands = item.get("brands", []) or []
                for detected_brand in brands:
                    for task_brand in task_brands:
                        if (
                            normalize_brand(detected_brand) == normalize_brand(task_brand)
                            or BasePlatform.contains_brand_mention(detected_brand, task_brand)
                            or BasePlatform.contains_brand_mention(task_brand, detected_brand)
                        ):
                            matched_images.append(self._resolve_selected_image_path(item.get("path", "")))
                            matched_brands.append(task_brand)
                            platform = item.get("platform", "")
                            if platform and platform != "unknown":
                                detected_platforms.append(platform)
                            break

            matched_images = [p for p in matched_images if p]
            if not matched_images:
                continue

            webhook_url = task.get("webhook_url", "").strip()
            if not webhook_url or "YOUR_KEY_HERE" in webhook_url:
                continue

            matches.append({
                "task_name": task.get("name", task_brands[0]),
                "brands": list(dict.fromkeys(matched_brands)),
                "image_paths": list(dict.fromkeys(matched_images)),
                "platforms": list(dict.fromkeys(detected_platforms)),
                "webhook_url": webhook_url,
            })

        return matches

    def _resolve_selected_image_path(self, image_name):
        for path in self.selected_images:
            if Path(path).name == image_name:
                return path
        return None

    def _send_selected_matches(self):
        selected = [
            match for idx, match in enumerate(self.matched_task_payloads)
            if self.match_vars.get(idx) and self.match_vars[idx].get()
        ]
        if not selected:
            messagebox.showwarning("提示", "请至少勾选一个任务组", parent=self.dialog)
            return

        if not messagebox.askyesno("确认", f"确认发送到 {len(selected)} 个任务组对应的企业微信吗？", parent=self.dialog):
            return

        cooldown = self.config.get("default_notification", {}).get("cooldown_minutes", 30)
        interval = self.config.get("default_notification", {}).get("send_interval", 2)
        self._set_status("发送企业微信中...")

        def worker():
            failures = []
            for match in selected:
                notifier = WeComNotifier(
                    webhook_url=match["webhook_url"],
                    cooldown_minutes=cooldown,
                    send_interval=interval,
                )
                ok = notifier.send_detected_images(
                    task_name=match["task_name"],
                    brands=match["brands"],
                    screenshot_paths=match["image_paths"],
                    detected_platforms=match["platforms"],
                )
                if not ok:
                    failures.append(match["task_name"])

            def done():
                if failures:
                    self._set_status("部分发送失败")
                    messagebox.showwarning(
                        "完成",
                        f"以下任务组发送失败: {', '.join(failures)}",
                        parent=self.dialog
                    )
                else:
                    self._set_status("发送完成")
                    messagebox.showinfo("成功", "已发送到对应企业微信", parent=self.dialog)

            self.dialog.after(0, done)

        self._run_async(worker, "发送企业微信失败")
