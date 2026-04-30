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


def _split_keyword_import_candidates(value: Any) -> list[str]:
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
    parts = re.split(r"[\n,，;；|｜、]+", text)
    return [
        candidate
        for candidate in (_normalize_keyword_import_candidate(part) for part in parts)
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
