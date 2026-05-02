"""Selector intent registry for conservative selector diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


SuccessCheck = Literal["presence_only", "click_then_input_empty", "click_then_state_active"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class FieldIntent:
    platform: str
    field_name: str
    intent_zh: str
    intent_en: str
    text_synonyms: tuple[str, ...] = ()
    aria_keywords: tuple[str, ...] = ()
    testid_keywords: tuple[str, ...] = ()
    class_keywords: tuple[str, ...] = ()
    role_hints: tuple[str, ...] = ()
    tag_hints: tuple[str, ...] = ()
    icon_hints: tuple[str, ...] = ()
    success_check: SuccessCheck = "presence_only"
    success_check_params: dict = field(default_factory=dict)
    risk_level: RiskLevel = "high"
    auto_write_enabled: bool = False


FIELD_REGISTRY: dict[str, dict[str, FieldIntent]] = {
    "doubao": {
        "new_chat_selector": FieldIntent(
            platform="doubao",
            field_name="new_chat_selector",
            intent_zh="新建对话按钮",
            intent_en="new chat button",
            text_synonyms=("新对话", "新建对话", "new chat", "new conversation", "chat"),
            aria_keywords=("新对话", "新建对话", "new chat", "new conversation"),
            testid_keywords=("create_conversation", "new_chat", "new-chat", "conversation"),
            class_keywords=("new-chat", "newChat", "conversation"),
            role_hints=("button", "link", "div"),
            tag_hints=("button", "a", "div"),
            icon_hints=("M10.6254 20.3752V6.69549", "M8 0.599609"),
            success_check="click_then_state_active",
            success_check_params={"input_selector": 'textarea.semi-input-textarea.semi-input-textarea-autosize[placeholder="发消息..."]'},
            risk_level="low",
            auto_write_enabled=False,
        ),
    },
    "deepseek": {
        "new_chat_selector": FieldIntent(
            platform="deepseek",
            field_name="new_chat_selector",
            intent_zh="新建对话按钮",
            intent_en="new chat button",
            text_synonyms=("新建", "新对话", "new chat", "newchat", "new conversation", "chat"),
            aria_keywords=("新建", "新对话", "new chat", "new conversation"),
            testid_keywords=("new", "new_chat", "new-chat", "newConversation"),
            class_keywords=("new-chat", "newChat", "newConversation"),
            role_hints=("button",),
            tag_hints=("button", "a", "div"),
            icon_hints=('path[d^="M8 0"]', 'path[d^="M8 0.599609"]'),
            success_check="click_then_state_active",
            success_check_params={"input_selector": "textarea"},
            risk_level="low",
            auto_write_enabled=False,
        ),
    },
    "kimi": {
        "new_chat_selector": FieldIntent(
            platform="kimi",
            field_name="new_chat_selector",
            intent_zh="新建对话按钮",
            intent_en="new chat button",
            text_synonyms=("新建对话", "新对话", "new chat", "new conversation", "AddConversation"),
            aria_keywords=("新建对话", "new chat", "new conversation"),
            testid_keywords=("addconversation", "new_chat", "new-conversation"),
            class_keywords=("AddConversation", "new-chat", "conversation"),
            role_hints=("button", "link"),
            tag_hints=("button", "a", "div", "svg"),
            icon_hints=("AddConversation",),
            success_check="click_then_state_active",
            success_check_params={
                "input_selector": '.chat-input-editor[contenteditable="true"], [data-lexical-editor="true"][contenteditable="true"]'
            },
            risk_level="low",
            auto_write_enabled=False,
        ),
    },
    "tongyi": {
        "new_chat_selector": FieldIntent(
            platform="tongyi",
            field_name="new_chat_selector",
            intent_zh="新建对话按钮",
            intent_en="new chat button",
            text_synonyms=("新建对话", "新对话", "new chat", "new conversation"),
            aria_keywords=("新建对话", "new chat", "new conversation"),
            testid_keywords=("new_chat", "new-chat", "newConversation"),
            class_keywords=("newChat", "new-chat", "conversation"),
            role_hints=("button",),
            tag_hints=("button", "a", "div"),
            icon_hints=("newChat", "new-chat"),
            success_check="click_then_state_active",
            success_check_params={"input_selector": 'div[contenteditable="true"], textarea'},
            risk_level="low",
            auto_write_enabled=False,
        ),
    },
    "wenxin": {
        "new_chat_selector": FieldIntent(
            platform="wenxin",
            field_name="new_chat_selector",
            intent_zh="新建会话按钮",
            intent_en="new session button",
            text_synonyms=("新会话", "新建会话", "new session", "new conversation"),
            aria_keywords=("新会话", "new session", "new conversation"),
            testid_keywords=("new_session", "new-session", "session"),
            class_keywords=("session", "new-session"),
            role_hints=("button", "link"),
            tag_hints=("button", "img", "div"),
            icon_hints=("New Session Btn",),
            success_check="click_then_state_active",
            success_check_params={"input_selector": 'div[role="textbox"][contenteditable="true"][data-slate-editor="true"]'},
            risk_level="low",
            auto_write_enabled=False,
        ),
    },
    "yuanbao": {
        "new_chat_selector": FieldIntent(
            platform="yuanbao",
            field_name="new_chat_selector",
            intent_zh="新建对话按钮",
            intent_en="new chat button",
            text_synonyms=("新对话", "新建对话", "new chat", "new conversation"),
            aria_keywords=("新对话", "new chat", "new conversation"),
            testid_keywords=("new_chat", "new-chat", "newconversation"),
            class_keywords=("newchat", "new-chat", "icon-yb-ic_newchat_20"),
            role_hints=("button", "div", "span"),
            tag_hints=("button", "div", "span"),
            icon_hints=("icon-yb-ic_newchat_20",),
            success_check="click_then_state_active",
            success_check_params={"input_selector": 'textarea, div[contenteditable="true"]'},
            risk_level="low",
            auto_write_enabled=False,
        ),
    },
}


def get_field_intent(platform: str, field_name: str) -> FieldIntent | None:
    platform_key = str(platform or "").strip().lower()
    field_key = str(field_name or "").strip()
    if not platform_key or not field_key:
        return None
    return FIELD_REGISTRY.get(platform_key, {}).get(field_key)
