"""DOM probing helpers for selector diagnosis and conservative fallbacks."""

from __future__ import annotations

from typing import Any


DEFAULT_INTERACTIVE_SELECTOR = "button, a[role='button'], div[role='button'], [tabindex='0']"


def collect_interactive_candidates(
    page: Any,
    *,
    interactive_selector: str = DEFAULT_INTERACTIVE_SELECTOR,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Collect visible interactive DOM candidates without clicking anything."""
    candidates = page.evaluate(
        """({interactiveSelector, limit}) => {
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const unique = (items) => Array.from(new Set(items.filter(Boolean)));
            const buildSelectorHints = (node) => {
                const hints = [];
                const tag = String(node.tagName || '').toLowerCase();
                const role = normalize(node.getAttribute('role'));
                const testId = normalize(node.getAttribute('data-testid'));
                const aria = normalize(node.getAttribute('aria-label'));
                const title = normalize(node.getAttribute('title'));
                const className = normalize(node.getAttribute('class'));
                const text = normalize(node.innerText || node.textContent);
                const escapedText = text.replace(/"/g, '\\"');
                const compactPathPrefix = (value) => {
                    const text = String(value || '');
                    const match = text.match(/^M\\s*[-0-9.]+\\s+[-0-9.]+/);
                    return match ? match[0] : text.slice(0, 16);
                };
                const pathPrefixes = Array.from(node.querySelectorAll('path[d]'))
                    .map((path) => compactPathPrefix(path.getAttribute('d')))
                    .filter(Boolean);
                const firstPathPrefix = pathPrefixes[0] || "";
                const escapedPathPrefix = firstPathPrefix.replace(/"/g, '\\"');
                if (testId) hints.push(`[data-testid="${testId.replace(/"/g, '\\"')}"]`);
                if (tag && aria) hints.push(`${tag}[aria-label="${aria.replace(/"/g, '\\"')}"]`);
                if (tag && title) hints.push(`${tag}[title="${title.replace(/"/g, '\\"')}"]`);
                if (role && aria) hints.push(`[role="${role.replace(/"/g, '\\"')}"][aria-label="${aria.replace(/"/g, '\\"')}"]`);
                if (tag && text && text.length <= 32) {
                    hints.push(`text="${escapedText}"`);
                    if (role) hints.push(`[role="${role.replace(/"/g, '\\"')}"]:has-text("${escapedText}")`);
                    hints.push(`${tag}:text-is("${escapedText}")`);
                    hints.push(`${tag}:has-text("${escapedText}")`);
                    if (firstPathPrefix) hints.push(`${tag}:has(path[d^="${escapedPathPrefix}"]):has-text("${escapedText}")`);
                }
                if (className) {
                    const stableClasses = className
                        .split(/\\s+/)
                        .filter((item) => item && !/^_[a-zA-Z0-9_-]{5,}$/.test(item) && !/[0-9]{4,}|__[a-zA-Z0-9_-]{4,}/.test(item))
                        .slice(0, 3);
                    if (stableClasses.length > 0) hints.push(`${tag || '*'}${stableClasses.map((item) => `.${CSS.escape(item)}`).join('')}`);
                }
                return unique(hints).slice(0, 8);
            };

            const nodes = Array.from(document.querySelectorAll(interactiveSelector || "button, a[role='button'], div[role='button'], [tabindex='0']"));
            const output = [];
            for (const node of nodes) {
                if (output.length >= Math.max(1, Number(limit || 200))) break;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                const visible = (
                    rect.width > 0 &&
                    rect.height > 0 &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    style.opacity !== '0'
                );
                if (!visible) continue;
                const paths = Array.from(node.querySelectorAll('path[d]'))
                    .map((path) => String(path.getAttribute('d') || '').slice(0, 80))
                    .filter(Boolean)
                    .slice(0, 8);
                output.push({
                    tag: String(node.tagName || '').toLowerCase(),
                    role: normalize(node.getAttribute('role')),
                    text: normalize(node.innerText || node.textContent),
                    ariaLabel: normalize(node.getAttribute('aria-label')),
                    title: normalize(node.getAttribute('title')),
                    placeholder: normalize(node.getAttribute('placeholder')),
                    testId: normalize(node.getAttribute('data-testid')),
                    className: normalize(node.getAttribute('class')),
                    href: normalize(node.getAttribute('href')),
                    selectorHints: buildSelectorHints(node),
                    svgPathPrefixes: paths,
                    bbox: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                    },
                });
            }
            return output;
        }""",
        {
            "interactiveSelector": str(interactive_selector or DEFAULT_INTERACTIVE_SELECTOR),
            "limit": max(1, int(limit or 200)),
        },
    )
    return candidates if isinstance(candidates, list) else []


def inspect_selector(page: Any, selector: str) -> dict[str, Any]:
    """Inspect an existing selector without clicking."""
    text = str(selector or "").strip()
    if not text:
        return {"selector": "", "count": 0, "visible": False, "error": "selector 为空"}
    try:
        return page.evaluate(
            """(selector) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                try {
                    const nodes = Array.from(document.querySelectorAll(selector));
                    const first = nodes[0] || null;
                    if (!first) return {selector, count: 0, visible: false, error: ""};
                    const rect = first.getBoundingClientRect();
                    const style = window.getComputedStyle(first);
                    const visible = (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.opacity !== '0'
                    );
                    return {
                        selector,
                        count: nodes.length,
                        visible,
                        text: normalize(first.innerText || first.textContent),
                        ariaLabel: normalize(first.getAttribute('aria-label')),
                        title: normalize(first.getAttribute('title')),
                        testId: normalize(first.getAttribute('data-testid')),
                        tag: String(first.tagName || '').toLowerCase(),
                        bbox: {
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                        },
                        error: "",
                    };
                } catch (error) {
                    return {selector, count: 0, visible: false, error: String(error && error.message || error || "selector 检查失败")};
                }
            }""",
            text,
        )
    except Exception as exc:
        return {"selector": text, "count": 0, "visible": False, "error": str(exc)}


def click_interactive_candidate(
    page: Any,
    *,
    primary_selector: str = "",
    direct_selectors: list[str] | tuple[str, ...] | None = None,
    text_keywords: list[str] | tuple[str, ...] | None = None,
    icon_selectors: list[str] | tuple[str, ...] | None = None,
    interactive_selector: str = DEFAULT_INTERACTIVE_SELECTOR,
) -> bool:
    """Click the first visible DOM candidate matching known selector/text hints.

    This preserves the behavior of the existing DeepSeek DOM fallback while
    making the core probing logic reusable by future selector diagnostics.
    """
    selectors = [
        str(primary_selector or "").strip(),
        *[str(item or "").strip() for item in (direct_selectors or ())],
    ]
    keywords = [str(item or "").strip().lower() for item in (text_keywords or ()) if str(item or "").strip()]
    icons = [str(item or "").strip() for item in (icon_selectors or ()) if str(item or "").strip()]
    return bool(
        page.evaluate(
            """({directSelectors, keywords, iconSelectors, interactiveSelector}) => {
                const clickNode = (node) => {
                    if (!node) return false;
                    try { node.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}
                    for (const eventName of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                        try {
                            node.dispatchEvent(new MouseEvent(eventName, {bubbles: true, cancelable: true, view: window}));
                        } catch (_) {}
                    }
                    try { node.click(); } catch (_) {}
                    return true;
                };

                for (const selector of (directSelectors || []).filter(Boolean)) {
                    let node = null;
                    try { node = document.querySelector(selector); } catch (_) { node = null; }
                    if (clickNode(node)) return true;
                }

                const candidates = Array.from(document.querySelectorAll(interactiveSelector || "button, a[role='button'], div[role='button'], [tabindex='0']"));
                for (const node of candidates) {
                    const text = String(node.innerText || node.textContent || '').trim().toLowerCase();
                    const label = String(node.getAttribute('aria-label') || node.getAttribute('title') || '').trim().toLowerCase();
                    const marker = `${text} ${label}`;
                    if (!marker) continue;
                    if ((keywords || []).some((keyword) => marker.includes(String(keyword || '').toLowerCase()))) {
                        if (clickNode(node)) return true;
                    }
                }

                for (const selector of (iconSelectors || []).filter(Boolean)) {
                    for (const node of document.querySelectorAll('button')) {
                        let matched = false;
                        try { matched = !!node.querySelector(selector); } catch (_) { matched = false; }
                        if (matched && clickNode(node)) return true;
                    }
                }

                return false;
            }""",
            {
                "directSelectors": [item for item in selectors if item],
                "keywords": keywords,
                "iconSelectors": icons,
                "interactiveSelector": str(interactive_selector or DEFAULT_INTERACTIVE_SELECTOR),
            },
        )
    )
