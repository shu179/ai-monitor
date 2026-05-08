"""
DeepSeek 平台适配器
特点：流式输出，需要检测生成完成状态
"""

import time
import random
from typing import Tuple, Optional
from core.selector_heal.dom_probe import click_interactive_candidate
from .base import BasePlatform, InterruptionDetected


class DeepSeekPlatform(BasePlatform):
    """DeepSeek AI 平台监控"""

    target_url = "https://chat.deepseek.com/"
    input_selector = "textarea"
    result_selector = ".ds-message .ds-markdown"
    new_chat_selector = "button:has(path[d^='M8 0.599609'])"
    chat_container_selector = ".ds-virtual-list"
    think_content_selector = ".ds-think-content"
    deep_think_selector = "button:has-text('深度思考'), div:has-text('深度思考'), span:has-text('深度思考')"
    generation_pause_selector = 'path[d^="M2 4.88"], path[d^="M2 4.87988"], path[d^="M2 4.8"]'
    prefer_last_result_block = True
    use_automation_control_flag = True
    use_automation_user_agent = False
    use_automation_extra_headers = False
    use_automation_ignore_default_args = False
    use_automation_stealth_scripts = False
    use_automation_storage_warmup = False
    use_external_chrome_cdp = True
    prefer_headed_runtime = False
    external_chrome_launch_target_url = True
    skip_runtime_startup_goto = True

    def __init__(self, user_data_dir: str):
        super().__init__(user_data_dir)
        self._deep_think_cached_state: Optional[bool] = None

    def _conversation_snapshot(self) -> dict:
        try:
            return self.page.evaluate("""() => {
                const answers = Array.from(document.querySelectorAll('.ds-markdown'))
                    .filter((el) => !el.closest('.ds-think-content'))
                    .map((el) => (el.innerText || '').trim())
                    .filter(Boolean);
                const combined = answers.join('\\n');
                const input = document.querySelector('textarea');
                return {
                    answerCount: answers.length,
                    answerLength: combined.length,
                    inputValue: input ? String(input.value || '').trim() : '',
                    path: String(location.pathname || ''),
                    href: String(location.href || ''),
                };
            }""")
        except Exception:
            return {"answerCount": 0, "answerLength": 0, "inputValue": "", "path": "", "href": ""}

    def _click_new_chat_button(self, *, force: bool = False) -> bool:
        selectors = [
            self.new_chat_selector,
            "button[aria-label*='新建']",
            "button[aria-label*='新对话']",
            "button[title*='新建']",
            "button[title*='新对话']",
            "[data-testid*='new']",
            "[class*='new-chat']",
            "[class*='newChat']",
            "[class*='newConversation']",
        ]
        for selector in selectors:
            if not selector:
                continue
            try:
                self._raise_if_stop_requested()
                btn = self.page.locator(selector).first
                if btn.count() <= 0:
                    continue
                self._wait_for_locator(btn, timeout_ms=2000)
                btn.scroll_into_view_if_needed(timeout=2000)
                self._click_locator(btn, timeout_ms=2500, force=force)
                return True
            except Exception as e:
                self._reraise_stop_requested(e)
                continue
        return False

    def _click_new_chat_via_dom(self) -> bool:
        try:
            self._raise_if_stop_requested()
            return click_interactive_candidate(
                self.page,
                primary_selector=self.new_chat_selector,
                direct_selectors=[
                    "button[aria-label*='新建']",
                    "button[aria-label*='新对话']",
                    "button[title*='新建']",
                    "button[title*='新对话']",
                    "[data-testid*='new']",
                    "[class*='new-chat']",
                    "[class*='newChat']",
                    "[class*='newConversation']",
                ],
                text_keywords=["新建", "新对话", "new chat", "newchat", "new conversation", "chat"],
                icon_selectors=['path[d^="M8 0"], path[d^="M8 0.599609"]'],
            )
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _open_fresh_chat_fallback(self) -> bool:
        try:
            self._raise_if_stop_requested()
            self.page.goto(self.target_url, wait_until="domcontentloaded", timeout=15000)
            self._cooperative_sleep(random.uniform(1.0, 1.5))
            self._wait_for_page_selector(self.input_selector, timeout_ms=10000)
            return True
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _wait_until_new_chat_ready(self, before: dict, timeout: float = 6.0) -> bool:
        return super()._wait_until_new_chat_ready(before, timeout=timeout)

    def start_new_chat(self) -> None:
        """DeepSeek新建对话：SVG selector不稳定，优先用JS查找新建按钮"""
        try:
            self._raise_if_stop_requested()
            self._deep_think_cached_state = None
            before = self._conversation_snapshot()
            strategies = [
                ("locator", lambda: self._click_new_chat_button(force=False)),
                ("locator_force", lambda: self._click_new_chat_button(force=True)),
                ("dom", self._click_new_chat_via_dom),
                ("learned", lambda: self._attempt_learned_selector_heal("new_chat_selector", label="新对话")),
                ("selector_agent", lambda: self._attempt_selector_agent_heal("new_chat_selector", label="新对话")),
                ("goto_home", self._open_fresh_chat_fallback),
            ]
            last_error = "未找到可用的新对话按钮"
            for strategy_name, strategy in strategies:
                clicked = strategy()
                if not clicked:
                    continue
                self._cooperative_sleep(random.uniform(0.8, 1.5))
                self._wait_for_page_selector(self.input_selector, timeout_ms=10000)
                if self._wait_until_new_chat_ready(before):
                    print(f"[{self.name}] 已开启新对话")
                    return
                last_error = f"策略 {strategy_name} 已执行，但未确认切换到新对话"
            raise Exception(last_error)
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 开启新对话失败: {e}")
            raise

    def is_generation_complete(self, page_text: str, start_time: float) -> bool:
        try:
            self._raise_if_stop_requested()
            state = self._get_generation_signal_state() or {}
            if state.get("is_generating"):
                return False
            if state.get("send_ready"):
                return True
            if int(state.get("answer_count", 0) or 0) > 0 and int(state.get("answer_length", 0) or 0) > 0:
                return True
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _has_submit_started_signal(self) -> bool | None:
        try:
            self._raise_if_stop_requested()
            state = self._get_generation_signal_state() or {}
            if state.get("is_generating"):
                return True
            if int(state.get("answer_count", 0) or 0) > 0:
                return True
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            return None

    def _get_generation_signal_state(self) -> dict:
        try:
            self._raise_if_stop_requested()
            snapshot = self.page.evaluate(
                """({inputSel, resultSel, thinkSel, pauseSel}) => {
                    const normalize = (value) => String(value || '').trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.opacity !== '0' &&
                            style.pointerEvents !== 'none' &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };
                    const controls = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"], a[role="button"], [tabindex="0"]'));
                    const collectSignals = (el) => [
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('data-testid'),
                        el.innerText,
                        el.textContent,
                        el.className,
                    ].filter(Boolean).join(' ');
                    const controlFor = (el) => (
                        el?.closest?.('button, [role="button"], div[role="button"], a[role="button"], [tabindex="0"]')
                        || el
                    );
                    const escapeAttr = (value) => String(value || '').replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
                    const selectorHintsFor = (el) => {
                        const hints = [];
                        if (!el) return hints;
                        const tag = String(el.tagName || '').toLowerCase() || '*';
                        const aria = normalize(el.getAttribute?.('aria-label'));
                        const title = normalize(el.getAttribute?.('title'));
                        const testId = normalize(el.getAttribute?.('data-testid'));
                        const text = normalize(el.innerText || el.textContent);
                        if (aria) hints.push(`${tag}[aria-label="${escapeAttr(aria)}"]`);
                        if (aria.includes('停止')) hints.push(`${tag}[aria-label*="停止"]`);
                        if (/stop/i.test(aria)) hints.push(`${tag}[aria-label*="stop" i]`);
                        if (title) hints.push(`${tag}[title="${escapeAttr(title)}"]`);
                        if (title.includes('停止')) hints.push(`${tag}[title*="停止"]`);
                        if (testId) hints.push(`${tag}[data-testid="${escapeAttr(testId)}"]`);
                        if (text && text.length <= 20) hints.push(`text="${escapeAttr(text)}"`);
                        const cls = String(el.className || '');
                        if (cls.includes('ds-icon-button')) hints.push(`${tag}[class*="ds-icon-button"]`);
                        return Array.from(new Set(hints)).slice(0, 8);
                    };
                    const visibleControls = controls.filter((el) => isVisible(el));
                    const stopSignalPattern = /停止回答|停止生成|暂停生成|暂停|停止|stop generating|stop response|stop/i;
                    const sendSignalPattern = /发送消息|发送|send message|send/i;

                    const configuredStopSelectors = String(pauseSel || '')
                        .split(',')
                        .map((item) => item.trim())
                        .filter(Boolean);
                    const stopPathSelectors = Array.from(new Set([
                        ...configuredStopSelectors,
                        'path[d^="M2 4.88"]',
                        'path[d^="M2 4.87988"]',
                        'path[d^="M2 4.8"]',
                    ]));
                    const stopPathMarkers = [
                        '11.12V4.88Z',
                        '12.3199 2 11.12V4.88Z',
                    ];
                    const stopPathSet = new Set();
                    const configuredStopControlSet = new Set();
                    const pathLooksVisible = (path) => {
                        if (!path) return false;
                        const control = controlFor(path);
                        if (!control || !isVisible(control)) return false;
                        const svg = path.closest?.('svg');
                        if (svg && !isVisible(svg)) return false;
                        return isVisible(path) || isVisible(control);
                    };
                    const addConfiguredStopNode = (node) => {
                        if (!node) return;
                        const tag = String(node.tagName || '').toLowerCase();
                        if (tag === 'path') {
                            if (pathLooksVisible(node)) stopPathSet.add(node);
                            return;
                        }
                        const control = controlFor(node);
                        if (control && isVisible(control)) {
                            configuredStopControlSet.add(control);
                        }
                        try {
                            for (const path of node.querySelectorAll('svg path, path')) {
                                if (pathLooksVisible(path)) stopPathSet.add(path);
                            }
                        } catch (_) {}
                    };
                    for (const selector of stopPathSelectors) {
                        try {
                            for (const node of document.querySelectorAll(selector)) {
                                addConfiguredStopNode(node);
                            }
                        } catch (_) {}
                    }
                    for (const node of document.querySelectorAll('svg path')) {
                        const d = String(node.getAttribute('d') || '');
                        if (!d) continue;
                        if (
                            d.startsWith('M2 4.88') ||
                            d.startsWith('M2 4.87988') ||
                            d.startsWith('M2 4.8') ||
                            stopPathMarkers.some((marker) => d.includes(marker))
                        ) {
                            if (pathLooksVisible(node)) stopPathSet.add(node);
                        }
                    }
                    const stopPathDetails = Array.from(stopPathSet).map((path) => {
                        const d = String(path.getAttribute('d') || '');
                        const prefix = d.slice(0, 64);
                        const control = controlFor(path);
                        const tag = String(control?.tagName || '').toLowerCase() || '*';
                        return {
                            dPrefix: prefix,
                            selector: `path[d^="${escapeAttr(prefix)}"]`,
                            controlSelector: `${tag}:has(path[d^="${escapeAttr(prefix)}"])`,
                            controlTag: tag,
                            controlRole: String(control?.getAttribute?.('role') || ''),
                            controlText: normalize(control?.innerText || control?.textContent),
                            controlAriaLabel: normalize(control?.getAttribute?.('aria-label')),
                            controlTitle: normalize(control?.getAttribute?.('title')),
                            controlClassName: String(control?.className || ''),
                            selectorHints: [
                                `path[d^="${escapeAttr(prefix)}"]`,
                                `${tag}:has(path[d^="${escapeAttr(prefix)}"])`,
                                ...selectorHintsFor(control),
                            ],
                        };
                    });
                    const stopPathCount = stopPathDetails.length;

                    const textStopControls = visibleControls.filter((el) => {
                        const signals = collectSignals(el);
                        return stopSignalPattern.test(String(signals || ''));
                    });
                    const stopControlDetails = Array.from(new Set([
                        ...Array.from(configuredStopControlSet),
                        ...textStopControls,
                    ])).map((el) => ({
                        tag: String(el.tagName || '').toLowerCase(),
                        role: String(el.getAttribute('role') || ''),
                        text: normalize(el.innerText || el.textContent),
                        ariaLabel: normalize(el.getAttribute('aria-label')),
                        title: normalize(el.getAttribute('title')),
                        testId: normalize(el.getAttribute('data-testid')),
                        className: String(el.className || ''),
                        selectorHints: selectorHintsFor(el),
                    }));
                    const stopVisibleCount = stopControlDetails.length;
                    const sendVisibleCount = visibleControls.filter((el) => {
                        if (el.disabled) return false;
                        const signals = collectSignals(el);
                        if (!sendSignalPattern.test(String(signals || ''))) return false;
                        return !stopSignalPattern.test(String(signals || ''));
                    }).length;

                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputText = normalize(
                        input ? (input.value || input.innerText || input.textContent || '') : ''
                    );
                    const answers = resultSel
                        ? Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el) && !(thinkSel && el.closest(thinkSel)))
                            .map((el) => normalize(el.innerText || el.textContent || ''))
                            .filter(Boolean)
                        : [];

                    return {
                        stop_visible_count: stopVisibleCount,
                        stop_path_count: stopPathCount,
                        send_visible_count: sendVisibleCount,
                        input_length: inputText.length,
                        answer_count: answers.length,
                        answer_length: answers.join('\\n').length,
                        stop_controls: stopControlDetails.slice(0, 8),
                        stop_paths: stopPathDetails.slice(0, 8),
                        stop_selector_hints: Array.from(new Set([
                            ...stopPathDetails.flatMap((item) => item.selectorHints || []),
                            ...stopControlDetails.flatMap((item) => item.selectorHints || []),
                        ])).slice(0, 12),
                    };
                }""",
                {
                    "inputSel": self.input_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "pauseSel": self.generation_pause_selector or "",
                },
            ) or {}
            stop_visible_count = int(snapshot.get("stop_visible_count", 0) or 0)
            stop_path_count = int(snapshot.get("stop_path_count", 0) or 0)
            send_visible_count = int(snapshot.get("send_visible_count", 0) or 0)
            input_length = int(snapshot.get("input_length", 0) or 0)
            answer_count = int(snapshot.get("answer_count", 0) or 0)
            answer_length = int(snapshot.get("answer_length", 0) or 0)
            is_generating = stop_visible_count > 0 or stop_path_count > 0
            send_ready = send_visible_count > 0 and not is_generating
            return {
                "is_generating": is_generating,
                "send_ready": send_ready,
                "stop_visible_count": stop_visible_count,
                "stop_path_count": stop_path_count,
                "send_visible_count": send_visible_count,
                "input_length": input_length,
                "answer_count": answer_count,
                "answer_length": answer_length,
                "stop_controls": list(snapshot.get("stop_controls") or []),
                "stop_paths": list(snapshot.get("stop_paths") or []),
                "stop_selector_hints": list(snapshot.get("stop_selector_hints") or []),
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def _get_generation_debug_state(self) -> dict:
        try:
            self._raise_if_stop_requested()
            state = self._get_generation_signal_state() or {}
            snapshot = self._conversation_snapshot() or {}
            state["path"] = str(snapshot.get("path") or "")
            return state
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def _allow_text_stable_completion(self, debug_state: dict | None = None) -> bool:
        state = dict(debug_state or {})
        if bool(state.get("is_generating")):
            return False
        if int(state.get("stop_visible_count", 0) or 0) > 0:
            return False
        if int(state.get("stop_path_count", 0) or 0) > 0:
            return False
        return True

    def _get_deep_think_toggle_meta(self) -> dict:
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate("""() => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const compact = (value) => normalize(value).toLowerCase();
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0' &&
                        style.pointerEvents !== 'none' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };
                const collectText = (el) => normalize([
                    el.innerText,
                    el.textContent,
                    el.getAttribute?.('aria-label'),
                    el.getAttribute?.('title'),
                ].filter(Boolean).join(' '));
                const controlFor = (el) => (
                    el.closest('button, [role="button"], [role="switch"], label, [aria-pressed], [aria-checked]')
                    || el
                );
                const attrsFor = (el) => ({
                    pressed: String(el.getAttribute('aria-pressed') || '').toLowerCase(),
                    checked: String(el.getAttribute('aria-checked') || '').toLowerCase(),
                    state: String(el.getAttribute('data-state') || '').toLowerCase(),
                    active: String(el.getAttribute('data-active') || '').toLowerCase(),
                    selected: String(el.getAttribute('data-selected') || '').toLowerCase(),
                });
                const scoreCandidate = (el, text, rect) => {
                    const cls = String(el.className || '');
                    let score = 0;
                    if (cls.includes('ds-toggle-button')) score += 120;
                    if (cls.includes('ds-atom-button')) score += 80;
                    if (cls.includes('selected')) score += 20;
                    if (text === '深度思考' || text === '深度思考 深度思考') score += 80;
                    if (text.includes('深度思考') && !text.includes('智能搜索')) score += 40;
                    if (rect.width <= 220) score += 25;
                    if (rect.height <= 64) score += 25;
                    if (rect.width >= window.innerWidth * 0.5) score -= 160;
                    if (rect.height >= 120) score -= 80;
                    if (text.length >= 40) score -= 120;
                    return score;
                };

                const nodes = Array.from(document.querySelectorAll('button, [role="button"], [role="switch"], label, div, span, a'));
                const seen = new Set();
                const candidates = [];

                for (const node of nodes) {
                    const rawText = collectText(node);
                    if (!compact(rawText).includes('深度思考')) continue;
                    const control = controlFor(node);
                    if (!control || seen.has(control) || !isVisible(control)) continue;
                    seen.add(control);
                    const text = collectText(control) || rawText;
                    const rect = control.getBoundingClientRect();
                    const cls = String(control.className || '');
                    const attrs = attrsFor(control);
                    const style = window.getComputedStyle(control);
                    const colorText = [
                        String(style.color || ''),
                        String(style.backgroundColor || ''),
                        String(style.borderColor || ''),
                        String(style.boxShadow || ''),
                    ].join(' | ');
                    candidates.push({
                        control,
                        text,
                        className: cls,
                        attrs,
                        rect: {
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                            left: Math.round(rect.left),
                            top: Math.round(rect.top),
                        },
                        colorText,
                        score: scoreCandidate(control, text, rect),
                    });
                }

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) return {found: false, active: null, confidence: 'none'};

                const cls = String(best.className || '');
                const attrs = best.attrs || {};
                const colorText = String(best.colorText || '');
                const bluePattern = /(59,\\s*130,\\s*246|37,\\s*99,\\s*235|96,\\s*165,\\s*250|56,\\s*189,\\s*248|24,\\s*144,\\s*255|0,\\s*122,\\s*255)/;
                const activeByAria = attrs.pressed === 'true' || attrs.checked === 'true';
                const activeByData = ['active', 'on', 'checked', 'selected'].includes(attrs.state)
                    || attrs.active === 'true'
                    || attrs.selected === 'true';
                const activeByClass = cls.includes('ds-toggle-button--selected')
                    || /(active|selected|checked|current|on)/i.test(cls);
                const activeByBlue = bluePattern.test(colorText);
                const inactiveByAria = attrs.pressed === 'false' || attrs.checked === 'false';
                const inactiveByData = ['inactive', 'off', 'unchecked', 'unselected'].includes(attrs.state)
                    || attrs.active === 'false'
                    || attrs.selected === 'false';
                const inactiveByClass = cls.includes('ds-toggle-button') && !cls.includes('ds-toggle-button--selected');

                let active = null;
                let confidence = 'low';
                if (activeByAria || activeByData || activeByClass || activeByBlue) {
                    active = true;
                    confidence = cls.includes('ds-toggle-button--selected') ? 'high' : 'medium';
                } else if (inactiveByAria || inactiveByData || inactiveByClass) {
                    active = false;
                    confidence = inactiveByClass ? 'high' : 'medium';
                }

                return {
                    found: true,
                    active,
                    confidence,
                    text: best.text,
                    className: best.className,
                    colorText: best.colorText,
                    rect: best.rect,
                    score: best.score,
                };
            }""") or {"found": False, "active": None, "confidence": "none"}
        except Exception:
            return {"found": False, "active": None, "confidence": "none"}

    def _get_deep_think_state(self):
        """返回当前深度思考开关状态。active=None 表示按钮找到了，但状态判断不明确。"""
        return self._get_deep_think_toggle_meta()

    def _click_deep_think_toggle(self) -> bool:
        try:
            return bool(self.page.evaluate("""() => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const compact = (value) => normalize(value).toLowerCase();
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0' &&
                        style.pointerEvents !== 'none' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };
                const collectText = (el) => normalize([
                    el.innerText,
                    el.textContent,
                    el.getAttribute?.('aria-label'),
                    el.getAttribute?.('title'),
                ].filter(Boolean).join(' '));
                const controlFor = (el) => (
                    el.closest('button, [role="button"], [role="switch"], label, [aria-pressed], [aria-checked]')
                    || el
                );
                const scoreCandidate = (el, text, rect) => {
                    const cls = String(el.className || '');
                    let score = 0;
                    if (cls.includes('ds-toggle-button')) score += 120;
                    if (cls.includes('ds-atom-button')) score += 80;
                    if (text === '深度思考' || text === '深度思考 深度思考') score += 80;
                    if (text.includes('深度思考') && !text.includes('智能搜索')) score += 40;
                    if (rect.width <= 220) score += 25;
                    if (rect.height <= 64) score += 25;
                    if (rect.width >= window.innerWidth * 0.5) score -= 160;
                    if (rect.height >= 120) score -= 80;
                    if (text.length >= 40) score -= 120;
                    return score;
                };

                const nodes = Array.from(document.querySelectorAll('button, [role="button"], [role="switch"], label, div, span, a'));
                const seen = new Set();
                let best = null;

                for (const node of nodes) {
                    const rawText = collectText(node);
                    if (!compact(rawText).includes('深度思考')) continue;
                    const control = controlFor(node);
                    if (!control || seen.has(control) || !isVisible(control)) continue;
                    seen.add(control);
                    const text = collectText(control) || rawText;
                    const rect = control.getBoundingClientRect();
                    const score = scoreCandidate(control, text, rect);
                    if (!best || score > best.score) {
                        best = { control, score };
                    }
                }

                if (!best || !best.control) return false;
                const control = best.control;
                try { control.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
                for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                    try {
                        control.dispatchEvent(new MouseEvent(eventName, {bubbles: true, cancelable: true, view: window}));
                    } catch (_) {}
                }
                try { control.click(); } catch (_) {}
                return true;
            }"""))
        except Exception:
            return False

    def _wait_for_deep_think_state(self, expected_active: bool, timeout_seconds: float = 1.5):
        deadline = time.monotonic() + max(0.5, timeout_seconds)
        last_state = {"found": False, "active": None, "confidence": "none"}
        while time.monotonic() < deadline:
            self._raise_if_stop_requested()
            last_state = self._get_deep_think_state()
            if last_state.get("active") is expected_active:
                return last_state
            self._cooperative_sleep(0.15)
        return last_state

    def enable_deep_think(self) -> bool:
        try:
            self._raise_if_stop_requested()
            state = self._get_deep_think_state()
            if not state['found']:
                print(f"[{self.name}] 深度思考按钮未找到")
                return False

            if self.deep_think:
                if state.get('active') is True:
                    self._deep_think_cached_state = True
                    print(f"[{self.name}] 深度思考已激活")
                    return True
                if state.get('active') is None and self._deep_think_cached_state is True:
                    print(f"[{self.name}] 深度思考状态判断不明确，沿用会话缓存：已开启")
                    return True

                if not self._click_deep_think_toggle():
                    print(f"[{self.name}] 深度思考按钮点击失败")
                    return False
                self._cooperative_sleep(0.35)
                after_state = self._wait_for_deep_think_state(True)
                if after_state.get('active') is True:
                    self._deep_think_cached_state = True
                    print(f"[{self.name}] 已开启深度思考")
                    return True

                # DeepSeek 的开启态是蓝色按钮；若 DOM 一时判断不明，仍沿用当前会话缓存，避免下一轮反手点回去。
                self._deep_think_cached_state = True
                print(
                    f"[{self.name}] 深度思考点击后状态判断不明确，沿用会话缓存为已开启: "
                    f"confidence={after_state.get('confidence')} color={after_state.get('colorText', '')}"
                )
                return True
            else:
                if state.get('active') is False:
                    self._deep_think_cached_state = False
                    return False
                if state.get('active') is None and self._deep_think_cached_state is False:
                    return False

                if not self._click_deep_think_toggle():
                    print(f"[{self.name}] 深度思考按钮点击失败")
                    return False
                self._cooperative_sleep(0.35)
                after_state = self._wait_for_deep_think_state(False)
                if after_state.get('active') is False:
                    self._deep_think_cached_state = False
                    print(f"[{self.name}] 已关闭深度思考")
                    return False

                self._deep_think_cached_state = False
                print(
                    f"[{self.name}] 深度思考关闭后状态判断不明确，沿用会话缓存为已关闭: "
                    f"confidence={after_state.get('confidence')} color={after_state.get('colorText', '')}"
                )
                return False
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 深度思考按钮操作失败: {e}")
            return False

    def extract_answer_references(self) -> list[dict]:
        """
        DeepSeek 专用引用提取：点击"X 个网页"按钮展开引用列表，然后提取外部链接
        """
        try:
            self._raise_if_stop_requested()

            # 1. 先点击"X 个网页"按钮展开引用列表
            try:
                ref_button = self.page.locator('.f93f59e4').first
                if ref_button.count() > 0:
                    button_text = ref_button.inner_text()
                    print(f"[{self.name}] 找到引用按钮: {button_text}，点击展开...")
                    self._click_locator(ref_button, timeout_ms=3000)
                    self._cooperative_sleep(2.0)  # 等待引用列表加载
                else:
                    print(f"[{self.name}] 未找到引用按钮")
                    return []
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 点击引用按钮失败: {e}")
                return []

            # 2. 提取所有外部链接（排除 DeepSeek 内部链接）
            references = self.page.evaluate("""() => {
                const references = [];
                let index = 1;

                // 辅助函数：从 URL 提取域名
                const extractSource = (url) => {
                    try {
                        const urlObj = new URL(url);
                        let hostname = urlObj.hostname.replace(/^www\\./, '');
                        const parts = hostname.split('.');
                        if (parts.length >= 2) {
                            if (parts.length >= 3 && parts[parts.length - 2].length <= 3) {
                                return parts.slice(-3).join('.');
                            }
                            return parts.slice(-2).join('.');
                        }
                        return hostname;
                    } catch (e) {
                        return '';
                    }
                };

                // 辅助函数：格式化来源名称
                const formatSourceName = (domain) => {
                    const sourceMap = {
                        'sina.com.cn': '新浪网',
                        'sina.cn': '新浪网',
                        'qq.com': '腾讯新闻',
                        'zhihu.com': '知乎',
                        '36kr.com': '36氪',
                        'baidu.com': '百度',
                        'sohu.com': '搜狐',
                        '163.com': '网易',
                        'ifeng.com': '凤凰网',
                        'people.com.cn': '人民网',
                        'xinhuanet.com': '新华网',
                        'chinanews.com.cn': '中新网',
                        'thepaper.cn': '澎湃新闻',
                        'jiemian.com': '界面新闻',
                        'caixin.com': '财新网',
                        'pconline.com.cn': '太平洋电脑网',
                        'cnet.com': 'CNET',
                        'ai-indeed.com': 'AI Indeed',
                        'gzdaily.cn': '广州日报',
                        'myfone.blog': 'MyFone',
                        'syncfusion.com': 'Syncfusion',
                        'zhiding.cn': '至顶网',
                        'china.com.cn': '中国网',
                        'hbtv.com.cn': '湖北广电',
                        'jiemian.com': '界面新闻',
                        'investorscn.com': '投资者网',
                        'xnnews.com.cn': '西南新闻网',
                        'hgdaily.com.cn': '黄冈日报',
                        'chinaz.com': '站长之家',
                        '10jqka.com.cn': '同花顺',
                        'xtrb.cn': '湘潭日报',
                        'ithome.com': 'IT之家',
                        'yitangwl.com': '易唐网络',
                        'huaweicloud.com': '华为云',
                        'dawuhanapp.com': '大武汉',
                        'wuhan.gov.cn': '武汉市政府',
                        'whhlwdj.gov.cn': '武汉洪山区',
                    };
                    return sourceMap[domain] || domain;
                };

                // 获取所有链接
                const allLinks = document.querySelectorAll('a[href]');
                const seenUrls = new Set();

                allLinks.forEach((link) => {
                    const url = link.href;

                    // 过滤条件：
                    // 1. 排除 javascript: 和 #
                    // 2. 排除 DeepSeek 内部链接
                    // 3. 去重
                    if (!url || url.startsWith('javascript:') || url === '#') return;
                    if (url.includes('chat.deepseek.com')) return;
                    if (seenUrls.has(url)) return;

                    seenUrls.add(url);
                    const domain = extractSource(url);

                    // 尝试获取链接的标题（可能在父元素或相邻元素中）
                    let title = link.innerText?.trim() || '';
                    if (!title || title.length < 5) {
                        // 尝试从父元素获取更多上下文
                        const parent = link.closest('div, li, p');
                        if (parent) {
                            title = parent.innerText?.trim().substring(0, 100) || '';
                        }
                    }

                    references.push({
                        index: index++,
                        title: title || url,
                        url: url,
                        source: formatSourceName(domain),
                    });
                });

                return references;
            }""")

            if isinstance(references, list):
                print(f"[{self.name}] 提取到 {len(references)} 条外部引用链接")
                return references
            return []

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 引用信息提取失败: {exc}")
            return []

    def extract_body_references(self) -> list[dict]:
        """
        DeepSeek 正文引用源提取：抓取回答正文中的上标引用链接。
        只保留明确的引用标记（上标数字、脚注引用等），过滤叙述时提及的普通链接。
        """
        try:
            self._raise_if_stop_requested()
            references = self.page.evaluate("""() => {
                const references = [];
                const seenUrls = new Set();
                let index = 1;

                const extractSource = (url) => {
                    try {
                        const urlObj = new URL(url);
                        let hostname = urlObj.hostname.replace(/^www\\./, '');
                        const parts = hostname.split('.');
                        if (parts.length >= 3 && parts[parts.length - 2].length <= 3) {
                            return parts.slice(-3).join('.');
                        }
                        return parts.length >= 2 ? parts.slice(-2).join('.') : hostname;
                    } catch (e) { return ''; }
                };

                const formatSourceName = (domain) => {
                    const sourceMap = {
                        'sina.com.cn': '新浪网', 'sina.cn': '新浪网', 'qq.com': '腾讯新闻',
                        'zhihu.com': '知乎', '36kr.com': '36氪', 'baidu.com': '百度',
                        'sohu.com': '搜狐', '163.com': '网易', 'ifeng.com': '凤凰网',
                        'people.com.cn': '人民网', 'xinhuanet.com': '新华网',
                        'chinanews.com.cn': '中新网', 'thepaper.cn': '澎湃新闻',
                        'jiemian.com': '界面新闻', 'caixin.com': '财新网',
                        'pconline.com.cn': '太平洋电脑网', 'cnet.com': 'CNET',
                        'ithome.com': 'IT之家', '10jqka.com.cn': '同花顺',
                        'huaweicloud.com': '华为云', 'chinadaily.com.cn': '中国日报',
                    };
                    return sourceMap[domain] || domain;
                };

                // 判断一个链接是否是真正的引用标记（不是叙述性链接）
                const isCitationLink = (link) => {
                    const text = (link.innerText || link.textContent || '').trim();
                    const rawText = (link.innerText || link.textContent || '');

                    // 1. 在 <sup> 标签内 → 是上标引用
                    if (link.closest('sup')) return true;

                    // 2. 文本是纯数字或 [数字] 格式（含 [1] 格式）
                    const digits = text.replace(/[^0-9]/g, '');
                    const nonDigits = text.replace(/[0-9[\\]]/g, '').trim();
                    if (text.length > 0 && text.length <= 5 && digits.length > 0 && nonDigits.length === 0) return true;

                    // 3. DeepSeek 特有格式：链接文本去空白后以"-"开头且包含数字
                    const compact = rawText.replace(/\\s/g, '');
                    if (compact.length <= 4 && compact.charAt(0) === '-' && /[0-9]/.test(compact)) return true;

                    // 4. 有明确的引用属性
                    if (link.hasAttribute('data-footnote-ref')) return true;
                    if (link.getAttribute('role') === 'doc-noteref') return true;
                    const cls = String(link.className || '');
                    if (/cite|footnote|ref/i.test(cls)) return true;

                    // 5. 父元素是 <sup>
                    const parent = link.parentElement;
                    if (parent && parent.tagName === 'SUP') return true;

                    // 不是引用标记
                    return false;
                };

                // 只扫描回答正文（排除思考区域）
                const answerBlocks = Array.from(document.querySelectorAll('.ds-markdown'))
                    .filter(el => !el.closest('.ds-think-content'));

                for (const block of answerBlocks) {
                    for (const link of block.querySelectorAll('a[href]')) {
                        const url = link.href;
                        if (!url || url.startsWith('javascript:') || url === '#') continue;
                        if (url.includes('chat.deepseek.com')) continue;
                        if (!isCitationLink(link)) continue;
                        if (seenUrls.has(url)) continue;

                        seenUrls.add(url);
                        const domain = extractSource(url);
                        // 提取引用编号中的数字作为标识
                        const rawText = (link.innerText || link.textContent || '').trim();
                        const numMatch = rawText.match(/\\d+/);
                        const title = numMatch ? `[${numMatch[0]}]` : rawText || url;
                        references.push({
                            index: index++,
                            title: title,
                            url: url,
                            source: formatSourceName(domain),
                        });
                    }
                }

                return references;
            }""")

            if isinstance(references, list):
                print(f"[{self.name}] 正文引用源提取到 {len(references)} 条")
                return references
            return []

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 正文引用提取失败: {exc}")
            return []

    def _get_answer_text(self) -> str:
        """DeepSeek 只采最后一轮 assistant message 的 markdown 正文，排除思考和引用面板。"""
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel, shouldScroll}) => {
                    const normalize = (value) => String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
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
                    const isExcluded = (el) => {
                        if (!el) return false;
                        try {
                            if (thinkSel && el.closest(thinkSel)) return true;
                            return false;
                        } catch (_) {
                            return false;
                        }
                    };
                    const answerBlockSelector = '.ds-message .ds-markdown';
                    const collectPreferredBlocks = () => Array.from(
                        document.querySelectorAll(resultSel || answerBlockSelector)
                    ).filter((el) => isVisible(el) && !isExcluded(el) && el.closest('.ds-message'));

                    const removableSelectors = [
                        thinkSel,
                        'script',
                        'style',
                        'noscript',
                        'button',
                        'textarea',
                        'input',
                        'select',
                        'canvas',
                        'svg',
                        'iframe',
                        'video',
                        'audio',
                        'img',
                        'picture',
                        'figure',
                        'figcaption',
                        '[contenteditable="false"]',
                        '[aria-hidden="true"]',
                        '.ds-markdown-cite',
                        '[class*="ds-markdown-cite"]',
                        '[class*="search-view"]',
                        '[class*="searchView"]',
                    ].filter(Boolean);
                    const citationOnlyPattern = /^(?:-?\\s*\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?)(?:\\s*[、,，;；]\\s*(?:-?\\s*\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?))*$/;
                    const inlineCitationTags = new Set(['a', 'span', 'sup', 'em', 'strong', 'small', 'i', 'b']);
                    const hasBlockDescendant = (el) => Boolean(el && el.querySelector && el.querySelector('p, li, ul, ol, table, thead, tbody, tr, td, th, blockquote, pre, code, h1, h2, h3, h4, h5, h6'));
                    const isIconOnlyReferenceNode = (el) => {
                        if (!el || !el.parentElement) return false;
                        const tag = String(el.tagName || '').toLowerCase();
                        if (!['span', 'a', 'sup'].includes(tag)) return false;
                        const text = normalize(el.textContent || '');
                        const hasSvg = Boolean(el.querySelector && el.querySelector('svg'));
                        if (!hasSvg) return false;
                        const hiddenDash = Array.from(el.children || []).some((child) => {
                            if (!child || String(child.tagName || '').toLowerCase() !== 'span') return false;
                            const childText = normalize(child.textContent || '');
                            const style = child.getAttribute('style') || '';
                            return childText === '-' && /opacity\\s*:\\s*0/i.test(style);
                        });
                        if (hiddenDash) return true;
                        return text === '-' || text === '';
                    };
                    const isCitationOnlyNode = (el) => {
                        if (!el || !el.parentElement) return false;
                        const tag = String(el.tagName || '').toLowerCase();
                        const text = normalize(el.textContent || '');
                        if (!text || !citationOnlyPattern.test(text)) return false;
                        if (hasBlockDescendant(el)) return false;
                        if (inlineCitationTags.has(tag)) return true;
                        return el.children.length === 0;
                    };
                    const sanitizeElement = (el) => {
                        if (!el) return;
                        const tag = String(el.tagName || '').toLowerCase();
                        const text = normalize(el.textContent || '');
                        if (tag === 'sup' || isCitationOnlyNode(el) || isIconOnlyReferenceNode(el)) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && (el.closest('sup') || citationOnlyPattern.test(text))) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && !hasBlockDescendant(el) && !normalize(el.textContent || '') && !el.querySelector('img, picture')) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && !String(el.getAttribute('href') || '').trim()) {
                            el.replaceWith(...Array.from(el.childNodes));
                        }
                    };
                    const extractCleanBlockText = (node) => {
                        if (!node) return '';
                        const clone = node.cloneNode(true);
                        clone.querySelectorAll(removableSelectors.join(',')).forEach((el) => el.remove());
                        sanitizeElement(clone);
                        Array.from(clone.querySelectorAll('*')).reverse().forEach((el) => sanitizeElement(el));
                        return normalize(clone.innerText || clone.textContent || '');
                    };

                    const blocks = collectPreferredBlocks();
                    const grouped = [];
                    for (const block of blocks) {
                        const speech = block.closest('.ds-message') || block.parentElement;
                        const text = extractCleanBlockText(block);
                        if (!speech || !text) continue;
                        const last = grouped[grouped.length - 1];
                        if (!last || last.speech !== speech) {
                            grouped.push({ speech, texts: [text] });
                        } else {
                            last.texts.push(text);
                        }
                    }

                    if (grouped.length > 0) {
                        return grouped[grouped.length - 1].texts.join('\\n').trim();
                    }
                    return '';
                }""",
                {
                    "containerSel": self.chat_container_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "shouldScroll": self._consume_answer_read_scroll(),
                },
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._get_answer_text()

    def _capture_answer_snapshot(self) -> dict:
        """采集最后一轮 DeepSeek assistant message 的完整正文块，保留干净 HTML。"""
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel, shouldScroll}) => {
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

                    const normalize = (value) => String(value || '').trim();
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
                    const isExcluded = (el) => {
                        if (!el) return false;
                        try {
                            if (thinkSel && el.closest(thinkSel)) return true;
                            return false;
                        } catch (_) {
                            return false;
                        }
                    };
                    const answerBlockSelector = '.ds-message .ds-markdown';
                    const collectPreferredBlocks = () => Array.from(
                        document.querySelectorAll(resultSel || answerBlockSelector)
                    ).filter((el) => isVisible(el) && !isExcluded(el) && el.closest('.ds-message'));

                    const removableSelectors = [
                        thinkSel,
                        'script',
                        'style',
                        'noscript',
                        'button',
                        'textarea',
                        'input',
                        'select',
                        'canvas',
                        'svg',
                        'iframe',
                        'video',
                        'audio',
                        'img',
                        'picture',
                        'figure',
                        'figcaption',
                        '[contenteditable="false"]',
                        '[aria-hidden="true"]',
                        '.ds-markdown-cite',
                        '[class*="ds-markdown-cite"]',
                        '[class*="search-view"]',
                        '[class*="searchView"]',
                    ].filter(Boolean);
                    const allowedAttrs = new Set(['href', 'src', 'alt', 'title', 'colspan', 'rowspan']);
                    const citationOnlyPattern = /^(?:-?\\s*\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?)(?:\\s*[、,，;；]\\s*(?:-?\\s*\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?))*$/;
                    const inlineCitationTags = new Set(['a', 'span', 'sup', 'em', 'strong', 'small', 'i', 'b']);
                    const hasBlockDescendant = (el) => Boolean(el && el.querySelector && el.querySelector('p, li, ul, ol, table, thead, tbody, tr, td, th, blockquote, pre, code, h1, h2, h3, h4, h5, h6'));
                    const isIconOnlyReferenceNode = (el) => {
                        if (!el || !el.parentElement) return false;
                        const tag = String(el.tagName || '').toLowerCase();
                        if (!['span', 'a', 'sup'].includes(tag)) return false;
                        const text = normalize(el.textContent || '');
                        const hasSvg = Boolean(el.querySelector && el.querySelector('svg'));
                        if (!hasSvg) return false;
                        const hiddenDash = Array.from(el.children || []).some((child) => {
                            if (!child || String(child.tagName || '').toLowerCase() !== 'span') return false;
                            const childText = normalize(child.textContent || '');
                            const style = child.getAttribute('style') || '';
                            return childText === '-' && /opacity\\s*:\\s*0/i.test(style);
                        });
                        if (hiddenDash) return true;
                        return text === '-' || text === '';
                    };
                    const isCitationOnlyNode = (el) => {
                        if (!el || !el.parentElement) return false;
                        const tag = String(el.tagName || '').toLowerCase();
                        const text = normalize(el.textContent || '');
                        if (!text || !citationOnlyPattern.test(text)) return false;
                        if (hasBlockDescendant(el)) return false;
                        if (inlineCitationTags.has(tag)) return true;
                        return el.children.length === 0;
                    };
                    const cleanupEmptyNodes = (root) => {
                        const structuralSelector = 'table, thead, tbody, tr, th, td, ul, ol, li, pre, code, blockquote, hr, br';
                        for (let round = 0; round < 4; round += 1) {
                            let removed = false;
                            Array.from(root.querySelectorAll('*')).reverse().forEach((el) => {
                                if (!el || !el.parentElement) return;
                                if (el.matches(structuralSelector)) return;
                                const text = normalize(el.textContent || '');
                                if (text) return;
                                if (el.querySelector(structuralSelector)) return;
                                el.remove();
                                removed = true;
                            });
                            if (!removed) break;
                        }
                    };
                    const sanitizeElement = (el) => {
                        if (!el) return;
                        const tag = String(el.tagName || '').toLowerCase();
                        const text = normalize(el.textContent || '');
                        if (tag === 'sup' || isCitationOnlyNode(el) || isIconOnlyReferenceNode(el)) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && (el.closest('sup') || citationOnlyPattern.test(text))) {
                            el.remove();
                            return;
                        }
                        for (const attr of Array.from(el.attributes || [])) {
                            const name = String(attr.name || '').toLowerCase();
                            if (!allowedAttrs.has(name)) {
                                el.removeAttribute(attr.name);
                            }
                        }
                        if (tag === 'a') {
                            const href = String(el.getAttribute('href') || '').trim();
                            if (!hasBlockDescendant(el) && !normalize(el.textContent || '') && !el.querySelector('img, picture')) {
                                el.remove();
                                return;
                            }
                            if (!href) {
                                el.replaceWith(...Array.from(el.childNodes));
                            }
                        }
                    };
                    const extractCleanBlockText = (node) => {
                        if (!node) return '';
                        const clone = node.cloneNode(true);
                        clone.querySelectorAll(removableSelectors.join(',')).forEach((el) => el.remove());
                        sanitizeElement(clone);
                        Array.from(clone.querySelectorAll('*')).reverse().forEach((el) => sanitizeElement(el));
                        return normalize(clone.innerText || clone.textContent || '');
                    };
                    const sanitizeBlockHtml = (node) => {
                        if (!node) return '';
                        const clone = node.cloneNode(true);
                        clone.querySelectorAll(removableSelectors.join(',')).forEach((el) => el.remove());
                        sanitizeElement(clone);
                        Array.from(clone.querySelectorAll('*')).reverse().forEach((el) => sanitizeElement(el));
                        cleanupEmptyNodes(clone);
                        const text = normalize(clone.innerText || clone.textContent || '');
                        if (!text) return '';
                        const tag = String(clone.tagName || '').toLowerCase();
                        const unwrapTags = new Set(['div', 'section', 'article', 'aside', 'footer', 'details', 'summary']);
                        const attrs = Array.from(clone.attributes || []);
                        const flattenedHtml = (
                            unwrapTags.has(tag) && attrs.length === 0
                                ? String(clone.innerHTML || '')
                                : String(clone.outerHTML || '')
                        ).trim();
                        if (!flattenedHtml) return '';
                        return '<section class="answer-block">' + flattenedHtml + '</section>';
                    };

                    const blocks = collectPreferredBlocks();
                    const groups = [];
                    for (const block of blocks) {
                        const speech = block.closest('.ds-message') || block.parentElement;
                        const text = extractCleanBlockText(block);
                        if (!speech || !text) continue;
                        const last = groups[groups.length - 1];
                        if (!last || last.speech !== speech) {
                            const attrs = [
                                speech.getAttribute('data-id'),
                                speech.getAttribute('data-testid'),
                                speech.id,
                            ].filter(Boolean);
                            groups.push({
                                speech,
                                speechKey: attrs[0] || `speech:${groups.length}`,
                                blocks: [block],
                            });
                        } else {
                            last.blocks.push(block);
                        }
                    }

                    const current = groups.length > 0 ? groups[groups.length - 1] : null;
                    if (!current) {
                        return {
                            root_key: '',
                            blocks: [],
                            raw_text: '',
                            raw_html: '',
                        };
                    }

                    const snapshotBlocks = current.blocks.map((block, index) => {
                        const text = extractCleanBlockText(block);
                        if (!text) return null;
                        const blockKey = [
                            block.getAttribute('data-id'),
                            block.getAttribute('data-testid'),
                            block.id,
                        ].filter(Boolean)[0] || `${current.speechKey}:block:${index}`;
                        return {
                            key: blockKey,
                            order: index,
                            text,
                            html: sanitizeBlockHtml(block),
                        };
                    }).filter(Boolean);

                    return {
                        root_key: current.speechKey,
                        blocks: snapshotBlocks,
                        raw_text: snapshotBlocks.map((item) => item.text).join('\\n').trim(),
                        raw_html: snapshotBlocks.map((item) => item.html || '').filter(Boolean).join('\\n').trim(),
                    };
                }""",
                {
                    "containerSel": self.chat_container_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "shouldScroll": self._consume_answer_read_scroll(),
                },
            ) or {"root_key": "", "blocks": [], "raw_text": "", "raw_html": ""}
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._capture_answer_snapshot()

    def _get_answer_html(self) -> str:
        """DeepSeek DOM 复排只保留最后一轮 assistant message 的干净 markdown HTML。"""
        try:
            self._raise_if_stop_requested()
            snapshot = self._capture_answer_snapshot() or {}
            return str(snapshot.get("raw_html") or "").strip()
        except Exception as e:
            self._reraise_stop_requested(e)
            return ""
