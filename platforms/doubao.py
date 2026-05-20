"""
豆包平台适配器
"""

import re
import time
import random

from core.app_paths import resolve_app_dir
from core.time_utils import local_now
from .base import BasePlatform, InterruptionDetected


class DoubaoPlatform(BasePlatform):
    screenshot_brand_top_padding_px = 56
    screenshot_brand_top_ratio = 0.18
    use_automation_control_flag = True
    use_automation_user_agent = False
    use_automation_extra_headers = False
    use_automation_ignore_default_args = False
    use_automation_stealth_scripts = False
    use_automation_storage_warmup = False
    use_external_chrome_cdp = True
    """豆包 AI 平台监控"""

    target_url = "https://www.doubao.com/chat/"
    input_selector = 'textarea.semi-input-textarea.semi-input-textarea-autosize[placeholder="发消息..."]'
    new_chat_selector = 'xpath=//div[contains(@class,"sidebar_nav_item") and contains(normalize-space(.),"新对话")]'
    chat_container_selector = '[class*="scrollable"]'
    composer_status_selector = 'button#flow-end-msg-send, div[data-trigger-type="hover"][data-state]'
    result_selector = (
        '[data-testid*="message-content"], '
        '[data-testid*="message_content"], '
        '[data-testid*="messageText"], '
        '[data-testid*="message_text"], '
        '[data-testid*="answer"], '
        '[data-testid*="markdown"], '
        '[class*="markdown"], '
        '[class*="answer"], '
        '[class*="message-content"]'
    )
    think_content_selector = (
        '[data-testid*="deep-think"], '
        '[data-testid*="deep_think"], '
        '[data-testid*="thinking"], '
        '[data-testid*="search-result"], '
        '[class*="deep-think"], '
        '[class*="deep_think"], '
        '[class*="thinking"], '
        '[class*="cot"], '
        '[class*="searchResult"]'
    )
    prefer_last_result_block = True
    deep_think_menu_trigger_selector = 'button[aria-haspopup="menu"][data-slot="dropdown-menu-trigger"]:visible'
    deep_think_think_selector = 'xpath=//*[@role="menuitem" and contains(normalize-space(.), "思考")]'
    deep_think_quick_selector = 'xpath=//*[@role="menuitem" and contains(normalize-space(.), "快速")]'
    send_button_selector = '#flow-end-msg-send'
    stop_generating_selector = 'div[data-trigger-type="hover"][data-state]:has(svg path[d^="M12 0.5C18.3513 0.5"])'
    voice_button_selector = (
        'xpath=//div[@data-trigger-type="hover" and contains(@class,"cursor-pointer") '
        'and .//svg/path[contains(@d,"M19.8628 9.29346")]]'
    )
    status_button_selector = 'button#flow-end-msg-send, div[data-trigger-type="hover"][data-state]'
    _status_voice_path_prefix = "M19.8628 9.29346"
    _status_send_path_prefix = "M4.93934 10.2598"
    _status_pause_path_prefix = "M12 0.5C18.3513 0.5"

    # 豆包页面上带 verify class 的正常布局元素，排除误报。
    # #captcha_container 不再加入白名单，否则真实拦截层会绕过 base._detect_overlay。
    _overlay_whitelist_selectors = [
        '[class*="verify"][class*="input"]',
        '[class*="verify"][class*="btn"]',
        '[class*="verify"][class*="icon"]',
    ]
    _overlay_extra_selectors = [
        '[class*="semi-modal"],[class*="SemiModal"]',
        '[class*="semi-dialog"],[class*="SemiDialog"]',
        '[class*="semi-modal-mask"],[class*="SemiModalMask"]',
        '[class*="semi-portal"],[class*="SemiPortal"]',
    ]
    def _disable_captcha_pointer_intercept(self) -> None:
        """豆包有时会残留空的 captcha_container，但仍拦截点击。"""
        try:
            self.page.evaluate("""() => {
                const el = document.getElementById('captcha_container');
                if (!el) return;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const hasVisibleText = (el.innerText || '').trim().length > 0;
                const hasMedia = !!el.querySelector('img, canvas, iframe, svg');
                const looksLikeRealCaptcha = (
                    rect.width > 800 &&
                    rect.height > 400 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0' &&
                    (hasVisibleText || hasMedia)
                );
                if (!looksLikeRealCaptcha) {
                    el.style.pointerEvents = 'none';
                }
            }""")
        except Exception:
            pass

    def _click_selector(
        self,
        selector: str,
        label: str,
        *,
        timeout_ms: int = 5000,
        force: bool = False,
    ) -> bool:
        if not selector:
            return False
        try:
            self._raise_if_stop_requested()
            self._disable_captcha_pointer_intercept()
            locator = self.page.locator(selector).first
            self._wait_for_locator(locator, timeout_ms=timeout_ms)
            self._click_locator(locator, timeout_ms=timeout_ms, force=force)
            print(f"[{self.name}] 已点击{label}: {selector}")
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 点击{label}失败: {e}")
            return False

    def _is_selector_visible(self, selector: str) -> bool:
        if not selector:
            return False
        try:
            locator = self.page.locator(selector).first
            return bool(locator.is_visible(timeout=500))
        except Exception:
            return False

    def _get_composer_locator(self, *, timeout_ms: int = 3000):
        self._raise_if_stop_requested()
        chat_input = self.page.locator(self.input_selector).first
        self._wait_for_locator(chat_input, timeout_ms=timeout_ms)
        composer = chat_input.locator(
            "xpath=ancestor::div[.//*[@id='flow-end-msg-send' or (@data-trigger-type='hover' and @data-state)]][1]"
        )
        self._wait_for_locator(composer, timeout_ms=timeout_ms)
        return composer

    def _get_mode_trigger_locator(self, *, timeout_ms: int = 2000):
        try:
            composer = self._get_composer_locator(timeout_ms=timeout_ms)
            triggers = composer.locator('button[aria-haspopup="menu"][data-slot="dropdown-menu-trigger"]')
            count = triggers.count()
            for idx in range(count):
                self._raise_if_stop_requested()
                candidate = triggers.nth(idx)
                try:
                    if not candidate.is_visible(timeout=300):
                        continue
                except Exception:
                    continue
                try:
                    text = " ".join((candidate.text_content(timeout=300) or "").split())
                except Exception:
                    text = ""
                if any(label in text for label in ("快速", "思考", "专家", "自动思考", "深度思考", "深思", "R1")):
                    return candidate
        except Exception as e:
            self._reraise_stop_requested(e)
        return None

    def _wait_for_submit_started(self, before_input: str, timeout: float = 8.0) -> bool:
        keyword = str(getattr(self, "_last_prompt_text", "") or "")
        keyword_compact = self._normalize_compact_text(keyword)
        before_input_compact = self._normalize_compact_text(before_input)
        before_snapshot = self._composer_snapshot(keyword)
        baseline_result_count = int(before_snapshot.get("resultCount", 0) or 0)
        baseline_result_length = int(before_snapshot.get("resultLength", 0) or 0)
        baseline_container_length = int(before_snapshot.get("containerLength", 0) or 0)
        baseline_keyword_visible = bool(before_snapshot.get("keywordVisible"))
        deadline = time.monotonic() + max(2.0, timeout)
        while time.monotonic() < deadline:
            self._raise_if_stop_requested()
            self._disable_captcha_pointer_intercept()
            self.check_for_interruption(check_input_visible=False)
            snapshot = self._composer_snapshot(keyword)
            state = self._get_status_button_state()
            input_compact = self._normalize_compact_text(snapshot.get("inputText") or "")
            input_cleared = not input_compact
            if keyword_compact and input_compact:
                input_cleared = keyword_compact not in input_compact

            result_count = int(snapshot.get("resultCount", 0) or 0)
            result_length = int(snapshot.get("resultLength", 0) or 0)
            container_length = int(snapshot.get("containerLength", 0) or 0)
            keyword_visible = bool(snapshot.get("keywordVisible"))
            stop_visible = bool(snapshot.get("stopVisible"))
            pause_state = state == "pause" or stop_visible
            result_grew = (
                result_count > baseline_result_count
                or result_length > baseline_result_length + max(12, min(80, len(keyword_compact) + 8))
            )
            container_grew = container_length > baseline_container_length + max(12, min(120, len(keyword) + 8))

            # 豆包无头模式下偶尔会短暂出现“暂停态”假阳性，但会话里并没有真正落下问题。
            # 这里要求输入框已清空，且会话区出现了问题气泡/新内容，才判定发送成功。
            if input_cleared and (
                (keyword_visible and not baseline_keyword_visible)
                or result_grew
                or (pause_state and keyword_visible)
                or (pause_state and container_grew)
            ):
                return True
            self._cooperative_sleep_jittered(0.2, spread=0.32)
        debug_snapshot = self._composer_snapshot(keyword)
        print(
            f"[{self.name}] 提交确认未通过: "
            f"before_input_len={len(before_input_compact)}, "
            f"after_input_len={len(self._normalize_compact_text(debug_snapshot.get('inputText') or ''))}, "
            f"keyword_visible={'是' if debug_snapshot.get('keywordVisible') else '否'}, "
            f"result_count={int(debug_snapshot.get('resultCount', 0) or 0)}, "
            f"result_length={int(debug_snapshot.get('resultLength', 0) or 0)}, "
            f"container_length={int(debug_snapshot.get('containerLength', 0) or 0)}, "
            f"stop_visible={'是' if debug_snapshot.get('stopVisible') else '否'}, "
            f"href={debug_snapshot.get('href') or ''}"
        )
        return False

    def _get_status_button_state(self) -> str:
        self._raise_if_stop_requested()
        try:
            snapshot = self._get_status_button_snapshot()
            state = self._classify_status_button_state(
                snapshot.get("paths"),
                text=snapshot.get("text"),
                data_state=snapshot.get("dataState"),
                outer_html=snapshot.get("outerHTML"),
            )
            if state != "unknown":
                return state
            guessed_state = str(snapshot.get("stateGuess") or "")
            if guessed_state in {"pause", "send", "voice"}:
                return guessed_state
            if self._is_selector_visible(self.stop_generating_selector):
                return "pause"
            if self._is_selector_visible(self.send_button_selector):
                return "send"
            if self._is_selector_visible(self.voice_button_selector):
                return "voice"
        except Exception as e:
            self._reraise_stop_requested(e)
        return "unknown"

    @staticmethod
    def _selector_looks_xpath(selector: str) -> bool:
        text = str(selector or "").strip()
        return bool(text) and (
            text.startswith("xpath=")
            or text.startswith("//")
            or text.startswith("(//")
        )

    def _classify_status_button_state(
        self,
        paths: list[str] | None,
        *,
        text: str = "",
        data_state: str = "",
        outer_html: str = "",
    ) -> str:
        for path in paths or []:
            if not path:
                continue
            if self._status_pause_path_prefix in path:
                return "pause"
            if self._status_send_path_prefix in path:
                return "send"
            if self._status_voice_path_prefix in path:
                return "voice"
        fallback_text = " ".join(
            part for part in [
                str(text or "").strip(),
                str(data_state or "").strip(),
                str(outer_html or "").strip(),
            ]
            if part
        )
        if fallback_text:
            lowered = fallback_text.lower()
            if (
                "停止生成" in fallback_text
                or "停止回答" in fallback_text
                or "break-btn" in lowered
            ):
                return "pause"
            if (
                "发送消息" in fallback_text
                or "发送" in fallback_text
                or "flow-end-msg-send" in lowered
                or "send-msg-btn" in lowered
            ):
                return "send"
            if "语音输入" in fallback_text or "voice" in lowered:
                return "voice"
        return "unknown"

    def _resolve_status_button_locator(self, *, required_state: str = "", timeout_ms: int = 1500):
        self._raise_if_stop_requested()
        ordered = [
            ("pause", self.stop_generating_selector),
            ("send", self.send_button_selector),
            ("voice", self.voice_button_selector),
        ]
        if required_state:
            ordered = [item for item in ordered if item[0] == required_state]
        wait_ms = max(300, int(timeout_ms or 300))
        for state, selector in ordered:
            if not selector:
                continue
            locator = self.page.locator(selector).first
            try:
                self._wait_for_locator(locator, timeout_ms=wait_ms)
                return state, locator
            except Exception:
                continue
        if required_state:
            return "", None
        try:
            composer = self._get_composer_locator(timeout_ms=min(wait_ms, 1200))
            locator = composer.locator(self.composer_status_selector).first
            self._wait_for_locator(locator, timeout_ms=min(wait_ms, 1200))
            return "", locator
        except Exception:
            return "", None

    def _get_status_button_snapshot(self) -> dict:
        try:
            state_guess, button = self._resolve_status_button_locator(timeout_ms=1500)
            if button is None:
                return {"found": False}
            return {
                "found": True,
                "stateGuess": state_guess,
                "text": (button.text_content(timeout=300) or "").strip(),
                "className": button.get_attribute("class") or "",
                "outerHTML": button.evaluate("(el) => String(el.outerHTML || '')"),
                "dataState": button.get_attribute("data-state") or "",
                "paths": button.evaluate(
                    """(el) => {
                        const isVisible = (node) => {
                            if (!node) return false;
                            const style = window.getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            return (
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                style.opacity !== '0' &&
                                rect.width > 0 &&
                                rect.height > 0
                            );
                        };
                        if (!isVisible(el)) return [];
                        return Array.from(el.querySelectorAll('svg path'))
                            .filter((node) => isVisible(node.closest('svg') || node))
                            .map((node) => String(node.getAttribute('d') || ''))
                            .filter(Boolean);
                    }"""
                ) or [],
                "disabled": False,
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"found": False}

    def _get_generation_debug_state(self) -> dict:
        try:
            self._raise_if_stop_requested()
            status_snapshot = self._get_status_button_snapshot()
            status_state = self._classify_status_button_state(status_snapshot.get("paths"))
            if status_state == "unknown":
                status_state = str(status_snapshot.get("stateGuess") or "")
            snapshot = self.page.evaluate(
                """({resultSel, thinkSel, inputSel, stopSel, sendSel, voiceSel}) => {
                    const normalize = (value) => String(value || '')
                        .replace(/\\u00a0/g, ' ')
                        .replace(/[ \\t]+\\n/g, '\\n')
                        .replace(/\\n{3,}/g, '\\n\\n')
                        .replace(/[ \\t]{2,}/g, ' ')
                        .trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const countVisible = (selector) => {
                        if (!selector) return 0;
                        try {
                            return Array.from(document.querySelectorAll(selector)).filter((el) => isVisible(el)).length;
                        } catch (_) {
                            return 0;
                        }
                    };
                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputText = normalize(
                        input ? (input.value || input.innerText || input.textContent || '') : ''
                    );
                    const resultNodes = resultSel
                        ? Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el) && !(thinkSel && el.closest(thinkSel)))
                        : [];
                    const resultTexts = resultNodes
                        .map((el) => normalize(el.innerText || el.textContent || ''))
                        .filter(Boolean);
                    return {
                        stopVisibleCount: countVisible(stopSel),
                        sendVisibleCount: countVisible(sendSel),
                        voiceVisibleCount: countVisible(voiceSel),
                        inputLength: inputText.length,
                        inputPreview: inputText.slice(0, 80),
                        resultCount: resultTexts.length,
                        resultLength: resultTexts.join('\\n').length,
                        resultPreview: (resultTexts[resultTexts.length - 1] || '').slice(0, 120),
                    };
                }""",
                {
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "inputSel": self.input_selector or "",
                    "stopSel": self.stop_generating_selector or "",
                    "sendSel": self.send_button_selector or "",
                    "voiceSel": self.voice_button_selector or "",
                },
            ) or {}
            paths = list(status_snapshot.get("paths") or [])
            return {
                "status_state": status_state,
                "status_found": bool(status_snapshot.get("found")),
                "status_data_state": str(status_snapshot.get("dataState") or ""),
                "status_path_count": len(paths),
                "stop_visible_count": int(snapshot.get("stopVisibleCount", 0) or 0),
                "send_visible_count": int(snapshot.get("sendVisibleCount", 0) or 0),
                "voice_visible_count": int(snapshot.get("voiceVisibleCount", 0) or 0),
                "input_length": int(snapshot.get("inputLength", 0) or 0),
                "result_count": int(snapshot.get("resultCount", 0) or 0),
                "result_length": int(snapshot.get("resultLength", 0) or 0),
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def _allow_text_stable_completion(self, debug_state: dict | None = None) -> bool:
        state = dict(debug_state or {})
        status_state = str(state.get("status_state") or "")
        stop_visible_count = int(state.get("stop_visible_count", 0) or 0)
        return status_state != "pause" and stop_visible_count <= 0

    def _click_status_button(self, *, required_state: str = "") -> bool:
        try:
            snapshot = self._get_status_button_snapshot()
            state = self._classify_status_button_state(snapshot.get("paths"))
            if state == "unknown":
                state = str(snapshot.get("stateGuess") or "")
            if required_state and state != required_state:
                return False
            _, button = self._resolve_status_button_locator(required_state=required_state, timeout_ms=1500)
            if button is None:
                return False
            self._click_locator(button, timeout_ms=1500)
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _click_mode_menu_item_with_text(self, labels: list[str]) -> bool:
        if not labels:
            return False
        try:
            if not self._wait_mode_menu_open(timeout_ms=2500):
                print(f"[{self.name}] 模式菜单未展开")
                return False
            items = self.page.locator('[role="menuitem"], [role="menuitemradio"], [role="radio"], button')
            count = items.count()
            for idx in range(count):
                self._raise_if_stop_requested()
                item = items.nth(idx)
                try:
                    if not item.is_visible(timeout=300):
                        continue
                    in_menu = item.evaluate(
                        """(el) => Boolean(el.closest('[role="menu"], [role="dialog"], [role="listbox"]'))"""
                    )
                    if not in_menu:
                        continue
                    text = " ".join((item.text_content(timeout=300) or "").split())
                except Exception:
                    continue
                if any(label and label in text for label in labels):
                    self._click_locator(item, timeout_ms=2000)
                    print(f"[{self.name}] 已点击模式菜单项: {text}")
                    return True
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 点击模式菜单项失败: {e}")
        return False

    def _select_chat_mode(self, *, target: str) -> bool:
        if target not in {"think", "quick"}:
            return False
        current = self._get_deep_think_state()
        current_text = str((current.get("primary") or {}).get("text") or "").strip()
        current_active = bool(current.get("active"))
        if target == "quick" and current_text.startswith("快速") and not current_active:
            return True
        if target == "think" and current_active:
            return True
        trigger = self._get_mode_trigger_locator(timeout_ms=2000)
        if not trigger:
            return target == "quick"
        try:
            self._click_locator(trigger, timeout_ms=2000)
            print(f"[{self.name}] 已点击模式入口: composer-root")
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 点击模式入口失败: {e}")
            return False
        self._cooperative_sleep(random.uniform(0.25, 0.5))
        target_labels = self._mode_labels_for_target("思考" if target == "think" else "快速")
        if not self._click_mode_menu_item_with_text(target_labels):
            return False
        self._cooperative_sleep(random.uniform(0.3, 0.6))
        current = self._get_deep_think_state()
        current_text = str((current.get("primary") or {}).get("text") or "").strip()
        current_active = bool(current.get("active"))
        if target == "quick":
            return current_text.startswith("快速") and not current_active
        return current_active

    def _click_new_chat_via_dom(self) -> bool:
        try:
            self._raise_if_stop_requested()
            result = self.page.evaluate("""() => {
                const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };
                const clickNode = (node) => {
                    if (!node || !isVisible(node)) return false;
                    try { node.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
                    try { node.click(); } catch (_) {}
                    return true;
                };

                const preferred = document.querySelector("div[data-testid='create_conversation_button']");
                if (clickNode(preferred)) {
                    return { clicked: true, source: "preferred" };
                }

                const candidates = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"], div[data-testid], a'));
                for (const el of candidates) {
                    if (!isVisible(el)) continue;
                    const text = norm([
                        el.innerText,
                        el.textContent,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('data-testid'),
                    ].filter(Boolean).join(' '));
                    if (!text) continue;
                    if (
                        text.includes('新对话') ||
                        text.includes('开启新对话') ||
                        text.includes('create_conversation') ||
                        text.includes('conversation_button')
                    ) {
                        if (clickNode(el)) {
                            return { clicked: true, source: text.slice(0, 80) };
                        }
                    }
                }
                return { clicked: false, source: '' };
            }""") or {}
            if result.get("clicked"):
                print(f"[{self.name}] 已通过DOM触发新对话: {result.get('source')}")
                return True
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _conversation_snapshot(self) -> dict:
        try:
            return self.page.evaluate(
                """({resultSel, thinkSel, inputSel}) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const answers = Array.from(document.querySelectorAll(resultSel || ''))
                        .filter((el) => isVisible(el) && !(thinkSel && el.closest(thinkSel)))
                        .map((el) => String(el.innerText || el.textContent || '').trim())
                        .filter(Boolean);
                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputValue = input ? String(input.value || input.innerText || input.textContent || '').trim() : '';
                    return {
                        answerCount: answers.length,
                        answerLength: answers.join('\\n').length,
                        inputValue,
                        href: String(location.href || ''),
                        path: String(location.pathname || ''),
                    };
                }""",
                {
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "inputSel": self.input_selector or "",
                },
            ) or {"answerCount": 0, "answerLength": 0, "inputValue": "", "href": "", "path": ""}
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"answerCount": 0, "answerLength": 0, "inputValue": "", "href": "", "path": ""}

    def _open_fresh_chat_fallback(self) -> bool:
        try:
            self._raise_if_stop_requested()
            self.page.goto(self.target_url, wait_until="domcontentloaded", timeout=15000)
            self._cooperative_sleep(random.uniform(1.0, 1.8))
            self._wait_for_page_selector(self.input_selector, timeout_ms=10000)
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _wait_until_new_chat_ready(self, before: dict, timeout: float = 6.0) -> bool:
        return super()._wait_until_new_chat_ready(before, timeout=timeout)

    def start_new_chat(self) -> None:
        """优先使用固定 selector，失效时再退回轻量 DOM 兜底。"""
        try:
            self._raise_if_stop_requested()
            self._disable_captcha_pointer_intercept()
            self.check_for_interruption(check_input_visible=False)
            before = self._conversation_snapshot()
            last_error = "豆包新对话入口不可用"
            if self._click_selector(self.new_chat_selector, "新对话", timeout_ms=3000):
                if self._wait_and_confirm_new_chat(before, sleep_seconds=random.uniform(0.5, 1.0), timeout_ms=8000):
                    print(f"[{self.name}] 已开启新对话")
                    return
                last_error = "已点击新对话按钮，但未确认切换到新会话"
            if self._click_new_chat_via_dom():
                if self._wait_and_confirm_new_chat(before, sleep_seconds=random.uniform(0.5, 1.0), timeout_ms=8000):
                    print(f"[{self.name}] 已通过DOM开启新对话")
                    return
                last_error = "已通过DOM点击新对话按钮，但未确认切换到新会话"
            if self._attempt_learned_selector_heal("new_chat_selector", label="新对话"):
                print(f"[{self.name}] 已通过 learned selector 开启新对话")
                return
            if self._open_fresh_chat_fallback():
                print(f"[{self.name}] 新对话按钮不可用，已回到首页")
                return
            raise RuntimeError(last_error)
        except InterruptionDetected:
            raise
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 开启新对话失败: {e}")
            raise

    def take_long_screenshot(self, rank: int = 0, quality: int = 85, brand: str = "") -> str:
        from PIL import Image
        import io
        from core.screenshot_tools import decorate_screenshot

        ts = local_now().strftime("%m%d_%H%M%S")
        screenshots_dir = resolve_app_dir("screenshots")
        filename = screenshots_dir / f"{self.name}_{ts}.jpg"
        preview_png, preview_boxes, preview_scale = self._capture_brand_preview(brand)

        try:
            png_bytes = self._stitch_screenshot()
        except Exception as e:
            print(f"[{self.name}] 拼接截图失败，fallback: {e}")
            png_bytes = self.page.screenshot(type="png")

        img = Image.open(io.BytesIO(png_bytes))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        img.save(filename, format='JPEG', quality=quality, optimize=True)
        self.last_screenshot_meta = {
            "screenshot": str(filename),
            "highlight_count": len(preview_boxes),
            "preview_available": bool(preview_png),
        }
        meta = decorate_screenshot(
            str(filename),
            platform_name=self.name,
            brand=brand,
            preview_png=preview_png,
            preview_boxes=preview_boxes,
            preview_scale=preview_scale,
            draw_boxes_on_main=False,
        )
        self.last_screenshot_meta.update(meta)
        print(f"[{self.name}] 长截图已保存: {filename}")
        return str(filename)

    def _stitch_screenshot(self) -> bytes:
        from PIL import Image
        import io

        info = self.page.evaluate("""() => {
            const el = document.querySelector('[class*="scrollable"]');
            if (!el) return null;
            const rect = el.getBoundingClientRect();
            el.scrollTo(0, -999999);
            const minScroll = el.scrollTop;
            el.scrollTo(0, 0);
            return {
                clientHeight: el.clientHeight,
                scrollHeight: el.scrollHeight,
                minScroll: minScroll,
                left: Math.round(rect.left),
                top: Math.round(rect.top),
                width: Math.round(rect.width),
            };
        }""")
        if not info:
            raise Exception("容器未找到")

        clip = {"x": info['left'], "y": info['top'], "width": info['width'], "height": info['clientHeight']}
        client_height = info['clientHeight']
        min_scroll = info['minScroll']  # 负值，顶部位置
        step = int(client_height * 0.85)

        def capture_at(scroll_top: float):
            self._raise_if_stop_requested()
            actual_scroll = self.page.evaluate("""(target) => {
                const el = document.querySelector('[class*="scrollable"]');
                if (!el) return null;
                el.scrollTo(0, target);
                return el.scrollTop;
            }""", scroll_top)
            self._cooperative_sleep_jittered(0.4, spread=0.2)
            actual_scroll = self.page.evaluate("""() => {
                const el = document.querySelector('[class*="scrollable"]');
                return el ? el.scrollTop : null;
            }""")
            if actual_scroll is None:
                raise Exception("滚动容器丢失")
            frame_bytes = self.page.screenshot(type="png", clip=clip)
            frame = Image.open(io.BytesIO(frame_bytes))
            if frame.mode != "RGB":
                frame = frame.convert("RGB")
            return float(actual_scroll), frame

        first_scroll, first_frame = capture_at(min_scroll)
        dpr = first_frame.height / client_height if client_height else 1.0
        total_content_px = max(
            first_frame.height,
            int(round((abs(min_scroll) + client_height) * dpr)),
        )

        captures = [(first_scroll, first_frame)]
        current_scroll = first_scroll
        stagnant_rounds = 0

        while current_scroll < -0.5:
            self._raise_if_stop_requested()
            target_scroll = min(current_scroll + step, 0)
            actual_scroll, frame = capture_at(target_scroll)

            if abs(actual_scroll - captures[-1][0]) > 0.5:
                captures.append((actual_scroll, frame))
                stagnant_rounds = 0
            else:
                stagnant_rounds += 1

            current_scroll = actual_scroll
            if target_scroll >= 0 and actual_scroll >= -0.5:
                break
            if stagnant_rounds >= 2:
                break

        if not captures:
            raise Exception("未截到任何帧")

        captures.sort(key=lambda item: item[0])
        result = Image.new("RGB", (captures[0][1].width, total_content_px))
        for scroll_top, frame in captures:
            start_px = int(round((scroll_top - min_scroll) * dpr))
            if start_px >= total_content_px:
                continue
            paste_h = min(frame.height, total_content_px - start_px)
            if paste_h <= 0:
                continue
            result.paste(frame.crop((0, 0, frame.width, paste_h)), (0, start_px))

        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return buf.getvalue()

    def type_like_human(self, text: str) -> None:
        self._disable_captcha_pointer_intercept()
        self._raise_if_stop_requested()
        self.check_for_interruption(check_input_visible=False)
        chat_input = self.page.locator(self.input_selector).first
        try:
            self._wait_for_locator(chat_input, timeout_ms=5000)
            chat_input.focus(timeout=3000)
        except Exception as e:
            self._reraise_stop_requested(e)
            raise RuntimeError("豆包输入框不可聚焦") from e
        self._cooperative_sleep(random.uniform(0.25, 0.6))
        cleared = False
        try:
            chat_input.fill("")
            cleared = True
        except Exception:
            cleared = False
        if not cleared:
            import sys
            select_all = "Meta+A" if sys.platform == "darwin" else "Control+A"
            chat_input.press(select_all, timeout=1500)
            self._cooperative_sleep(random.uniform(0.08, 0.2))
            chat_input.press("Backspace", timeout=1500)
        self._cooperative_sleep(random.uniform(0.15, 0.35))
        try:
            chat_input.press_sequentially(text, delay=random.randint(50, 120))
        except Exception:
            for char in text:
                self._raise_if_stop_requested()
                chat_input.type(char, delay=random.randint(50, 120))
                if random.random() < 0.1:
                    self._cooperative_sleep(random.uniform(0.2, 0.5))
        self._cooperative_sleep(random.uniform(0.3, 0.6))
        self._last_prompt_text = text
        if not self._wait_for_input_value(text):
            raise RuntimeError("豆包输入框未确认写入关键词，已取消本次提交")

    def _read_input_value(self) -> str:
        try:
            return self.page.evaluate(
                """(selector) => {
                    const input = document.querySelector(selector);
                    if (!input) return '';
                    return String(input.value || input.innerText || input.textContent || '').trim();
                }""",
                self.input_selector,
            ) or ""
        except Exception:
            return ""

    def _wait_for_input_value(self, expected_text: str, timeout: float = 4.0) -> bool:
        expected = self._normalize_compact_text(expected_text)
        if not expected:
            return True
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            self._raise_if_stop_requested()
            current = self._normalize_compact_text(self._read_input_value())
            if current == expected or expected in current:
                return True
            self._cooperative_sleep_jittered(0.15, spread=0.35)
        return False

    def _composer_snapshot(self, keyword: str = "") -> dict:
        try:
            return self.page.evaluate(
                """({inputSel, resultSel, containerSel, thinkSel, stopSel, keyword}) => {
                    const normalize = (value) => String(value || '')
                        .replace(/\\u00a0/g, ' ')
                        .replace(/[ \\t]+\\n/g, '\\n')
                        .replace(/\\n{3,}/g, '\\n\\n')
                        .replace(/[ \\t]{2,}/g, ' ')
                        .trim();
                    const queryAllSafe = (selector) => {
                        if (!selector) return [];
                        const text = String(selector || '').trim();
                        if (!text || text.startsWith('xpath=') || text.startsWith('//') || text.startsWith('(//')) {
                            return [];
                        }
                        try {
                            return Array.from(document.querySelectorAll(text));
                        } catch (_) {
                            return [];
                        }
                    };
                    const compact = (value) => normalize(value).replace(/[\\s\\W_]+/g, '').toLowerCase();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const cleanupText = (value) => normalize(value)
                        .split('\\n')
                        .map((line) => normalize(line))
                        .filter(Boolean)
                        .join('\\n');
                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputText = cleanupText(
                        input ? (input.value || input.innerText || input.textContent || '') : ''
                    );
                    const resultTexts = resultSel
                        ? queryAllSafe(resultSel)
                            .filter((el) => isVisible(el) && !(thinkSel && el.closest(thinkSel)))
                            .map((el) => cleanupText(el.innerText || el.textContent || ''))
                            .filter(Boolean)
                        : [];
                    const container = containerSel ? document.querySelector(containerSel) : document.body;
                    const containerText = cleanupText(
                        container ? (container.innerText || container.textContent || '') : ''
                    );
                    const stopVisible = queryAllSafe(stopSel)
                        .some((el) => isVisible(el));
                    const keywordCompact = compact(keyword || '');
                    const containerCompact = compact(containerText);
                    return {
                        inputText,
                        inputLength: inputText.length,
                        resultCount: resultTexts.length,
                        resultLength: resultTexts.join('\\n').length,
                        containerLength: containerText.length,
                        keywordVisible: Boolean(keywordCompact && containerCompact.includes(keywordCompact)),
                        stopVisible,
                        href: String(location.href || ''),
                        path: String(location.pathname || ''),
                        title: String(document.title || ''),
                        readyState: String(document.readyState || ''),
                    };
                }""",
                {
                    "inputSel": self.input_selector or "",
                    "resultSel": self.result_selector or "",
                    "containerSel": self.chat_container_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "stopSel": self.stop_generating_selector,
                    "keyword": keyword or "",
                },
            )
        except Exception:
            return {
                "inputText": "",
                "inputLength": 0,
                "resultCount": 0,
                "resultLength": 0,
                "containerLength": 0,
                "keywordVisible": False,
                "stopVisible": False,
                "href": "",
                "path": "",
                "title": "",
                "readyState": "",
            }

    def _has_submit_started_signal(self) -> bool | None:
        try:
            self._raise_if_stop_requested()
            self._disable_captcha_pointer_intercept()
            self.check_for_interruption(check_input_visible=False)
            return self._get_status_button_state() == "pause"
        except Exception as e:
            self._reraise_stop_requested(e)
            return None

    def _click_send_button_via_dom(self) -> bool:
        try:
            result = self.page.evaluate(
                """({inputSel, sendSel}) => {
                    const input = document.querySelector(inputSel);
                    if (!input) return { clicked: false, reason: 'no-input' };
                    const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0 &&
                            style.pointerEvents !== 'none'
                        );
                    };
                    const isDisabled = (el) => {
                        const disabled = el.getAttribute('disabled');
                        const ariaDisabled = String(el.getAttribute('aria-disabled') || '').toLowerCase();
                        return disabled !== null || ariaDisabled === 'true';
                    };
                    const clickNode = (el) => {
                        if (!el || !isVisible(el) || isDisabled(el)) return false;
                        try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
                        try { el.click(); } catch (_) {}
                        return true;
                    };
                    const getText = (el) => norm([
                        el.innerText,
                        el.textContent,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('data-testid'),
                        el.getAttribute('data-dbx-name'),
                    ].filter(Boolean).join(' '));
                    if (sendSel) {
                        const direct = document.querySelector(sendSel);
                        if (clickNode(direct)) {
                            return { clicked: true, text: 'direct-send-selector', score: 999 };
                        }
                    }
                    const inputRect = input.getBoundingClientRect();
                    const inputCenterY = inputRect.top + inputRect.height / 2;
                    const selectors = 'button, [role="button"], div[role="button"]';
                    const candidates = [];
                    for (const el of document.querySelectorAll(selectors)) {
                        if (!isVisible(el) || isDisabled(el)) continue;
                        if (el === input || el.contains(input)) continue;
                        const text = getText(el);
                        if (/停止|重新生成|复制|分享|点赞|点踩|新对话|快速|思考|自动|专家|deep-thinking|conversation/i.test(text)) {
                            continue;
                        }
                        const rect = el.getBoundingClientRect();
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const dx = centerX - inputRect.right;
                        const dy = Math.abs(centerY - inputCenterY);
                        let score = 0;
                        if (/发送|send|submit/i.test(text)) score += 200;
                        if (/arrow|plane|submit|send/i.test(text)) score += 60;
                        if (dx >= -20 && dx <= 180) score += 80;
                        if (dy <= 120) score += 60;
                        if (rect.width <= 80 && rect.height <= 80) score += 20;
                        if (text) score += Math.max(0, 40 - text.length);
                        candidates.push({
                            el,
                            text,
                            score,
                            rect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
                        });
                    }
                    candidates.sort((a, b) => b.score - a.score);
                    const best = candidates[0];
                    if (!best || best.score < 120) {
                        return {
                            clicked: false,
                            reason: best ? `low-score:${best.score}` : 'no-candidate',
                        };
                    }
                    clickNode(best.el);
                    return {
                        clicked: true,
                        text: best.text,
                        score: best.score,
                    };
                }""",
                {
                    "inputSel": self.input_selector,
                    "sendSel": self.send_button_selector,
                },
            ) or {}
            if result.get("clicked"):
                print(f"[{self.name}] 已通过DOM点击发送按钮: {result.get('text') or 'icon-button'}")
                return True
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def submit_prompt(self) -> None:
        keyword = str(getattr(self, "_last_prompt_text", "") or "")
        before_input = self._read_input_value()
        try:
            self._raise_if_stop_requested()
            self._disable_captcha_pointer_intercept()
            if self._normalize_compact_text(before_input) != self._normalize_compact_text(keyword):
                raise RuntimeError("豆包输入框内容与本轮关键词不一致，已取消提交")
            chat_input = self.page.locator(self.input_selector).first
            chat_input.focus(timeout=3000)
            chat_input.press("Enter", timeout=3000)
            strategy_name = "input_enter"
            if self._wait_for_submit_started(before_input, timeout=8.0):
                print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}）")
                return
            if self._wait_for_submit_start_signal(timeout=4.0):
                print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}; pause-signal）")
                return
            status_after = self._get_status_button_state()
            if status_after == "pause":
                print(f"[{self.name}] 状态按钮已切到暂停态（策略: {strategy_name}）")
                return
            after_input = self._normalize_compact_text(self._read_input_value())
            if before_input and not after_input:
                self._cooperative_sleep_jittered(0.8, spread=0.18)
                if not self._normalize_compact_text(self._read_input_value()):
                    print(f"[{self.name}] 输入框已清空，按已发送处理（策略: {strategy_name}; input-cleared）")
                    return
            print(f"[{self.name}] Enter 提交未确认，尝试点击发送按钮兜底")
            if self._click_send_button_via_dom():
                strategy_name = "dom_send_button"
                if self._wait_for_submit_started(before_input, timeout=8.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}）")
                    return
                if self._wait_for_submit_start_signal(timeout=4.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}; pause-signal）")
                    return
                status_after = self._get_status_button_state()
                if status_after == "pause":
                    print(f"[{self.name}] 状态按钮已切到暂停态（策略: {strategy_name}）")
                    return
                after_input = self._normalize_compact_text(self._read_input_value())
                if before_input and not after_input:
                    self._cooperative_sleep_jittered(0.8, spread=0.18)
                    if not self._normalize_compact_text(self._read_input_value()):
                        print(f"[{self.name}] 输入框已清空，按已发送处理（策略: {strategy_name}; input-cleared）")
                        return
            raise RuntimeError(f"豆包未确认问题已发送，当前状态按钮={status_after}")
        except InterruptionDetected:
            raise
        except Exception as e:
            self._reraise_stop_requested(e)
            raise RuntimeError(f"豆包提交失败: {e}") from e

    def _get_answer_text(self) -> str:
        try:
            self._consume_answer_read_scroll()
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel, inputSel}) => {
                    const container = containerSel ? document.querySelector(containerSel) : document.body;
                    const normalize = (value) => String(value || '')
                        .replace(/\\u00a0/g, ' ')
                        .replace(/[ \\t]+\\n/g, '\\n')
                        .replace(/\\n{3,}/g, '\\n\\n')
                        .replace(/[ \\t]{2,}/g, ' ')
                        .trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputText = normalize(
                        input
                            ? (input.value || input.innerText || input.textContent || '')
                            : ''
                    );
                    const isNoiseLine = (line) => {
                        const text = normalize(line);
                        if (!text) return true;
                        if (inputText && text === inputText) return true;
                        return (
                            /^(开启新对话|新对话|发消息|上传附件|重新生成|复制|分享|点赞|点踩|停止回答|停止生成)$/.test(text) ||
                            /^(快速|思考|自动|专家)$/.test(text) ||
                            /^已(完成思考|思考完成)(，|,|。|！|!|$)/.test(text) ||
                            /^参考\\s*\\d+\\s*篇资料(，|,|。|！|!|$)/.test(text) ||
                            /^已阅读\\s*\\d+\\s*个网页(，|,|。|！|!|$)/.test(text) ||
                            /^深度思考中(，|,|。|！|!|$)/.test(text) ||
                            /^深度思考智能搜索内容由 AI 生成，请仔细甄别$/.test(text) ||
                            /^内容由 AI 生成，请仔细甄别$/.test(text)
                        );
                    };
                    const cleanupText = (value) => {
                        const lines = normalize(value)
                            .split('\\n')
                            .map((line) => normalize(line))
                            .filter((line) => !isNoiseLine(line));
                        const deduped = [];
                        for (const line of lines) {
                            if (!line) continue;
                            if (deduped.length > 0 && deduped[deduped.length - 1] === line) continue;
                            deduped.push(line);
                        }
                        return normalize(deduped.join('\\n'));
                    };
                    const looksLikeAnswer = (value) => {
                        const text = cleanupText(value);
                        if (!text || text.length < 20) return false;
                        const compact = text.replace(/\\s+/g, '');
                        if (compact.length < 16) return false;
                        if (inputText && compact === inputText.replace(/\\s+/g, '')) return false;
                        return /[\\u4e00-\\u9fffA-Za-z0-9]/.test(compact);
                    };
                    const isInsideExcluded = (el) => {
                        if (!el) return false;
                        if (thinkSel && el.closest(thinkSel)) return true;
                        return Boolean(
                            el.closest(
                                'textarea, input, button, nav, header, footer, aside, form, [role="button"], [role="menu"], [role="dialog"], [contenteditable="true"]'
                            )
                        );
                    };
                    const pushUniqueText = (arr, text) => {
                        if (!text) return;
                        const compact = text.replace(/\\s+/g, '');
                        if (!compact) return;
                        for (let i = 0; i < arr.length; i += 1) {
                            const existingCompact = arr[i].replace(/\\s+/g, '');
                            if (!existingCompact) continue;
                            if (existingCompact === compact || existingCompact.includes(compact)) return;
                            if (compact.includes(existingCompact)) {
                                arr[i] = text;
                                return;
                            }
                        }
                        arr.push(text);
                    };

                    const groups = [];
                    const groupMap = new Map();
                    const attachToGroup = (node, text) => {
                        const anchor = node.closest(
                            '[data-testid*="message"], [data-testid*="conversation"], [class*="message"], [class*="conversation"], [class*="chat-item"], article, li, section'
                        ) || node.parentElement || node;
                        if (!anchor) return;
                        let group = groupMap.get(anchor);
                        if (!group) {
                            group = { anchor, texts: [], order: groups.length };
                            groupMap.set(anchor, group);
                            groups.push(group);
                        }
                        pushUniqueText(group.texts, text);
                    };

                    if (resultSel) {
                        for (const el of document.querySelectorAll(resultSel)) {
                            if (!isVisible(el) || isInsideExcluded(el)) continue;
                            const text = cleanupText(el.innerText || el.textContent || '');
                            if (!looksLikeAnswer(text)) continue;
                            attachToGroup(el, text);
                        }
                    }

                    if (groups.length === 0) {
                        const root = container || document.body;
                        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
                        let current = walker.currentNode;
                        while (current) {
                            const el = current;
                            if (
                                isVisible(el) &&
                                !isInsideExcluded(el) &&
                                el.childElementCount <= 8
                            ) {
                                const text = cleanupText(el.innerText || el.textContent || '');
                                if (looksLikeAnswer(text)) {
                                    attachToGroup(el, text);
                                }
                            }
                            current = walker.nextNode();
                        }
                    }

                    if (groups.length > 0) {
                        const scored = groups
                            .map((group) => {
                                const text = cleanupText(group.texts.join('\\n'));
                                const rect = group.anchor.getBoundingClientRect();
                                return {
                                    text,
                                    order: group.order,
                                    top: rect.top,
                                    bottom: rect.bottom,
                                };
                            })
                            .filter((group) => looksLikeAnswer(group.text))
                            .sort((a, b) => {
                                if (a.order !== b.order) return a.order - b.order;
                                return a.bottom - b.bottom;
                            });
                        if (scored.length > 0) {
                            const merged = [];
                            for (const group of scored) {
                                pushUniqueText(merged, group.text);
                            }
                            return cleanupText(merged.join('\\n'));
                        }
                    }

                    const fallbackRoot = container || document.body;
                    const fallbackText = cleanupText(fallbackRoot.innerText || document.body.innerText || '');
                    return looksLikeAnswer(fallbackText) ? fallbackText : '';
                }""",
                {
                    "containerSel": self.chat_container_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "inputSel": self.input_selector or "",
                },
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._get_answer_text()

    def is_generation_complete(self, page_text: str, start_time: float) -> bool:
        try:
            self._raise_if_stop_requested()
            self._disable_captcha_pointer_intercept()
            status_state = self._get_status_button_state()
            return bool(self.page.evaluate(
                """({resultSel, thinkSel, inputSel, stopSel, statusState}) => {
                    const normalize = (value) => String(value || '')
                        .replace(/\\u00a0/g, ' ')
                        .replace(/[ \\t]+\\n/g, '\\n')
                        .replace(/\\n{3,}/g, '\\n\\n')
                        .replace(/[ \\t]{2,}/g, ' ')
                        .trim();
                    const queryAllSafe = (selector) => {
                        if (!selector) return [];
                        const text = String(selector || '').trim();
                        if (!text || text.startsWith('xpath=') || text.startsWith('//') || text.startsWith('(//')) {
                            return [];
                        }
                        try {
                            return Array.from(document.querySelectorAll(text));
                        } catch (_) {
                            return [];
                        }
                    };
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputText = normalize(
                        input ? (input.value || input.innerText || input.textContent || '') : ''
                    );
                    const isNoiseLine = (line) => {
                        const text = normalize(line);
                        if (!text) return true;
                        if (inputText && text === inputText) return true;
                        return (
                            /^(开启新对话|新对话|发消息|上传附件|重新生成|复制|分享|点赞|点踩|停止回答|停止生成)$/.test(text) ||
                            /^(快速|思考|自动|专家)$/.test(text) ||
                            /^已(完成思考|思考完成)(，|,|。|！|!|$)/.test(text) ||
                            /^参考\\s*\\d+\\s*篇资料(，|,|。|！|!|$)/.test(text) ||
                            /^已阅读\\s*\\d+\\s*个网页(，|,|。|！|!|$)/.test(text) ||
                            /^深度思考中(，|,|。|！|!|$)/.test(text) ||
                            /^深度思考智能搜索内容由 AI 生成，请仔细甄别$/.test(text) ||
                            /^内容由 AI 生成，请仔细甄别$/.test(text)
                        );
                    };
                    const cleanupText = (value) => {
                        const lines = normalize(value)
                            .split('\\n')
                            .map((line) => normalize(line))
                            .filter((line) => !isNoiseLine(line));
                        return normalize(lines.join('\\n'));
                    };
                    const looksLikeAnswer = (value) => {
                        const text = cleanupText(value);
                        if (!text || text.length < 20) return false;
                        const compact = text.replace(/\\s+/g, '');
                        if (compact.length < 16) return false;
                        if (inputText && compact === inputText.replace(/\\s+/g, '')) return false;
                        return /[\\u4e00-\\u9fffA-Za-z0-9]/.test(compact);
                    };
                    if (statusState === 'pause') return false;
                    const hasStopButton = queryAllSafe(stopSel)
                        .some((el) => isVisible(el));
                    if (hasStopButton) return false;
                    const resultNodes = resultSel
                        ? queryAllSafe(resultSel)
                            .filter((el) => isVisible(el) && !(thinkSel && el.closest(thinkSel)))
                        : [];
                    return resultNodes.some((el) => looksLikeAnswer(el.innerText || el.textContent || ''));
                }""",
                {
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "inputSel": self.input_selector or "",
                    "stopSel": self.stop_generating_selector,
                    "statusState": status_state,
                },
            ))
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _get_deep_think_state(self) -> dict:
        """兼容豆包新旧 UI，尽量从按钮/开关/菜单项中识别深度思考状态。"""
        try:
            trigger = self._get_mode_trigger_locator(timeout_ms=1500)
            if not trigger:
                return {"found": False, "active": False, "primary": None, "candidates": []}
            text = " ".join((trigger.text_content(timeout=300) or "").split())
            active = (
                ("思考" in text or "自动思考" in text or "深度思考" in text or "深思" in text or "R1" in text)
                and "快速" not in text
            )
            return {
                "found": bool(text),
                "active": active,
                "primary": {
                    "text": text,
                    "role": trigger.get_attribute("role") or "",
                    "tag": "BUTTON",
                    "cls": trigger.get_attribute("class") or "",
                    "checked": trigger.get_attribute("data-state") or "",
                    "in_menu": False,
                    "score": 999,
                },
                "candidates": [],
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"found": False, "active": False, "primary": None, "candidates": []}

    def _click_best_deep_think_candidate(self, patterns, *, prefer_menu: bool = False) -> dict:
        return self.page.evaluate("""(payload) => {
            const patterns = payload.patterns || [];
            const preferMenu = Boolean(payload.preferMenu);
            const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const isCompactText = (text) => text && text.length <= 24 && !/[\\n\\r]/.test(text);
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0' &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            };
            const getText = (el) => norm([
                el.innerText,
                el.textContent,
                el.getAttribute('aria-label'),
                el.getAttribute('title'),
                el.getAttribute('data-testid'),
                el.getAttribute('data-dbx-name'),
            ].filter(Boolean).join(' '));

            const selectors = [
                'button',
                '[role="button"]',
                '[role="switch"]',
                '[role="radio"]',
                '[role="menuitem"]',
                '[role="menuitemradio"]',
            ].join(',');
            const input = document.querySelector('textarea, [contenteditable="true"]');
            const inputRect = input ? input.getBoundingClientRect() : null;
            const candidates = [];

            for (const el of document.querySelectorAll(selectors)) {
                if (!isVisible(el)) continue;
                const text = getText(el);
                if (!text) continue;
                if (!isCompactText(text)) continue;
                if (!patterns.some((pattern) => text.includes(pattern))) continue;

                const rect = el.getBoundingClientRect();
                const role = norm(el.getAttribute('role'));
                const cls = norm(el.className);
                let score = 0;
                for (const pattern of patterns) {
                    if (text.includes(pattern)) score += pattern.length * 20;
                }
                if (preferMenu && el.closest('[role="menu"], [role="dialog"], [role="listbox"]')) score += 60;
                if (!preferMenu && el.closest('[role="menu"], [role="dialog"], [role="listbox"]')) score -= 15;
                if (el.tagName === 'BUTTON') score += 20;
                if (role === 'button' || role === 'switch' || role === 'radio' || role === 'menuitem' || role === 'menuitemradio') score += 15;
                if (/开启|开/.test(text)) score += 10;
                if (/关闭/.test(text)) score -= 40;
                if (/active|selected|checked|on/i.test(cls)) score += 8;
                if (inputRect) {
                    const dx = Math.abs((rect.left + rect.width / 2) - (inputRect.left + inputRect.width / 2));
                    const dy = Math.abs((rect.top + rect.height / 2) - (inputRect.top + inputRect.height / 2));
                    score += Math.max(0, 600 - dx) / 40;
                    score += Math.max(0, 320 - dy) / 12;
                    if (rect.bottom <= inputRect.top + 180 && rect.bottom >= inputRect.top - 120) score += 20;
                }
                candidates.push({ el, text, rect, score, role, tag: el.tagName });
            }

            candidates.sort((a, b) => b.score - a.score);
            const best = candidates[0];
            if (!best) {
                return { clicked: false, reason: 'no-candidate', candidates: [] };
            }
            best.el.click();
            return {
                clicked: true,
                text: best.text,
                role: best.role,
                tag: best.tag,
                score: best.score,
                candidates: candidates.slice(0, 6).map((item) => ({
                    text: item.text,
                    role: item.role,
                    tag: item.tag,
                    score: item.score,
                })),
            };
        }""", {"patterns": patterns, "preferMenu": prefer_menu})

    def _click_by_locator(self, selector: str, label: str, *, quiet: bool = False) -> bool:
        """优先通过明确 selector 点击。"""
        try:
            self._raise_if_stop_requested()
            locator = self.page.locator(selector).first
            self._wait_for_locator(locator, timeout_ms=2000)
            self._click_locator(locator, timeout_ms=2000)
            print(f"[{self.name}] 已通过selector点击{label}: {selector}")
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            if not quiet:
                print(f"[{self.name}] selector点击{label}失败: {e}")
            return False

    def _click_mode_control_by_selector(self, labels, *, in_menu: bool) -> bool:
        """通过文案定位豆包模式控件；菜单态和非菜单态分开处理。"""
        try:
            self._raise_if_stop_requested()
            result = self.page.evaluate("""(payload) => {
                const labels = payload.labels || [];
                const inMenu = Boolean(payload.inMenu);
                const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };
                const getText = (el) => norm([
                    el.innerText,
                    el.textContent,
                    el.getAttribute('aria-label'),
                    el.getAttribute('title'),
                    el.getAttribute('data-testid'),
                    el.getAttribute('data-dbx-name'),
                ].filter(Boolean).join(' '));
                const selectors = inMenu
                    ? '[role="menuitem"], [role="menuitemradio"], [role="radio"], button, [role="button"]'
                    : 'button, [role="button"], [role="switch"], [role="radio"]';
                const candidates = [];
                const matchLabel = (text, label) => {
                    const normalizedText = norm(text).toLowerCase();
                    const normalizedLabel = norm(label).toLowerCase();
                    if (!normalizedText || !normalizedLabel) return false;
                    return (
                        normalizedText === normalizedLabel ||
                        normalizedText.startsWith(normalizedLabel + ' ') ||
                        normalizedText.startsWith(normalizedLabel) ||
                        normalizedText.includes(normalizedLabel)
                    );
                };
                const isMenuNode = (el) => Boolean(el.closest('[role="menu"], [role="dialog"], [role="listbox"]'));
                const fireMouseEvent = (target, type, init = {}) => {
                    const eventInit = {
                        bubbles: true,
                        cancelable: true,
                        composed: true,
                        view: window,
                        button: 0,
                        buttons: 1,
                        ...init,
                    };
                    target.dispatchEvent(new MouseEvent(type, eventInit));
                };
                const firePointerEvent = (target, type, init = {}) => {
                    const eventInit = {
                        bubbles: true,
                        cancelable: true,
                        composed: true,
                        view: window,
                        pointerId: 1,
                        pointerType: 'mouse',
                        isPrimary: true,
                        button: 0,
                        buttons: 1,
                        ...init,
                    };
                    if (typeof PointerEvent === 'function') {
                        target.dispatchEvent(new PointerEvent(type, eventInit));
                    } else {
                        fireMouseEvent(target, type, eventInit);
                    }
                };
                const performInteractiveClick = (el, preferCaret) => {
                    const clickTarget = preferCaret
                        ? (el.querySelector('[data-dbx-name="button-caret"]') || el.querySelector('svg[data-dbx-name="button-caret"]') || el)
                        : el;
                    const rect = clickTarget.getBoundingClientRect();
                    const pointInit = {
                        clientX: rect.left + rect.width / 2,
                        clientY: rect.top + rect.height / 2,
                    };
                    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch {}
                    try { el.focus(); } catch {}
                    firePointerEvent(clickTarget, 'pointerover', pointInit);
                    fireMouseEvent(clickTarget, 'mouseover', pointInit);
                    firePointerEvent(clickTarget, 'pointerenter', pointInit);
                    fireMouseEvent(clickTarget, 'mouseenter', pointInit);
                    firePointerEvent(clickTarget, 'pointermove', pointInit);
                    fireMouseEvent(clickTarget, 'mousemove', pointInit);
                    firePointerEvent(clickTarget, 'pointerdown', pointInit);
                    fireMouseEvent(clickTarget, 'mousedown', pointInit);
                    firePointerEvent(clickTarget, 'pointerup', pointInit);
                    fireMouseEvent(clickTarget, 'mouseup', pointInit);
                    if (typeof clickTarget.click === 'function') {
                        clickTarget.click();
                    } else if (typeof el.click === 'function') {
                        el.click();
                    } else {
                        fireMouseEvent(clickTarget, 'click', pointInit);
                    }
                    return clickTarget === el ? 'element' : 'caret';
                };
                for (const el of document.querySelectorAll(selectors)) {
                    if (!isVisible(el)) continue;
                    const text = getText(el);
                    if (!text) continue;
                    const menuMatched = isMenuNode(el);
                    if (menuMatched !== inMenu) continue;
                    for (const label of labels) {
                        if (!matchLabel(text, label)) continue;
                        candidates.push({
                            el,
                            text,
                            hasPopup: norm(el.getAttribute('aria-haspopup')).toLowerCase(),
                            score:
                                label.length * 100 +
                                (norm(text).toLowerCase() === norm(label).toLowerCase() ? 80 : 0) +
                                (norm(text).toLowerCase().startsWith(norm(label).toLowerCase()) ? 30 : 0) +
                                (el.tagName === 'BUTTON' ? 20 : 0),
                        });
                    }
                }
                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) return { clicked: false, text: '', count: 0 };
                const strategy = performInteractiveClick(
                    best.el,
                    !inMenu && best.hasPopup === 'menu',
                );
                return {
                    clicked: true,
                    text: best.text,
                    count: candidates.length,
                    strategy,
                    has_popup: best.hasPopup,
                };
            }""", {"labels": labels, "inMenu": in_menu})
            if result.get("clicked"):
                print(
                    f"[{self.name}] 已通过selector点击{'菜单项' if in_menu else '模式入口'}: "
                    f"{result.get('text')} (strategy={result.get('strategy')}, has_popup={result.get('has_popup')})"
                )
                return True
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] selector点击模式控件失败: {e}")
            return False

    def _mode_labels_for_target(self, target_label: str) -> list[str]:
        if target_label == "思考":
            return ["思考", "自动思考", "深度思考", "深思", "R1"]
        if target_label == "快速":
            return ["快速", "标准", "默认"]
        return [target_label]

    def _wait_mode_menu_open(self, timeout_ms: int = 2500) -> bool:
        try:
            deadline = time.monotonic() + max(0.5, timeout_ms / 1000.0)
            while time.monotonic() < deadline:
                self._raise_if_stop_requested()
                if self.page.evaluate("""() => {
                    const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const getText = (el) => norm([
                        el.innerText,
                        el.textContent,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('data-testid'),
                        el.getAttribute('data-dbx-name'),
                    ].filter(Boolean).join(' '));
                    const items = document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], [role="radio"], button, [role="button"]');
                    for (const el of items) {
                        if (!isVisible(el)) continue;
                        if (!el.closest('[role="menu"], [role="dialog"], [role="listbox"]')) continue;
                        const text = getText(el);
                        if (/思考|快速|自动思考|深度思考|深思/i.test(text)) {
                            return true;
                        }
                    }
                    return false;
                }"""):
                    return True
                self._cooperative_sleep_jittered(0.15, spread=0.35)
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _open_mode_menu_by_selector(self) -> bool:
        """点击当前模式按钮，并确认模式菜单已展开。"""
        state = self._get_deep_think_state()
        current_text = str((state.get('primary') or {}).get('text') or "").strip()
        labels = []
        if current_text:
            labels.append(current_text)
        labels.extend(["思考", "快速", "专家"])
        deduped = []
        for label in labels:
            if label and label not in deduped:
                deduped.append(label)
        if self._click_mode_control_by_selector(deduped, in_menu=False):
            return self._wait_mode_menu_open()
        return False

    def _switch_mode_by_selector(self, target_label: str) -> bool:
        """按豆包真实交互切换模式：先点当前模式，再点菜单项。"""
        self._raise_if_stop_requested()
        target_labels = self._mode_labels_for_target(target_label)
        if not self._open_mode_menu_by_selector():
            return False
        self._cooperative_sleep(random.uniform(0.35, 0.7))
        if target_label == "思考" and self._click_by_locator(self.deep_think_think_selector, "思考菜单项", quiet=True):
            self._cooperative_sleep(random.uniform(0.5, 0.9))
            return True
        if target_label == "快速" and self._click_by_locator(self.deep_think_quick_selector, "快速菜单项", quiet=True):
            self._cooperative_sleep(random.uniform(0.5, 0.9))
            return True
        if self._click_mode_control_by_selector(target_labels, in_menu=True):
            self._cooperative_sleep(random.uniform(0.5, 0.9))
            return True
        fallback = self._click_best_deep_think_candidate(target_labels, prefer_menu=True)
        if fallback.get('clicked'):
            self._cooperative_sleep(random.uniform(0.5, 0.9))
            return True
        return False

    def _disable_deep_think(self) -> bool:
        """把豆包切回快速模式，避免继承上次残留的思考模式。"""
        self._raise_if_stop_requested()
        state = self._get_deep_think_state()
        primary_text = str((state.get('primary') or {}).get('text') or "")
        if primary_text.startswith("快速") and not state.get('active'):
            print(f"[{self.name}] 当前已是快速模式")
            return True

        if self._switch_mode_by_selector("快速"):
            state = self._get_deep_think_state()
            primary_text = str((state.get('primary') or {}).get('text') or "")
            if primary_text.startswith("快速") and not state.get('active'):
                print(f"[{self.name}] 已通过selector切换到快速模式")
                return True

        if self._open_mode_menu_by_selector():
            self._cooperative_sleep(random.uniform(0.45, 0.8))
            menu_choice = self._click_best_deep_think_candidate(["快速"], prefer_menu=True)
            if menu_choice.get('clicked'):
                self._cooperative_sleep(random.uniform(0.6, 1.0))
                state = self._get_deep_think_state()
                primary_text = str((state.get('primary') or {}).get('text') or "")
                if primary_text.startswith("快速") and not state.get('active'):
                    print(f"[{self.name}] 已切换到快速模式")
                    return True
                if "快速" in str(menu_choice.get('text') or ""):
                    print(f"[{self.name}] 已触发快速模式切换")
                    return True

        state = self._get_deep_think_state()
        primary_text = str((state.get('primary') or {}).get('text') or "")
        if primary_text.startswith("快速") and not state.get('active'):
            print(f"[{self.name}] 当前已是快速模式")
            return True
        return False

    def _get_nearby_clickable_controls(self) -> list:
        """调试用：收集输入框附近可点击控件，便于定位豆包 UI 改版。"""
        return self.page.evaluate("""() => {
            const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0' &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            };
            const getText = (el) => norm([
                el.innerText,
                el.textContent,
                el.getAttribute('aria-label'),
                el.getAttribute('title'),
                el.getAttribute('data-testid'),
                el.getAttribute('data-dbx-name'),
            ].filter(Boolean).join(' '));
            const isClickable = (el) => {
                const role = norm(el.getAttribute('role')).toLowerCase();
                const tabindex = el.getAttribute('tabindex');
                const style = window.getComputedStyle(el);
                return (
                    el.tagName === 'BUTTON' ||
                    ['button', 'switch', 'radio', 'menuitem', 'menuitemradio'].includes(role) ||
                    el.hasAttribute('onclick') ||
                    (tabindex !== null && tabindex !== '-1') ||
                    style.cursor === 'pointer'
                );
            };

            const input = document.querySelector('textarea, [contenteditable="true"]');
            const inputRect = input ? input.getBoundingClientRect() : null;
            const selectors = [
                'button',
                '[role]',
                '[tabindex]',
                '[data-testid]',
                '[aria-label]',
                '[title]',
            ].join(',');
            const candidates = [];

            for (const el of document.querySelectorAll(selectors)) {
                if (!isVisible(el) || !isClickable(el)) continue;
                const text = getText(el);
                if (!text || text.length > 32 || text === '发消息...') continue;

                const rect = el.getBoundingClientRect();
                const role = norm(el.getAttribute('role'));
                let score = 0;
                if (inputRect) {
                    const dx = Math.abs((rect.left + rect.width / 2) - (inputRect.left + inputRect.width / 2));
                    const dy = Math.abs((rect.top + rect.height / 2) - (inputRect.top + inputRect.height / 2));
                    score += Math.max(0, 700 - dx) / 40;
                    score += Math.max(0, 360 - dy) / 10;
                }
                if (el.tagName === 'BUTTON') score += 12;
                if (role) score += 6;
                candidates.push({
                    text,
                    role,
                    tag: el.tagName,
                    score,
                });
            }

            candidates.sort((a, b) => b.score - a.score);
            return candidates.slice(0, 12);
        }""")

    @staticmethod
    def _parse_reference_expected_count(text: str) -> int:
        match = re.search(r"参考\s*(\d+)\s*篇资料", str(text or ""))
        if not match:
            return 0
        try:
            return max(0, int(match.group(1)))
        except Exception:
            return 0

    @staticmethod
    def _reference_wheel_max_passes(expected_count: int) -> int:
        count = max(0, int(expected_count or 0))
        if count <= 0:
            return 8
        return min(36, max(8, (count + 2) // 3 + 5))

    def extract_answer_references(self) -> list[dict]:
        """
        豆包平台抓取源提取：点击"参考X篇资料"按钮展开右侧面板，
        扫描面板内的 <a href> 链接（按屏幕位置定位，不依赖 CSS hash 类名）。
        """
        try:
            self._raise_if_stop_requested()

            try:
                # 精确匹配底部操作栏的"参考 X 篇资料"按钮，排除顶部思考摘要
                ref_button = self.page.locator('div, span').filter(
                    has_text=re.compile(r'^参考\s*\d+\s*篇资料$')
                ).last
                if ref_button.count() == 0:
                    print(f"[{self.name}] 未找到参考资料按钮")
                    return []
                button_text = ref_button.inner_text()
                expected_count = self._parse_reference_expected_count(button_text)
                print(f"[{self.name}] 找到参考按钮: {button_text}，点击展开...")
                ref_button.click(timeout=3000)
                self._cooperative_sleep_jittered(2.5, spread=0.15)
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 点击参考按钮失败: {e}")
                return []

            collect_script = r"""() => {
                const results = [];
                const seenUrls = new Set();
                let index = 1;
                const extractDomain = (url) => {
                    try {
                        let hostname = new URL(url).hostname.replace(/^www[.]/, '');
                        const parts = hostname.split('.');
                        if (parts.length >= 3 && parts[parts.length - 2].length <= 3) {
                            return parts.slice(-3).join('.');
                        }
                        return parts.length >= 2 ? parts.slice(-2).join('.') : hostname;
                    } catch (e) { return ''; }
                };

                const formatSource = (domain) => {
                    const map = {
                        'zol.com.cn': 'ZOL中关村', 'pconline.com.cn': '太平洋电脑',
                        'toutiao.com': '今日头条', 'ixigua.com': '西瓜视频',
                        'iesdouyin.com': '抖音', 'douyin.com': '抖音',
                        'zhihu.com': '知乎', 'weibo.com': '微博',
                        'sina.com.cn': '新浪网', 'sohu.com': '搜狐',
                        '163.com': '网易', 'qq.com': '腾讯',
                        'baidu.com': '百度', 'huawei.com': '华为官网',
                        'smzdm.com': '什么值得买', 'xueqiu.com': '雪球',
                        'bilibili.com': 'B站', 'sspai.com': '少数派',
                        'mnw.cn': '闽南网', 'ask.zol.com.cn': 'ZOL问答',
                        'ifanr.com': '爱范儿', '36kr.com': '36氪',
                    };
                    return map[domain] || domain;
                };

                // 扫描页面所有外部链接，侧边栏链接均为 doubao.com 内部链接可安全排除
                for (const a of document.querySelectorAll('a[href]')) {
                    const url = a.href;
                    if (!url || url.startsWith('javascript:') || url === '#') continue;
                    if (url.includes('doubao.com') || url.includes('bytedance.com')) continue;
                    if (/\.(jpg|jpeg|png|gif|webp|svg|ico|bmp)(\?|$)/i.test(url)) continue;
                    if (seenUrls.has(url)) continue;

                    seenUrls.add(url);
                    const rawText = (a.innerText || '').trim();
                    const title = rawText.split(String.fromCharCode(10))[0].trim();
                    const domain = extractDomain(url);
                    const source = formatSource(domain);

                    results.push({ index: index++, title, url, source });
                }

                return results;
            }"""
            scroll_probe_script = """() => {
                const W = window.innerWidth;
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0'
                    );
                };
                const isExternalReferenceLink = (a) => {
                    try {
                        const url = a.href || '';
                        if (!url || url.startsWith('javascript:') || url === '#') return false;
                        if (url.includes('doubao.com') || url.includes('bytedance.com')) return false;
                        if (/\\.(jpg|jpeg|png|gif|webp|svg|ico|bmp)(\\?|$)/i.test(url)) return false;
                        return true;
                    } catch (_) {
                        return false;
                    }
                };
                const isReferenceScrollable = (el) => {
                    if (!el || el === document.body || el === document.documentElement) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.height >= 180 &&
                        rect.width >= 220 &&
                        rect.left >= W * 0.25 &&
                        rect.right >= W * 0.82 &&
                        rect.width <= W * 0.78 &&
                        (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                        el.scrollHeight > el.clientHeight + 8
                    );
                };
                const scoreCandidate = (el, linkHits) => {
                    const rect = el.getBoundingClientRect();
                    return linkHits * 10000 + rect.right * 3 - rect.left + Math.min(1800, el.scrollHeight - el.clientHeight);
                };

                const candidateHits = new Map();
                for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                    if (!isExternalReferenceLink(a) || !isVisible(a)) continue;
                    const linkRect = a.getBoundingClientRect();
                    if (linkRect.right < W * 0.55) continue;
                    let node = a.parentElement;
                    while (node && node !== document.body && node !== document.documentElement) {
                        if (isReferenceScrollable(node)) {
                            candidateHits.set(node, (candidateHits.get(node) || 0) + 1);
                            break;
                        }
                        node = node.parentElement;
                    }
                }

                let scrollable = null;
                let bestScore = -1;
                for (const [el, hits] of candidateHits.entries()) {
                    const score = scoreCandidate(el, hits);
                    if (score > bestScore) {
                        scrollable = el;
                        bestScore = score;
                    }
                }

                if (!scrollable) {
                    for (const el of Array.from(document.querySelectorAll('*'))) {
                        if (!isReferenceScrollable(el)) continue;
                        const score = scoreCandidate(el, 0);
                        if (score > bestScore) {
                            scrollable = el;
                            bestScore = score;
                        }
                    }
                }
                if (!scrollable) return null;
                const rect = scrollable.getBoundingClientRect();
                return {
                    x: rect.x,
                    y: rect.y,
                    width: rect.width,
                    height: rect.height,
                    scrollTop: Number(scrollable.scrollTop || 0),
                    scrollHeight: Number(scrollable.scrollHeight || 0),
                    clientHeight: Number(scrollable.clientHeight || 0),
                };
            }"""

            def collect_visible_references() -> list[dict]:
                try:
                    current = self.page.evaluate(collect_script)
                except Exception as exc:
                    self._reraise_stop_requested(exc)
                    return []
                return current if isinstance(current, list) else []

            seen_by_url: dict[str, dict] = {}

            def merge_references(items: list[dict]) -> None:
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url") or "").strip()
                    if not url:
                        continue
                    if url in seen_by_url:
                        continue
                    seen_by_url[url] = {
                        "index": 0,
                        "title": str(item.get("title") or "").strip(),
                        "url": url,
                        "source": str(item.get("source") or "").strip(),
                    }

            merge_references(collect_visible_references())

            stalled_passes = 0
            for _ in range(self._reference_wheel_max_passes(expected_count)):
                if expected_count > 0 and len(seen_by_url) >= expected_count:
                    break
                self._raise_if_stop_requested()
                metrics = self.page.evaluate(scroll_probe_script) or {}
                if not metrics:
                    break
                before_top = float(metrics.get("scrollTop", 0) or 0)
                remaining = max(
                    0.0,
                    float(metrics.get("scrollHeight", 0) or 0)
                    - float(metrics.get("clientHeight", 0) or 0)
                    - before_top,
                )
                if remaining <= 10:
                    break
                if not self._perform_auxiliary_wheel_pass(
                    box={
                        "x": float(metrics.get("x", 0) or 0),
                        "y": float(metrics.get("y", 0) or 0),
                        "width": float(metrics.get("width", 0) or 0),
                        "height": float(metrics.get("height", 0) or 0),
                    },
                    remaining=remaining,
                    clamp_to_input=False,
                    x_range=(0.48, 0.62),
                    y_range=(0.36, 0.72),
                ):
                    break
                self._cooperative_sleep_jittered(0.9, spread=0.24)
                merge_references(collect_visible_references())

                after_metrics = self.page.evaluate(scroll_probe_script) or {}
                after_top = float(after_metrics.get("scrollTop", before_top) or 0)
                after_remaining = max(
                    0.0,
                    float(after_metrics.get("scrollHeight", 0) or 0)
                    - float(after_metrics.get("clientHeight", 0) or 0)
                    - after_top,
                )
                if abs(after_top - before_top) <= 2:
                    stalled_passes += 1
                else:
                    stalled_passes = 0
                if after_remaining <= 10 or stalled_passes >= 2:
                    break

            merge_references(collect_visible_references())

            references = list(seen_by_url.values())
            for index, ref in enumerate(references, start=1):
                ref["index"] = index
            if expected_count > 0 and len(references) != expected_count:
                print(f"[{self.name}] 参考资料期望 {expected_count} 条，当前提取 {len(references)} 条")
            if references:
                print(f"[{self.name}] 提取到 {len(references)} 条平台抓取源")
            return references

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 平台抓取源提取失败: {exc}")
            return []

    def enable_deep_think(self) -> bool:
        try:
            self._raise_if_stop_requested()
            self._disable_captcha_pointer_intercept()
            self.check_for_interruption(check_input_visible=False)
            if not self.deep_think:
                return self._select_chat_mode(target="quick")
            if self._select_chat_mode(target="think"):
                print(f"[{self.name}] 已切换到思考模式")
                return True
            return False
        except InterruptionDetected:
            raise
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 深度思考按钮操作失败: {e}")
            return False
