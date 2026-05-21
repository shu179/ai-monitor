"""
阿里通义千问 平台适配器
"""

import base64
import json as _json
import re
import time
import random
from .base import BasePlatform


class TongyiPlatform(BasePlatform):

    # 通义千问对话 SSE 流的 endpoint 关键字（用于网络拦截定位 reference 数据）
    _chat_endpoint_marker = "chat2.qianwen.com/api/v2/chat"

    use_external_chrome_cdp = True
    external_chrome_launch_target_url = True
    skip_runtime_startup_goto = True
    target_url = "https://tongyi.aliyun.com/qianwen"
    target_url_aliases = ["https://www.qianwen.com", "https://qianwen.aliyun.com"]
    input_selector = 'div[contenteditable="true"], textarea'
    result_selector = (
        '#qk-markdown-react, '
        '.markdown-pc-special-class, '
        '[class*="answerItem"] .qk-markdown, '
        '[class*="answerItem"] .markdown-pc-special-class, '
        '.qk-markdown'
    )
    # 兼容 tongyi.aliyun.com 和 qianwen.com 两个域名的新建对话按钮
    new_chat_selector = 'button:has-text("新建对话"), button[aria-label="新建对话"], button[class*="newChat"], button[class*="new-chat"]'
    chat_container_selector = ".message-list-scroll-container"
    deep_think_selector = (
        'span[data-input-capsule-login-gate^="deep-think"] button, '
        'button[aria-label="思考"][aria-pressed], '
        'button:has([data-icon-type="qwpcicon-deepThinking"]), '
        'button[aria-label="深度思考"], '
        'button:has-text("深度思考"), '
        'button:has-text("思考")'
    )
    think_content_selector = (
        '[class*="thinkingContent"], [class*="ThinkingContent"], '
        '[class*="thinkingWrap"], [class*="ThinkingWrap"], '
        '[class*="thinkingTitle"], [class*="ThinkingTitle"], '
        '[class*="thinkingHeader"], [class*="ThinkingHeader"], [class*="thinking-header"], '
        '[class*="thinkContent"], [class*="ThinkContent"], [class*="think-content"], '
        '[class*="deep-think"], [class*="deepThink"], '
        '[class*="reasoning"], [class*="Reasoning"], '
        '[class*="thought"], [class*="Thought"], '
        '[class*="cot"], [data-card_name="deep_think"], '
        '[data-testid*="think"], [data-testid*="reason"], [data-testid*="cot"]'
    )
    prefer_last_result_block = True
    _overlay_detection_enabled = False

    def __init__(self, user_data_dir: str):
        super().__init__(user_data_dir)
        # SSE 拦截缓存：最近一次对话流的完整 body（用于解析 reference）
        self._chat_sse_body: str = ""
        # in-flight request ids for chat endpoint，用于在 loadingFinished 时识别
        self._chat_pending_request_ids: set = set()
        # CDP session 引用：避免被 GC，page 重建时重新安装
        self._chat_cdp_session = None
        self._chat_cdp_installed_for_page = None

    def _install_shared_js_helpers(self) -> None:
        super()._install_shared_js_helpers()
        # 在每次 page 创建/重建后同步装 CDP listener，跟着 page 的生命周期走
        self._install_chat_response_capture()

    def _install_chat_response_capture(self) -> None:
        """订阅 chat2.qianwen.com/api/v2/chat 的 SSE 响应，缓存供 extract_answer_references 用。

        失败不致命 —— extract_answer_references 会回退到点击展开 + 文本解析的旧路径。
        """
        page = getattr(self, "page", None)
        context = getattr(self, "context", None)
        if not page or not context:
            return
        if self._chat_cdp_installed_for_page is page:
            return  # 已经装过当前 page

        try:
            client = context.new_cdp_session(page)
            client.send("Network.enable")
        except Exception as exc:
            print(f"[{self.name}] CDP 网络订阅安装失败，引用抓取将回退到点击路径: {exc}")
            self._chat_cdp_session = None
            self._chat_cdp_installed_for_page = None
            return

        marker = self._chat_endpoint_marker
        pending = self._chat_pending_request_ids

        def on_response_received(params):
            try:
                url = (params.get("response") or {}).get("url", "")
                if marker in url:
                    pending.add(params.get("requestId", ""))
            except Exception:
                pass

        def on_loading_finished(params):
            try:
                rid = params.get("requestId", "")
                if rid not in pending:
                    return
                pending.discard(rid)
                result = client.send("Network.getResponseBody", {"requestId": rid})
                body = result.get("body") or ""
                if result.get("base64Encoded"):
                    try:
                        body = base64.b64decode(body).decode("utf-8", errors="replace")
                    except Exception:
                        body = ""
                if body:
                    self._chat_sse_body = body
            except Exception as exc:
                # body 读失败不致命，下次拦截再试
                print(f"[{self.name}] CDP 读取 chat SSE body 失败: {exc}")

        def on_loading_failed(params):
            try:
                pending.discard(params.get("requestId", ""))
            except Exception:
                pass

        try:
            client.on("Network.responseReceived", on_response_received)
            client.on("Network.loadingFinished", on_loading_finished)
            client.on("Network.loadingFailed", on_loading_failed)
            self._chat_cdp_session = client
            self._chat_cdp_installed_for_page = page
            print(f"[{self.name}] 已安装 chat SSE 响应拦截器（reference 直抓）")
        except Exception as exc:
            print(f"[{self.name}] CDP 事件订阅失败: {exc}")
            self._chat_cdp_session = None
            self._chat_cdp_installed_for_page = None

    def start_new_chat(self) -> None:
        # 新对话开始前清掉上一轮 SSE 缓存，避免读到旧引用
        self._chat_sse_body = ""
        self._chat_pending_request_ids.clear()
        super().start_new_chat()

    def _get_answer_text(self) -> str:
        """Tongyi 只采最后一轮 assistant markdown，排除提问区、思考区和来源侧栏。"""
        try:
            self._raise_if_stop_requested()
            self._consume_answer_read_scroll()
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel}) => {
                    const h = window.__aiMonitorHelpers__ || {};
                    const normalize = h.normalize || ((value) => String(value || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim());

                    const isVisible = h.isVisible || ((el) => Boolean(el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0));
                    const matchesSelector = (el, selector) => {
                        if (!el || !selector) return false;
                        try { return el.matches(selector) || Boolean(el.closest(selector)); }
                        catch (_) { return false; }
                    };
                    const looksLikeThinkingText = (text) => (
                        /^(深度思考中|思考中|正在思考|思考过程|已深度思考|已思考)/.test(normalize(text || ''))
                    );
                    const looksLikeThinkingNode = (el) => {
                        if (!el) return false;
                        if (matchesSelector(el, thinkSel)) return true;
                        let node = el;
                        for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
                            const marker = [
                                node.className,
                                node.id,
                                node.getAttribute && node.getAttribute('data-testid'),
                                node.getAttribute && node.getAttribute('aria-label'),
                                node.getAttribute && node.getAttribute('data-card_name'),
                            ].filter(Boolean).join(' ');
                            if (/(thinkingContent|thinkingWrap|thinkingTitle|thinkingHeader|thinking-header|thinkContent|think-content|deep-think|deepThink|reasoning|cot|thought)/i.test(String(marker || ''))) {
                                return true;
                            }
                            if (node === el && looksLikeThinkingText(node.innerText || node.textContent || '')) {
                                return true;
                            }
                        }
                        return false;
                    };
                    const isExcluded = (el) => {
                        if (!el) return false;
                        try {
                            if (looksLikeThinkingNode(el)) return true;
                            return Boolean(
                                el.closest(
                                    '.questionItem-u8_ahH, .bubble-VIVxZ8, .contentBox-PXNuf2, .content-hKtCkw, ' +
                                    '.deep-think-source-tyxrYL, .list-XPxyL2, .bg-mask-50'
                                )
                            );
                        } catch (_) {
                            return false;
                        }
                    };
                    const resolveSpeech = (block) => (
                        block.closest('#qk-markdown-react')
                        || block.closest('.markdown-pc-special-class')
                        || block.closest('[class*="answerItem"]')
                        || block.closest('[class*="containerWrap"]')
                        || block.parentElement
                    );
                    const collectFallbackSpeechBlocks = () => {
                        const leaves = Array.from(document.querySelectorAll(
                            '.qk-md-text.complete, .qk-md-head, h1.qk-md-head, h2.qk-md-head, h3.qk-md-head, h4.qk-md-head, h5.qk-md-head, h6.qk-md-head'
                        )).filter((el) => isVisible(el) && !isExcluded(el));
                        const seen = new Set();
                        const results = [];
                        for (const leaf of leaves) {
                            const speech = resolveSpeech(leaf);
                            if (!speech || seen.has(speech)) continue;
                            seen.add(speech);
                            results.push(speech);
                        }
                        return results;
                    };
                    const collectPreferredBlocks = () => {
                        const candidates = Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el) && !isExcluded(el));
                        const filtered = candidates.filter((el) => !candidates.some((other) => (
                            other !== el &&
                            el.contains(other) &&
                            isVisible(other) &&
                            !isExcluded(other)
                        )));
                        return filtered.length > 0 ? filtered : collectFallbackSpeechBlocks();
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
                        '[class*="thinkingContent"]',
                        '[class*="ThinkingContent"]',
                        '[class*="thinkingWrap"]',
                        '[class*="ThinkingWrap"]',
                        '[class*="thinkingTitle"]',
                        '[class*="ThinkingTitle"]',
                        '[class*="thinkingHeader"]',
                        '[class*="ThinkingHeader"]',
                        '[class*="thinking-header"]',
                        '[class*="thinkContent"]',
                        '[class*="ThinkContent"]',
                        '[class*="think-content"]',
                        '[class*="deep-think"]',
                        '[class*="deepThink"]',
                        '[class*="reasoning"]',
                        '[class*="Reasoning"]',
                        '[class*="thought"]',
                        '[class*="Thought"]',
                        '[class*="cot"]',
                        '[data-card_name="deep_think"]',
                        '[data-testid*="think"]',
                        '[data-testid*="reason"]',
                        '[data-testid*="cot"]',
                        '.questionItem-u8_ahH',
                        '.bubble-VIVxZ8',
                        '.contentBox-PXNuf2',
                        '.content-hKtCkw',
                        '.deep-think-source-tyxrYL',
                        '.list-XPxyL2',
                        '.bg-mask-50',
                        '[class*="video_note_list_item_list"]',
                        '[class*="note-item"]',
                        '[class*="note-box"]',
                        '[class*="note-cover"]',
                        '[class*="item-title"]',
                        '[class*="item-user"]',
                        '[id^="gi-"][data-sc-action="onClick"]',
                    ].filter(Boolean);
                    const sanitizeElement = (el) => {
                        if (!el) return;
                        const tag = String(el.tagName || '').toLowerCase();
                        const marker = [
                            String(el.className || ''),
                            String(el.getAttribute && el.getAttribute('data-card_name') || ''),
                            String(el.getAttribute && el.getAttribute('data-testid') || ''),
                            String(el.getAttribute && el.getAttribute('data-sc-action') || ''),
                            String(el.id || ''),
                        ].join(' ');
                        const hasMediaCard = Boolean(
                            el.querySelector &&
                            el.querySelector('img, picture, figure, video, canvas')
                        );
                        const looksLikeTongyiImageCard =
                            /(video_note_list_item_list|note-item|note-box|note-cover|item-title|item-user|apollo-cover|data-sc-action|gi-)/i.test(marker);
                        if (['div', 'section', 'article', 'aside', 'a'].includes(tag) && looksLikeTongyiImageCard) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && (hasMediaCard || /(image|img|card|cover|poster|thumb|thumbnail)/i.test(marker))) {
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
                        const speech = resolveSpeech(block);
                        const text = extractCleanBlockText(block);
                        if (looksLikeThinkingText(text)) continue;
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
                    if (blocks.length > 0) {
                        return extractCleanBlockText(blocks[blocks.length - 1]);
                    }
                    return '';
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
        """采集 Tongyi 最后一轮 assistant markdown 的干净正文快照。"""
        try:
            self._raise_if_stop_requested()
            self._consume_answer_read_scroll()
            return self.page.evaluate(
                """({containerSel, resultSel, thinkSel}) => {
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
                    const matchesSelector = (el, selector) => {
                        if (!el || !selector) return false;
                        try { return el.matches(selector) || Boolean(el.closest(selector)); }
                        catch (_) { return false; }
                    };
                    const looksLikeThinkingText = (text) => (
                        /^(深度思考中|思考中|正在思考|思考过程|已深度思考|已思考)/.test(normalize(text || ''))
                    );
                    const looksLikeThinkingNode = (el) => {
                        if (!el) return false;
                        if (matchesSelector(el, thinkSel)) return true;
                        let node = el;
                        for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
                            const marker = [
                                node.className,
                                node.id,
                                node.getAttribute && node.getAttribute('data-testid'),
                                node.getAttribute && node.getAttribute('aria-label'),
                                node.getAttribute && node.getAttribute('data-card_name'),
                            ].filter(Boolean).join(' ');
                            if (/(thinkingContent|thinkingWrap|thinkingTitle|thinkingHeader|thinking-header|thinkContent|think-content|deep-think|deepThink|reasoning|cot|thought)/i.test(String(marker || ''))) {
                                return true;
                            }
                            if (node === el && looksLikeThinkingText(node.innerText || node.textContent || '')) {
                                return true;
                            }
                        }
                        return false;
                    };
                    const isExcluded = (el) => {
                        if (!el) return false;
                        try {
                            if (looksLikeThinkingNode(el)) return true;
                            return Boolean(
                                el.closest(
                                    '.questionItem-u8_ahH, .bubble-VIVxZ8, .contentBox-PXNuf2, .content-hKtCkw, ' +
                                    '.deep-think-source-tyxrYL, .list-XPxyL2, .bg-mask-50'
                                )
                            );
                        } catch (_) {
                            return false;
                        }
                    };
                    const resolveSpeech = (block) => (
                        block.closest('#qk-markdown-react')
                        || block.closest('.markdown-pc-special-class')
                        || block.closest('[class*="answerItem"]')
                        || block.closest('[class*="containerWrap"]')
                        || block.parentElement
                    );
                    const collectFallbackSpeechBlocks = () => {
                        const leaves = Array.from(document.querySelectorAll(
                            '.qk-md-text.complete, .qk-md-head, h1.qk-md-head, h2.qk-md-head, h3.qk-md-head, h4.qk-md-head, h5.qk-md-head, h6.qk-md-head'
                        )).filter((el) => isVisible(el) && !isExcluded(el));
                        const seen = new Set();
                        const results = [];
                        for (const leaf of leaves) {
                            const speech = resolveSpeech(leaf);
                            if (!speech || seen.has(speech)) continue;
                            seen.add(speech);
                            results.push(speech);
                        }
                        return results;
                    };
                    const collectPreferredBlocks = () => {
                        const candidates = Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el) && !isExcluded(el));
                        const filtered = candidates.filter((el) => !candidates.some((other) => (
                            other !== el &&
                            el.contains(other) &&
                            isVisible(other) &&
                            !isExcluded(other)
                        )));
                        return filtered.length > 0 ? filtered : collectFallbackSpeechBlocks();
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
                        '[class*="thinkingContent"]',
                        '[class*="ThinkingContent"]',
                        '[class*="thinkingWrap"]',
                        '[class*="ThinkingWrap"]',
                        '[class*="thinkingTitle"]',
                        '[class*="ThinkingTitle"]',
                        '[class*="thinkingHeader"]',
                        '[class*="ThinkingHeader"]',
                        '[class*="thinking-header"]',
                        '[class*="thinkContent"]',
                        '[class*="ThinkContent"]',
                        '[class*="think-content"]',
                        '[class*="deep-think"]',
                        '[class*="deepThink"]',
                        '[class*="reasoning"]',
                        '[class*="Reasoning"]',
                        '[class*="thought"]',
                        '[class*="Thought"]',
                        '[class*="cot"]',
                        '[data-card_name="deep_think"]',
                        '[data-testid*="think"]',
                        '[data-testid*="reason"]',
                        '[data-testid*="cot"]',
                        '.questionItem-u8_ahH',
                        '.bubble-VIVxZ8',
                        '.contentBox-PXNuf2',
                        '.content-hKtCkw',
                        '.deep-think-source-tyxrYL',
                        '.list-XPxyL2',
                        '.bg-mask-50',
                        '[class*="video_note_list_item_list"]',
                        '[class*="note-item"]',
                        '[class*="note-box"]',
                        '[class*="note-cover"]',
                        '[class*="item-title"]',
                        '[class*="item-user"]',
                        '[id^="gi-"][data-sc-action="onClick"]',
                    ].filter(Boolean);
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
                        const marker = [
                            String(el.className || ''),
                            String(el.getAttribute && el.getAttribute('data-card_name') || ''),
                            String(el.getAttribute && el.getAttribute('data-testid') || ''),
                            String(el.getAttribute && el.getAttribute('data-sc-action') || ''),
                            String(el.id || ''),
                        ].join(' ');
                        const hasMediaCard = Boolean(
                            el.querySelector &&
                            el.querySelector('img, picture, figure, video, canvas')
                        );
                        const looksLikeTongyiImageCard =
                            /(video_note_list_item_list|note-item|note-box|note-cover|item-title|item-user|apollo-cover|data-sc-action|gi-)/i.test(marker);
                        if (['div', 'section', 'article', 'aside', 'a'].includes(tag) && looksLikeTongyiImageCard) {
                            el.remove();
                            return;
                        }
                        if (tag === 'a' && (hasMediaCard || /(image|img|card|cover|poster|thumb|thumbnail)/i.test(marker))) {
                            el.remove();
                            return;
                        }
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
                        const speech = resolveSpeech(block);
                        const text = extractCleanBlockText(block);
                        if (looksLikeThinkingText(text)) continue;
                        if (!speech || !text) continue;
                        const last = groups[groups.length - 1];
                        if (!last || last.speech !== speech) {
                            const attrs = [
                                speech.getAttribute('data-id'),
                                speech.getAttribute('data-testid'),
                                speech.id,
                                speech.className,
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
                        if (blocks.length > 0) {
                            const fallback = blocks[blocks.length - 1];
                            const text = extractCleanBlockText(fallback);
                            const html = sanitizeBlockHtml(fallback);
                            if (text || html) {
                                return {
                                    root_key: 'fallback:last-block',
                                    blocks: [{
                                        key: 'fallback:last-block:0',
                                        order: 0,
                                        text,
                                        html,
                                    }],
                                    raw_text: text,
                                    raw_html: html,
                                };
                            }
                        }
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
                    "thinkSel": self.think_content_selector or "",
                },
            ) or {"root_key": "", "blocks": [], "raw_text": "", "raw_html": ""}
        except Exception as e:
            self._reraise_stop_requested(e)
            return super()._capture_answer_snapshot()

    def _get_answer_html(self) -> str:
        """Tongyi DOM 复排只保留最后一轮 assistant markdown 的干净 HTML。"""
        try:
            self._raise_if_stop_requested()
            snapshot = self._capture_answer_snapshot() or {}
            return str(snapshot.get("raw_html") or "").strip()
        except Exception as e:
            self._reraise_stop_requested(e)
            return ""

    def _get_deep_think_state(self) -> dict:
        try:
            return self.page.evaluate("""() => {
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
                const iconTypesFor = (el) => Array.from(el.querySelectorAll('[data-icon-type], use'))
                    .map((node) => [
                        node.getAttribute('data-icon-type'),
                        node.getAttribute('href'),
                        node.getAttribute('xlink:href'),
                    ].filter(Boolean).join(' '))
                    .join(' ');
                const collectText = (el) => normalize([
                    el.innerText,
                    el.textContent,
                    el.getAttribute && el.getAttribute('aria-label'),
                    el.getAttribute && el.getAttribute('title'),
                    el.getAttribute && el.getAttribute('data-input-capsule-login-gate'),
                    iconTypesFor(el),
                ].filter(Boolean).join(' '));
                const controlFor = (node) => (
                    node.closest('span[data-input-capsule-login-gate^="deep-think"] button')
                    || node.closest('button, [role="button"], [aria-pressed], [aria-checked]')
                    || node
                );
                const scoreCandidate = (control, text, rect) => {
                    const cls = String(control.className || '');
                    const gate = control.closest('[data-input-capsule-login-gate]');
                    const gateValue = String(gate?.getAttribute('data-input-capsule-login-gate') || '');
                    const iconTypes = iconTypesFor(control);
                    const aria = String(control.getAttribute('aria-label') || '');
                    let score = 0;
                    if (/^deep-think/.test(gateValue)) score += 260;
                    if (/qwpcicon-deepThinking|deepThinking/i.test(iconTypes)) score += 240;
                    if (aria === '思考' || text === '思考') score += 150;
                    if (/深度思考/.test(text) || /深度思考/.test(aria)) score += 180;
                    if (control.hasAttribute('aria-pressed') || control.hasAttribute('aria-checked')) score += 90;
                    if (control.tagName === 'BUTTON') score += 70;
                    if (cls.includes('bg-option')) score += 25;
                    if (rect.width <= 140 && rect.height <= 56) score += 35;
                    if (rect.width >= window.innerWidth * 0.45) score -= 160;
                    if (text.length >= 32) score -= 120;
                    if (/deep-research|qwpcicon-deepResearch|研究|任务助理|更多|PPT|AI生图|代码|翻译|AI写作|实时记录/i.test([gateValue, iconTypes, text].join(' '))) {
                        score -= 260;
                    }
                    return score;
                };

                const nodes = Array.from(document.querySelectorAll(
                    'span[data-input-capsule-login-gate^="deep-think"] button, ' +
                    'button[aria-label="思考"][aria-pressed], ' +
                    'button[aria-label="深度思考"], ' +
                    'button, [role="button"], [aria-pressed], [aria-checked], [data-icon-type], use'
                ));
                const seen = new Set();
                const candidates = [];
                for (const node of nodes) {
                    const control = controlFor(node);
                    if (!control || seen.has(control) || !isVisible(control) || isDisabled(control)) continue;
                    seen.add(control);
                    const text = collectText(control) || collectText(node);
                    if (!/(思考|深度思考|deepThinking|deep-think)/i.test(text)) continue;
                    const rect = control.getBoundingClientRect();
                    const score = scoreCandidate(control, text, rect);
                    if (score <= 0) continue;
                    candidates.push({ control, text, score });
                }
                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best) return {found: false, active: false, text: "", attrs: {}, style: {}, score: 0};

                const btn = best.control;
                const text = normalize(btn.innerText || btn.textContent || btn.getAttribute('aria-label') || best.text || '');
                const cls = String(btn.className || '');
                const style = window.getComputedStyle(btn);
                const attrs = {
                    className: cls,
                    ariaPressed: btn.getAttribute('aria-pressed') || '',
                    ariaChecked: btn.getAttribute('aria-checked') || '',
                    ariaSelected: btn.getAttribute('aria-selected') || '',
                    dataState: btn.getAttribute('data-state') || '',
                    dataSelected: btn.getAttribute('data-selected') || '',
                    dataInputCapsuleLoginGate: btn.closest('[data-input-capsule-login-gate]')?.getAttribute('data-input-capsule-login-gate') || '',
                    disabled: !!btn.disabled,
                };

                // 只用 ARIA 属性检测（不用 className 避免误判通义按钮类名中的 "on" 等词）
                const activeAttr = [attrs.ariaPressed, attrs.ariaChecked, attrs.ariaSelected, attrs.dataState, attrs.dataSelected]
                    .some((value) => ['true', 'checked', 'selected', 'active', 'on', 'open'].includes(String(value || '').toLowerCase()));

                // 通义深度思考激活时按钮变紫色：默认色 rgb(6,10,38)；紫色系 b>80 且 r>50
                let activeColor = false;
                const colMatch = style.color && style.color.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                if (colMatch) {
                    const r = parseInt(colMatch[1]), g = parseInt(colMatch[2]), b = parseInt(colMatch[3]);
                    const isDefaultDark = r < 20 && g < 20 && b < 60;
                    if (!isDefaultDark && b > 80 && r > 50) activeColor = true;
                }

                return {
                    found: true,
                    active: activeAttr || activeColor,
                    text,
                    attrs,
                    style: {
                        color: style.color,
                        backgroundColor: style.backgroundColor,
                        borderColor: style.borderColor,
                        boxShadow: style.boxShadow,
                    },
                    score: best.score,
                };
            }""")
        except Exception:
            return {"found": False, "active": False, "text": "", "attrs": {}, "style": {}}

    def _click_deep_think_button(self) -> bool:
        try:
            self._raise_if_stop_requested()
            for selector in [
                self.deep_think_selector,
                'span[data-input-capsule-login-gate^="deep-think"] button',
                'button[aria-label="思考"][aria-pressed]',
                'button:has([data-icon-type="qwpcicon-deepThinking"])',
                'button[aria-label="深度思考"]',
                'button:has-text("深度思考")',
                'button:has-text("思考")',
            ]:
                try:
                    btn = self.page.locator(selector).first
                    if btn.count() <= 0:
                        continue
                    btn.scroll_into_view_if_needed(timeout=2000)
                    self._click_locator(btn, timeout_ms=2500, force=False)
                    return True
                except Exception as e:
                    self._reraise_stop_requested(e)
                    continue
            clicked = self.page.evaluate("""() => {
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
                const iconTypesFor = (el) => Array.from(el.querySelectorAll('[data-icon-type], use'))
                    .map((node) => [
                        node.getAttribute('data-icon-type'),
                        node.getAttribute('href'),
                        node.getAttribute('xlink:href'),
                    ].filter(Boolean).join(' '))
                    .join(' ');
                const collectText = (el) => normalize([
                    el.innerText,
                    el.textContent,
                    el.getAttribute && el.getAttribute('aria-label'),
                    el.getAttribute && el.getAttribute('title'),
                    el.getAttribute && el.getAttribute('data-input-capsule-login-gate'),
                    iconTypesFor(el),
                ].filter(Boolean).join(' '));
                const controlFor = (node) => (
                    node.closest('span[data-input-capsule-login-gate^="deep-think"] button')
                    || node.closest('button, [role="button"], [aria-pressed], [aria-checked]')
                    || node
                );
                const scoreCandidate = (control, text, rect) => {
                    const gate = control.closest('[data-input-capsule-login-gate]');
                    const gateValue = String(gate?.getAttribute('data-input-capsule-login-gate') || '');
                    const iconTypes = iconTypesFor(control);
                    const aria = String(control.getAttribute('aria-label') || '');
                    let score = 0;
                    if (/^deep-think/.test(gateValue)) score += 260;
                    if (/qwpcicon-deepThinking|deepThinking/i.test(iconTypes)) score += 240;
                    if (aria === '思考' || text === '思考') score += 150;
                    if (/深度思考/.test(text) || /深度思考/.test(aria)) score += 180;
                    if (control.hasAttribute('aria-pressed') || control.hasAttribute('aria-checked')) score += 90;
                    if (control.tagName === 'BUTTON') score += 70;
                    if (rect.width <= 140 && rect.height <= 56) score += 35;
                    if (rect.width >= window.innerWidth * 0.45) score -= 160;
                    if (text.length >= 32) score -= 120;
                    if (/deep-research|qwpcicon-deepResearch|研究|任务助理|更多|PPT|AI生图|代码|翻译|AI写作|实时记录/i.test([gateValue, iconTypes, text].join(' '))) {
                        score -= 260;
                    }
                    return score;
                };

                const nodes = Array.from(document.querySelectorAll(
                    'span[data-input-capsule-login-gate^="deep-think"] button, ' +
                    'button[aria-label="思考"][aria-pressed], ' +
                    'button[aria-label="深度思考"], ' +
                    'button, [role="button"], [aria-pressed], [aria-checked], [data-icon-type], use'
                ));
                const seen = new Set();
                let best = null;
                for (const node of nodes) {
                    const control = controlFor(node);
                    if (!control || seen.has(control) || !isVisible(control) || isDisabled(control)) continue;
                    seen.add(control);
                    const text = collectText(control) || collectText(node);
                    if (!/(思考|深度思考|deepThinking|deep-think)/i.test(text)) continue;
                    const rect = control.getBoundingClientRect();
                    const score = scoreCandidate(control, text, rect);
                    if (!best || score > best.score) best = { control, score };
                }
                if (!best || best.score < 120) return false;
                const btn = best.control;
                try { btn.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
                for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                    try {
                        btn.dispatchEvent(new MouseEvent(eventName, {bubbles: true, cancelable: true, view: window}));
                    } catch (_) {}
                }
                try { btn.click(); } catch (_) {}
                return true;
            }""")
            return bool(clicked)
        except Exception as e:
            self._reraise_stop_requested(e)
            return False

    def start_new_chat(self) -> None:
        """新版通义的新建入口就是左侧文本按钮。"""
        before = self._conversation_snapshot()
        last_error = "未找到可用的新建对话按钮"
        if not self.new_chat_selector:
            if self._attempt_learned_selector_heal("new_chat_selector", label="新建对话"):
                print(f"[{self.name}] 已通过 learned selector 开启新对话")
            return
            return
        try:
            self._raise_if_stop_requested()
            btn = self.page.locator(self.new_chat_selector).first
            self._wait_for_locator(btn, timeout_ms=5000)
            btn.scroll_into_view_if_needed(timeout=3000)
            try:
                self._click_locator(btn, timeout_ms=5000)
            except Exception as e:
                self._reraise_stop_requested(e)
                clicked = self.page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button'))
                        .find((el) => String(el.innerText || '').trim().includes('新建对话'));
                    if (!btn) return false;
                    btn.click();
                    return true;
                }""")
                if not clicked:
                    raise
            if self._wait_and_confirm_new_chat(before, sleep_seconds=random.uniform(0.82, 1.18)):
                print(f"[{self.name}] 已开启新对话")
                return
            last_error = "已点击新建对话按钮，但未确认切换到新会话"
        except Exception as e:
            self._reraise_stop_requested(e)
            last_error = str(e) or last_error
        if self._attempt_learned_selector_heal("new_chat_selector", label="新建对话"):
            print(f"[{self.name}] 已通过 learned selector 开启新对话")
            return
        print(f"[{self.name}] 开启新对话失败，继续: {last_error}")

    def enable_deep_think(self) -> bool:
        try:
            self._raise_if_stop_requested()
            state = self._get_deep_think_state()
            if not state.get("found"):
                print(f"[{self.name}] 深度思考按钮未找到")
                return False

            if self.deep_think and state.get("active"):
                print(f"[{self.name}] 深度思考已激活")
                return True

            if (not self.deep_think) and (not state.get("active")):
                return False

            before_signature = {
                "text": str(state.get("text") or ""),
                "attrs": state.get("attrs") or {},
                "style": state.get("style") or {},
            }
            clicked = self._click_deep_think_button()
            if not clicked:
                print(f"[{self.name}] 深度思考按钮点击失败")
                return False

            self._cooperative_sleep_jittered(0.8, spread=0.18)
            state = self._get_deep_think_state()
            after_signature = {
                "text": str(state.get("text") or ""),
                "attrs": state.get("attrs") or {},
                "style": state.get("style") or {},
            }

            if self.deep_think:
                if state.get("active"):
                    print(f"[{self.name}] 深度思考已开启")
                    return True
                if before_signature != after_signature and state.get("found"):
                    print(f"[{self.name}] 深度思考已触发，状态已变化")
                    return True
                print(f"[{self.name}] 深度思考开启失败，当前状态: {after_signature}")
                return False

            if not state.get("active"):
                print(f"[{self.name}] 已关闭深度思考")
                return False
            if before_signature != after_signature:
                print(f"[{self.name}] 已触发关闭深度思考")
                return False
            print(f"[{self.name}] 关闭深度思考失败，当前状态: {after_signature}")
            return False
        except Exception as e:
            self._reraise_stop_requested(e)
            print(f"[{self.name}] 深度思考按钮操作失败: {e}")
            return False

    def _detect_overlay(self) -> bool:
        """通义有常驻的高z-index元素，不做遮罩检测避免误判"""
        return False

    def is_generation_complete(self, page_text: str, start_time: float) -> bool:
        try:
            self._raise_if_stop_requested()
            return bool(self.page.evaluate("""({thinkSel}) => {
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
                const matchesSelector = (el, selector) => {
                    if (!el || !selector) return false;
                    try { return el.matches(selector) || Boolean(el.closest(selector)); }
                    catch (_) { return false; }
                };
                const looksLikeThinkingText = (text) => (
                    /^(深度思考中|思考中|正在思考|思考过程)/.test(normalize(text || ''))
                );
                const looksLikeThinkingNode = (el) => {
                    if (!el) return false;
                    if (matchesSelector(el, thinkSel)) return true;
                    let node = el;
                    for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
                        const marker = [
                            node.className,
                            node.id,
                            node.getAttribute && node.getAttribute('data-testid'),
                            node.getAttribute && node.getAttribute('aria-label'),
                            node.getAttribute && node.getAttribute('data-card_name'),
                        ].filter(Boolean).join(' ');
                        if (/(thinkingContent|thinkingWrap|thinkingTitle|thinkingHeader|thinking-header|thinkContent|think-content|deep-think|deepThink|reasoning|cot|thought)/i.test(String(marker || ''))) {
                            return true;
                        }
                        if (node === el && looksLikeThinkingText(node.innerText || node.textContent || '')) {
                            return true;
                        }
                    }
                    return false;
                };

                const stopCandidates = Array.from(document.querySelectorAll('button, [role="button"]'));
                const hasStopButton = stopCandidates.some((el) => {
                    if (!isVisible(el)) return false;
                    const signals = [
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.innerText,
                        el.textContent,
                        el.className,
                    ].filter(Boolean).join(' ');
                    return /停止回答|停止生成|停止/i.test(String(signals || ''));
                });
                if (hasStopButton) return false;

                const hasActivePrinting = Array.from(document.querySelectorAll(
                    '.qk-md-printing-text, [class*="qk-md-printing"]'
                )).some((el) => isVisible(el));
                if (hasActivePrinting) return false;

                const hasActiveThinking = Array.from(document.querySelectorAll(
                    '#qk-markdown-react, .qk-markdown, .markdown-pc-special-class, [class*="answerItem"], [class*="markdown"], .qk-md-paragraph'
                )).some((el) => isVisible(el) && looksLikeThinkingText(el.innerText || el.textContent || ''));
                if (hasActiveThinking) return false;

                const blocks = Array.from(document.querySelectorAll(
                    '[class*="answerItem"] .qk-markdown, [class*="answerItem"] .markdown-pc-special-class, #qk-markdown-react, .qk-markdown, .markdown-pc-special-class'
                )).filter((el) => {
                    const text = String(el.innerText || '').trim();
                    if (!text) return false;
                    if (looksLikeThinkingNode(el) || looksLikeThinkingText(text)) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                });
                const lastBlock = blocks.length ? blocks[blocks.length - 1] : null;
                if (!lastBlock) return false;

                const className = String(lastBlock.className || '');
                const text = String(lastBlock.innerText || '').trim();
                if (!text) return false;

                const hasCompleteClass = className.includes('qk-markdown-complete');
                return hasCompleteClass;
            }""", {"thinkSel": self.think_content_selector or ""}))
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

                const stopCandidates = Array.from(document.querySelectorAll('button, [role="button"]'));
                return stopCandidates.some((el) => {
                    if (!isVisible(el)) return false;
                    const signals = [
                        el.getAttribute('aria-label'),
                        el.getAttribute('title'),
                        el.innerText,
                        el.textContent,
                        el.className,
                    ].filter(Boolean).join(' ');
                    return /停止回答|停止生成|停止/i.test(String(signals || ''));
                });
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
                    const matchesSelector = (el, selector) => {
                        if (!el || !selector) return false;
                        try { return el.matches(selector) || Boolean(el.closest(selector)); }
                        catch (_) { return false; }
                    };
                    const looksLikeThinkingText = (text) => (
                        /^(深度思考中|思考中|正在思考|思考过程|已深度思考|已思考)/.test(normalize(text || ''))
                    );
                    const looksLikeThinkingNode = (el) => {
                        if (!el) return false;
                        if (matchesSelector(el, thinkSel)) return true;
                        const marker = [
                            el.className,
                            el.id,
                            el.getAttribute && el.getAttribute('data-testid'),
                            el.getAttribute && el.getAttribute('aria-label'),
                            el.getAttribute && el.getAttribute('data-card_name'),
                        ].filter(Boolean).join(' ');
                        return (
                            /(thinkingContent|thinkingWrap|thinkingTitle|thinkingHeader|thinking-header|thinkContent|think-content|deep-think|deepThink|reasoning|cot|thought)/i.test(String(marker || '')) ||
                            looksLikeThinkingText(el.innerText || el.textContent || '')
                        );
                    };
                    const controls = Array.from(document.querySelectorAll('button, [role="button"]'));
                    const stopVisible = controls.some((el) => {
                        if (!isVisible(el)) return false;
                        const signals = [
                            el.getAttribute('aria-label'),
                            el.getAttribute('title'),
                            el.innerText,
                            el.textContent,
                            el.className,
                        ].filter(Boolean).join(' ');
                        return /停止回答|停止生成|停止/i.test(String(signals || ''));
                    });
                    const sendReady = controls.some((el) => {
                        if (!isVisible(el) || el.disabled) return false;
                        const signals = [
                            el.getAttribute('aria-label'),
                            el.getAttribute('title'),
                            el.innerText,
                            el.textContent,
                            el.className,
                        ].filter(Boolean).join(' ');
                        return /发送消息|发送/i.test(String(signals || ''));
                    });
                    const activeThinking = Array.from(document.querySelectorAll(
                        '#qk-markdown-react, .qk-markdown, .markdown-pc-special-class, [class*="answerItem"], [class*="markdown"], .qk-md-paragraph'
                    )).some((el) => isVisible(el) && /^(深度思考中|思考中|正在思考|思考过程)/.test(normalize(el.innerText || el.textContent || '')));
                    const activePrinting = Array.from(document.querySelectorAll(
                        '.qk-md-printing-text, [class*="qk-md-printing"]'
                    )).some((el) => isVisible(el));
                    const answers = resultSel
                        ? Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el) && !looksLikeThinkingNode(el))
                            .map((el) => normalize(el.innerText || el.textContent || ''))
                            .filter(Boolean)
                        : [];
                    const lastBlock = resultSel
                        ? Array.from(document.querySelectorAll(resultSel))
                            .filter((el) => isVisible(el) && !looksLikeThinkingNode(el))
                            .slice(-1)[0]
                        : null;
                    const input = inputSel ? document.querySelector(inputSel) : null;
                    const inputText = normalize(
                        input ? (input.value || input.innerText || input.textContent || '') : ''
                    );
                    return {
                        stopVisible,
                        sendReady,
                        activeThinking,
                        activePrinting,
                        lastBlockComplete: Boolean(lastBlock && String(lastBlock.className || '').includes('qk-markdown-complete')),
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
                "send_ready": bool(snapshot.get("sendReady")),
                "active_thinking": bool(snapshot.get("activeThinking")),
                "active_printing": bool(snapshot.get("activePrinting")),
                "last_block_complete": bool(snapshot.get("lastBlockComplete")),
                "input_length": int(snapshot.get("inputLength", 0) or 0),
                "answer_count": int(snapshot.get("answerCount", 0) or 0),
                "answer_length": int(snapshot.get("answerLength", 0) or 0),
            }
        except Exception as e:
            self._reraise_stop_requested(e)
            return {"debug_error": str(e)}

    def _allow_text_stable_completion(self, debug_state: dict | None = None) -> bool:
        state = debug_state if isinstance(debug_state, dict) else {}
        return (
            not bool(state.get("stop_visible"))
            and not bool(state.get("active_thinking"))
            and not bool(state.get("active_printing"))
            and bool(state.get("last_block_complete"))
        )

    def extract_answer_references(self) -> list[dict]:
        """
        通义千问引用抓取（优先 SSE 拦截 + 点击兜底）。

        主路径：从 chat2.qianwen.com/api/v2/chat 的 SSE 流里解析
        data.messages[].mime_type=='multi_load/iframe' →
        meta_data.multi_load[].type=='source_group_web' →
        content.list[].type=='source' → content.list[]
        每项含 title/url/name/ref_num，**无需任何点击**、**全部条数都有真实 URL**。

        兜底路径：CDP 拦截失效或未触发搜索时，回退到旧的「点击展开 + 逐张拦截新页面」
        实现（前 3 条真实 URL，第 4 条起只用 https://domain）。
        """
        try:
            self._raise_if_stop_requested()
            sse_body = str(self._chat_sse_body or "")
            if sse_body:
                references = self._parse_references_from_sse(sse_body)
                if references:
                    print(f"[{self.name}] 已通过 SSE 拦截提取 {len(references)} 条引用（无需点击）")
                    return references
                # 解析失败时打印关键计数，便于定位 endpoint 漂移 / 结构变化 / body 截断
                self._log_sse_parse_diagnostics(sse_body)
                print(f"[{self.name}] SSE 已缓存但未解析到引用，回退到点击路径")
            else:
                print(f"[{self.name}] 未捕获 SSE 响应（CDP 失效或未触发搜索），回退到点击路径")
            return self._extract_answer_references_via_click()
        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 平台抓取源提取失败: {exc}")
            return []

    def _log_sse_parse_diagnostics(self, body: str) -> None:
        """SSE 解析失败时打印关键计数，让日志足以判断是哪种失败模式。"""
        try:
            print(
                f"[{self.name}] SSE 诊断: 长度={len(body)}, "
                f"含'data:'={body.count('data:')}, "
                f"含'multi_load/iframe'={body.count('multi_load/iframe')}, "
                f"含'source_group_web'={body.count('source_group_web')}, "
                f"含'bar/progress'={body.count('bar/progress')}, "
                f"含'\"web_source\"'={body.count(chr(34) + 'web_source' + chr(34))}, "
                f"含'\"url\"'={body.count(chr(34) + 'url' + chr(34))}, "
                f"含'http'={body.count('http')}"
            )
        except Exception:
            pass

    @staticmethod
    def _parse_references_from_sse(body: str) -> list[dict]:
        """从 SSE event-stream 文本里抽取引用列表。

        通义千问对同一 URL 在不同模式下用了两种 mime_type / 嵌套结构：

        - 深度思考模式：
          mime_type='multi_load/iframe' → meta_data.multi_load[].type='source_group_web'
          → content.list[].type='source' → content.list[]（嵌套 4 层）

        - 非深度思考模式：
          mime_type='bar/progress' → meta_data.list[]（type='web_source'，平铺）

        两种结构的引用项字段名都一致（title / url / name），可共用提取逻辑。
        按 url 去重、index 从 1 递增；流被截断/非 JSON 行（心跳、注释）跳过。
        """
        if not body:
            return []
        chunks = re.split(r'\n+data:', '\n' + body)
        events: list[str] = []
        for chunk in chunks:
            text = chunk.strip()
            if text.startswith('data:'):
                text = text[5:].strip()
            if text and text != '[DONE]':
                events.append(text)

        all_refs: list[dict] = []
        seen_urls: set[str] = set()

        def _add_ref(item: dict) -> None:
            url = str((item or {}).get('url') or '').strip()
            if not url or url in seen_urls:
                return
            seen_urls.add(url)
            all_refs.append({
                'index': len(all_refs) + 1,
                'title': str(item.get('title') or '').strip(),
                'url': url,
                'source': str(item.get('name') or '').strip(),
            })

        for ev in events:
            try:
                obj = _json.loads(ev)
            except Exception:
                continue
            messages = (obj.get('data') or {}).get('messages') or []
            for msg in messages:
                mime = msg.get('mime_type', '')
                meta = msg.get('meta_data') or {}
                if mime == 'multi_load/iframe':
                    # 深度思考：嵌套结构
                    for group in meta.get('multi_load') or []:
                        if group.get('type') != 'source_group_web':
                            continue
                        for src_block in (group.get('content') or {}).get('list') or []:
                            if src_block.get('type') != 'source':
                                continue
                            for item in (src_block.get('content') or {}).get('list') or []:
                                _add_ref(item)
                elif mime == 'bar/progress':
                    # 非深度思考：平铺结构，type='web_source'
                    for item in meta.get('list') or []:
                        if str(item.get('type') or '') not in ('web_source', 'source'):
                            continue
                        _add_ref(item)
        return all_refs

    def _extract_answer_references_via_click(self) -> list[dict]:
        """旧路径：点击「X篇来源」按钮展开面板 + 逐张点击拦截 URL（CDP 失效时兜底）。

        面板无 <a href>，只能通过点击每张卡片捕获新标签页 URL；前 3 条真实 URL，
        第 4 条起只用 https://domain 兜底。
        """
        try:
            self._raise_if_stop_requested()

            try:
                btn = self.page.locator('div, span').filter(
                    has_text=re.compile(r'^\d+\s*篇来源$')
                ).last
                if btn.count() == 0:
                    print(f"[{self.name}] 未找到来源按钮")
                    return []
                btn_text = btn.inner_text()
                print(f"[{self.name}] 找到来源按钮: {btn_text}，点击展开...")
                btn.click(timeout=3000)
                self._cooperative_sleep_jittered(2.5, spread=0.15)
            except Exception as e:
                self._reraise_stop_requested(e)
                print(f"[{self.name}] 点击来源按钮失败: {e}")
                return []

            # Step 1: 用 JS 找右侧面板里所有来源卡片的文字信息
            card_info = self.page.evaluate("""() => {
                const W = window.innerWidth;
                const results = [];
                const seen = new Set();

                const leafEls = Array.from(document.querySelectorAll('*')).filter(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.left > W * 0.55 && el.children.length === 0;
                });

                const nameEls = leafEls.filter(el => {
                    const text = (el.innerText || '').trim();
                    return text.length > 3 && text.length < 80 && /[a-zA-Z0-9][.][a-zA-Z]{2,}/.test(text);
                });

                for (const nameEl of nameEls) {
                    // 找可点击的卡片祖先
                    let card = nameEl.parentElement;
                    while (card && card.parentElement) {
                        const rect = card.getBoundingClientRect();
                        if (rect.left > W * 0.55 && rect.height > 40 && rect.height < 250 && rect.width > 150) break;
                        card = card.parentElement;
                    }
                    if (!card) continue;
                    const cardRect = card.getBoundingClientRect();
                    const cardKey = Math.round(cardRect.top) + '-' + Math.round(cardRect.height);
                    if (seen.has(cardKey)) continue;
                    seen.add(cardKey);

                    const nameText = (nameEl.innerText || '').trim();
                    const parts = nameText.split(/\\s+/);
                    const domain = parts[parts.length - 1] || '';
                    const source = parts.slice(0, -1).join('');

                    let title = '';
                    const parent = nameEl.parentElement;
                    if (parent) {
                        for (const sib of parent.querySelectorAll('*')) {
                            const t = (sib.innerText || '').trim();
                            if (t && t !== nameText && t.length > 5 && t.length < 100 && sib.children.length === 0) {
                                title = t; break;
                            }
                        }
                    }
                    results.push({ domain, source, title, cardKey });
                }
                return results;
            }""") or []

            # Step 2: 逐一点击卡片，拦截新标签页 URL
            references = []
            for i, info in enumerate(card_info):
                domain = info.get("domain", "")
                fallback_url = ("https://" + domain) if domain else ""
                title = info.get("title") or info.get("source", "")
                source = info.get("source", "")
                card_key = info.get("cardKey", "")
                if i >= 3:
                    # 第4条起直接用域名，不再点击
                    references.append({"index": i + 1, "title": title, "url": fallback_url, "source": source})
                    continue
                try:
                    self._raise_if_stop_requested()
                    with self.page.context.expect_page(timeout=4000) as page_info:
                        self.page.evaluate("""(cardKey) => {
                            const W = window.innerWidth;
                            const seen = new Set();
                            const leafEls = Array.from(document.querySelectorAll('*')).filter(el => {
                                const rect = el.getBoundingClientRect();
                                return rect.left > W * 0.55 && el.children.length === 0;
                            });
                            const nameEls = leafEls.filter(el => {
                                const text = (el.innerText || '').trim();
                                return text.length > 3 && text.length < 80 && /[a-zA-Z0-9][.][a-zA-Z]{2,}/.test(text);
                            });
                            for (const nameEl of nameEls) {
                                let card = nameEl.parentElement;
                                while (card && card.parentElement) {
                                    const rect = card.getBoundingClientRect();
                                    if (rect.left > W * 0.55 && rect.height > 40 && rect.height < 250 && rect.width > 150) break;
                                    card = card.parentElement;
                                }
                                if (!card) continue;
                                const cardRect = card.getBoundingClientRect();
                                const key = Math.round(cardRect.top) + '-' + Math.round(cardRect.height);
                                if (key === cardKey) { card.click(); return; }
                            }
                        }""", card_key)
                    new_page = page_info.value
                    url = new_page.url
                    new_page.close()
                    print(f"[{self.name}] 来源[{i+1}] {source}: {url}")
                except Exception as e:
                    self._reraise_stop_requested(e)
                    url = fallback_url
                    print(f"[{self.name}] 来源[{i+1}] {source}: 点击失败，用域名 {url}")
                references.append({"index": i + 1, "title": title, "url": url, "source": source})
                self._cooperative_sleep_jittered(0.3, spread=0.25)


            if isinstance(references, list):
                print(f"[{self.name}] 提取到 {len(references)} 条平台抓取源")
                return references
            return []

        except Exception as exc:
            self._reraise_stop_requested(exc)
            print(f"[{self.name}] 平台抓取源提取失败: {exc}")
            return []
