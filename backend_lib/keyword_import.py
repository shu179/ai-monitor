"""Keyword import parsing helpers for the local web backend."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from backend_lib.article_import import (
    _normalize_import_header,
    _read_article_import_tables,
    _stringify_table_cell,
)


KEYWORD_IMPORT_EXTENSIONS = {".xlsx", ".xlsm", ".csv", ".docx", ".doc", ".txt"}
MAX_KEYWORD_IMPORT_CHARS = 25

_KEYWORD_IMPORT_HEADER_ALIASES = {
    "关键词",
    "检索关键词",
    "搜索关键词",
    "监控关键词",
    "查询关键词",
    "关键词组",
    "搜索词",
    "检索词",
    "查询词",
    "词条",
    "keyword",
    "keywords",
    "searchkeyword",
    "searchkeywords",
    "query",
    "queries",
    "searchterm",
    "searchterms",
}


def _match_keyword_import_header(value: Any) -> bool:
    normalized = _normalize_import_header(value)
    if not normalized:
        return False
    if normalized in _KEYWORD_IMPORT_HEADER_ALIASES:
        return True
    return (
        any(token in normalized for token in ("关键词", "搜索词", "检索词", "查询词"))
        and not any(token in normalized for token in ("数量", "个数", "次数", "排名", "结果", "平台"))
        and len(normalized) <= 18
    )


def _decode_plain_text(data: bytes) -> str:
    last_error = ""
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "utf-16le"):
        try:
            return data.decode(encoding)
        except Exception as exc:
            last_error = str(exc)
    raise ValueError(f"无法识别文本编码{f'：{last_error}' if last_error else ''}")


def _normalize_keyword_import_candidate(value: Any) -> str:
    text = _stringify_table_cell(value)
    if not text:
        return ""
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text).strip()
    text = re.sub(r"^[\s\-_*•·●○◆◇■□]+", "", text).strip()
    text = re.sub(r"^(?:\d+|[一二三四五六七八九十]+)\s*[\.、\)）:：]\s*", "", text).strip()
    text = re.sub(
        r"^(?:关键词|检索关键词|搜索关键词|监控关键词|查询关键词|搜索词|检索词|查询词|keyword|keywords|query|queries)\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = text.strip(" \t\r\n\"'“”‘’`·,，;；、。")
    if not text:
        return ""
    if _match_keyword_import_header(text) and len(text) <= 18:
        return ""
    if re.search(r"https?://|www\.|[\w.+-]+@[\w.-]+", text, re.IGNORECASE):
        return ""
    if len(text) < 2 or len(text) > MAX_KEYWORD_IMPORT_CHARS:
        return ""
    if len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)) < 2:
        return ""
    return text


def _keyword_import_token_length(text: Any) -> int:
    """统计中英文字符数（忽略空白和标点），用于判断片段是不是真正"成形的关键词"。"""
    return len(re.findall(r"[一-鿿A-Za-z0-9]", str(text or "")))


def _looks_like_compound_keyword_line(pieces: list[str]) -> bool:
    """判断一行按"、""，"切出来的片段是不是「长尾主关键词 + 一串短卖点」混杂。

    例如 "不飞粉的大地色眼影推荐，适合新手，日常通勤，自然百搭" 切出 4 段，
    最长 11 个字、最短 4 个字，长度极不均匀——这显然是"主词 + 卖点"被一起粘进来了，
    旧逻辑会把"适合新手"、"日常通勤"、"自然百搭"都当成独立关键词，让导出的"关键词大类"
    出现一堆 3-4 字短词、并且让那些短词大量误命中其它品牌的文章。
    """
    if len(pieces) < 2:
        return False
    char_lens = [_keyword_import_token_length(piece) for piece in pieces]
    return max(char_lens) >= 10 and min(char_lens) <= 5


def _fallback_split_by_punctuation(text: str) -> list[str]:
    """单 cell 整体作为关键词失败时（太长或被规范化拒绝），用顿号/逗号兜底切一下。"""
    pieces = [piece.strip() for piece in re.split(r"[,，;；|｜、]+", text) if piece.strip()]
    if not pieces:
        return []
    if _looks_like_compound_keyword_line(pieces):
        return [max(pieces, key=_keyword_import_token_length)]
    return pieces


def _split_keyword_import_candidates(value: Any) -> list[str]:
    """把一个 cell（或文本块）解析成关键词列表。

    用户场景以"每个 cell 一个完整关键词"为主，所以默认行为是：
      - 单 cell 不含换行 → 整体作为一个关键词，**不**按顿号/逗号再切，
        避免把"干皮水润保湿、服帖不卡粉的粉底液推荐"这种本身就含顿号的长尾词切碎。
      - cell 内含换行（常见于从 Word / 网页 / AI 输出粘贴进来带回车的脏数据）
        → 按行处理，每行各自再走一次"整体当关键词"的判断。
      - 整体超过 25 字或规范化拒绝 → 退回顿号切，并对"长尾 + 一串短卖点"模式只取最长那段。
    """
    text = _stringify_table_cell(value)
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"^(?:关键词|检索关键词|搜索关键词|监控关键词|查询关键词|搜索词|检索词|查询词|keyword|keywords|query|queries)\s*[:：]\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )

    if "\n" not in text:
        whole = _normalize_keyword_import_candidate(text)
        if whole:
            return [whole]
        return [
            candidate
            for candidate in (
                _normalize_keyword_import_candidate(part)
                for part in _fallback_split_by_punctuation(text)
            )
            if candidate
        ]

    raw_parts: list[str] = []
    for line in re.split(r"\n+", text):
        stripped_line = line.strip()
        if not stripped_line:
            continue
        whole_line = _normalize_keyword_import_candidate(stripped_line)
        if whole_line:
            raw_parts.append(stripped_line)
            continue
        raw_parts.extend(_fallback_split_by_punctuation(stripped_line))
    return [
        candidate
        for candidate in (_normalize_keyword_import_candidate(part) for part in raw_parts)
        if candidate
    ]


def _dedupe_keyword_import_candidates(values: list[str], *, limit: int = 500) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = re.sub(r"\s+", " ", value).strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _detect_keyword_import_columns(rows: list[list[str]]) -> tuple[int, list[int]]:
    best_index = -1
    best_columns: list[int] = []
    best_score = 0
    for row_index, row in enumerate(rows[:10]):
        columns = [col_index for col_index, cell in enumerate(row) if _match_keyword_import_header(cell)]
        if not columns:
            continue
        score = len(columns) * 4
        if row_index <= 2:
            score += 2
        if score > best_score:
            best_index = row_index
            best_columns = columns
            best_score = score
    return best_index, best_columns


def _extract_keyword_import_from_tables(file_name: str, data: bytes) -> tuple[list[str], dict[str, Any]]:
    tables = _read_article_import_tables(file_name, data)
    keywords: list[str] = []
    details: list[dict[str, Any]] = []
    for sheet_name, rows in tables:
        header_index, columns = _detect_keyword_import_columns(rows)
        before_count = len(keywords)
        if columns:
            for row in rows[header_index + 1:]:
                for col_index in columns:
                    if col_index < len(row):
                        keywords.extend(_split_keyword_import_candidates(row[col_index]))
            details.append({
                "sheet": sheet_name,
                "mode": "header",
                "columns": columns,
                "count": len(keywords) - before_count,
            })
            continue

        for row in rows:
            non_empty = [_stringify_table_cell(cell) for cell in row if _stringify_table_cell(cell)]
            if not non_empty:
                continue
            cells = non_empty if len(non_empty) <= 3 else non_empty[:1]
            for cell in cells:
                keywords.extend(_split_keyword_import_candidates(cell))
        details.append({
            "sheet": sheet_name,
            "mode": "freeform",
            "count": len(keywords) - before_count,
        })
    return _dedupe_keyword_import_candidates(keywords), {"sheets": details}


def _extract_docx_text_lines(data: bytes) -> list[str]:
    lines: list[str] = []
    xml_names: list[str] = []
    with zipfile.ZipFile(BytesIO(data)) as archive:
        xml_names = [
            name for name in archive.namelist()
            if name == "word/document.xml"
            or re.fullmatch(r"word/(?:header|footer|footnotes|endnotes)\d*\.xml", name)
        ]
        for name in xml_names:
            xml_data = archive.read(name)
            root = ElementTree.fromstring(xml_data)
            for paragraph in root.iter():
                if not str(paragraph.tag).endswith("}p"):
                    continue
                text_parts = [
                    node.text or ""
                    for node in paragraph.iter()
                    if str(node.tag).endswith("}t") and node.text
                ]
                text = "".join(text_parts).strip()
                if text:
                    lines.append(text)
    return lines


def _extract_legacy_doc_text_lines(data: bytes) -> list[str]:
    lines: list[str] = []
    for encoding in ("utf-16le", "gb18030", "latin1"):
        try:
            text = data.decode(encoding, errors="ignore")
        except Exception:
            continue
        matches = re.findall(r"[\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9\s_\-+&（）()《》【】/／:：,，、.。]{1,120}", text)
        lines.extend(match.strip() for match in matches if match.strip())
    return lines


def _extract_keyword_import_from_text_lines(lines: list[str]) -> list[str]:
    keywords: list[str] = []
    for line in lines:
        stripped = _stringify_table_cell(line)
        if not stripped:
            continue
        if re.search(r"(关键词|搜索词|检索词|查询词|keyword|query)\s*[:：]", stripped, re.IGNORECASE):
            keywords.extend(_split_keyword_import_candidates(stripped))
            continue
        if re.match(r"^\s*(?:[\-*•·●○]|\d+[\.、\)）]|[一二三四五六七八九十]+[、\.])\s*", stripped):
            keywords.extend(_split_keyword_import_candidates(stripped))
            continue
        if len(stripped) <= 60:
            keywords.extend(_split_keyword_import_candidates(stripped))
    return _dedupe_keyword_import_candidates(keywords)


def _extract_keyword_import_items(file_name: str, data: bytes) -> tuple[list[str], dict[str, Any]]:
    original_name = Path(str(file_name or "keywords.xlsx")).name
    suffix = Path(original_name).suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".csv"}:
        return _extract_keyword_import_from_tables(original_name, data)
    if suffix == ".docx":
        lines = _extract_docx_text_lines(data)
        keywords = _extract_keyword_import_from_text_lines(lines)
        return keywords, {"line_count": len(lines), "source": "docx"}
    if suffix == ".doc":
        lines = _extract_legacy_doc_text_lines(data)
        keywords = _extract_keyword_import_from_text_lines(lines)
        return keywords, {"line_count": len(lines), "source": "doc"}
    if suffix == ".txt":
        text = _decode_plain_text(data)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        keywords = _extract_keyword_import_from_text_lines(lines)
        return keywords, {"line_count": len(lines), "source": "txt"}
    raise ValueError("目前支持导入 .xlsx/.xlsm/.csv 表格和 .docx/.doc Word 文档")
