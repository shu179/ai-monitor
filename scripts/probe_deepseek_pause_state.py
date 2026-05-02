#!/usr/bin/env python3
"""Probe DeepSeek's in-generation stop/pause state from a real headed browser."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.app_paths import resolve_app_path
from core.browser_platform_factory import create_browser_platform
from core.config_watcher import load_config


DEFAULT_PROMPT = (
    "请用中文详细说明生成式 AI 搜索优化的完整流程，至少分 12 点展开，"
    "每一点给出实践建议和风险提示。"
)


PROBE_JS = r"""() => {
    const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
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
    const collectSignals = (el) => normalize([
        el.getAttribute?.('aria-label'),
        el.getAttribute?.('title'),
        el.getAttribute?.('data-testid'),
        el.getAttribute?.('data-state'),
        el.getAttribute?.('aria-pressed'),
        el.getAttribute?.('aria-disabled'),
        el.innerText,
        el.textContent,
        el.className,
    ].filter(Boolean).join(' '));
    const cssToken = (value) => String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    const selectorHintsFor = (el) => {
        const hints = [];
        if (!el) return hints;
        const tag = String(el.tagName || '').toLowerCase() || '*';
        const aria = normalize(el.getAttribute?.('aria-label'));
        const title = normalize(el.getAttribute?.('title'));
        const testId = normalize(el.getAttribute?.('data-testid'));
        const text = normalize(el.innerText || el.textContent);
        if (aria) hints.push(`${tag}[aria-label="${cssToken(aria)}"]`);
        if (aria.includes('停止')) hints.push(`${tag}[aria-label*="停止"]`);
        if (/stop/i.test(aria)) hints.push(`${tag}[aria-label*="stop" i]`);
        if (title) hints.push(`${tag}[title="${cssToken(title)}"]`);
        if (title.includes('停止')) hints.push(`${tag}[title*="停止"]`);
        if (testId) hints.push(`${tag}[data-testid="${cssToken(testId)}"]`);
        if (text && text.length <= 20) hints.push(`text="${cssToken(text)}"`);
        const cls = String(el.className || '');
        if (cls.includes('ds-button')) hints.push(`${tag}[class*="ds-button"]`);
        if (cls.includes('ds-icon-button')) hints.push(`${tag}[class*="ds-icon-button"]`);
        return Array.from(new Set(hints)).slice(0, 8);
    };
    const controlFor = (el) => (
        el?.closest?.('button, [role="button"], div[role="button"], a[role="button"], [tabindex="0"]')
        || el
    );
    const rectFor = (el) => {
        const rect = el.getBoundingClientRect();
        return {
            x: Math.round(rect.left),
            y: Math.round(rect.top),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
        };
    };

    const stopSignalPattern = /停止回答|停止生成|暂停生成|暂停|停止|stop generating|stop response|stop/i;
    const sendSignalPattern = /发送消息|发送|send message|send/i;
    const controls = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"], a[role="button"], [tabindex="0"]'));
    const visibleControls = controls.filter((el) => isVisible(el));

    const stopControls = visibleControls
        .map((el) => ({ el, signals: collectSignals(el) }))
        .filter((item) => stopSignalPattern.test(item.signals))
        .map((item) => ({
            tag: String(item.el.tagName || '').toLowerCase(),
            role: String(item.el.getAttribute?.('role') || ''),
            text: normalize(item.el.innerText || item.el.textContent),
            ariaLabel: normalize(item.el.getAttribute?.('aria-label')),
            title: normalize(item.el.getAttribute?.('title')),
            testId: normalize(item.el.getAttribute?.('data-testid')),
            className: String(item.el.className || ''),
            signals: item.signals,
            bbox: rectFor(item.el),
            selectorHints: selectorHintsFor(item.el),
        }));

    const stopPathSelectors = [
        'path[d^="M2 4.88"]',
        'path[d^="M2 4.87988"]',
        'path[d^="M2 4.8"]',
    ];
    const stopPathMarkers = [
        '11.12V4.88Z',
        '12.3199 2 11.12V4.88Z',
    ];
    const pathSet = new Set();
    for (const selector of stopPathSelectors) {
        try {
            for (const node of document.querySelectorAll(selector)) pathSet.add(node);
        } catch (_) {}
    }
    for (const node of document.querySelectorAll('svg path')) {
        const d = String(node.getAttribute('d') || '');
        if (!d) continue;
        if (stopPathMarkers.some((marker) => d.includes(marker))) pathSet.add(node);
    }
    const stopPaths = Array.from(pathSet)
        .map((path) => {
            const control = controlFor(path);
            const d = String(path.getAttribute('d') || '');
            const prefix = d.slice(0, 48);
            return {
                dPrefix: prefix,
                d,
                controlTag: String(control?.tagName || '').toLowerCase(),
                controlRole: String(control?.getAttribute?.('role') || ''),
                controlText: normalize(control?.innerText || control?.textContent),
                controlAriaLabel: normalize(control?.getAttribute?.('aria-label')),
                controlTitle: normalize(control?.getAttribute?.('title')),
                controlTestId: normalize(control?.getAttribute?.('data-testid')),
                controlClassName: String(control?.className || ''),
                controlBbox: control ? rectFor(control) : null,
                selectorHints: [
                    `path[d^="${cssToken(prefix)}"]`,
                    control ? `${String(control.tagName || '').toLowerCase() || '*'}:has(path[d^="${cssToken(prefix)}"])` : '',
                    ...selectorHintsFor(control),
                ].filter(Boolean),
                visible: control ? isVisible(control) : isVisible(path),
            };
        })
        .filter((item) => item.visible);

    const sendControls = visibleControls
        .map((el) => ({ el, signals: collectSignals(el) }))
        .filter((item) => !item.el.disabled && sendSignalPattern.test(item.signals) && !stopSignalPattern.test(item.signals))
        .map((item) => ({
            tag: String(item.el.tagName || '').toLowerCase(),
            text: normalize(item.el.innerText || item.el.textContent),
            ariaLabel: normalize(item.el.getAttribute?.('aria-label')),
            title: normalize(item.el.getAttribute?.('title')),
            className: String(item.el.className || ''),
            bbox: rectFor(item.el),
            selectorHints: selectorHintsFor(item.el),
        }));

    const answers = Array.from(document.querySelectorAll('.ds-message .ds-markdown'))
        .filter((el) => isVisible(el) && !el.closest('.ds-think-content'))
        .map((el) => normalize(el.innerText || el.textContent))
        .filter(Boolean);

    return {
        href: String(location.href || ''),
        stop_visible_count: stopControls.length,
        stop_path_count: stopPaths.length,
        send_visible_count: sendControls.length,
        is_generating: stopControls.length > 0 || stopPaths.length > 0,
        answer_count: answers.length,
        answer_length: answers.join('\n').length,
        stop_controls: stopControls.slice(0, 8),
        stop_paths: stopPaths.slice(0, 8),
        send_controls: sendControls.slice(0, 5),
    };
}"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 DeepSeek 生成中的暂停/停止态 DOM 表达")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="用于触发较长生成的提示词")
    parser.add_argument("--brand", default="__probe__", help="内部占位 brand，不影响探针结果")
    parser.add_argument("--timeout", type=float, default=45.0, help="采样超时时间，秒")
    parser.add_argument("--interval", type=float, default=0.35, help="采样间隔，秒")
    parser.add_argument("--no-close", action="store_true", help="结束后保留浏览器窗口，便于人工观察")
    return parser.parse_args()


def _first_non_empty(values: list[str]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    generating_samples = [sample for sample in samples if sample.get("is_generating")]
    path_counter: Counter[str] = Counter()
    selector_counter: Counter[str] = Counter()
    text_counter: Counter[str] = Counter()
    class_counter: Counter[str] = Counter()

    for sample in generating_samples:
        for item in sample.get("stop_paths") or []:
            prefix = str(item.get("dPrefix") or "").strip()
            if prefix:
                path_counter[prefix] += 1
            for hint in item.get("selectorHints") or []:
                selector_counter[str(hint)] += 1
        for item in sample.get("stop_controls") or []:
            text = _first_non_empty([
                item.get("ariaLabel", ""),
                item.get("title", ""),
                item.get("text", ""),
            ])
            if text:
                text_counter[text] += 1
            class_name = str(item.get("className") or "").strip()
            if class_name:
                class_counter[class_name] += 1
            for hint in item.get("selectorHints") or []:
                selector_counter[str(hint)] += 1

    best_selector = ""
    for selector, _ in selector_counter.most_common():
        if ":has(path" in selector or "aria-label" in selector or "title" in selector or selector.startswith("text="):
            best_selector = selector
            break

    return {
        "sample_count": len(samples),
        "generating_sample_count": len(generating_samples),
        "saw_pause_state": bool(generating_samples),
        "suggested_selector": best_selector,
        "top_path_prefixes": path_counter.most_common(5),
        "top_selectors": selector_counter.most_common(10),
        "top_texts": text_counter.most_common(10),
        "top_classes": class_counter.most_common(5),
        "first_generating_sample": generating_samples[0] if generating_samples else None,
        "last_sample": samples[-1] if samples else None,
    }


def main() -> int:
    args = parse_args()
    config = load_config(resolve_app_path("config.yaml"))
    platform = create_browser_platform(
        "deepseek",
        config=config,
        inspect=True,
        stop_checker=None,
    )
    platform.prefer_headed_runtime = True
    platform.inspect = True
    samples: list[dict[str, Any]] = []
    output_dir = resolve_app_path("logs/selector_heal")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"deepseek_pause_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    try:
        platform.start()
        platform.ensure_logged_in(timeout=20)
        platform.start_new_chat()
        platform.deep_think = False
        platform.type_like_human(args.prompt)
        platform.submit_prompt()

        deadline = time.time() + max(5.0, float(args.timeout or 0.0))
        saw_generating = False
        while time.time() < deadline:
            sample = platform.page.evaluate(PROBE_JS) or {}
            sample["ts"] = time.time()
            samples.append(sample)
            if sample.get("is_generating"):
                saw_generating = True
            elif saw_generating:
                break
            time.sleep(max(0.1, float(args.interval or 0.35)))

        summary = _summarize(samples)
        payload = {
            "ok": bool(summary.get("saw_pause_state")),
            "platform": "deepseek",
            "prompt": args.prompt,
            "summary": summary,
            "samples": samples,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**payload, "samples": f"{len(samples)} samples saved to {output_path}"}, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 2
    finally:
        if not args.no_close:
            try:
                platform.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
