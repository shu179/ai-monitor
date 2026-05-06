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
    _overlay_detection_enabled = False

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
            if not clicked and self._attempt_selector_agent_heal("new_chat_selector", label="新对话"):
                print(f"[{self.name}] 已通过 selector_agent 开启新对话")
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
            self.page.evaluate("""() => {
                const el = document.querySelector('.agent-chat__list__content-wrapper');
                if (el) el.scrollTop = el.scrollHeight;
            }""")
            self._cooperative_sleep(0.5)
        except Exception as e:
            self._reraise_stop_requested(e)
            pass
        super()._scroll_brand_into_view(brand)

    def _get_answer_text(self) -> str:
        """元宝一轮回答经常被拆成多个 markdown 块，这里取最后一轮回答的全部正文。"""
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
                    "shouldScroll": self._consume_answer_read_scroll(),
                },
            ) or ""
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._get_answer_text()

    def _capture_answer_snapshot(self) -> dict:
        """只采最后一轮 AI speech 下的 markdown 块，避免局部渲染时丢全文。"""
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
                    "shouldScroll": self._consume_answer_read_scroll(),
                },
            ) or {"root_key": "", "blocks": [], "raw_text": "", "raw_html": ""}
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._capture_answer_snapshot()

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
        deadline = time.time() + max(1.0, float(timeout or 0.0))
        while time.time() < deadline:
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
            self._cooperative_sleep(0.2)
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
                ("input_enter", lambda: chat_input.press("Enter", timeout=3000)),
                ("keyboard_enter", lambda: self.page.keyboard.press("Enter")),
            ]
            for strategy_name, submit_action in strategies:
                submit_action()
                if self._wait_for_submit_started(before_input, timeout=8.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}）")
                    return
                if self._wait_for_submit_start_signal(timeout=4.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: {strategy_name}; submit-signal）")
                    return
            print(f"[{self.name}] Enter 提交未确认，尝试点击发送按钮兜底")
            if self._click_send_button_via_dom():
                if self._wait_for_submit_started(before_input, timeout=8.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: dom_send_button）")
                    return
                if self._wait_for_submit_start_signal(timeout=4.0):
                    print(f"[{self.name}] 已确认问题已发送（策略: dom_send_button; submit-signal）")
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

    def extract_answer_references(self) -> list[dict]:
        """
        元宝平台抓取源：点击「源」按钮展开面板，从卡片 data-url 属性读取链接。
        """
        try:
            self._raise_if_stop_requested()

            try:
                self.page.evaluate("""(selector) => {
                    const el = document.querySelector(selector);
                    if (el) el.scrollTop = el.scrollHeight;
                    else window.scrollTo(0, document.body.scrollHeight);
                }""", self.chat_container_selector or "")
                self._cooperative_sleep(0.6)
                btn_locator = self.page.locator('[class*="ToolbarSearchGuid_source"]')
                if btn_locator.count() == 0:
                    print(f"[{self.name}] 未找到源按钮")
                    return []
                btn = btn_locator.last
                print(f"[{self.name}] 找到源按钮，点击展开...")
                try:
                    btn.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                clicked = False
                try:
                    self._click_locator(btn, timeout_ms=2500, force=True)
                    clicked = True
                except Exception:
                    clicked = bool(self.page.evaluate("""() => {
                        const candidates = Array.from(document.querySelectorAll('[class*="ToolbarSearchGuid_source"]'));
                        const target = candidates.length > 0 ? candidates[candidates.length - 1] : null;
                        if (!target) return false;
                        const clickable = target.closest('button, [role="button"], span, div') || target;
                        try {
                            clickable.scrollIntoView({block: 'center', inline: 'center'});
                        } catch (_) {}
                        for (const node of [clickable, target]) {
                            if (!node) continue;
                            try {
                                node.dispatchEvent(new MouseEvent('pointerdown', {bubbles: true, cancelable: true, view: window}));
                                node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                                node.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                                node.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                                node.click?.();
                                return true;
                            } catch (_) {}
                        }
                        return false;
                    }"""))
                if not clicked:
                    print(f"[{self.name}] 源按钮点击未成功触发")
                    return []
                self._cooperative_sleep(2.0)
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 点击源按钮失败: {e}")
                return []

            references = self.page.evaluate("""() => {
                const results = [];
                const seenUrls = new Set();
                let index = 1;

                const extractDomain = (url) => {
                    try { return new URL(url).hostname.replace(/^www\\./, ''); } catch { return ''; }
                };

                for (const card of document.querySelectorAll('[data-url]')) {
                    const url = card.getAttribute('data-url') || '';
                    if (!url || seenUrls.has(url)) continue;
                    seenUrls.add(url);
                    const titleEl = card.querySelector('[class*="ref_card-title"]');
                    const title = (titleEl ? titleEl.innerText || titleEl.textContent : '').trim().slice(0, 80);
                    const sourceEl = card.querySelector('[class*="ref_card-foot__source_txt"]');
                    const source = (sourceEl ? sourceEl.innerText || sourceEl.textContent : '').trim()
                        || extractDomain(url);
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
