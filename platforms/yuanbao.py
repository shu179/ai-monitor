"""
腾讯元宝 平台适配器
"""

import time
import random
from .base import BasePlatform, InterruptionDetected


class YuanbaoPlatform(BasePlatform):

    # 使用外部 Chrome + CDP 二次连接模式，避免 Patchright 在 headless
    # launch_persistent_context 阶段偶发 Browser.getWindowForTarget。
    use_external_chrome_cdp = True
    external_chrome_launch_target_url = True
    skip_runtime_startup_goto = True
    target_url = "https://yuanbao.tencent.com/chat"
    input_selector = 'textarea, div[contenteditable="true"]'
    result_selector = ".agent-chat__conv--ai__speech_show .hyc-content-md .hyc-common-markdown"
    new_chat_selector = ".yb-icon.icon-yb-ic_newchat_20"
    chat_container_selector = ".agent-chat__list__content-wrapper"
    think_content_selector = (
        '[class*="hyc-component-deepsearch-cot__think"], '
        '[class*="deepsearch-cot__think"], '
        '[class*="deepsearchCot__think"], '
        '.hyc-common-markdown-style-cot, '
        '[class*="agent-chat__conv--user"], '
        '[class*="agent-chat__bubble--user"], '
        '[class*="ToolbarSearchGuid"], '
        '[class*="ref-list"]'
    )
    prefer_last_result_block = True
    deep_think_selector = "button:has-text('Deep thinking'), div:has-text('Deep thinking'), span:has-text('Deep thinking'), button:has-text('深度思考'), div:has-text('深度思考')"
    reference_open_selector = (
        '#search-guide-tool[data-toolbar-type="citation"], '
        '[data-toolbar-type="citation"], '
        'div#search-guide-tool, '
        '[class*="ToolbarSearchGuid_searchGuidTool"], '
        '[class*="ToolbarSearchGuid_source"], '
        '[class*="ToolbarSearchGuid"][role="button"], '
        '[class*="ToolbarSearchGuid"], '
        'button:has-text("来源"), [role="button"]:has-text("来源"), '
        'button:has-text("引用"), [role="button"]:has-text("引用"), '
        'button:has-text("参考"), [role="button"]:has-text("参考"), '
        'button:has-text("网页"), [role="button"]:has-text("网页"), '
        'button:has-text("源"), [role="button"]:has-text("源")'
    )
    _overlay_detection_enabled = False

    @staticmethod
    def _debug_state_indicates_generation_active(debug_state: dict | None) -> bool:
        if not isinstance(debug_state, dict):
            return False
        return any(
            bool(debug_state.get(key))
            for key in ("stop_visible", "loading_visible", "stream_icon_visible")
        )

    def _allow_text_stable_completion(self, debug_state: dict | None = None) -> bool:
        """元宝的 done 块很干净，但流式中会只稳定半截；生成态未消失前不走文本稳定兜底。"""
        if self._debug_state_indicates_generation_active(debug_state):
            return False
        if debug_state:
            return True
        try:
            return not self._debug_state_indicates_generation_active(self._get_generation_debug_state())
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return False

    def start_new_chat(self) -> None:
        """元宝新建对话：span被nav遮挡，用JS直接触发点击"""
        before = self._conversation_snapshot()
        last_error = "未找到可用的新对话入口"
        try:
            self._raise_if_stop_requested()
            clicked = False
            if self.new_chat_selector:
                try:
                    btn = self.page.locator(self.new_chat_selector).first
                    self._wait_for_locator(btn, timeout_ms=3000)
                    self._click_locator(btn, timeout_ms=3000)
                    clicked = True
                except Exception as e:
                    self._reraise_stop_requested(e)
                    clicked = self.page.evaluate("""(selector) => {
                        const el = document.querySelector(selector);
                        if (!el) return false;
                        el.click();
                        return true;
                    }""", self.new_chat_selector)
            if not clicked:
                clicked = bool(self.page.evaluate("""() => {
                    const span = document.querySelector('.yb-icon.icon-yb-ic_newchat_20');
                    if (!span) return false;
                    span.click();
                    return true;
                }"""))
            if clicked and self._wait_and_confirm_new_chat(before, sleep_seconds=random.uniform(1.0, 2.0)):
                print(f"[{self.name}] 已开启新对话")
                return
            if clicked:
                last_error = "已点击新对话入口，但未确认切换到新会话"
                clicked = False
            if not clicked and self._attempt_learned_selector_heal("new_chat_selector", label="新对话"):
                print(f"[{self.name}] 已通过 learned selector 开启新对话")
                return
            if self._open_fresh_chat_fallback():
                print(f"[{self.name}] 新对话按钮不可用，已回到首页")
                return
            raise RuntimeError(last_error)
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 开启新对话失败，继续: {last_error or e}")

    def _scroll_brand_into_view(self, brand: str) -> None:
        """元宝先滚到容器底部（最新回答在底部），再定位品牌词。"""
        try:
            self._raise_if_stop_requested()
            self._wheel_scroll_selector_to_end(
                self.chat_container_selector or ".agent-chat__list__content-wrapper",
                last=False,
                max_passes=5,
            )
            self._cooperative_sleep_jittered(0.5, spread=0.2)
        except Exception as e:
            self._reraise_stop_requested(e)
            pass
        super()._scroll_brand_into_view(brand)

    def _get_answer_text(self) -> str:
        """元宝一轮回答经常被拆成多个 markdown 块，这里取最后一轮回答的全部正文。"""
        try:
            self._raise_if_stop_requested()
            self._consume_answer_read_scroll()
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel}) => {
                    const normalize = (value) => String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                    const container = containerSel ? document.querySelector(containerSel) : document.body;

                    const isExcluded = (el) => {
                        if (!el) return false;
                        try {
                            return Boolean(thinkSel && el.closest(thinkSel));
                        } catch (_) {
                            return false;
                        }
                    };
                    const answerBlockSelector = '.agent-chat__conv--ai__speech_show .hyc-content-md .hyc-common-markdown';
                    const collectPreferredBlocks = () => {
                        const preferred = Array.from(document.querySelectorAll(
                            '.agent-chat__conv--ai__speech_show .hyc-content-md.hyc-content-md-done .hyc-common-markdown'
                        )).filter((el) => !isExcluded(el) && el.closest('.hyc-content-md'));
                        if (preferred.length > 0) return preferred;
                        return Array.from(document.querySelectorAll(resultSel || answerBlockSelector))
                            .filter((el) => !isExcluded(el) && el.closest('.hyc-content-md'));
                    };
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
                        '[class*="reference"]',
                        '[class*="Reference"]',
                        '[class*="citation"]',
                        '[class*="Citation"]',
                        '[class*="footnote"]',
                        '[class*="Footnote"]',
                        '[class*="quote-extra"]',
                        '[class*="source"]',
                        '[class*="Source"]',
                        '[class*="ref-list"]',
                        '[class*="ref-list__trigger"]',
                        '[data-num]',
                        '[data-idx-list]',
                        '[data-idx]',
                    ].filter(Boolean);
                    const citationOnlyPattern = /^(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?)(?:\\s*[、,，;；]\\s*(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?))*$/;
                    const inlineCitationTags = new Set(['a', 'span', 'sup', 'em', 'strong', 'small', 'i', 'b']);
                    const hasBlockDescendant = (el) => Boolean(el && el.querySelector && el.querySelector('p, li, ul, ol, table, thead, tbody, tr, td, th, blockquote, pre, code, h1, h2, h3, h4, h5, h6'));
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
                        if (tag === 'sup' || isCitationOnlyNode(el)) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && (el.closest('sup') || citationOnlyPattern.test(text))) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && !String(el.getAttribute('href') || '').trim()) {
                            el.replaceWith(...Array.from(el.childNodes));
                            return;
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
                    if (blocks.length <= 0) {
                        return container ? String(container.innerText || '').trim() : '';
                    }

                    const grouped = [];
                    for (const block of blocks) {
                        const speech = block.closest('.agent-chat__conv--ai__speech_show') || block.parentElement;
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

                    return blocks.map((el) => extractCleanBlockText(el)).filter(Boolean).join('\\n');
                }""",
                {
                    "containerSel": self.chat_container_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                },
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._get_answer_text()

    def _capture_answer_snapshot(self) -> dict:
        """只采最后一轮 AI speech 下的 markdown 块，避免局部渲染时丢全文。"""
        try:
            self._raise_if_stop_requested()
            self._consume_answer_read_scroll()
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel}) => {
                    const normalize = (value) => String(value || '').trim();
                    const compact = (value) => normalize(value).replace(/[\\s\\W_]+/g, '').toLowerCase();
                    const isExcluded = (el) => {
                        if (!el) return false;
                        try {
                            return Boolean(thinkSel && el.closest(thinkSel));
                        } catch (_) {
                            return false;
                        }
                    };
                    const answerBlockSelector = '.agent-chat__conv--ai__speech_show .hyc-content-md .hyc-common-markdown';
                    const collectPreferredBlocks = () => {
                        const preferred = Array.from(document.querySelectorAll(
                            '.agent-chat__conv--ai__speech_show .hyc-content-md.hyc-content-md-done .hyc-common-markdown'
                        )).filter((el) => !isExcluded(el) && el.closest('.hyc-content-md'));
                        if (preferred.length > 0) return preferred;
                        return Array.from(document.querySelectorAll(resultSel || answerBlockSelector))
                            .filter((el) => !isExcluded(el) && el.closest('.hyc-content-md'));
                    };
                    const sanitizeBlockHtml = (node) => {
                        if (!node) return '';
                        const clone = node.cloneNode(true);
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
                            '[class*="reference"]',
                            '[class*="Reference"]',
                            '[class*="citation"]',
                            '[class*="Citation"]',
                            '[class*="footnote"]',
                            '[class*="Footnote"]',
                            '[class*="quote-extra"]',
                            '[class*="source"]',
                            '[class*="Source"]',
                            '[class*="ref-list"]',
                            '[class*="ref-list__trigger"]',
                            '[data-num]',
                            '[data-idx-list]',
                            '[data-idx]',
                        ].filter(Boolean);
                        const allowedAttrs = new Set(['href', 'src', 'alt', 'title', 'colspan', 'rowspan']);
                        const citationOnlyPattern = /^(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?)(?:\\s*[、,，;；]\\s*(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?))*$/;
                        const inlineCitationTags = new Set(['a', 'span', 'sup', 'em', 'strong', 'small', 'i', 'b']);
                        const hasBlockDescendant = (el) => Boolean(el && el.querySelector && el.querySelector('p, li, ul, ol, table, thead, tbody, tr, td, th, blockquote, pre, code, h1, h2, h3, h4, h5, h6'));
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
                            if (tag === 'sup' || isCitationOnlyNode(el)) {
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

                        clone.querySelectorAll(removableSelectors.join(',')).forEach((el) => el.remove());
                        sanitizeElement(clone);
                        Array.from(clone.querySelectorAll('*')).reverse().forEach((el) => sanitizeElement(el));
                        cleanupEmptyNodes(clone);
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
                    if (blocks.length <= 0) {
                        return {
                            root_key: '',
                            blocks: [],
                            raw_text: '',
                            raw_html: '',
                        };
                    }

                    const buildSpeechKey = (speech, fallbackIndex) => {
                        if (!speech) return `speech:${fallbackIndex}`;
                        const attrs = [
                            speech.getAttribute('data-id'),
                            speech.getAttribute('data-testid'),
                            speech.id,
                        ].filter(Boolean);
                        if (attrs.length > 0) return attrs[0];
                        return `speech:${fallbackIndex}`;
                    };

                    const groups = [];
                    for (const block of blocks) {
                        const speech = block.closest('.agent-chat__conv--ai__speech_show') || block.parentElement;
                        const text = extractCleanBlockText(block);
                        if (!speech || !text) continue;
                        const last = groups[groups.length - 1];
                        if (!last || last.speech !== speech) {
                            groups.push({
                                speech,
                                speechKey: buildSpeechKey(speech, groups.length),
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
                        };
                    }

                    const snapshotBlocks = current.blocks
                        .map((block, index) => {
                            const text = extractCleanBlockText(block);
                            if (!text) return null;
                            const compactText = compact(text);
                            const prefixKey = compactText ? compactText.slice(0, 48) : '';
                            const blockKey = [
                                block.getAttribute('data-id'),
                                block.getAttribute('data-testid'),
                                block.id,
                            ].filter(Boolean)[0] || (prefixKey ? `${current.speechKey}:block:${prefixKey}` : `${current.speechKey}:block:${index}`);
                            return {
                                key: blockKey,
                                order: index,
                                text,
                                html: sanitizeBlockHtml(block),
                            };
                        })
                        .filter(Boolean);

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
                },
            ) or {"root_key": "", "blocks": [], "raw_text": "", "raw_html": ""}
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._capture_answer_snapshot()

    def _get_stable_answer_text(
        self,
        get_text,
        *,
        keyword: str = "",
        brand: str = "",
        attempts: int = 8,
        interval: float = 1.0,
    ) -> tuple[str, bool]:
        """
        元宝不自动滚动且 done 块可能先稳定半截。
        最终补抓必须同时满足：生成态消失、done-only 文本可用、连续两次长度稳定。
        """
        best_text = ""
        best_score = -1
        stable_inactive_reads = 0
        last_inactive_compact = ""
        max_attempts = max(int(attempts or 0), 10)

        for index in range(max_attempts):
            self._raise_if_stop_requested()
            try:
                self.check_for_interruption()
            except InterruptionDetected:
                raise

            self._schedule_answer_poll_read(force_scroll=True)
            wait_seconds = 0.7 if index == 0 else max(0.8, float(interval or 0.0))
            self._cooperative_sleep(wait_seconds)

            try:
                snapshot = self._capture_answer_snapshot()
            except Exception as exc:
                self._reraise_stop_requested(exc)
                snapshot = {}
            captured_text = self._merge_answer_snapshot(
                snapshot,
                keyword=keyword,
                brand=brand,
            )
            if captured_text:
                answer_text = captured_text
            else:
                try:
                    answer_text = get_text() or ""
                except Exception as exc:
                    self._reraise_stop_requested(exc)
                    raise

            compact = self._normalize_compact_text(answer_text)
            if len(compact) > best_score:
                best_text = answer_text
                best_score = len(compact)

            debug_state = {}
            try:
                debug_state = self._get_generation_debug_state() or {}
            except Exception as exc:
                self._reraise_stop_requested(exc)
            if self._debug_state_indicates_generation_active(debug_state):
                stable_inactive_reads = 0
                last_inactive_compact = ""
                continue

            if not self.has_usable_answer_text(answer_text, keyword=keyword, brand=brand):
                stable_inactive_reads = 0
                last_inactive_compact = compact
                continue

            if compact and compact == last_inactive_compact:
                stable_inactive_reads += 1
            else:
                stable_inactive_reads = 1
                last_inactive_compact = compact

            if stable_inactive_reads >= 2:
                if index > 0:
                    print(f"[{self.name}] 元宝回答完成后稳定补抓，在第 {index + 1} 次确认后提取成功")
                return answer_text, True

        if best_text:
            reason = self.explain_unusable_answer_text(best_text, keyword=keyword, brand=brand) or "yuanbao-final-not-stable"
            preview = best_text[:300].replace("\n", "\\n")
            print(f"[{self.name}] 元宝回答最终补抓未稳定: {reason}; 预览: {preview}")
        return best_text, False

    def _get_answer_html(self) -> str:
        """元宝 DOM 复排只保留最终回答 markdown，不回退整段会话容器。"""
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate(
                """({resultSel, thinkSel, lastOnly}) => {
                    const h = window.__aiMonitorHelpers__ || {};
                    const normalize = h.normalize || ((value) => String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim());
                    const isVisible = h.isVisible || ((el) => Boolean(el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0));
                    const isExcluded = (el) => {
                        if (!el) return false;
                        try {
                            return Boolean(thinkSel && el.closest(thinkSel));
                        } catch (_) {
                            return false;
                        }
                    };
                    const answerBlockSelector = '.agent-chat__conv--ai__speech_show .hyc-content-md .hyc-common-markdown';
                    const collectPreferredBlocks = () => {
                        const doneBlocks = Array.from(document.querySelectorAll(
                            '.agent-chat__conv--ai__speech_show .hyc-content-md.hyc-content-md-done .hyc-common-markdown'
                        )).filter((el) => isVisible(el) && !isExcluded(el) && el.closest('.hyc-content-md'));
                        const blocks = doneBlocks.length > 0
                            ? doneBlocks
                            : Array.from(document.querySelectorAll(resultSel || answerBlockSelector))
                                .filter((el) => isVisible(el) && !isExcluded(el) && el.closest('.hyc-content-md'));
                        if (!lastOnly || blocks.length <= 0) return blocks;
                        const lastSpeech = blocks[blocks.length - 1]?.closest('.agent-chat__conv--ai__speech_show');
                        return lastSpeech
                            ? blocks.filter((el) => el.closest('.agent-chat__conv--ai__speech_show') === lastSpeech)
                            : [blocks[blocks.length - 1]];
                    };
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
                        '[class*="reference"]',
                        '[class*="Reference"]',
                        '[class*="citation"]',
                        '[class*="Citation"]',
                        '[class*="footnote"]',
                        '[class*="Footnote"]',
                        '[class*="quote-extra"]',
                        '[class*="source"]',
                        '[class*="Source"]',
                        '[class*="ref-list"]',
                        '[class*="ref-list__trigger"]',
                        '[class*="agent-chat__conv--user"]',
                        '[class*="agent-chat__bubble--user"]',
                        '[data-num]',
                        '[data-idx-list]',
                        '[data-idx]',
                    ].filter(Boolean);
                    const allowedAttrs = new Set(['href', 'src', 'alt', 'title', 'colspan', 'rowspan']);
                    const citationOnlyPattern = /^(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?)(?:\\s*[、,，;；]\\s*(?:\\[?\\d+\\]?|[①②③④⑤⑥⑦⑧⑨⑩]+|\\(\\d+\\)|\\d+[、.]?))*$/;
                    const inlineCitationTags = new Set(['a', 'span', 'sup', 'em', 'strong', 'small', 'i', 'b']);
                    const hasBlockDescendant = (el) => Boolean(el && el.querySelector && el.querySelector('p, li, ul, ol, table, thead, tbody, tr, td, th, blockquote, pre, code, h1, h2, h3, h4, h5, h6'));
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
                        if (tag === 'sup' || isCitationOnlyNode(el)) {
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
                            if (!href) {
                                el.replaceWith(...Array.from(el.childNodes));
                            }
                        }
                    };

                    const blocks = collectPreferredBlocks();
                    const htmlParts = [];
                    for (const node of blocks) {
                        const clone = node.cloneNode(true);
                        clone.querySelectorAll(removableSelectors.join(',')).forEach((el) => el.remove());
                        sanitizeElement(clone);
                        Array.from(clone.querySelectorAll('*')).reverse().forEach((el) => sanitizeElement(el));
                        cleanupEmptyNodes(clone);
                        const text = normalize(clone.innerText || clone.textContent || '');
                        if (!text) continue;
                        const tag = String(clone.tagName || '').toLowerCase();
                        const unwrapTags = new Set(['div', 'section', 'article', 'aside', 'footer', 'details', 'summary']);
                        const attrs = Array.from(clone.attributes || []);
                        const flattenedHtml = (
                            unwrapTags.has(tag) && attrs.length === 0
                                ? String(clone.innerHTML || '')
                                : String(clone.outerHTML || '')
                        ).trim();
                        if (flattenedHtml) {
                            htmlParts.push('<section class="answer-block">' + flattenedHtml + '</section>');
                        }
                    }
                    return htmlParts.join('\\n').trim();
                }""",
                {
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                    "lastOnly": bool(self.prefer_last_result_block),
                },
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return ""

    def _read_input_value(self) -> str:
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate(
                """(selector) => {
                    const input = document.querySelector(selector);
                    if (!input) return '';
                    return String(input.value || input.innerText || input.textContent || '').trim();
                }""",
                self.input_selector,
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return ""

    def _wait_for_submit_started(self, before_input: str, timeout: float = 8.0) -> bool:
        before_compact = self._normalize_compact_text(before_input)
        deadline = time.monotonic() + max(1.0, float(timeout or 0.0))
        while time.monotonic() < deadline:
            self._raise_if_stop_requested()
            signal = self._has_submit_started_signal()
            if signal:
                return True
            debug_state = self._get_generation_debug_state() or {}
            input_length = int(debug_state.get("input_length", 0) or 0)
            answer_count = int(debug_state.get("answer_count", 0) or 0)
            answer_length = int(debug_state.get("answer_length", 0) or 0)
            if input_length <= 0 and (answer_count > 0 or answer_length > 0):
                return True
            current_compact = self._normalize_compact_text(self._read_input_value())
            if before_compact and current_compact != before_compact and not current_compact:
                return True
            self._cooperative_sleep_jittered(0.2, spread=0.32)
        return False

    def _click_send_button_via_dom(self) -> bool:
        try:
            self._raise_if_stop_requested()
            result = self.page.evaluate(
                """(inputSel) => {
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
                    const getText = (el) => norm([
                        el.innerText,
                        el.textContent,
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.getAttribute('data-testid'),
                        el.className,
                    ].filter(Boolean).join(' '));
                    const inputRect = input.getBoundingClientRect();
                    const inputCenterY = inputRect.top + inputRect.height / 2;
                    const selectors = 'button, [role="button"], div[role="button"], span[role="button"]';
                    const candidates = [];
                    for (const el of document.querySelectorAll(selectors)) {
                        if (!isVisible(el) || isDisabled(el)) continue;
                        if (el === input || el.contains(input)) continue;
                        const text = getText(el);
                        if (/停止|重新生成|复制|分享|点赞|点踩|新对话|快速|思考|deep|联网|搜索/i.test(text)) {
                            continue;
                        }
                        const rect = el.getBoundingClientRect();
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const dx = centerX - inputRect.right;
                        const dy = Math.abs(centerY - inputCenterY);
                        let score = 0;
                        if (/发送|send|submit/i.test(text)) score += 220;
                        if (/arrow|plane|submit|send|enter/i.test(text)) score += 80;
                        if (dx >= -24 && dx <= 220) score += 80;
                        if (dy <= 120) score += 60;
                        if (rect.width <= 96 && rect.height <= 96) score += 20;
                        candidates.push({ el, text, score });
                    }
                    candidates.sort((a, b) => b.score - a.score);
                    const best = candidates[0];
                    if (!best || best.score < 120) {
                        return { clicked: false, reason: best ? `low-score:${best.score}` : 'no-candidate' };
                    }
                    best.el.click();
                    return { clicked: true, text: best.text, score: best.score };
                }""",
                self.input_selector,
            ) or {}
            if result.get("clicked"):
                print(f"[{self.name}] 已通过DOM点击发送按钮: {result.get('text') or 'icon-button'}")
                return True
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def submit_prompt(self) -> None:
        before_input = self._read_input_value()
        try:
            self._raise_if_stop_requested()
            if not self._normalize_compact_text(before_input):
                raise RuntimeError("元宝输入框为空，已取消提交")
            chat_input = self.page.locator(self.input_selector).first
            try:
                chat_input.focus(timeout=3000)
            except Exception as e:
                self._reraise_stop_requested(e)
            strategies = [
                ("input_enter", lambda: chat_input.press("Enter", timeout=3000), 3.0, 1.5),
                ("keyboard_enter", lambda: self.page.keyboard.press("Enter"), 2.5, 1.5),
            ]
            for strategy_name, submit_action, started_timeout, signal_timeout in strategies:
                submit_action()
                if self._wait_for_submit_started(before_input, timeout=started_timeout):
                    print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}）")
                    return
                if self._wait_for_submit_start_signal(timeout=signal_timeout):
                    print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}; submit-signal）")
                    return
                current_input = self._normalize_compact_text(self._read_input_value())
                if not current_input:
                    print(f"[{self.name}] 输入框已清空，按已发送处理（策略: {strategy_name}; input-cleared）")
                    return
            print(f"[{self.name}] Enter 提交未确认，尝试点击发送按钮兜底")
            if self._click_send_button_via_dom():
                if self._wait_for_submit_started(before_input, timeout=5.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: dom_send_button）")
                    return
                if self._wait_for_submit_start_signal(timeout=2.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: dom_send_button; submit-signal）")
                    return
                if not self._normalize_compact_text(self._read_input_value()):
                    print(f"[{self.name}] 输入框已清空，按已发送处理（策略: dom_send_button; input-cleared）")
                    return
            debug_state = self._get_generation_debug_state() or {}
            raise RuntimeError(
                "元宝未确认问题已发送，"
                f"input_length={int(debug_state.get('input_length', 0) or 0)}, "
                f"answer_count={int(debug_state.get('answer_count', 0) or 0)}, "
                f"answer_length={int(debug_state.get('answer_length', 0) or 0)}"
            )
        except InterruptionDetected:
            raise
        except Exception as e:
            self._reraise_stop_requested(e)
            raise RuntimeError(f"元宝提交失败: {e}") from e

    def is_generation_complete(self, page_text: str, start_time: float) -> bool:
        try:
            self._raise_if_stop_requested()
            return self.page.evaluate("""() => {
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
                if (Array.from(document.querySelectorAll('[class*="stopBtn"], [class*="stop-btn"], [class*="stopGenerate"], [class*="stop_btn"]')).some((el) => isVisible(el))) return false;
                if (Array.from(document.querySelectorAll('[class*="yb-loading"], [class*="hyc-loading"], [class*="chatLoading"]')).some((el) => isVisible(el))) return false;
                if (Array.from(document.querySelectorAll('rect[x="7.71448"]')).some((el) => isVisible(el.closest('svg') || el))) return false;
                return true;
            }""")
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def _has_submit_started_signal(self) -> bool | None:
        try:
            self._raise_if_stop_requested()
            return bool(self.page.evaluate("""() => {
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
                if (Array.from(document.querySelectorAll('[class*="stopBtn"], [class*="stop-btn"], [class*="stopGenerate"], [class*="stop_btn"]')).some((el) => isVisible(el))) return true;
                if (Array.from(document.querySelectorAll('[class*="yb-loading"], [class*="hyc-loading"], [class*="chatLoading"]')).some((el) => isVisible(el))) return true;
                if (Array.from(document.querySelectorAll('rect[x="7.71448"]')).some((el) => isVisible(el.closest('svg') || el))) return true;
                return false;
            }"""))
        except Exception as e:
            self._reraise_stop_requested(e)
            return None

    def _get_generation_debug_state(self) -> dict:
        try:
            self._raise_if_stop_requested()
            snapshot = self.page.evaluate(
                """({inputSel, resultSel, thinkSel}) => {
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
                    const stopVisible = Array.from(document.querySelectorAll(
                        '[class*="stopBtn"], [class*="stop-btn"], [class*="stopGenerate"], [class*="stop_btn"]'
                    )).some((el) => isVisible(el));
                    const loadingVisible = Array.from(document.querySelectorAll(
                        '[class*="yb-loading"], [class*="hyc-loading"], [class*="chatLoading"]'
                    )).some((el) => isVisible(el));
                    const streamIconVisible = Array.from(document.querySelectorAll('rect[x="7.71448"]'))
                        .some((el) => isVisible(el));
                    return {
                        stopVisible,
                        loadingVisible,
                        streamIconVisible,
                        inputLength: inputText.length,
                        answerCount: answers.length,
                        answerLength: answers.join('\\n').length,
                    };
                }""",
                {
                    "inputSel": self.input_selector or "",
                    "resultSel": self.result_selector or "",
                    "thinkSel": self.think_content_selector or "",
                },
            ) or {}
            return {
                "stop_visible": bool(snapshot.get("stopVisible")),
                "loading_visible": bool(snapshot.get("loadingVisible")),
                "stream_icon_visible": bool(snapshot.get("streamIconVisible")),
                "input_length": int(snapshot.get("inputLength", 0) or 0),
                "answer_count": int(snapshot.get("answerCount", 0) or 0),
                "answer_length": int(snapshot.get("answerLength", 0) or 0),
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def _detect_overlay(self) -> bool:
        """元宝有常驻的下载提示条，不做遮罩检测避免误判"""
        return False

    def _collect_reference_cards(self) -> list[dict]:
        try:
            self._raise_if_stop_requested()
            references = self.page.evaluate("""() => {
                const results = [];
                const seenUrls = new Set();
                let index = 1;

                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
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
                const isReferenceUrl = (url) => {
                    try {
                        const parsed = new URL(url, location.href);
                        if (!/^https?:$/.test(parsed.protocol)) return false;
                        if (/\\.(jpg|jpeg|png|gif|webp|svg|ico|bmp)(\\?|$)/i.test(parsed.href)) return false;
                        return true;
                    } catch (_) {
                        return false;
                    }
                };
                const extractDomain = (url) => {
                    try { return new URL(url, location.href).hostname.replace(/^www\\./, ''); } catch (_) { return ''; }
                };
                const absoluteUrl = (url) => {
                    try { return new URL(url, location.href).href; } catch (_) { return String(url || ''); }
                };
                const cardFor = (node) => {
                    let current = node;
                    let best = node;
                    for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
                        if (!isVisible(current)) continue;
                        const cls = String(current.className || '');
                        const rect = current.getBoundingClientRect();
                        const looksCardLike = (
                            /ref[_-]?card|reference|citation|source|card|doc-container|doc/i.test(cls) ||
                            current.hasAttribute('data-url') ||
                            current.hasAttribute('data-idx') ||
                            ['LI', 'ARTICLE'].includes(current.tagName)
                        );
                        if (looksCardLike && rect.width >= 120 && rect.height >= 24) {
                            best = current;
                            break;
                        }
                    }
                    return best || node;
                };
                const isReferenceContext = (node, card) => {
                    const marker = [
                        node.className,
                        card?.className,
                        node.getAttribute?.('data-idx'),
                        card?.getAttribute?.('data-idx'),
                        node.getAttribute?.('data-web-site-name'),
                        card?.getAttribute?.('data-web-site-name'),
                    ].filter(Boolean).join(' ');
                    return /ref[_-]?card|ref-list|reference|citation|source|site|web-site|ToolbarSearchGuid/i.test(String(marker || ''));
                };
                const textFrom = (root, selectors) => {
                    for (const selector of selectors) {
                        try {
                            const el = root.querySelector(selector);
                            const text = normalize(el?.innerText || el?.textContent || el?.getAttribute?.('title') || '');
                            if (text) return text;
                        } catch (_) {}
                    }
                    return '';
                };

                const nodes = Array.from(document.querySelectorAll('[data-url], [data-href], a[href]'));
                for (const node of nodes) {
                    const hasExplicitUrl = node.hasAttribute('data-url') || node.hasAttribute('data-href');
                    const rawUrl = node.getAttribute('data-url') || node.getAttribute('data-href') || node.href || node.getAttribute('href') || '';
                    const url = absoluteUrl(rawUrl);
                    if (!isReferenceUrl(url) || seenUrls.has(url)) continue;
                    const card = cardFor(node);
                    if (!isVisible(card)) continue;
                    if (!hasExplicitUrl && !isReferenceContext(node, card)) continue;
                    if (!hasExplicitUrl) {
                        try {
                            const host = new URL(url, location.href).hostname;
                            if (/yuanbao[.]tencent[.]com$/i.test(host)) continue;
                        } catch (_) {}
                    }
                    seenUrls.add(url);
                    const title = (
                        textFrom(card, [
                            '[class*="ref_card-title"]',
                            '[class*="title"]',
                            '[class*="Title"]',
                            'h1',
                            'h2',
                            'h3',
                            'h4',
                            'a'
                        ]) ||
                        normalize(node.innerText || node.textContent || node.getAttribute('title') || '')
                    ).slice(0, 100);
                    const source = (
                        textFrom(card, [
                            '[class*="ref_card-foot__source_txt"]',
                            '[class*="source"]',
                            '[class*="Source"]',
                            '[class*="site"]',
                            '[class*="Site"]'
                        ]) ||
                        normalize(card.getAttribute('data-web-site-name') || node.getAttribute('data-web-site-name') || '') ||
                        extractDomain(url)
                    ).slice(0, 80);
                    results.push({ index: index++, title: title || url, url, source });
                }
                return results;
            }""")
            return references if isinstance(references, list) else []
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return []

    @staticmethod
    def _merge_reference_cards(reference_groups: list[list[dict] | None]) -> list[dict]:
        merged: list[dict] = []
        seen_urls: set[str] = set()
        for references in reference_groups:
            for item in references or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                merged.append({
                    "index": len(merged) + 1,
                    "title": str(item.get("title") or url).strip() or url,
                    "url": url,
                    "source": str(item.get("source") or "").strip(),
                })
        return merged

    def _reference_panel_scroll_metrics(self) -> dict | None:
        try:
            self._raise_if_stop_requested()
            metrics = self.page.evaluate("""() => {
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
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const referenceNodes = Array.from(document.querySelectorAll(
                    '[data-url], [data-href], a[href], [class*="ref_card"], [class*="ref-list"], [class*="reference"], [class*="citation"], [class*="source"]'
                )).filter((el) => isVisible(el));
                const candidates = [];
                const pushCandidate = (el, seedScore = 0) => {
                    if (!el || !isVisible(el)) return;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const scrollHeight = Number(el.scrollHeight || 0);
                    const clientHeight = Number(el.clientHeight || 0);
                    if (scrollHeight <= clientHeight + 16 || clientHeight <= 24) return;
                    const marker = [
                        el.id,
                        el.className,
                        el.getAttribute && el.getAttribute('role'),
                        el.getAttribute && el.getAttribute('data-testid'),
                    ].filter(Boolean).join(' ');
                    if (el === document.body || el === document.documentElement) return;
                    if (/agent-dialogue__content|agent-chat__list__content-wrapper|agent-chat__list__content|chat-content/i.test(marker)) return;
                    const text = normalize(el.innerText || el.textContent || '');
                    let score = seedScore;
                    if (/(t-popup|popup|popper|reference|citation|source|ref-list|ref_card|docs)/i.test(marker)) score += 260;
                    if (/(来源|引用|参考|网页|资料|源)/.test(text)) score += 90;
                    score += Math.min(240, el.querySelectorAll('[data-url], [data-href], a[href]').length * 24);
                    if (rect.left >= window.innerWidth * 0.45) score += 70;
                    if (rect.width >= 180 && rect.height >= 80) score += 40;
                    if (/agent-chat__list__content-wrapper|chat-content/i.test(marker)) score -= 160;
                    if (style.overflowY === 'auto' || style.overflowY === 'scroll') score += 35;
                    candidates.push({el, score, rect, scrollHeight, clientHeight});
                };

                for (const node of referenceNodes) {
                    let current = node;
                    for (let depth = 0; current && depth < 10; depth += 1, current = current.parentElement) {
                        pushCandidate(current, Math.max(0, 80 - depth * 8));
                    }
                }
                for (const el of document.querySelectorAll('.t-popup, [class*="popup"], [class*="ref-list"], [class*="source"], [class*="reference"], [class*="citation"]')) {
                    pushCandidate(el, 80);
                }

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) return null;
                for (const el of document.querySelectorAll('[data-yb-reference-scroll-root="1"]')) {
                    try { el.removeAttribute('data-yb-reference-scroll-root'); } catch (_) {}
                }
                best.el.setAttribute('data-yb-reference-scroll-root', '1');
                return {
                    x: best.rect.left,
                    y: best.rect.top,
                    width: best.rect.width,
                    height: best.rect.height,
                    scrollTop: Number(best.el.scrollTop || 0),
                    scrollHeight: best.scrollHeight,
                    clientHeight: best.clientHeight,
                    score: best.score,
                };
            }""")
            return metrics if isinstance(metrics, dict) else None
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return None

    def _scroll_reference_panel_once(self, metrics: dict | None = None) -> bool:
        try:
            self._raise_if_stop_requested()
            payload = metrics if isinstance(metrics, dict) else {}
            result = self.page.evaluate("""(metrics) => {
                const el = document.querySelector('[data-yb-reference-scroll-root="1"]');
                if (!el) return {ok: false};
                const before = Number(el.scrollTop || 0);
                const clientHeight = Number(el.clientHeight || metrics.clientHeight || 0);
                const maxTop = Math.max(0, Number(el.scrollHeight || metrics.scrollHeight || 0) - clientHeight);
                const step = Math.max(180, Math.round(clientHeight * 0.82));
                el.scrollTop = Math.min(maxTop, before + step);
                try { el.dispatchEvent(new Event('scroll', {bubbles: true})); } catch (_) {}
                return {
                    ok: Number(el.scrollTop || 0) > before + 2,
                    before,
                    after: Number(el.scrollTop || 0),
                    maxTop,
                };
            }""", payload) or {}
            return bool(result.get("ok"))
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return False

    def _collect_reference_cards_with_scroll_sampling(
        self,
        initial_references: list[dict] | None = None,
        *,
        max_passes: int = 10,
    ) -> list[dict]:
        groups: list[list[dict] | None] = [initial_references if initial_references is not None else self._collect_reference_cards()]
        merged = self._merge_reference_cards(groups)
        stagnant_passes = 0
        for _ in range(max(0, int(max_passes or 0))):
            metrics = self._reference_panel_scroll_metrics()
            if not metrics:
                break
            remaining = max(
                0.0,
                float(metrics.get("scrollHeight", 0) or 0)
                - float(metrics.get("clientHeight", 0) or 0)
                - float(metrics.get("scrollTop", 0) or 0),
            )
            if remaining <= 12:
                break
            if not self._scroll_reference_panel_once(metrics):
                break
            self._cooperative_sleep_jittered(0.22, spread=0.22)
            before_count = len(merged)
            groups.append(self._collect_reference_cards())
            merged = self._merge_reference_cards(groups)
            stagnant_passes = stagnant_passes + 1 if len(merged) == before_count else 0
            if stagnant_passes >= 3:
                break
        return merged

    def _wait_for_reference_cards(self, *, timeout_seconds: float = 1.4) -> list[dict]:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds or 0.1))
        last_references: list[dict] = []
        while time.monotonic() < deadline:
            self._raise_if_stop_requested()
            last_references = self._collect_reference_cards()
            if last_references:
                return last_references
            self._cooperative_sleep_jittered(0.18, spread=0.2)
        return last_references

    def _scroll_latest_answer_toolbar_into_view(self) -> bool:
        try:
            self._raise_if_stop_requested()
            result = self.page.evaluate("""(containerSel) => {
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
                const isScrollable = (el) => {
                    if (!el || !isVisible(el)) return false;
                    return Number(el.scrollHeight || 0) > Number(el.clientHeight || 0) + 12;
                };
                const candidates = [];
                const addCandidate = (el, score) => {
                    if (!el || !isScrollable(el)) return;
                    if (el === document.body || el === document.documentElement) return;
                    if (candidates.some((item) => item.el === el)) return;
                    candidates.push({el, score});
                };

                for (const selector of [
                    containerSel,
                    '.agent-chat__list__content-wrapper',
                    '.agent-chat__list__content',
                    '#chat-content'
                ].filter(Boolean)) {
                    try {
                        const nodes = Array.from(document.querySelectorAll(selector));
                        nodes.forEach((el, index) => addCandidate(el, 500 - index));
                    } catch (_) {}
                }

                const latestAi = (
                    document.querySelector('.agent-chat__list__item--ai.agent-chat__list__item--last') ||
                    Array.from(document.querySelectorAll('.agent-chat__list__item--ai')).pop()
                );
                let current = latestAi;
                for (let depth = 0; current && depth < 9; depth += 1, current = current.parentElement) {
                    addCandidate(current, 420 - depth * 20);
                }

                candidates.sort((a, b) => b.score - a.score);
                const target = candidates[0]?.el || null;
                if (!target) return {found: false, scrolled: false};
                const before = Number(target.scrollTop || 0);
                const maxTop = Math.max(0, Number(target.scrollHeight || 0) - Number(target.clientHeight || 0));
                target.scrollTop = maxTop;
                try { target.dispatchEvent(new Event('scroll', {bubbles: true})); } catch (_) {}
                const after = Number(target.scrollTop || 0);
                return {
                    found: true,
                    scrolled: Math.abs(after - before) > 1,
                    before,
                    after,
                    maxTop,
                };
            }""", self.chat_container_selector or "")
            return bool(isinstance(result, dict) and result.get("found"))
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return False

    def _latest_reference_button_is_open(self) -> bool:
        try:
            self._raise_if_stop_requested()
            return bool(self.page.evaluate("""() => {
                const candidateSelector = [
                    '#search-guide-tool[data-toolbar-type="citation"]',
                    '[data-toolbar-type="citation"]',
                    'div#search-guide-tool',
                    '[class*="ToolbarSearchGuid_searchGuidTool"]',
                    '[class*="ToolbarSearchGuid_source"]'
                ].join(',');
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
                const controlFor = (el) => (
                    el.closest('#search-guide-tool, [data-toolbar-type="citation"], [class*="ToolbarSearchGuid_searchGuidTool"], button, [role="button"]') ||
                    el
                );
                const latestAi = (
                    document.querySelector('.agent-chat__list__item--ai.agent-chat__list__item--last') ||
                    Array.from(document.querySelectorAll('.agent-chat__list__item--ai')).pop()
                );
                const roots = [];
                if (latestAi) roots.push(latestAi);
                roots.push(document);
                const seen = new Set();
                for (const root of roots) {
                    let nodes = [];
                    try { nodes = Array.from(root.querySelectorAll(candidateSelector)); } catch (_) {}
                    for (const node of nodes.reverse()) {
                        const control = controlFor(node);
                        if (!control || seen.has(control) || !isVisible(control)) continue;
                        seen.add(control);
                        const marker = [
                            control.id,
                            control.className,
                            control.getAttribute && control.getAttribute('data-toolbar-type'),
                            control.getAttribute && control.getAttribute('data-state'),
                            control.getAttribute && control.getAttribute('aria-expanded'),
                        ].filter(Boolean).join(' ');
                        if (!/(search-guide-tool|ToolbarSearchGuid|citation|源|source)/i.test(marker)) continue;
                        return (
                            /t-popup-open|open|expanded/i.test(marker) ||
                            String(control.getAttribute('aria-expanded') || '').toLowerCase() === 'true'
                        );
                    }
                }
                return false;
            }"""))
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return False

    def _click_reference_open_button(self) -> bool:
        try:
            self._raise_if_stop_requested()
            result = self.page.evaluate("""() => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
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
                const isDisabled = (el) => (
                    el.disabled ||
                    el.getAttribute('disabled') !== null ||
                    String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
                );
                const collectText = (el) => normalize([
                    el.innerText,
                    el.textContent,
                    el.getAttribute && el.getAttribute('aria-label'),
                    el.getAttribute && el.getAttribute('title'),
                    el.getAttribute && el.getAttribute('data-testid'),
                    el.getAttribute && el.getAttribute('data-role'),
                    el.className,
                ].filter(Boolean).join(' '));
                const controlFor = (el) => (
                    el.closest('#search-guide-tool, [data-toolbar-type="citation"], [class*="ToolbarSearchGuid_searchGuidTool"], button, [role="button"]') ||
                    el
                );
                const scoreCandidate = (el, text, rect, rootScore, order) => {
                    const cls = String(el.className || '');
                    const toolbarType = String(el.getAttribute('data-toolbar-type') || '');
                    let score = rootScore + order;
                    if (el.id === 'search-guide-tool') score += 340;
                    if (/citation/i.test(toolbarType)) score += 320;
                    if (/ToolbarSearchGuid_searchGuidTool/i.test(cls)) score += 300;
                    if (/ToolbarSearchGuid_source/i.test(cls)) score += 220;
                    if (/ToolbarSearchGuid/i.test(cls)) score += 190;
                    if (/source|reference|citation|ref-list/i.test(cls)) score += 90;
                    if (/^源$/.test(text)) score += 180;
                    if (/(来源|引用|参考|网页|资料)/.test(text)) score += 130;
                    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') score += 70;
                    if (rect.width <= 160 && rect.height <= 72) score += 45;
                    if (rect.width >= window.innerWidth * 0.6) score -= 170;
                    if (rect.height >= 180) score -= 120;
                    if (text.length >= 80) score -= 180;
                    if (/停止|发送|重新生成|复制|分享|点赞|点踩|新对话|下载|登录|设置/i.test(text)) score -= 260;
                    return score;
                };
                const candidateSelector = [
                    '#search-guide-tool[data-toolbar-type="citation"]',
                    '[data-toolbar-type="citation"]',
                    'div#search-guide-tool',
                    '[class*="ToolbarSearchGuid_searchGuidTool"]',
                    '[class*="ToolbarSearchGuid_source"]'
                ].join(',');
                const latestAi = (
                    document.querySelector('.agent-chat__list__item--ai.agent-chat__list__item--last') ||
                    Array.from(document.querySelectorAll('.agent-chat__list__item--ai')).pop()
                );
                const latestToolbars = latestAi
                    ? Array.from(latestAi.querySelectorAll('.agent-chat__conv--ai__toolbar, .agent-chat__toolbar__right, .agent-chat__toolbar'))
                    : [];
                const allToolbars = Array.from(document.querySelectorAll('.agent-chat__conv--ai__toolbar, .agent-chat__toolbar__right, .agent-chat__toolbar'));
                const rootSpecs = [];
                if (latestToolbars.length > 0) rootSpecs.push({root: latestToolbars[latestToolbars.length - 1], score: 1800});
                if (latestAi) rootSpecs.push({root: latestAi, score: 1400});
                if (allToolbars.length > 0) rootSpecs.push({root: allToolbars[allToolbars.length - 1], score: 900});
                rootSpecs.push({root: document, score: 0});

                const seen = new Set();
                let best = null;
                for (const spec of rootSpecs) {
                    let nodes = [];
                    try {
                        nodes = Array.from(spec.root.querySelectorAll(candidateSelector));
                    } catch (_) {
                        nodes = [];
                    }
                    nodes.forEach((node, index) => {
                        const control = controlFor(node);
                        if (!control || seen.has(control) || !isVisible(control) || isDisabled(control)) return;
                        seen.add(control);
                        const text = collectText(control) || collectText(node);
                        const marker = [
                            text,
                            control.id,
                            control.getAttribute && control.getAttribute('data-toolbar-type'),
                            control.className,
                        ].filter(Boolean).join(' ');
                        if (!/(源|来源|引用|参考|网页|资料|search-guide-tool|ToolbarSearchGuid|source|reference|citation)/i.test(marker)) return;
                        const rect = control.getBoundingClientRect();
                        const score = scoreCandidate(control, marker, rect, spec.score, index);
                        if (!best || score > best.score) {
                            best = { control, text: marker, score };
                        }
                    });
                    if (best && spec.score >= 1400) break;
                }

                if (!best || best.score < 300) {
                    return {clicked: false, score: best ? best.score : 0, text: best ? best.text : ''};
                }
                const targets = [best.control];
                const source = best.control.querySelector && best.control.querySelector('[class*="ToolbarSearchGuid_source"]');
                if (source) targets.push(source);
                for (const target of targets) {
                    for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                        try {
                            target.dispatchEvent(new MouseEvent(eventName, {bubbles: true, cancelable: true, view: window}));
                        } catch (_) {}
                    }
                    try { target.click(); } catch (_) {}
                }
                return {clicked: true, score: best.score, text: best.text};
            }""") or {}
            if result.get("clicked"):
                print(f"[{self.name}] 已通过DOM点击源按钮: {str(result.get('text') or '')[:40]}")
                return True
            return False
        except Exception as exc:
            self._reraise_stop_requested(exc)
            return False

    def extract_answer_references(self) -> list[dict]:
        """
        元宝平台抓取源：点击「源」按钮展开面板，从卡片 data-url 属性读取链接。
        """
        try:
            self._raise_if_stop_requested()

            try:
                self._scroll_latest_answer_toolbar_into_view()
                self._cooperative_sleep_jittered(0.35, spread=0.2)

                latest_panel_open = self._latest_reference_button_is_open()
                existing_references = self._collect_reference_cards() if latest_panel_open else []
                if not latest_panel_open:
                    print(f"[{self.name}] 尝试点击源/引用按钮展开面板...")
                    clicked = self._click_reference_open_button()
                    if not clicked:
                        fallback_references = self._collect_reference_cards()
                        if fallback_references:
                            print(f"[{self.name}] 未确认点击源按钮，但已读取 {len(fallback_references)} 条引用")
                            existing_references = self._merge_reference_cards([existing_references, fallback_references])
                        else:
                            print(f"[{self.name}] 未找到可用的源/引用按钮")
                            return []
                    loaded_references = self._wait_for_reference_cards(timeout_seconds=1.6)
                    if loaded_references:
                        existing_references = self._merge_reference_cards([existing_references, loaded_references])
                else:
                    if not existing_references:
                        existing_references = self._wait_for_reference_cards(timeout_seconds=0.8)
                    if existing_references:
                        print(f"[{self.name}] 引用面板已展开，开始滚动采样 {len(existing_references)} 条")
                    else:
                        print(f"[{self.name}] 未找到可用的源/引用按钮")
                        return []
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 点击源按钮失败: {e}")
                return []

            references = self._collect_reference_cards_with_scroll_sampling(existing_references, max_passes=10)

            if isinstance(references, list):
                print(f"[{self.name}] 提取到 {len(references)} 条平台抓取源")
                return references
            return []

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 平台抓取源提取失败: {exc}")
            return []

    def extract_body_references(self) -> list[dict]:
        """
        元宝正文引用：扫描 ref-list__trigger 元素，通过 data-idx-list 与
        面板卡片 data-idx 交叉获取 URL（需 extract_answer_references 已展开面板）。
        """
        try:
            self._raise_if_stop_requested()
            references = self.page.evaluate("""() => {
                const results = [];
                const seenUrls = new Set();
                let index = 1;

                const idxToUrl = {};
                for (const card of document.querySelectorAll('[data-url][data-idx]')) {
                    const idx = card.getAttribute('data-idx');
                    const url = card.getAttribute('data-url') || '';
                    if (idx && url) idxToUrl[idx] = url;
                }

                const extractDomain = (url) => {
                    try { return new URL(url).hostname.replace(/^www\\./, ''); } catch { return ''; }
                };

                for (const trigger of document.querySelectorAll('[class*="ref-list__trigger"]')) {
                    const num = (trigger.getAttribute('data-num') || '').trim();
                    const idxList = (trigger.getAttribute('data-idx-list') || '').split(',');
                    const siteName = (trigger.getAttribute('data-web-site-name') || '').trim();

                    let url = '';
                    for (const idx of idxList) {
                        if (idxToUrl[idx.trim()]) { url = idxToUrl[idx.trim()]; break; }
                    }

                    if (!url || seenUrls.has(url)) continue;
                    seenUrls.add(url);
                    results.push({ index: index++, title: `[${num}]`, url,
                        source: siteName || extractDomain(url) });
                }
                return results;
            }""")

            if isinstance(references, list):
                print(f"[{self.name}] 正文引用源提取到 {len(references)} 条")
                return references
            return []

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 正文引用提取失败: {exc}")
            return []
