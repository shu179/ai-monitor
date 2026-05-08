"""
百度文心一言 平台适配器
"""

import re
import time
import random
from .base import BasePlatform, InterruptionDetected


class WenxinPlatform(BasePlatform):

    use_external_chrome_cdp = True
    target_url = "https://yiyan.baidu.com/"
    input_selector = 'div[role="textbox"][contenteditable="true"][data-slate-editor="true"]'
    result_selector = '#answer_text_id, .custom-html.md-stream-desktop'
    new_chat_selector = 'img[alt="New Session Btn"]'
    chat_container_selector = '#DIALOGUE_CONTAINER_ID'
    prefer_last_result_block = True
    deep_think_menu_selector = '[class*="inputToolbarLeft"] [class*="item__"]'
    deep_think_menu_container_selector = '[class*="dtModeContainer__"]'
    deep_think_enable_item_selector = '[class*="dtModeItem__"]'
    generating_selector = '[data-auto-test="stop_response"], [class*="stopDealBtn__"], [class*="stopBtn__"], [class*="pause__"]'
    send_button_selectors = [
        'div[class*="btnContainer__"]',
        'span[class*="sendInner__"]',
    ]
    always_headed = True  # 百度反爬强，无头模式登录态无法保持

    # 文心的聊天容器 id 含 "DIALOGUE"，会被通用 [class*="dialog"] 误判为弹窗
    _overlay_whitelist_selectors = [
        "#DIALOGUE_CONTAINER_ID",
        "[id*=\"DIALOGUE\"]",
        "[class*=\"dialogueContainer\"]",
        "[class*=\"DialogueContainer\"]",
        "[class*=\"chatContainer\"]",
        "[class*=\"ChatContainer\"]",
        "[class*=\"answerContainer\"]",
        "[class*=\"conversationContainer\"]",
    ]

    # 文心 UI 广泛使用含 "dialog/Dialog" 的 class 命名（非弹窗），排除该通用选择器
    _overlay_skip_selectors = {
        '[class*="dialog"],[class*="Dialog"]',
        '[class*="mask"],[class*="Mask"]',
    }

    def _get_overlay_candidate_selectors(self) -> list[str]:
        base = super()._get_overlay_candidate_selectors()
        return [s for s in base if s not in self._overlay_skip_selectors]

    def _detect_overlay(self) -> bool:
        try:
            return self.page.evaluate("""() => {
                const popupSelectors = [
                    '[class*="modal"],[class*="Modal"]',
                    '[class*="overlay"],[class*="Overlay"]',
                    '[class*="captcha"],[class*="Captcha"]',
                    '[class*="verify"],[class*="Verify"]',
                    '[role="dialog"],[role="alertdialog"]',
                    'iframe[src*="captcha"],iframe[src*="verify"]',
                    // 百度登录弹窗
                    '#passport-login-pop',
                    '.pop-mask',
                    '[id^="TANGRAM__PSP_"]',
                    '.tang-pass-pop-login',
                ];
                for (const sel of popupSelectors) {
                    try {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const s = window.getComputedStyle(el);
                            if (s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0') {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 100 && rect.height > 50) return true;
                            }
                        }
                    } catch(e) {}
                }
                return false;
            }""")
        except Exception:
            return False

    def _get_deep_think_state(self) -> dict:
        try:
            self._raise_if_stop_requested()
            btn = self.page.locator(self.deep_think_menu_selector).first
            self._wait_for_locator(btn, timeout_ms=3000)
            text = (btn.inner_text(timeout=1000) or "").strip()
            cls = btn.get_attribute("class") or ""
            active = (
                "active" in cls
                or "开启" in text
                or "自动" in text
            )
            return {
                "found": True,
                "active": active,
                "text": text,
                "class": cls,
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {
                "found": False,
                "active": False,
                "text": "",
                "class": "",
                "error": str(e),
            }

    def _open_deep_think_menu(self) -> bool:
        try:
            self._raise_if_stop_requested()
            trigger = self.page.locator(self.deep_think_menu_selector).first
            self._click_locator(trigger, timeout_ms=3000)
            self._wait_for_page_selector(self.deep_think_menu_container_selector, timeout_ms=3000)
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _click_deep_think_menu_item(self, target_text: str) -> bool:
        try:
            self._raise_if_stop_requested()
            items = self.page.locator(self.deep_think_enable_item_selector)
            count = items.count()
            for idx in range(count):
                self._raise_if_stop_requested()
                item = items.nth(idx)
                text = (item.inner_text(timeout=1000) or "").strip()
                if target_text not in text:
                    continue
                self._click_locator(item, timeout_ms=3000)
                return True
        except Exception as e:
            self._reraise_stop_requested(e)
            pass
        try:
            return bool(self.page.evaluate(
                """({itemSelector, targetText}) => {
                    const items = Array.from(document.querySelectorAll(itemSelector));
                    for (const item of items) {
                        const text = String(item.innerText || item.textContent || '').trim();
                        if (!text.includes(targetText)) continue;
                        item.click();
                        return true;
                    }
                    return false;
                }""",
                {"itemSelector": self.deep_think_enable_item_selector, "targetText": target_text},
            ))
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _switch_deep_think_mode(self, target_text: str) -> bool:
        self._raise_if_stop_requested()
        if not self._open_deep_think_menu():
            return False
        if not self._click_deep_think_menu_item(target_text):
            return False
        self._cooperative_sleep(0.6)
        state = self._get_deep_think_state()
        text = str(state.get("text") or "")
        if target_text == "关闭":
            return (not state.get("active")) or ("思考" == text)
        return target_text in text or state.get("active")

    def enable_deep_think(self) -> bool:
        self._raise_if_stop_requested()
        state = self._get_deep_think_state()
        if not state.get("found"):
            print(f"[{self.name}] 深度思考按钮未找到")
            return False

        current_text = str(state.get("text") or "")
        if not self.deep_think:
            if not state.get("active") and current_text == "思考":
                print(f"[{self.name}] 当前已是非深度思考模式")
                return False
            ok = self._switch_deep_think_mode("关闭")
            if ok:
                print(f"[{self.name}] 已关闭深度思考")
                return False
            print(f"[{self.name}] 关闭深度思考失败，当前状态: {current_text}")
            return False

        if "开启" in current_text:
            print(f"[{self.name}] 深度思考已激活")
            return True

        ok = self._switch_deep_think_mode("开启")
        if ok:
            print(f"[{self.name}] 深度思考已开启")
            return True
        print(f"[{self.name}] 深度思考开启失败，当前状态: {current_text}")
        return False

    def _dismiss_login_popup(self) -> bool:
        """检测并关闭百度登录弹窗，返回是否发现弹窗（需要人工处理）。"""
        try:
            self._raise_if_stop_requested()
            popup = self.page.locator('#passport-login-pop, .pop-mask, [id^="TANGRAM__PSP_"]').first
            if popup.is_visible(timeout=1000):
                return True
        except Exception as e:
            self._reraise_stop_requested(e)
            pass
        return False

    def start_new_chat(self) -> None:
        """点击新建对话前先检查登录弹窗，避免点击被遮挡导致30s超时。"""
        self._raise_if_stop_requested()
        if self._dismiss_login_popup():
            self._wait_for_human_resolution(
                "文心一言检测到登录弹窗（扫码登录），请扫码登录后点击「确定」继续。"
            )
        before = self._conversation_snapshot()
        last_error = "未找到可用的新会话按钮"
        if not self.new_chat_selector:
            if self._attempt_learned_selector_heal("new_chat_selector", label="新会话"):
                print(f"[{self.name}] 已通过 learned selector 开启新会话")
                return
            if self._attempt_selector_agent_heal("new_chat_selector", label="新会话"):
                print(f"[{self.name}] 已通过 selector_agent 开启新会话")
                return
            return
        try:
            btn = self.page.locator(self.new_chat_selector).first
            self._wait_for_locator(btn, timeout_ms=5000)
            self._click_locator(btn, timeout_ms=5000)
            if self._wait_and_confirm_new_chat(before, sleep_seconds=1.5):
                print(f"[{self.name}] 已开启新对话")
                return
            last_error = "已点击新会话按钮，但未确认切换到新会话"
        except Exception as e:
            self._reraise_stop_requested(e)
            last_error = str(e) or last_error
        if self._attempt_learned_selector_heal("new_chat_selector", label="新会话"):
            print(f"[{self.name}] 已通过 learned selector 开启新会话")
            return
        if self._attempt_selector_agent_heal("new_chat_selector", label="新会话"):
            print(f"[{self.name}] 已通过 selector_agent 开启新会话")
            return
        print(f"[{self.name}] 开启新对话失败，继续: {last_error}")

    def type_like_human(self, text: str) -> None:
        """文心使用 Slate 编辑器，必须走真实键盘输入，不能直接改 DOM。"""
        import sys

        select_all = "Meta+A" if sys.platform == "darwin" else "Control+A"
        self._raise_if_stop_requested()
        editor = self.page.locator(self.input_selector).first
        self._click_locator(editor, timeout_ms=5000)
        try:
            editor.focus(timeout=2000)
        except Exception as e:
            self._reraise_stop_requested(e)
            pass
        self._cooperative_sleep(0.15)

        # 先用真实快捷键清空，确保 Slate 内部状态同步更新。
        try:
            self.page.keyboard.press(select_all)
            self._cooperative_sleep(0.1)
            self.page.keyboard.press("Backspace")
            self._cooperative_sleep(0.15)
        except Exception as e:
            self._reraise_stop_requested(e)
            pass

        try:
            if (editor.inner_text(timeout=1000) or "").strip():
                self.page.keyboard.press(select_all)
                self._cooperative_sleep(0.1)
                self.page.keyboard.press("Delete")
                self._cooperative_sleep(0.15)
        except Exception as e:
            self._reraise_stop_requested(e)
            pass

        try:
            editor.press_sequentially(text, delay=random.randint(50, 100))
        except Exception as e:
            self._reraise_stop_requested(e)
            for char in text:
                self._raise_if_stop_requested()
                self.page.keyboard.type(char)
                self._cooperative_sleep(random.uniform(0.05, 0.1))
        self._last_prompt_text = text
        if not self._wait_for_input_value(text):
            raise RuntimeError("文心输入框未确认写入关键词，已取消本次提交")

    def _composer_snapshot(self) -> dict:
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate(
                """(inputSelector, resultSelector, chatContainerSelector) => {
                    const input = document.querySelector(inputSelector);
                    const inputText = input
                        ? String(input.innerText || input.textContent || input.value || "").trim()
                        : "";
                    let answers = Array.from(document.querySelectorAll(resultSelector))
                        .map((el) => String(el.innerText || "").trim())
                        .filter(Boolean);
                    if (answers.length <= 0) {
                        const container = document.querySelector(chatContainerSelector);
                        const text = container ? String(container.innerText || "").trim() : "";
                        if (text) answers = [text];
                    }
                    return {
                        inputText,
                        inputLength: inputText.length,
                        answerCount: answers.length,
                        answerLength: answers.join("\\n").length,
                    };
                }""",
                self.input_selector,
                self.result_selector,
                self.chat_container_selector,
            )
        except Exception as e:
            self._reraise_stop_requested(e)
            return {
                "inputText": "",
                "inputLength": 0,
                "answerCount": 0,
                "answerLength": 0,
            }

    def _has_submit_started_signal(self) -> bool | None:
        try:
            self._raise_if_stop_requested()
            return self._is_generating()
        except Exception as e:
            self._reraise_stop_requested(e)
            return None

    def _is_generating(self) -> bool:
        try:
            self._raise_if_stop_requested()
            return bool(
                self.page.evaluate(
                    """(generatingSelector) => {
                        const nodes = Array.from(document.querySelectorAll(generatingSelector));
                        return nodes.some((node) => {
                            const style = window.getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            return (
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                style.opacity !== '0' &&
                                rect.width > 0 &&
                                rect.height > 0
                            );
                        });
                    }""",
                    self.generating_selector,
                )
            )
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _get_generation_debug_state(self) -> dict:
        try:
            self._raise_if_stop_requested()
            snapshot = self._composer_snapshot() or {}
            return {
                "generating": bool(self._is_generating()),
                "input_length": int(snapshot.get("inputLength", 0) or 0),
                "answer_count": int(snapshot.get("answerCount", 0) or 0),
                "answer_length": int(snapshot.get("answerLength", 0) or 0),
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def is_generation_complete(self, page_text: str, start_time: float) -> bool:
        try:
            self._raise_if_stop_requested()
            has_answer = self.has_usable_answer_text(page_text or "")
            if not has_answer:
                return False
            return self.page.evaluate(
                """(generatingSelector) => {
                    const generating = Array.from(document.querySelectorAll(generatingSelector)).some((node) => {
                        const style = window.getComputedStyle(node);
                        const rect = node.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    });
                    return !generating;
                }""",
                self.generating_selector,
            )
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _get_answer_text(self) -> str:
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate(
                """({containerSel, resultSel, lastOnly, shouldScroll}) => {
                    const container = containerSel ? document.querySelector(containerSel) : document.body;
                    if (shouldScroll && container) {
                        const style = window.getComputedStyle(container);
                        if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                            container.scrollTop = container.scrollHeight;
                        } else {
                            window.scrollTo(0, document.body.scrollHeight);
                        }
                    } else if (shouldScroll) {
                        window.scrollTo(0, document.body.scrollHeight);
                    }

                    let blocks = Array.from(document.querySelectorAll(resultSel))
                        .map((el) => (el.innerText || '').trim())
                        .filter(Boolean);
                    if (blocks.length > 0) {
                        return (lastOnly ? blocks.slice(-1) : blocks).join('\\n');
                    }

                    return container ? String(container.innerText || '').trim() : '';
                }""",
                {
                    "containerSel": self.chat_container_selector,
                    "resultSel": self.result_selector,
                    "lastOnly": True,
                    "shouldScroll": self._consume_answer_read_scroll(),
                },
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return ""

    def _wait_for_submit_confirmation(self, before: dict, timeout: float = 4.0) -> bool:
        return self._wait_for_submit_start_signal(timeout=timeout)

    def _click_send_button_via_locator(self) -> bool:
        for selector in self.send_button_selectors:
            try:
                self._raise_if_stop_requested()
                locator = self.page.locator(selector)
                count = locator.count()
                if count <= 0:
                    continue
                for idx in range(count):
                    self._raise_if_stop_requested()
                    btn = locator.nth(idx)
                    try:
                        self._wait_for_locator(btn, timeout_ms=1200)
                    except Exception as e:
                        self._reraise_stop_requested(e)
                        continue
                    try:
                        btn.scroll_into_view_if_needed(timeout=1200)
                    except Exception:
                        pass
                    try:
                        self._click_locator(btn, timeout_ms=2000, force=True)
                        return True
                    except Exception as e:
                        self._reraise_stop_requested(e)
                        pass
                    try:
                        box = btn.bounding_box()
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            x = box["x"] + box["width"] / 2
                            y = box["y"] + box["height"] / 2
                            clicked = self.page.evaluate(
                                """({x, y}) => {
                                    const top = document.elementFromPoint(x, y);
                                    if (!top) return false;
                                    for (const node of [top, top.closest('div[class*="btnContainer__"]'), top.closest('span[class*="sendInner__"]')]) {
                                        if (!node) continue;
                                        try {
                                            node.dispatchEvent(new MouseEvent('pointerdown', {bubbles: true, cancelable: true, clientX: x, clientY: y}));
                                            node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, clientX: x, clientY: y}));
                                            node.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, clientX: x, clientY: y}));
                                            node.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: x, clientY: y}));
                                            node.click?.();
                                            return true;
                                        } catch (_) {}
                                    }
                                    return false;
                                }""",
                                {"x": x, "y": y},
                            )
                            if clicked:
                                return True
                            self.page.mouse.click(x, y)
                            return True
                    except Exception as e:
                        self._reraise_stop_requested(e)
                        pass
            except Exception as e:
                self._reraise_stop_requested(e)
                continue
        return False

    def _click_send_button_via_dom(self) -> bool:
        try:
            self._raise_if_stop_requested()
            return bool(
                self.page.evaluate(
                    """(inputSelector) => {
                        const input = document.querySelector(inputSelector);
                        if (!input) return false;

                        const clickNode = (node) => {
                            if (!node) return false;
                            const style = window.getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            if (
                                style.display === "none" ||
                                style.visibility === "hidden" ||
                                style.pointerEvents === "none" ||
                                rect.width <= 0 ||
                                rect.height <= 0
                            ) {
                                return false;
                            }
                            if (node.hasAttribute("disabled") || node.getAttribute("aria-disabled") === "true") {
                                return false;
                            }
                            try { node.scrollIntoView({block: "center", inline: "center"}); } catch (_) {}
                            for (const eventName of ["pointerdown", "mousedown", "mouseup", "click"]) {
                                try {
                                    node.dispatchEvent(new MouseEvent(eventName, {
                                        bubbles: true,
                                        cancelable: true,
                                        view: window,
                                    }));
                                } catch (_) {}
                            }
                            try { node.click(); } catch (_) {}
                            return true;
                        };

                        const collectCandidates = (root) => {
                            if (!root) return [];
                            return Array.from(
                                root.querySelectorAll(
                                    'div[class*="btnContainer__"], span[class*="sendInner__"]'
                                )
                            );
                        };

                        const scoreNode = (node) => {
                            const text = String(node.innerText || node.textContent || "").trim().toLowerCase();
                            const label = String(
                                node.getAttribute("aria-label") ||
                                node.getAttribute("title") ||
                                node.getAttribute("data-testid") ||
                                node.className ||
                                ""
                            ).trim().toLowerCase();
                            const marker = `${text} ${label}`;
                            let score = 0;
                            if (String(node.className || "").includes("btnContainer__")) score += 14;
                            if (String(node.className || "").includes("sendInner__")) score += 10;
                            if (text.length <= 6) score += 1;
                            const rect = node.getBoundingClientRect();
                            const inputRect = input.getBoundingClientRect();
                            const distance = Math.abs(rect.left - inputRect.right) + Math.abs(rect.top - inputRect.bottom);
                            score -= Math.min(5, distance / 200);
                            return score;
                        };

                        const roots = [];
                        let current = input.parentElement;
                        for (let i = 0; current && i < 5; i += 1, current = current.parentElement) {
                            roots.push(current);
                        }
                        roots.push(document.body);

                        const seen = new Set();
                        const candidates = [];
                        for (const root of roots) {
                            for (const node of collectCandidates(root)) {
                                if (seen.has(node)) continue;
                                seen.add(node);
                                candidates.push(node);
                            }
                        }

                        candidates.sort((a, b) => scoreNode(b) - scoreNode(a));
                        for (const node of candidates) {
                            const marker = String(
                                node.innerText ||
                                node.textContent ||
                                node.getAttribute("aria-label") ||
                                node.getAttribute("title") ||
                                node.getAttribute("data-testid") ||
                                node.className ||
                                ""
                            ).toLowerCase();
                            if (
                                !String(node.className || "").includes("btnContainer__") &&
                                !String(node.className || "").includes("sendInner__")
                            ) {
                                continue;
                            }
                            if (clickNode(node)) return true;
                        }
                        return false;
                    }""",
                    self.input_selector,
                )
            )
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _poll_until_complete(self, brand: str, on_rank, get_text=None, timeout: int = 300, min_wait: int = 2, keyword: str = "") -> None:
        if get_text is None:
            get_text = self._get_answer_text

        self._begin_answer_capture(keyword=keyword, brand=brand)
        start_time = time.monotonic()
        last_text = ""
        stable_count = 0
        while time.monotonic() - start_time < timeout:
            self._raise_if_stop_requested()
            try:
                self.check_for_interruption()
            except InterruptionDetected:
                print(f"[{self.name}] 人工干预完成，继续等待当前回答生成...")
                start_time = time.monotonic()
                stable_count = 0
                last_text = ""
                self._cooperative_sleep(2)
                continue

            self._schedule_answer_poll_read()
            page_text = get_text() or ""
            elapsed = time.monotonic() - start_time
            if elapsed < min_wait:
                last_text = page_text
                self._cooperative_sleep(1)
                continue

            if self._is_generating():
                stable_count = 0
                last_text = page_text
                self._cooperative_sleep(1)
                continue

            if not self.has_usable_answer_text(page_text, keyword=keyword, brand=brand):
                last_text = page_text
                self._cooperative_sleep(1)
                continue

            if page_text == last_text:
                stable_count += 1
            else:
                stable_count = 0
                last_text = page_text

            if stable_count >= 2:
                print(f"[{self.name}] 文心回答已稳定，判定生成完成")
                self._cooperative_sleep(0.8)
                self._schedule_answer_poll_read(force_scroll=True)
                final_text = get_text() or ""
                self.last_answer_text = final_text
                if not self.has_usable_answer_text(final_text, keyword=keyword, brand=brand):
                    self.last_error = "未获取到有效回答内容，可能回答尚未完全生成"
                    print(f"[{self.name}] {self.last_error}")
                    self._finish_answer_poll_metrics(
                        outcome="post_complete_unusable",
                        final_text=final_text,
                        keyword=keyword,
                        brand=brand,
                    )
                    return
                on_rank(self.parse_ranking(final_text, brand), final_text)
                self._finish_answer_poll_metrics(
                    outcome="ranked",
                    final_text=final_text,
                    keyword=keyword,
                    brand=brand,
                )
                return

            self._cooperative_sleep(1)

        print(f"[{self.name}] 等待生成超时（{timeout}s），尝试用当前内容解析排名")
        try:
            self._schedule_answer_poll_read(force_scroll=True)
            final_text = get_text() or ""
            self.last_answer_text = final_text
            if not self.has_usable_answer_text(final_text, keyword=keyword, brand=brand):
                self.last_error = "未获取到有效回答内容，可能回答尚未完全生成"
                print(f"[{self.name}] {self.last_error}")
                self._finish_answer_poll_metrics(
                    outcome="timeout_unusable",
                    final_text=final_text,
                    keyword=keyword,
                    brand=brand,
                )
                return
            on_rank(self.parse_ranking(final_text, brand), final_text)
            self._finish_answer_poll_metrics(
                outcome="timeout_ranked",
                final_text=final_text,
                keyword=keyword,
                brand=brand,
            )
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 超时兜底解析失败: {e}")
            self._finish_answer_poll_metrics(
                outcome="timeout_error",
                final_text=self.last_answer_text,
                keyword=keyword,
                brand=brand,
            )

    def submit_prompt(self) -> None:
        self._raise_if_stop_requested()
        before = self._composer_snapshot()
        strategies = [
            ("send_button", self._click_send_button_via_locator),
            ("send_button_dom", self._click_send_button_via_dom),
        ]

        for strategy_name, strategy in strategies:
            try:
                self._raise_if_stop_requested()
                if not strategy():
                    continue
                if self._wait_for_submit_confirmation(before):
                    print(f"[{self.name}] 已提交问题（策略: {strategy_name}）")
                    return
                print(f"[{self.name}] 提交策略 {strategy_name} 已执行，但未确认发送成功，继续尝试")
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 提交策略 {strategy_name} 失败: {e}")

        raise RuntimeError("文心一言发送按钮未触发，问题仍停留在输入框中")

    def extract_answer_references(self) -> list[dict]:
        """
        文心一言抓取源：
        1. 点击「深度思考已完成」展开思考区域
        2. 点击「参考N个网页」展开引用面板
        3. 扫描引用卡片
        """
        try:
            self._raise_if_stop_requested()

            # 第一步：如有深度思考区域，确保已展开（参考按钮在其内部）
            try:
                think_header = self.page.locator('[class*="topHeader__"]').filter(
                    has_text="深度思考已完成"
                ).last
                if think_header.count() > 0:
                    has_rotate = think_header.evaluate(
                        "el => el.className.includes('rotate')"
                    )
                    if not has_rotate:
                        print(f"[{self.name}] 点击展开深度思考区域...")
                        think_header.click(timeout=3000)
                        self._cooperative_sleep(1.5)
                    else:
                        print(f"[{self.name}] 深度思考区域已展开")
            except Exception as e:
                self._reraise_stop_requested(e)

            # 第二步：点击「参考N个网页」展开引用面板（两种结构）
            # 深度思考开：stepSiteRefCard__；深度思考关：titleText__ 在 container__ 内
            ref_btn = self.page.locator('[class*="stepSiteRefCard__"]').last
            if ref_btn.count() == 0:
                title_el = self.page.locator('[class*="titleText__"]').filter(
                    has_text=re.compile(r'参考\d+个网页')
                ).last
                if title_el.count() > 0:
                    ref_btn = title_el.locator('..')  # 点父容器
            if ref_btn.count() == 0:
                print(f"[{self.name}] 未找到参考网页按钮")
                return []
            btn_text = ref_btn.inner_text()
            print(f"[{self.name}] 找到引用按钮: {btn_text}，点击展开...")
            try:
                ref_btn.evaluate("el => el.click()")
                self._cooperative_sleep(2.0)
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 点击参考网页按钮失败: {e}")
                return []

            # 第三步：获取卡片信息，点击前3个捕获完整 URL
            card_info = self.page.evaluate("""() => {
                const results = [];
                for (const card of document.querySelectorAll('[class*="item__"]')) {
                    const titleEl = card.querySelector('[class*="titleInfo__"]');
                    const sourceEl = card.querySelector('[class*="siteText__"]');
                    const title = (titleEl ? titleEl.innerText || titleEl.textContent : '').trim().slice(0, 80);
                    const source = (sourceEl ? sourceEl.innerText || sourceEl.textContent : '').trim();
                    if (!title && !source) continue;
                    const rect = card.getBoundingClientRect();
                    results.push({
                        title,
                        source,
                        cardKey: Math.round(rect.top) + '-' + Math.round(rect.height),
                    });
                }
                return results;
            }""") or []

            if not card_info:
                print(f"[{self.name}] 未找到引用卡片")
                return []

            references = []
            for i, info in enumerate(card_info):
                source = info.get("source", "")
                fallback_url = f"https://{source}" if source and "." in source else ""
                card_key = info.get("cardKey", "")

                if i >= 3:
                    references.append({"index": i + 1, "title": info.get("title") or source, "url": fallback_url, "source": source})
                    continue

                try:
                    self._raise_if_stop_requested()
                    with self.page.context.expect_page(timeout=4000) as page_info:
                        self.page.evaluate("""(cardKey) => {
                            const cards = Array.from(document.querySelectorAll('[class*="item__"]'));
                            const card = cards.find(c => {
                                const r = c.getBoundingClientRect();
                                return Math.round(r.top) + '-' + Math.round(r.height) === cardKey;
                            });
                            if (card) card.click();
                        }""", card_key)
                    new_page = page_info.value
                    try:
                        new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    url = new_page.url
                    new_page.close()
                except Exception as e:
                    self._reraise_stop_requested(e)
                    url = fallback_url

                if url:
                    references.append({"index": i + 1, "title": info.get("title") or source, "url": url, "source": source})

            if references:
                print(f"[{self.name}] 提取到 {len(references)} 条平台抓取源")
                return references
            return []

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 平台抓取源提取失败: {exc}")
            return []
