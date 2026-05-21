"""
Kimi (Moonshot) 平台适配器
"""

import re
import time
import random

from .base import BasePlatform


class KimiPlatform(BasePlatform):
    """Kimi AI 平台监控"""

    use_external_chrome_cdp = True
    target_url = "https://www.kimi.com/"
    input_selector = '.chat-input-editor[contenteditable="true"], [data-lexical-editor="true"][contenteditable="true"]'
    result_selector = ".chat-content-item:not(.chat-content-item-user) .markdown"
    new_chat_selector = "button:has(svg[name='AddConversation']), [role='button']:has(svg[name='AddConversation']), svg[name='AddConversation']"
    chat_container_selector = ".chat-detail-main"
    think_content_selector = ""
    prefer_last_result_block = True
    _overlay_detection_enabled = False

    def _detect_overlay(self) -> bool:
        return False

    def _install_shared_js_helpers(self) -> None:
        super()._install_shared_js_helpers()
        # page 重建后跟着重装 wheel 冒泡拦截器
        self._install_wheel_bubble_guard()

    def _install_wheel_bubble_guard(self) -> None:
        """注入 wheel 冒泡拦截器，斩断 .chat-detail-main 到底/到顶时的整窗滚动。

        Kimi 的 .chat-detail-main 没拦 wheel；生成阶段容器已 scrollTop 到底，
        page.mouse.wheel 发真实 wheel 会因「容器吃不下」冒泡到 body / window，
        让整个浏览器窗口（连顶部菜单）抽动。

        策略：绑在 documentElement 的 capture phase，wheel target 在
        .chat-detail-main 内且容器已到底/到顶时 preventDefault + stopPropagation。
        这只影响"已经滚不动"的边界场景，对真实用户的正常滚动无任何影响（容器
        没到底时 guard 不动）。Playwright 真实 wheel 因此能保持 isTrusted=true
        的最强反检测信号。
        """
        if not getattr(self, "page", None):
            return
        try:
            self.page.evaluate("""(sel) => {
                if (window.__kimiWheelGuardInstalled) return true;
                window.__kimiWheelGuardInstalled = true;
                const containerSel = sel || '.chat-detail-main';
                const guard = (e) => {
                    const container = document.querySelector(containerSel);
                    if (!container || !container.contains(e.target)) return;
                    const maxTop = container.scrollHeight - container.clientHeight;
                    const atBottom = maxTop - container.scrollTop < 2;
                    const atTop = container.scrollTop < 2;
                    if ((e.deltaY > 0 && atBottom) || (e.deltaY < 0 && atTop)) {
                        e.preventDefault();
                        e.stopPropagation();
                    }
                };
                document.documentElement.addEventListener('wheel', guard, { capture: true, passive: false });
                return true;
            }""", self.chat_container_selector or ".chat-detail-main")
        except Exception as exc:
            self._reraise_stop_requested(exc)

    def _wheel_answer_view(self, *, delta_y: int | None = None, steps: int = 1) -> bool:
        """主路径走基类的真实 page.mouse.wheel（isTrusted=true，反检测信号最强），
        依赖 _install_wheel_bubble_guard 注入的 capture-phase 拦截器斩断冒泡。

        Guard 未装上（极少数：evaluate 失败/page 状态异常）时退化到合成
        WheelEvent(bubbles:false) + scrollTop 直接设值，保证最坏情况下也不抽搐。
        """
        if not getattr(self, "page", None):
            return False

        guard_active = False
        try:
            guard_active = bool(self.page.evaluate("() => !!window.__kimiWheelGuardInstalled"))
        except Exception:
            guard_active = False

        if guard_active:
            return super()._wheel_answer_view(delta_y=delta_y, steps=steps)

        # 兜底：合成 WheelEvent + 直接设 scrollTop。guard 没装上时也不让整窗滚。
        amount = int(delta_y if delta_y is not None else random.randint(760, 1180))
        try:
            result = self.page.evaluate("""(payload) => {
                const sel = payload.sel || '.chat-detail-main';
                const el = document.querySelector(sel);
                if (!el) return {ok: false};
                try {
                    el.dispatchEvent(new WheelEvent('wheel', {
                        deltaY: payload.delta,
                        bubbles: false,
                        cancelable: true,
                        view: window,
                    }));
                } catch (_) {}
                const before = Number(el.scrollTop || 0);
                const maxTop = Math.max(0, Number(el.scrollHeight || 0) - Number(el.clientHeight || 0));
                const next = Math.max(0, Math.min(maxTop, before + payload.delta));
                if (Math.abs(next - before) > 0.5) el.scrollTop = next;
                return {ok: true};
            }""", {"delta": amount, "sel": self.chat_container_selector or ".chat-detail-main"})
            return bool(isinstance(result, dict) and result.get("ok"))
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return False

    def _disable_landing_video_pointer_intercept(self) -> None:
        """Kimi 首页视频常驻且会拦截输入框点击，先禁用其指针事件。"""
        try:
            self._raise_if_stop_requested()
            self.page.evaluate("""() => {
                const videos = Array.from(document.querySelectorAll('video.video, video[src*="kimi-img.moonshot.cn"]'));
                for (const video of videos) {
                    const rect = video.getBoundingClientRect();
                    const style = window.getComputedStyle(video);
                    if (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden'
                    ) {
                        video.style.pointerEvents = 'none';
                        const parent = video.parentElement;
                        if (parent) {
                            parent.style.pointerEvents = 'none';
                        }
                    }
                }
            }""")
        except Exception as e:
            self._reraise_stop_requested(e)
            pass

    def start_new_chat(self) -> None:
        """Kimi 的新对话图标本身不可直接点击，改点最近的可点击父节点。"""
        self._disable_landing_video_pointer_intercept()
        clicked = False
        last_error = "未找到可用的新对话按钮"
        try:
            self._raise_if_stop_requested()
            before = self._conversation_snapshot()
            clicked = self.page.evaluate("""() => {
                const icon = document.querySelector("svg[name='AddConversation']");
                if (!icon) return false;
                const target = icon.closest('button, [role="button"], a, div');
                if (!target) return false;
                try { target.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
                for (const node of [target, icon]) {
                    if (!node) continue;
                    try {
                        node.dispatchEvent(new MouseEvent('pointerdown', {bubbles: true, cancelable: true, view: window}));
                        node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                        node.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                        node.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                    } catch (_) {}
                    try { node.click(); } catch (_) {}
                }
                return true;
            }""")
            if clicked:
                if self._wait_and_confirm_new_chat(before, sleep_seconds=random.uniform(0.8, 1.4)):
                    print(f"[{self.name}] 已开启新对话")
                    return
                clicked = False
                last_error = "已点击图标，但未确认切换到新会话"
            if not clicked:
                btn = self.page.locator(self.new_chat_selector).first
                btn.scroll_into_view_if_needed(timeout=3000)
                self._click_locator(btn, timeout_ms=5000, force=True)
                if self._wait_and_confirm_new_chat(before, sleep_seconds=random.uniform(0.8, 1.4)):
                    print(f"[{self.name}] 已开启新对话")
                    return
                clicked = False
                last_error = "已点击按钮，但未确认切换到新会话"
        except Exception as e:
            self._reraise_stop_requested(e)
            last_error = str(e) or last_error
        if not clicked and self._attempt_learned_selector_heal("new_chat_selector", label="新对话"):
            print(f"[{self.name}] 已通过 learned selector 开启新对话")
            return
        print(f"[{self.name}] 开启新对话失败，继续: {last_error}")

    def type_like_human(self, text: str) -> None:
        self._disable_landing_video_pointer_intercept()
        super().type_like_human(text)

    def enable_deep_think(self) -> bool:
        """Kimi 不支持深度思考，直接跳过。"""
        return False

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
                """({inputSel, resultSel}) => {
                    const h = window.__aiMonitorHelpers__ || {};
                    const normalize = h.trim || ((value) => String(value || '').trim());
                    const isVisible = h.isVisible || ((el) => Boolean(el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0));
                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputText = normalize(
                        input ? (input.value || input.innerText || input.textContent || '') : ''
                    );
                    const answers = resultSel
                        ? Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el))
                            .map((el) => normalize(el.innerText || el.textContent || ''))
                            .filter(Boolean)
                        : [];

                    const controls = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"], a[role="button"], .ds-icon-button, .ds-atom-button'))
                        .filter((el) => isVisible(el));
                    const signalText = (el) => [
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('data-testid'),
                        el.getAttribute('name'),
                        el.innerText,
                        el.textContent,
                    ].filter(Boolean).join(' ');
                    const iconNames = (el) => Array.from(el.querySelectorAll('svg'))
                        .filter((svg) => isVisible(svg))
                        .map((svg) => String(svg.getAttribute('name') || svg.getAttribute('data-icon') || '').trim().toLowerCase())
                        .filter(Boolean);

                    const stopTextPattern = /停止|暂停|stop|pause|停止回答|停止生成/i;
                    const sendTextPattern = /发送|send/i;
                    const iconNameMatches = (value, words) => {
                        const raw = String(value || '').trim();
                        if (!raw) return false;
                        const spaced = raw
                            .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
                            .replace(/[^A-Za-z0-9\u4e00-\u9fff]+/g, ' ')
                            .toLowerCase();
                        const tokens = spaced.split(/\\s+/).filter(Boolean);
                        return tokens.some((token) => words.includes(token));
                    };
                    const hasStopIconName = (names) => names.some((name) => iconNameMatches(name, ['stop', 'pause']));
                    const hasSendIconName = (names) => names.some((name) => iconNameMatches(name, ['send', 'submit', 'arrowup', 'up']));

                    let stopVisibleCount = 0;
                    let sendVisibleCount = 0;
                    let stopNamedIconCount = 0;
                    let sendNamedIconCount = 0;
                    const stopClassCount = Array.from(document.querySelectorAll('.send-button-container.stop'))
                        .filter((el) => isVisible(el)).length;
                    const sendButtonContainerCount = Array.from(document.querySelectorAll('.send-button-container'))
                        .filter((el) => isVisible(el) && !el.classList.contains('stop')).length;

                    for (const el of controls) {
                        const signals = signalText(el);
                        const names = iconNames(el);
                        const hasStopText = stopTextPattern.test(String(signals || ''));
                        const hasSendText = sendTextPattern.test(String(signals || ''));
                        const hasStopNamedIcon = hasStopIconName(names);
                        const hasSendNamedIcon = hasSendIconName(names);

                        if (hasStopText || hasStopNamedIcon) {
                            stopVisibleCount += 1;
                        }
                        if ((hasSendText || hasSendNamedIcon) && !hasStopText && !hasStopNamedIcon) {
                            if (!el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true') {
                                sendVisibleCount += 1;
                            }
                        }
                        if (hasStopNamedIcon) stopNamedIconCount += 1;
                        if (hasSendNamedIcon) sendNamedIconCount += 1;
                    }

                    const standaloneStopSvgs = Array.from(document.querySelectorAll('svg[name], svg[data-icon]'))
                        .filter((svg) => isVisible(svg) && !svg.closest('button, [role="button"], div[role="button"], a[role="button"], .ds-icon-button, .ds-atom-button'))
                        .map((svg) => String(svg.getAttribute('name') || svg.getAttribute('data-icon') || '').trim())
                        .filter((name) => iconNameMatches(name, ['stop', 'pause']));

                    return {
                        inputLength: inputText.length,
                        answerCount: answers.length,
                        answerLength: answers.join('\\n').length,
                        stopVisibleCount: stopVisibleCount + stopClassCount,
                        sendVisibleCount: sendVisibleCount + sendButtonContainerCount,
                        stopNamedIconCount: stopNamedIconCount + standaloneStopSvgs.length,
                        sendNamedIconCount,
                    };
                }""",
                {
                    "inputSel": self.input_selector or "",
                    "resultSel": self.result_selector or "",
                },
            ) or {}
            stop_visible_count = int(snapshot.get("stopVisibleCount", 0) or 0)
            send_visible_count = int(snapshot.get("sendVisibleCount", 0) or 0)
            stop_named_icon_count = int(snapshot.get("stopNamedIconCount", 0) or 0)
            send_named_icon_count = int(snapshot.get("sendNamedIconCount", 0) or 0)
            answer_count = int(snapshot.get("answerCount", 0) or 0)
            answer_length = int(snapshot.get("answerLength", 0) or 0)
            input_length = int(snapshot.get("inputLength", 0) or 0)
            is_generating = stop_visible_count > 0 or stop_named_icon_count > 0
            send_ready = (send_visible_count > 0 or send_named_icon_count > 0) and not is_generating
            return {
                "is_generating": is_generating,
                "send_ready": send_ready,
                "stop_visible_count": stop_visible_count,
                "send_visible_count": send_visible_count,
                "stop_named_icon_count": stop_named_icon_count,
                "send_named_icon_count": send_named_icon_count,
                "input_length": input_length,
                "answer_count": answer_count,
                "answer_length": answer_length,
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def _get_generation_debug_state(self) -> dict:
        try:
            self._raise_if_stop_requested()
            return self._get_generation_signal_state() or {}
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def _allow_text_stable_completion(self, debug_state: dict | None = None) -> bool:
        state = dict(debug_state or {})
        if bool(state.get("is_generating")):
            return False
        if int(state.get("stop_visible_count", 0) or 0) > 0:
            return False
        if int(state.get("stop_named_icon_count", 0) or 0) > 0:
            return False
        return True

    def _get_answer_text(self) -> str:
        """Kimi 只采最后一轮 assistant markdown，排除提问区和搜索网页面板。"""
        try:
            self._raise_if_stop_requested()
            self._consume_answer_read_scroll()
            return self.page.evaluate(
                """({containerSel, resultSel}) => {
                    const normalize = (value) => String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
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
                        return Boolean(
                            el.closest('.segment-user, .chat-content-item-user, .tool-content.webSearch, .search-site-tool-content, .side-console, .side-console-container, a.site')
                        );
                    };
                    const collectPreferredBlocks = () => Array.from(document.querySelectorAll(resultSel))
                        .filter((el) => isVisible(el) && !isExcluded(el) && el.closest('.chat-content-item'));

                    const removableSelectors = [
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
                        '.tool-content.webSearch',
                        '.search-site-tool-content',
                        '.side-console',
                        '.side-console-container',
                        'a.site',
                    ];
                    const sanitizeElement = (el) => {
                        if (!el) return;
                        const tag = String(el.tagName || '').toLowerCase();
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
                        const speech = block.closest('.chat-content-item:not(.chat-content-item-user)') || block.parentElement;
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
                },
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._get_answer_text()

    def _capture_answer_snapshot(self) -> dict:
        """采集 Kimi 最后一轮 assistant markdown 的干净正文快照。"""
        try:
            self._raise_if_stop_requested()
            self._consume_answer_read_scroll()
            return self.page.evaluate(
                """({containerSel, resultSel}) => {
                    const normalize = (value) => String(value || '').trim();
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
                        return Boolean(
                            el.closest('.segment-user, .chat-content-item-user, .tool-content.webSearch, .search-site-tool-content, .side-console, .side-console-container, a.site')
                        );
                    };
                    const collectPreferredBlocks = () => Array.from(document.querySelectorAll(resultSel))
                        .filter((el) => isVisible(el) && !isExcluded(el) && el.closest('.chat-content-item'));

                    const removableSelectors = [
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
                        '.tool-content.webSearch',
                        '.search-site-tool-content',
                        '.side-console',
                        '.side-console-container',
                        'a.site',
                    ];
                    const allowedAttrs = new Set(['href', 'src', 'alt', 'title', 'colspan', 'rowspan']);
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
                        for (const attr of Array.from(el.attributes || [])) {
                            const name = String(attr.name || '').toLowerCase();
                            if (!allowedAttrs.has(name)) {
                                el.removeAttribute(attr.name);
                            }
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
                        const speech = block.closest('.chat-content-item:not(.chat-content-item-user)') || block.parentElement;
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
                        return { root_key: '', blocks: [], raw_text: '', raw_html: '' };
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
                },
            ) or {"root_key": "", "blocks": [], "raw_text": "", "raw_html": ""}
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._capture_answer_snapshot()

    def _get_answer_html(self) -> str:
        """Kimi DOM 复排只保留最后一轮 assistant markdown 的干净 HTML。"""
        try:
            self._raise_if_stop_requested()
            snapshot = self._capture_answer_snapshot() or {}
            return str(snapshot.get("raw_html") or "").strip()
        except Exception as e:
            self._reraise_stop_requested(e)
            return ""

    def _click_kimi_reference_button(self) -> bool:
        """点开 Kimi 最新对话底部的「引用」按钮。

        旧实现单纯用 Playwright `.click()`，但 Kimi 的 .ref-action 是带双 Vue
        scope ID 的组件，CDP 真实点击偶尔不触发 onClick。改成：JS 评分锁定
        「最新 assistant 内的引用按钮」并打标记 → Playwright 真实点击 →
        失败回退 JS dispatchEvent 派发 pointerdown/mousedown/mouseup/click。
        """
        try:
            self._raise_if_stop_requested()
            marker = self.page.evaluate("""() => {
                // 优先取最新 assistant segment 内的 .ref-action，避免点到历史对话的按钮
                const latestSegment = (
                    Array.from(document.querySelectorAll('.chat-content-item-assistant')).pop() ||
                    Array.from(document.querySelectorAll('.segment-assistant')).pop()
                );
                let candidates = [];
                if (latestSegment) {
                    candidates = Array.from(latestSegment.querySelectorAll('div.ref-action, [class*="ref-action"]'));
                }
                if (candidates.length === 0) {
                    candidates = Array.from(document.querySelectorAll('div.ref-action, [class*="ref-action"]'));
                }
                // 过滤可见且尺寸合理的
                const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity) > 0 &&
                        rect.width > 0 && rect.height > 0
                    );
                };
                candidates = candidates.filter(isVisible);
                if (candidates.length === 0) {
                    document.querySelectorAll('[data-kimi-ref-click="1"]').forEach((el) => {
                        try { el.removeAttribute('data-kimi-ref-click'); } catch (_) {}
                    });
                    return {found: false};
                }
                // 最后一个一般就是最新的
                const best = candidates[candidates.length - 1];
                document.querySelectorAll('[data-kimi-ref-click="1"]').forEach((el) => {
                    try { el.removeAttribute('data-kimi-ref-click'); } catch (_) {}
                });
                try { best.setAttribute('data-kimi-ref-click', '1'); } catch (_) {}
                return {found: true, text: String(best.innerText || '').trim().slice(0, 30)};
            }""") or {}

            if not marker.get("found"):
                return False

            text_preview = str(marker.get("text") or "")[:30]
            target_selector = '[data-kimi-ref-click="1"]'

            try:
                locator = self.page.locator(target_selector).first
                self._click_locator(locator, timeout_ms=2500, force=False)
                print(f"[{self.name}] 已通过 Playwright 点击引用按钮: {text_preview}")
                return True
            except Exception as exc:
                self._reraise_stop_requested(exc)
                print(f"[{self.name}] Playwright 点击引用按钮失败，回退 JS 合成事件: {exc}")

            try:
                dispatched = self.page.evaluate("""() => {
                    const el = document.querySelector('[data-kimi-ref-click="1"]');
                    if (!el) return false;
                    for (const t of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                        try {
                            el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
                        } catch (_) {}
                    }
                    try { el.click(); } catch (_) {}
                    return true;
                }""")
                if dispatched:
                    print(f"[{self.name}] 已通过 JS 合成事件点击引用按钮: {text_preview}")
                    return True
                return False
            except Exception as exc:
                self._reraise_stop_requested(exc)
                return False
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return False

    def extract_answer_references(self) -> list[dict]:
        """
        Kimi 平台抓取源：先扫正文内联引用，再点击底部「引用」按钮展开面板扫取源列表。
        正文 a.rag-tag 在面板展开后会被 Vue 卸载，必须先于点击按钮执行。
        """
        try:
            self._raise_if_stop_requested()

            # 第一步：面板展开前先抓正文内联引用
            body_refs = self.page.evaluate("""() => {
                const results = [];
                const seenUrls = new Set();
                const extractDomain = (url) => {
                    try { return new URL(url).hostname.replace(/^www\\./, ''); } catch { return ''; }
                };
                for (const link of document.querySelectorAll('[data-site-name]')) {
                    const url = link.href || '';
                    if (!url || url.startsWith('javascript:') || url === '#') continue;
                    if (seenUrls.has(url)) continue;
                    seenUrls.add(url);
                    const source = (link.getAttribute('data-site-name') || '').trim() || extractDomain(url);
                    const rawIndex = (link.getAttribute('data-index') || '').split(',')[0].trim();
                    const label = rawIndex ? `[${rawIndex}]` : `[${results.length + 1}]`;
                    results.push({ url, source, label });
                }
                return results;
            }""") or []
            self.last_body_references = [
                {"index": i + 1, "title": r["label"], "url": r["url"], "source": r["source"]}
                for i, r in enumerate(body_refs)
            ]
            if self.last_body_references:
                print(f"[{self.name}] 正文引用源提取到 {len(self.last_body_references)} 条")
            else:
                print(f"[{self.name}] 未提取到正文引用源")

            # 第二步：点击引用按钮展开面板。Kimi 的 ref-action 是 Vue 组件（带双
            # data-v scope ID），Playwright 真实 click 偶尔不触发 onClick；先用
            # JS 评分标记目标 + Playwright 真实点击，失败再用 JS dispatchEvent 兜底
            if not self._click_kimi_reference_button():
                print(f"[{self.name}] 引用按钮点击失败/未找到")
                return []
            self._cooperative_sleep_jittered(2.0, spread=0.16)

            references = self.page.evaluate("""() => {
                const results = [];
                const seenUrls = new Set();
                let index = 1;

                const extractDomain = (url) => {
                    try {
                        const u = new URL(url);
                        return u.hostname.replace(/^www\\./, '');
                    } catch { return ''; }
                };

                // Kimi 把所有历史对话的 a.site-item 都预渲染在 DOM 里，全局扫会串。
                // 优先在「最新 assistant segment」或「右侧 .sites 面板」里找。
                let scope = null;
                const latestSegment = (
                    Array.from(document.querySelectorAll('.chat-content-item-assistant')).pop() ||
                    Array.from(document.querySelectorAll('.segment-assistant')).pop()
                );
                if (latestSegment && latestSegment.querySelectorAll('a.site-item').length > 0) {
                    scope = latestSegment;
                } else {
                    const sitesPanel = document.querySelector('.sites');
                    if (sitesPanel && sitesPanel.querySelectorAll('a.site-item').length > 0) {
                        scope = sitesPanel;
                    }
                }
                if (!scope) scope = document;

                for (const a of scope.querySelectorAll('a.site-item')) {
                    const url = a.href || '';
                    if (!url || url.startsWith('javascript:') || url === '#') continue;
                    if (seenUrls.has(url)) continue;
                    seenUrls.add(url);
                    const titleEl = a.querySelector('.site-title');
                    const title = (titleEl ? titleEl.innerText || titleEl.textContent : '').trim().slice(0, 80);
                    const source = extractDomain(url);
                    results.push({ index: index++, title: title || url, url, source });
                }
                return results;
            }""")

            if isinstance(references, list):
                print(f"[{self.name}] 提取到 {len(references)} 条平台抓取源")
                return references
            return []

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 平台抓取源提取失败: {exc}")
            return []

    def extract_body_references(self) -> list[dict]:
        # 正文引用已在 extract_answer_references() 中提前于面板展开前扫取并写入
        # self.last_body_references，此处直接返回避免重复扫描（面板展开后元素已卸载）
        return self.last_body_references
