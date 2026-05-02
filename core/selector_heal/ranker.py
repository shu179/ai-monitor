"""Deterministic selector candidate ranking."""

from __future__ import annotations

from typing import Any

from .registry import FieldIntent


def _lower_values(*values: Any) -> str:
    return " ".join(str(value or "").strip().lower() for value in values if str(value or "").strip())


def _contains_any(text: str, keywords: tuple[str, ...]) -> list[str]:
    lowered = str(text or "").lower()
    return [keyword for keyword in keywords if keyword and str(keyword).lower() in lowered]


def score_candidate(candidate: dict[str, Any], intent: FieldIntent) -> dict[str, Any]:
    """Score one DOM candidate against an intent using explainable rules."""
    score = 0.0
    reasons: list[str] = []

    testid_hits = _contains_any(str(candidate.get("testId") or ""), intent.testid_keywords)
    if testid_hits:
        score += 0.42
        reasons.append(f"data-testid 命中: {', '.join(testid_hits[:3])}")

    aria_title = _lower_values(candidate.get("ariaLabel"), candidate.get("title"))
    aria_hits = _contains_any(aria_title, intent.aria_keywords)
    if aria_hits:
        score += 0.32
        reasons.append(f"aria/title 命中: {', '.join(aria_hits[:3])}")

    text_hits = _contains_any(str(candidate.get("text") or ""), intent.text_synonyms)
    if text_hits:
        score += 0.28
        reasons.append(f"文本命中: {', '.join(text_hits[:3])}")

    class_hits = _contains_any(str(candidate.get("className") or ""), intent.class_keywords)
    if class_hits:
        score += 0.16
        reasons.append(f"class 命中: {', '.join(class_hits[:3])}")

    role = str(candidate.get("role") or "").strip().lower()
    tag = str(candidate.get("tag") or "").strip().lower()
    if role and role in {item.lower() for item in intent.role_hints}:
        score += 0.08
        reasons.append(f"role 匹配: {role}")
    if tag and tag in {item.lower() for item in intent.tag_hints}:
        score += 0.08
        reasons.append(f"标签匹配: {tag}")

    path_prefixes = [str(item or "") for item in (candidate.get("svgPathPrefixes") or [])]
    icon_hits = []
    for hint in intent.icon_hints:
        hint_text = str(hint or "")
        if not hint_text:
            continue
        if "M8 0.599609" in hint_text:
            needle = "M8 0.599609"
        elif "M8 0" in hint_text:
            needle = "M8 0"
        else:
            needle = hint_text
        if any(path.startswith(needle) or needle in path for path in path_prefixes):
            icon_hits.append(needle)
    if icon_hits:
        score += 0.24
        reasons.append(f"图标路径命中: {', '.join(icon_hits[:3])}")

    selector_hints = [str(item or "").strip() for item in (candidate.get("selectorHints") or []) if str(item or "").strip()]
    selector = selector_hints[0] if selector_hints else ""
    if not selector:
        score -= 0.2
        reasons.append("缺少可生成的 selector")

    return {
        "selector": selector,
        "score": round(max(0.0, min(1.0, score)), 3),
        "reason": "；".join(reasons) if reasons else "未命中明显语义特征",
        "candidate": candidate,
    }


def rank_candidates(
    candidates: list[dict[str, Any]],
    intent: FieldIntent,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Rank DOM candidates and keep useful selector suggestions."""
    ranked = [score_candidate(candidate, intent) for candidate in candidates]
    ranked = [item for item in ranked if item.get("selector")]
    ranked.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("selector") or "")))

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ranked:
        selector = str(item.get("selector") or "").strip()
        if not selector or selector in seen:
            continue
        seen.add(selector)
        deduped.append(item)
        if len(deduped) >= max(1, int(limit or 8)):
            break
    return deduped
