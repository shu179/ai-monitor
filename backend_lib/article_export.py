"""Article XLSX export helpers for the local web backend."""

from __future__ import annotations

import re
import unicodedata
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from core.article_store import (
    build_article_export_keyword_order,
    resolve_article_export_keywords,
    resolve_article_export_source,
)
from core.time_utils import local_now


def _sanitize_excel_text(value: Any) -> str:
    text = str(value or "")
    return "".join(
        ch for ch in text
        if ch in ("\t", "\n", "\r") or ord(ch) >= 32
    )


def _excel_column_name(index: int) -> str:
    result = ""
    current = max(1, int(index))
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _excel_safe_sheet_name(value: str, fallback: str = "文章汇总") -> str:
    sheet_name = str(value or "").strip() or fallback
    sheet_name = sheet_name[:31].replace("/", "_").replace("\\", "_").replace(":", "_").replace("*", "_").replace("?", "_").replace("[", "(").replace("]", ")")
    return sheet_name or fallback


def _build_excel_inline_cell(cell_ref: str, value: Any, style_id: int) -> str:
    text = _sanitize_excel_text(value)
    if text:
        escaped = xml_escape(text)
        if text.startswith((" ", "\t")) or text.endswith((" ", "\t", "\n")):
            return f'<c r="{cell_ref}" t="inlineStr" s="{style_id}"><is><t xml:space="preserve">{escaped}</t></is></c>'
        return f'<c r="{cell_ref}" t="inlineStr" s="{style_id}"><is><t>{escaped}</t></is></c>'
    return f'<c r="{cell_ref}" t="inlineStr" s="{style_id}"><is><t></t></is></c>'


def _build_excel_rich_inline_cell(cell_ref: str, runs: list[dict[str, Any]], style_id: int) -> str:
    rich_runs: list[str] = []
    for run in runs:
        text = _sanitize_excel_text(run.get("text", ""))
        if not text:
            continue
        props = [
            f'<rFont val="{xml_escape(str(run.get("font", "Microsoft YaHei UI")))}"/>',
            '<family val="2"/>',
            f'<sz val="{xml_escape(str(run.get("size", "10")))}"/>',
            f'<color rgb="{xml_escape(str(run.get("color", "FF173A43")))}"/>',
        ]
        if run.get("bold"):
            props.insert(0, "<b/>")
        if run.get("italic"):
            props.insert(0, "<i/>")
        space_attr = ' xml:space="preserve"' if text.startswith((" ", "\t")) or text.endswith((" ", "\t", "\n")) else ""
        rich_runs.append(
            "<r><rPr>"
            + "".join(props)
            + f"</rPr><t{space_attr}>{xml_escape(text)}</t></r>"
        )
    if not rich_runs:
        return _build_excel_inline_cell(cell_ref, "", style_id)
    return f'<c r="{cell_ref}" t="inlineStr" s="{style_id}"><is>{"".join(rich_runs)}</is></c>'


def _excel_display_units(value: Any) -> int:
    total = 0
    for ch in str(value or ""):
        total += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return total


def _article_export_row_height(title: str, url: str) -> int:
    title_lines = max(1, (_excel_display_units(title) + 43) // 44)
    link_lines = max(1, (len(str(url or "")) + 67) // 68)
    lines = min(4, max(title_lines, link_lines))
    return max(34, min(72, 24 + (lines * 13)))


def _article_export_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).timestamp()
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000 if value > 10_000_000_000 else float(value)

    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        number = float(text)
        return number / 1000 if number > 10_000_000_000 else number

    normalized = text.replace("Z", "+00:00")
    candidates = [normalized, normalized.replace("/", "-"), normalized.replace(".", "-")]
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).timestamp()
        except Exception:
            pass

    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?", text)
    if not match:
        return 0
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        ).timestamp()
    except Exception:
        return 0


def _first_article_export_timestamp(article: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        timestamp = _article_export_timestamp(article.get(key))
        if timestamp > 0:
            return timestamp
    return 0


def _article_export_date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value or "").strip()
    if not text:
        return ""
    timestamp = _article_export_timestamp(text)
    if text.isdigit() and timestamp > 0:
        try:
            return datetime.fromtimestamp(timestamp).date().isoformat()
        except Exception:
            return ""

    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if not match:
        return text[:10]
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _first_article_export_date_text(article: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _article_export_date_text(article.get(key))
        if text:
            return text
    return ""


def _sort_articles_for_export(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(article: dict[str, Any]) -> tuple[float, float, str]:
        published = _first_article_export_timestamp(
            article,
            ("published_ts", "published_at", "published", "ts"),
        )
        imported = _first_article_export_timestamp(
            article,
            ("imported_at", "created_at", "ts"),
        )
        return (published or -1, imported or -1, str(article.get("id") or ""))

    return sorted(articles, key=sort_key)


def _article_export_keyword_sort_key(label: str, keyword_order: dict[str, int]) -> tuple[int, str]:
    text = str(label or "").strip()
    return (keyword_order.get(text, len(keyword_order) + 1), text)


def _article_export_items(
    articles: list[dict[str, Any]],
    *,
    show_keyword_category: bool,
    config: dict[str, Any] | None = None,
    task_name: str = "",
) -> list[tuple[str, dict[str, Any]]]:
    if not show_keyword_category:
        return [("", article) for article in _sort_articles_for_export(articles)]

    keyword_order = build_article_export_keyword_order(config or {}, task_name)
    items: list[tuple[str, dict[str, Any]]] = []
    for article in articles:
        labels = resolve_article_export_keywords(article, config or {}, task_name) or ["未识别关键词"]
        for label in labels:
            items.append((label, article))

    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[tuple[int, str], float, float, str]:
        label, article = item
        published = _first_article_export_timestamp(
            article,
            ("published_ts", "published_at", "published", "ts"),
        )
        imported = _first_article_export_timestamp(
            article,
            ("imported_at", "created_at", "ts"),
        )
        return (
            _article_export_keyword_sort_key(label, keyword_order),
            published or -1,
            imported or -1,
            str(article.get("id") or ""),
        )

    return sorted(items, key=sort_key)


def _build_article_export_xlsx(
    *,
    brand_name: str,
    start_date: str,
    end_date: str,
    articles: list[dict[str, Any]],
    output_path: Path,
    show_keyword_category: bool = False,
    show_selfmedia_account: bool = True,
    config: dict[str, Any] | None = None,
    task_name: str = "",
) -> None:
    safe_sheet_name = _excel_safe_sheet_name(f"{brand_name}文章汇总")
    current_time = local_now().strftime("%Y-%m-%d %H:%M")
    filter_range = f"{start_date or '全部开始日期'} - {end_date or '全部结束日期'}"
    title_text = f"{brand_name} 文章汇总"
    subtitle_text = f"筛选范围：{filter_range}    导出时间：{current_time}"
    stat_text = f"共 {len(articles)} 篇文章记录"
    headers = ["来源", "类型", "文章标题", "文章链接", "发布时间"]
    if show_keyword_category:
        headers.insert(0, "关键词大类")
    export_items = _article_export_items(
        articles,
        show_keyword_category=show_keyword_category,
        config=config,
        task_name=task_name,
    )
    column_count = len(headers)
    last_col = _excel_column_name(column_count)

    sheet_rows: list[str] = []

    title_cells = [
        _build_excel_inline_cell("A1", title_text, 1),
        _build_excel_inline_cell(f"{last_col}1", "", 1),
    ]
    sheet_rows.append('<row r="1" ht="36" customHeight="1">' + "".join(title_cells) + "</row>")

    subtitle_cells = [
        _build_excel_inline_cell("A2", subtitle_text, 2),
        _build_excel_inline_cell(f"{last_col}2", "", 2),
    ]
    sheet_rows.append('<row r="2" ht="24" customHeight="1">' + "".join(subtitle_cells) + "</row>")

    stat_cells = [
        _build_excel_inline_cell("A3", stat_text, 3),
        _build_excel_inline_cell(f"{last_col}3", "", 3),
    ]
    sheet_rows.append('<row r="3" ht="24" customHeight="1">' + "".join(stat_cells) + "</row>")
    sheet_rows.append('<row r="4" ht="8" customHeight="1"></row>')

    header_cells = [
        _build_excel_inline_cell(f"{_excel_column_name(col_idx)}5", header, 4)
        for col_idx, header in enumerate(headers, start=1)
    ]
    sheet_rows.append('<row r="5" ht="30" customHeight="1">' + "".join(header_cells) + "</row>")

    for row_idx, (keyword_category, article) in enumerate(export_items, start=6):
        title = str(article.get("title", "") or "").strip()
        url = str(article.get("url", "") or "").strip()
        row_values = [
            resolve_article_export_source(article, show_selfmedia_account=show_selfmedia_account),
            "权威媒体" if str(article.get("media_type", "") or "").strip() == "authority" else "自媒体",
            title,
            url,
            _first_article_export_date_text(article, ("published_at", "published", "published_ts", "ts")),
        ]
        if show_keyword_category:
            row_values.insert(0, keyword_category)
        body_style_id = 5 if row_idx % 2 == 0 else 6
        link_style_id = 8 if row_idx % 2 == 0 else 9
        center_style_id = 10 if row_idx % 2 == 0 else 11
        row_style_ids = [body_style_id, center_style_id, body_style_id, link_style_id, center_style_id]
        if show_keyword_category:
            row_style_ids.insert(0, body_style_id)
        row_cells = [
            _build_excel_inline_cell(f"{_excel_column_name(col_idx)}{row_idx}", value, row_style_ids[col_idx - 1])
            for col_idx, value in enumerate(row_values, start=1)
        ]
        sheet_rows.append(
            f'<row r="{row_idx}" ht="{_article_export_row_height(title, url)}" customHeight="1">'
            f'{"".join(row_cells)}</row>'
        )

    footer_row_idx = len(export_items) + 8
    footer_cells = [
        _build_excel_rich_inline_cell(
            last_col + str(footer_row_idx),
            [
                {"text": "Surfaced", "font": "Palatino Linotype", "size": "11", "color": "FF173A43", "bold": True},
                {"text": ".", "font": "Palatino Linotype", "size": "11", "color": "FF14C7F3", "bold": True},
            ],
            7,
        ),
    ]
    sheet_rows.append(f'<row r="{footer_row_idx}" ht="26" customHeight="1">{"".join(footer_cells)}</row>')

    merges_xml = (
        '<mergeCells count="3">'
        f'<mergeCell ref="A1:{last_col}1"/>'
        f'<mergeCell ref="A2:{last_col}2"/>'
        f'<mergeCell ref="A3:{last_col}3"/>'
        "</mergeCells>"
    )
    col_widths = [20, 11, 46, 68, 19]
    if show_keyword_category:
        col_widths.insert(0, 24)
    cols_xml = "<cols>" + "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(col_widths, start=1)
    ) + "</cols>"

    worksheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetPr><tabColor rgb="FF14C7F3"/></sheetPr>'
        f'<dimension ref="A1:{last_col}{footer_row_idx}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane ySplit="5" topLeftCell="A6" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"{cols_xml}<sheetFormatPr defaultRowHeight=\"24\"/>"
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        f"{merges_xml}"
        f'<autoFilter ref="A5:{last_col}{max(5, len(export_items) + 5)}"/>'
        '<pageMargins left="0.4" right="0.4" top="0.55" bottom="0.55" header="0.3" footer="0.3"/>'
        "</worksheet>"
    )

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="8">
    <font><sz val="10.5"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FF334155"/></font>
    <font><b/><sz val="18"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FF173A43"/></font>
    <font><sz val="10"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FF5D6C88"/></font>
    <font><b/><sz val="10.5"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FF173A43"/></font>
    <font><b/><sz val="10.5"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FFFFFFFF"/></font>
    <font><sz val="10.5"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FF334155"/></font>
    <font><sz val="9.5"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FF16798C"/></font>
    <font><i/><sz val="10"/><name val="Microsoft YaHei UI"/><family val="2"/><color rgb="FF5D6C88"/></font>
  </fonts>
  <fills count="8">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFCFDFF"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFD9F7FE"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF173A43"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF8FDFF"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF1FCFF"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="3">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFE2F6FA"/></left>
      <right style="thin"><color rgb="FFE2F6FA"/></right>
      <top style="thin"><color rgb="FFE2F6FA"/></top>
      <bottom style="thin"><color rgb="FFE2F6FA"/></bottom>
      <diagonal/>
    </border>
    <border>
      <left style="thin"><color rgb="FF14C7F3"/></left>
      <right style="thin"><color rgb="FF14C7F3"/></right>
      <top style="thin"><color rgb="FF14C7F3"/></top>
      <bottom style="thin"><color rgb="FF14C7F3"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="12">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">
      <alignment horizontal="left" vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="2" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">
      <alignment horizontal="left" vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="3" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">
      <alignment horizontal="left" vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="4" fillId="4" borderId="2" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="5" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="5" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="7" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">
      <alignment horizontal="right" vertical="center"/>
    </xf>
    <xf numFmtId="0" fontId="6" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="6" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="5" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="5" fillId="6" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="top" wrapText="1"/>
    </xf>
  </cellXfs>
  <cellStyles count="1">
    <cellStyle name="Normal" xfId="0" builtinId="0"/>
  </cellStyles>
</styleSheet>"""

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>'
        f'<sheet name="{xml_escape(safe_sheet_name)}" sheetId="1" r:id="rId1"/>'
        '</sheets>'
        '</workbook>'
    )

    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

    root_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

    workbook_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

    created_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>Surfaced</dc:creator>'
        '<cp:lastModifiedBy>Surfaced</cp:lastModifiedBy>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{created_at}</dcterms:modified>'
        '</cp:coreProperties>'
    )
    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Surfaced</Application>
</Properties>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", root_rels_xml)
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
