"""Pure matching helpers for recognition mode.

This module intentionally avoids clipboard, browser, screenshot capture, and
network side effects. Keep it focused on stable data transformations covered by
characterization tests.
"""

from __future__ import annotations

from typing import Any


PLATFORM_ID_ALIASES = {
    "豆包": "doubao",
    "doubao": "doubao",
    "deepseek": "deepseek",
    "DeepSeek": "deepseek",
    "方舟 DeepSeek": "ark_deepseek",
    "ark_deepseek": "ark_deepseek",
    "ark deepseek": "ark_deepseek",
    "Kimi": "kimi",
    "kimi": "kimi",
    "元宝": "yuanbao",
    "yuanbao": "yuanbao",
    "通义千问": "tongyi",
    "通义": "tongyi",
    "tongyi": "tongyi",
    "qwen": "tongyi",
    "文心一言": "wenxin",
    "文心": "wenxin",
    "wenxin": "wenxin",
    "ernie": "wenxin",
    "ChatGPT": "chatgpt",
    "chatgpt": "chatgpt",
    "Claude": "claude",
    "claude": "claude",
    "Gemini": "gemini",
    "gemini": "gemini",
    "Perplexity": "perplexity",
    "perplexity": "perplexity",
}

PLATFORM_DISPLAY = {
    "local_model": "本地模型",
    "doubao": "豆包",
    "deepseek": "DeepSeek",
    "ark_deepseek": "方舟 DeepSeek",
    "kimi": "Kimi",
    "yuanbao": "元宝",
    "tongyi": "通义千问",
    "wenxin": "文心一言",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
}


def normalize_keyword_brand_pair(keyword: str, brand: str) -> tuple[str, str]:
    return (str(keyword or "").strip().lower(), str(brand or "").strip().lower())


def normalize_platform_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return PLATFORM_ID_ALIASES.get(raw, PLATFORM_ID_ALIASES.get(raw.lower(), raw.lower()))


def display_platform_name(platform_name: Any) -> str:
    normalized = normalize_platform_id(platform_name)
    if normalized:
        return PLATFORM_DISPLAY.get(normalized, normalized)
    text = str(platform_name or "").strip()
    return PLATFORM_DISPLAY.get(text, text)


def normalized_platform_list(platforms: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    values = [
        normalize_platform_id(platform)
        for platform in (platforms or [])
        if normalize_platform_id(platform)
    ]
    return list(dict.fromkeys(values))


def build_matched_pairs(
    keyword: str,
    brands: list[str],
    platforms: list[str] | None = None,
) -> list[dict]:
    keyword_text = str(keyword or "").strip()
    pairs = []
    seen = set()
    normalized_platforms = normalized_platform_list(platforms)
    for brand in brands or []:
        brand_text = str(brand or "").strip()
        pair_key = (
            normalize_keyword_brand_pair(keyword_text, brand_text),
            tuple(normalized_platforms),
        )
        if not keyword_text or pair_key in seen:
            continue
        seen.add(pair_key)
        pairs.append({
            "keyword": keyword_text,
            "brand": brand_text,
            "platforms": list(normalized_platforms),
        })
    return pairs


def serialize_matched_pairs(matched_pairs: list[dict]) -> list[dict]:
    serialized = []
    seen = set()
    for item in matched_pairs or []:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        brand = str(item.get("brand") or "").strip()
        platforms = normalized_platform_list(item.get("platforms") or [])
        pair_key = (
            normalize_keyword_brand_pair(keyword, brand),
            tuple(platforms),
        )
        if not keyword or pair_key in seen:
            continue
        seen.add(pair_key)
        serialized.append({
            "keyword": keyword,
            "brand": brand,
            "platforms": list(platforms),
        })
    return serialized


def expand_matched_pair_slots(matched_pairs: list[dict]) -> list[dict]:
    slots: list[dict] = []
    for pair in serialize_matched_pairs(matched_pairs or []):
        keyword = str(pair.get("keyword") or "").strip()
        brand = str(pair.get("brand") or "").strip()
        platforms = normalized_platform_list(pair.get("platforms") or [])
        if not platforms:
            slots.append({
                "keyword": keyword,
                "brand": brand,
                "platforms": [],
            })
            continue
        for platform in platforms:
            slots.append({
                "keyword": keyword,
                "brand": brand,
                "platforms": [platform],
            })
    return slots


def remaining_current_platforms_after_match(
    current_item: dict | None,
    matched_pairs: list[dict],
) -> list[str]:
    item = dict(current_item or {})
    current_platforms = normalized_platform_list(item.get("platforms") or [])
    if not current_platforms:
        return []

    current_keyword = str(item.get("keyword") or "").strip()
    current_keyword_key, _ = normalize_keyword_brand_pair(current_keyword, "")
    if not current_keyword_key:
        return current_platforms
    current_brand_keys = {
        normalize_keyword_brand_pair("", brand)[1]
        for brand in (item.get("brands") or [])
        if str(brand or "").strip()
    }

    matched_platforms: set[str] = set()
    for pair in expand_matched_pair_slots(matched_pairs or []):
        pair_keyword_key, pair_brand_key = normalize_keyword_brand_pair(
            str((pair or {}).get("keyword") or "").strip(),
            str((pair or {}).get("brand") or "").strip(),
        )
        if pair_keyword_key != current_keyword_key:
            continue
        if current_brand_keys and pair_brand_key and pair_brand_key not in current_brand_keys:
            continue
        for platform in (pair or {}).get("platforms") or []:
            normalized_platform = normalize_platform_id(platform)
            if normalized_platform:
                matched_platforms.add(normalized_platform)

    if not matched_platforms:
        return current_platforms
    return [platform for platform in current_platforms if platform not in matched_platforms]


def platforms_for_matched_item(item: dict, platform_hint: str = "") -> list[str]:
    normalized_hint = normalize_platform_id(platform_hint)
    item_platforms = normalized_platform_list((item or {}).get("platforms") or [])
    if normalized_hint and (not item_platforms or normalized_hint in item_platforms):
        return [normalized_hint]
    if len(item_platforms) == 1:
        return item_platforms
    if len(item_platforms) > 1:
        return []
    return item_platforms


def build_task_matched_pairs(
    *,
    task_name: str,
    matched_brands: list[str],
    current_item: dict | None,
    guide_items: list[dict],
    platform_hint: str = "",
) -> list[dict]:
    task_name_text = str(task_name or "").strip()
    if current_item and str(current_item.get("task_name") or "").strip() == task_name_text:
        return build_matched_pairs(
            current_item.get("keyword", ""),
            list(current_item.get("brands", matched_brands)),
            platforms_for_matched_item(current_item, platform_hint),
        )

    for item in guide_items or []:
        if str(item.get("task_name") or "").strip() != task_name_text:
            continue
        return build_matched_pairs(
            item.get("keyword", ""),
            list(item.get("brands", matched_brands)),
            platforms_for_matched_item(item, platform_hint),
        )
    return []


def build_keyword_updates_from_batch(
    batch: dict,
    *,
    image_paths: list[str],
    detected_platforms: list[str],
) -> list[dict]:
    matched_pairs = expand_matched_pair_slots(batch.get("matched_pairs") or [])
    normalized_paths = []
    seen_paths = set()
    for path in image_paths or []:
        path_text = str(path).strip()
        if not path_text or path_text in seen_paths:
            continue
        seen_paths.add(path_text)
        normalized_paths.append(path_text)
    normalized_platforms = normalized_platform_list(detected_platforms or [])
    updates = []
    path_index = 0
    for pair in matched_pairs:
        keyword = str(pair.get("keyword") or "").strip()
        brand = str(pair.get("brand") or "").strip()
        pair_platforms = normalized_platform_list(pair.get("platforms") or [])
        platform_name = (
            (pair_platforms[0] if pair_platforms else "")
            or (normalized_platforms[0] if normalized_platforms else "")
        )
        image_path = ""
        if path_index < len(normalized_paths):
            image_path = normalized_paths[path_index]
            path_index += 1
        if not keyword:
            continue
        updates.append({
            "keyword": keyword,
            "brand": brand,
            "run_success": True,
            "screenshot_saved": bool(image_path),
            "failure_reason": "" if image_path else "screenshot_save_failed",
            "platform": platform_name,
            "image_path": image_path,
        })
    if len(matched_pairs) > len(normalized_paths):
        print(
            f"[Recognition] 关键词与截图严格一对一绑定，"
            f"当前仅有 {len(normalized_paths)} 张唯一截图，"
            f"剩余 {len(matched_pairs) - len(normalized_paths)} 个关键词保留缺口"
        )
    return updates


__all__ = [
    "PLATFORM_DISPLAY",
    "PLATFORM_ID_ALIASES",
    "build_keyword_updates_from_batch",
    "build_matched_pairs",
    "build_task_matched_pairs",
    "display_platform_name",
    "expand_matched_pair_slots",
    "normalize_keyword_brand_pair",
    "normalize_platform_id",
    "normalized_platform_list",
    "platforms_for_matched_item",
    "remaining_current_platforms_after_match",
    "serialize_matched_pairs",
]
