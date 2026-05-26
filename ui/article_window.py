"""
文章录入管理窗口

功能：
- 粘贴 URL → 自动抓取标题/平台/媒体类型 → 存入 article_store
- 展示已录入文章列表，支持切换媒体类型
- 按任务组统计文章数
"""

from __future__ import annotations

from collections import defaultdict
import calendar
from datetime import datetime, date, timedelta
import re
import unicodedata
from pathlib import Path
from tkinter import filedialog
from xml.sax.saxutils import escape as xml_escape
import zipfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from core.article_store import (
    add_article,
    analyze_article_matches,
    build_article_export_keyword_order,
    build_article_export_keyword_plan,
    build_article_export_keyword_signature,
    get_articles,
    extract_domain,
    resolve_article_display_url,
    resolve_article_export_source,
    resolve_article_export_keywords,
    resolve_article_source,
    update_media_type,
)
from core.article_fetcher import fetch_article_info
from core.app_paths import resolve_app_path
from core.history import get_task_brand_names
from core.notifier import WeComNotifier
from ui.tk_compat import install_global_tk_behaviors

# ── 颜色令牌（与 main_window 一致）─────────────────────────────────────────
_BG      = "#F0F0F0"
_SURFACE = "#FFFFFF"
_BORDER  = "#D0D0D0"
_ACCENT  = "#2D7FF9"
_RED     = "#D93025"
_GRAY    = "#888888"

_AUTHORITY_COLOR = "#2D7FF9"   # 蓝色 = 权威媒体
_SELFMEDIA_COLOR = "#F5A623"   # 橙色 = 自媒体
_BAR_BG = "#EEF2F7"
_BAR_GRID = "#D8DEE8"
_BAR_TEXT = "#475569"
_BAR_EMPTY = "#CBD5E1"
_ACTIVE_BG = "#DCEBFF"
_INACTIVE_BG = "#F3F4F6"


def _parse_date_text(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    cur = start
    out = []
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _excel_col_name(index: int) -> str:
    name = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _sanitize_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]\:\*\?\/\\]", "_", str(name or "").strip())
    cleaned = cleaned or "Articles"
    return cleaned[:31]


def _article_short_title(title: str, limit: int = 26) -> str:
    text = str(title or "").strip()
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 1)] + "…"


def _excel_display_units(value) -> int:
    total = 0
    for ch in str(value or ""):
        total += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return total


def _article_export_row_height(title: str, url: str) -> int:
    title_lines = max(1, (_excel_display_units(title) + 43) // 44)
    link_lines = max(1, (len(str(url or "")) + 67) // 68)
    lines = min(4, max(title_lines, link_lines))
    return max(34, min(72, 24 + (lines * 13)))


def _article_export_timestamp(value) -> float:
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


def _first_article_export_timestamp(article: dict, keys: tuple[str, ...]) -> float:
    for key in keys:
        timestamp = _article_export_timestamp(article.get(key))
        if timestamp > 0:
            return timestamp
    return 0


def _article_export_date_text(value) -> str:
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


def _first_article_export_date_text(article: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _article_export_date_text(article.get(key))
        if text:
            return text
    return ""


def _sort_articles_for_export(rows: list[dict]) -> list[dict]:
    def sort_key(article: dict) -> tuple[float, float, str]:
        published = _first_article_export_timestamp(
            article,
            ("published_ts", "published_at", "published", "ts"),
        )
        imported = _first_article_export_timestamp(
            article,
            ("imported_at", "created_at", "ts"),
        )
        return (published or -1, imported or -1, str(article.get("id") or ""))

    return sorted(rows, key=sort_key)


ARTICLE_EXPORT_KEYWORD_UNKNOWN_LABEL = "未知"


def _article_export_keyword_sort_key(label: str, keyword_order: dict[str, int]) -> tuple[int, str]:
    text = str(label or "").strip()
    return (keyword_order.get(text, len(keyword_order) + 1), text)


def _article_export_items(
    rows: list[dict],
    *,
    show_keyword_category: bool,
    keyword_order: dict[str, int] | None = None,
) -> list[tuple[str, dict]]:
    rows = _sort_articles_for_export(rows)
    if not show_keyword_category:
        return [("", row) for row in rows]
    resolved_keyword_order = keyword_order or {}
    items: list[tuple[str, dict]] = []
    for row in rows:
        labels = [
            str(label or "").strip()
            for label in (row.get("keyword_categories") or [])
            if str(label or "").strip()
        ]
        labels.sort(key=lambda label: _article_export_keyword_sort_key(label, resolved_keyword_order))
        cell_value = "、".join(labels) if labels else ARTICLE_EXPORT_KEYWORD_UNKNOWN_LABEL
        items.append((cell_value, row))

    return items


def _build_articles_xlsx(
    path: Path,
    sheet_name: str,
    rows: list[dict],
    *,
    include_brand: bool = False,
    show_keyword_category: bool = False,
    keyword_order: dict[str, int] | None = None,
) -> None:
    headers = ["发表平台", "类型", "文章标题", "文章链接", "发表时间"]
    if include_brand:
        headers.insert(0, "品牌")
    if show_keyword_category:
        headers.insert(0, "关键词大类")
    export_items = _article_export_items(
        rows,
        show_keyword_category=show_keyword_category,
        keyword_order=keyword_order,
    )
    footer_col_idx = len(headers)
    last_col_name = _excel_col_name(footer_col_idx)

    def cell_ref(row_idx: int, col_idx: int) -> str:
        return f"{_excel_col_name(col_idx)}{row_idx}"

    def cell_xml(value: str, style_id: int, row_idx: int, col_idx: int) -> str:
        text = xml_escape(str(value or ""))
        ref = cell_ref(row_idx, col_idx)
        return f'<c r="{ref}" t="inlineStr" s="{style_id}"><is><t xml:space="preserve">{text}</t></is></c>'

    def rich_cell_xml(row_idx: int, col_idx: int, style_id: int) -> str:
        ref = cell_ref(row_idx, col_idx)
        return (
            f'<c r="{ref}" t="inlineStr" s="{style_id}"><is>'
            '<r><rPr><b/><rFont val="Palatino Linotype"/><family val="1"/><sz val="11"/><color rgb="FF173A43"/></rPr><t>Surfaced</t></r>'
            '<r><rPr><b/><rFont val="Palatino Linotype"/><family val="1"/><sz val="11"/><color rgb="FF14C7F3"/></rPr><t>.</t></r>'
            '</is></c>'
        )

    def row_xml(cells: list[tuple[str, int]], row_idx: int, height: int) -> str:
        cell_nodes = [
            cell_xml(value, style_id, row_idx, idx + 1)
            for idx, (value, style_id) in enumerate(cells)
        ]
        return f'<row r="{row_idx}" ht="{height}" customHeight="1">{"".join(cell_nodes)}</row>'

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="5">
    <font><sz val="10.5"/><color rgb="FF334155"/><name val="Microsoft YaHei UI"/><family val="2"/></font>
    <font><b/><sz val="10.5"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei UI"/><family val="2"/></font>
    <font><sz val="9.5"/><color rgb="FF16798C"/><name val="Microsoft YaHei UI"/><family val="2"/></font>
    <font><sz val="10.5"/><color rgb="FF334155"/><name val="Microsoft YaHei UI"/><family val="2"/></font>
    <font><i/><sz val="10"/><color rgb="FF5D6C88"/><name val="Microsoft YaHei UI"/><family val="2"/></font>
  </fonts>
  <fills count="5">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF173A43"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF8FDFF"/><bgColor indexed="64"/></patternFill></fill>
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
  <cellXfs count="9">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="2" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">
      <alignment horizontal="center" vertical="center" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="2" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1">
      <alignment horizontal="left" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="3" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1">
      <alignment horizontal="center" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="3" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1">
      <alignment horizontal="center" vertical="top" wrapText="1"/>
    </xf>
    <xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1">
      <alignment horizontal="right" vertical="center"/>
    </xf>
  </cellXfs>
  <cellStyles count="1">
    <cellStyle name="Normal" xfId="0" builtinId="0"/>
  </cellStyles>
</styleSheet>
"""

    data_rows = []
    row_idx = 2
    for keyword_category, item in export_items:
        media_type = str(item.get("media_type") or "selfmedia").strip()
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        body_style_id = 4 if row_idx % 2 == 0 else 5
        link_style_id = 2 if row_idx % 2 == 0 else 3
        center_style_id = 6 if row_idx % 2 == 0 else 7
        row_cells = [
            (item.get("platform", ""), body_style_id),
            ("权威媒体" if media_type == "authority" else "自媒体", center_style_id),
            (title, body_style_id),
            (url, link_style_id),
            (_first_article_export_date_text(item, ("published_at", "published", "published_ts", "ts")), center_style_id),
        ]
        if include_brand:
            row_cells.insert(0, (item.get("brand", ""), body_style_id))
        if show_keyword_category:
            row_cells.insert(0, (keyword_category, body_style_id))
        data_rows.append(row_xml(row_cells, row_idx, _article_export_row_height(title, url)))
        row_idx += 1

    footer_row = row_idx
    footer_xml = f'<row r="{footer_row}" ht="26" customHeight="1">{rich_cell_xml(footer_row, footer_col_idx, 8)}</row>'

    sheet_rows = [row_xml([(header, 1) for header in headers], 1, 30), *data_rows, footer_xml]
    sheet_dimension = f"A1:{last_col_name}{max(footer_row, len(export_items) + 1)}"
    col_widths = [22, 11, 46, 68, 19]
    if include_brand:
        col_widths.insert(0, 16)
    if show_keyword_category:
        col_widths.insert(0, 24)
    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(col_widths, start=1)
    )
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetPr><tabColor rgb="FF14C7F3"/></sheetPr>
  <dimension ref="{sheet_dimension}"/>
  <sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="24"/>
  <cols>
    {cols_xml}
  </cols>
  <sheetData>
    {''.join(sheet_rows)}
  </sheetData>
  <autoFilter ref="A1:{last_col_name}{len(export_items) + 1}"/>
  <pageMargins left="0.4" right="0.4" top="0.55" bottom="0.55" header="0.3" footer="0.3"/>
</worksheet>
"""

    workbook_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="{xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>
"""

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

    core_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:dcterms="http://purl.org/dc/terms/"
    xmlns:dcmitype="http://purl.org/dc/dcmitype/"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>Surfaced</dc:creator>
  <cp:lastModifiedBy>Surfaced</cp:lastModifiedBy>
  <dc:title>Article Export</dc:title>
</cp:coreProperties>
"""

    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
    xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Surfaced</Application>
</Properties>
"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("docProps/app.xml", app_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


class _CalendarPopup:
    """轻量级日期选择器。"""

    def __init__(self, parent: tk.Misc, initial_date: date, on_select):
        self._on_select = on_select
        self._selected = initial_date
        self._year = initial_date.year
        self._month = initial_date.month
        self._win = tk.Toplevel(parent)
        self._win.title("选择日期")
        self._win.transient(parent)
        self._win.grab_set()
        self._win.resizable(False, False)
        self._win.configure(bg=_BG)
        self._win.protocol("WM_DELETE_WINDOW", self._win.destroy)
        install_global_tk_behaviors(self._win)

        self._header_var = tk.StringVar()
        self._days_frame = tk.Frame(self._win, bg=_BG, padx=10, pady=10)

        header = tk.Frame(self._win, bg=_BG, padx=10, pady=10)
        header.pack(fill=tk.X)
        ttk.Button(header, text="◀", width=3, command=self._prev_month).pack(side=tk.LEFT)
        tk.Label(header, textvariable=self._header_var, bg=_BG, fg="#111827",
                 font=("PingFang SC", 11, "bold")).pack(side=tk.LEFT, expand=True)
        ttk.Button(header, text="▶", width=3, command=self._next_month).pack(side=tk.RIGHT)

        week_bar = tk.Frame(self._win, bg=_BG, padx=10)
        week_bar.pack(fill=tk.X)
        for label in ["一", "二", "三", "四", "五", "六", "日"]:
            tk.Label(week_bar, text=label, bg=_BG, fg=_GRAY, width=4).pack(side=tk.LEFT)

        self._days_frame.pack(fill=tk.BOTH, expand=True)
        self._render()

        self._win.update_idletasks()
        px = parent.winfo_rootx() + 40
        py = parent.winfo_rooty() + 80
        self._win.geometry(f"+{px}+{py}")

    def _prev_month(self):
        if self._month == 1:
            self._month = 12
            self._year -= 1
        else:
            self._month -= 1
        self._render()

    def _next_month(self):
        if self._month == 12:
            self._month = 1
            self._year += 1
        else:
            self._month += 1
        self._render()

    def _render(self):
        for child in self._days_frame.winfo_children():
            child.destroy()

        self._header_var.set(f"{self._year} 年 {self._month} 月")
        cal = calendar.Calendar(firstweekday=0)
        weeks = cal.monthdayscalendar(self._year, self._month)

        for week in weeks:
            row = tk.Frame(self._days_frame, bg=_BG)
            row.pack(fill=tk.X, pady=1)
            for day in week:
                if day == 0:
                    tk.Label(row, text="", width=4, bg=_BG).pack(side=tk.LEFT, padx=1, pady=1)
                    continue
                current = date(self._year, self._month, day)
                is_selected = current == self._selected
                btn = tk.Button(
                    row,
                    text=str(day),
                    width=4,
                    relief=tk.FLAT,
                    bd=0,
                    cursor="hand2",
                    bg=_ACTIVE_BG if is_selected else _SURFACE,
                    fg="#111827",
                    activebackground=_ACTIVE_BG,
                    command=lambda d=current: self._choose(d),
                )
                btn.pack(side=tk.LEFT, padx=1, pady=1)

    def _choose(self, value: date):
        self._selected = value
        try:
            self._on_select(value)
        finally:
            self._win.destroy()


class ArticleWindow:
    """文章录入管理弹窗（Toplevel）。

    参数
    ----
    root : tk.Tk 或 tk.Toplevel
        父窗口（由 TrayApp 的主线程传入）。
    config : dict
        完整的 YAML config，用于 fetch_article_info / match_tasks。
    """

    def __init__(self, root: tk.Misc, config: dict, task_name: str | None = None):
        self._root   = root
        self._config = config
        self._task_name = str(task_name or "").strip()
        self._fixed_brand = bool(self._task_name)
        self._win    = tk.Toplevel(root)
        self._win.title(
            f"{self._task_name} - 品牌文章汇总" if self._fixed_brand else "文章录入管理"
        )
        self._win.geometry("1080x760")
        self._win.minsize(840, 540)
        self._win.configure(bg=_BG)

        # 保证单例：关闭时释放引用
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._articles_cache: list[dict] = []
        self._media_filter = "all"  # all / authority / selfmedia
        self._bar_items: dict[int, dict] = {}
        self._date_popup = None

        self._build_ui()
        self._sync_media_buttons()
        self._refresh_list()
        install_global_tk_behaviors(self._win)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── 顶部输入区 ────────────────────────────────────────────────
        top = tk.Frame(self._win, bg=_BG, pady=8, padx=12)
        top.pack(fill=tk.X)

        tk.Label(top, text="文章 URL：", bg=_BG, font=("PingFang SC", 12)).pack(side=tk.LEFT)

        self._url_var = tk.StringVar()
        url_entry = ttk.Entry(top, textvariable=self._url_var, width=60)
        url_entry.pack(side=tk.LEFT, padx=(4, 8))
        url_entry.bind("<Return>", lambda _e: self._on_add())

        def _url_ctx(event, e=url_entry):
            m = tk.Menu(self._win, tearoff=0)
            m.add_command(label="复制", command=lambda: e.event_generate("<<CopyCompat>>"))
            m.add_command(label="剪切", command=lambda: e.event_generate("<<CutCompat>>"))
            m.add_command(label="粘贴", command=lambda: e.event_generate("<<PasteCompat>>"))
            m.add_command(label="全选", command=lambda: e.event_generate("<<SelectAllCompat>>"))
            m.post(event.x_root, event.y_root)
        url_entry.bind("<Button-3>", _url_ctx)
        url_entry.bind("<Button-2>", _url_ctx)

        self._add_btn = ttk.Button(top, text="录入", command=self._on_add)
        self._add_btn.pack(side=tk.LEFT)

        self._status_var = tk.StringVar(value="")
        tk.Label(top, textvariable=self._status_var, bg=_BG, fg=_GRAY,
                 font=("PingFang SC", 11)).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Button(top, text="刷新列表", command=self._refresh_list).pack(side=tk.RIGHT)

        # ── 筛选区 ────────────────────────────────────────────────
        filter_bar = tk.Frame(self._win, bg=_BG, padx=12, pady=4)
        filter_bar.pack(fill=tk.X)

        tk.Label(filter_bar, text="品牌：", bg=_BG, font=("PingFang SC", 11)).pack(side=tk.LEFT)
        self._brand_var = tk.StringVar(value=self._task_name or "全部品牌")
        if self._fixed_brand:
            self._brand_combo = None
            self._brand_value_label = tk.Label(
                filter_bar,
                textvariable=self._brand_var,
                bg=_BG,
                fg="#111827",
                font=("PingFang SC", 11, "bold"),
            )
            self._brand_value_label.pack(side=tk.LEFT, padx=(4, 12))
        else:
            self._brand_combo = ttk.Combobox(filter_bar, textvariable=self._brand_var, width=24, state="readonly")
            self._brand_combo.pack(side=tk.LEFT, padx=(4, 12))
            self._brand_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_view())

        tk.Label(filter_bar, text="时间范围：", bg=_BG, font=("PingFang SC", 11)).pack(side=tk.LEFT)
        self._range_var = tk.StringVar(value="最近30天")
        self._range_combo = ttk.Combobox(
            filter_bar,
            textvariable=self._range_var,
            width=12,
            state="readonly",
            values=["最近7天", "最近30天", "最近90天", "全部", "自定义"],
        )
        self._range_combo.pack(side=tk.LEFT, padx=(4, 8))
        self._range_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_range_preset())

        tk.Label(filter_bar, text="起始", bg=_BG, fg=_GRAY).pack(side=tk.LEFT)
        self._start_var = tk.StringVar()
        self._start_entry = ttk.Entry(filter_bar, textvariable=self._start_var, width=12)
        self._start_entry.pack(side=tk.LEFT, padx=(4, 8))
        self._start_entry.bind("<Button-1>", lambda _e: self._open_date_picker("start") or "break")
        tk.Label(filter_bar, text="结束", bg=_BG, fg=_GRAY).pack(side=tk.LEFT)
        self._end_var = tk.StringVar()
        self._end_entry = ttk.Entry(filter_bar, textvariable=self._end_var, width=12)
        self._end_entry.pack(side=tk.LEFT, padx=(4, 8))
        self._end_entry.bind("<Button-1>", lambda _e: self._open_date_picker("end") or "break")
        self._start_entry.bind("<KeyRelease>", lambda _e: self._range_var.set("自定义"))
        self._end_entry.bind("<KeyRelease>", lambda _e: self._range_var.set("自定义"))
        self._start_entry.bind("<Return>", lambda _e: self._refresh_view())
        self._end_entry.bind("<Return>", lambda _e: self._refresh_view())

        ttk.Button(filter_bar, text="应用", command=self._refresh_view).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(filter_bar, text="导出 Excel", command=self._export_current_view).pack(side=tk.LEFT, padx=(0, 10))

        self._media_btns = {}
        for label, key, color in (
            ("全部", "all", _GRAY),
            ("权威媒体", "authority", _AUTHORITY_COLOR),
            ("自媒体", "selfmedia", _SELFMEDIA_COLOR),
        ):
            btn = tk.Button(
                filter_bar,
                text=label,
                command=lambda k=key: self._toggle_media_filter(k),
                relief=tk.FLAT,
                bd=0,
                padx=10,
                pady=4,
                cursor="hand2",
                bg=_ACTIVE_BG if key == "all" else _INACTIVE_BG,
                fg=color,
                activebackground=_ACTIVE_BG,
                activeforeground=color,
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._media_btns[key] = btn

        self._range_var.trace_add("write", lambda *_: self._sync_range_controls())

        # ── 图表区 ────────────────────────────────────────────────
        chart_wrap = tk.Frame(self._win, bg=_BG, padx=12, pady=6)
        chart_wrap.pack(fill=tk.X)

        chart_card = tk.Frame(
            chart_wrap,
            bg=_SURFACE,
            highlightbackground=_BORDER,
            highlightthickness=1,
        )
        chart_card.pack(fill=tk.X)

        self._chart_header = tk.Frame(chart_card, bg=_SURFACE, padx=12, pady=10)
        self._chart_header.pack(fill=tk.X)
        self._chart_title_var = tk.StringVar(value="每天发表文章统计")
        tk.Label(self._chart_header, textvariable=self._chart_title_var, bg=_SURFACE, fg="#111827",
                 font=("PingFang SC", 12, "bold")).pack(anchor=tk.W)
        self._chart_meta_var = tk.StringVar(value="")
        tk.Label(self._chart_header, textvariable=self._chart_meta_var, bg=_SURFACE, fg=_GRAY,
                 font=("PingFang SC", 9)).pack(anchor=tk.W, pady=(4, 0))

        self._chart_canvas = tk.Canvas(chart_card, height=220, bg=_SURFACE, highlightthickness=0)
        self._chart_canvas.pack(fill=tk.X, padx=6, pady=(0, 8))
        self._chart_canvas.bind("<Configure>", lambda _e: self._draw_chart())
        self._chart_canvas.bind("<Button-1>", self._on_chart_click)

        legend = tk.Frame(chart_card, bg=_SURFACE, padx=12, pady=10)
        legend.pack(fill=tk.X)
        for label, color, key in (
            ("权威媒体", _AUTHORITY_COLOR, "authority"),
            ("自媒体", _SELFMEDIA_COLOR, "selfmedia"),
        ):
            item = tk.Frame(legend, bg=_SURFACE)
            item.pack(side=tk.LEFT, padx=(0, 14))
            swatch = tk.Canvas(item, width=14, height=14, bg=_SURFACE, highlightthickness=0)
            swatch.pack(side=tk.LEFT)
            swatch.create_rectangle(1, 1, 13, 13, fill=color, outline="")
            swatch.bind("<Button-1>", lambda _e, k=key: self._toggle_media_filter(k))
            lbl = tk.Label(item, text=label, bg=_SURFACE, fg=_BAR_TEXT, cursor="hand2",
                           font=("PingFang SC", 10, "bold"))
            lbl.pack(side=tk.LEFT, padx=(5, 0))
            lbl.bind("<Button-1>", lambda _e, k=key: self._toggle_media_filter(k))

        # ── 表格区 ───────────────────────────────────────────────────
        cols = ("ts", "title", "platform", "media_type", "tasks")
        col_headers = {
            "ts":         ("录入时间",  120),
            "title":      ("文章标题",  260),
            "platform":   ("来源平台",  110),
            "media_type": ("媒体类型",   80),
            "tasks":      ("关联任务",  180),
        }

        frame = tk.Frame(self._win, bg=_BG)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        style = ttk.Style()
        style.configure("Article.Treeview", rowheight=28, font=("PingFang SC", 11))
        style.configure("Article.Treeview.Heading", font=("PingFang SC", 11, "bold"))

        self._tree = ttk.Treeview(
            frame,
            columns=cols,
            show="headings",
            selectmode="browse",
            style="Article.Treeview",
        )
        for col in cols:
            header, width = col_headers[col]
            self._tree.heading(col, text=header)
            self._tree.column(col, width=width, minwidth=60,
                              anchor=tk.W if col != "media_type" else tk.CENTER)

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)

        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 双击切换媒体类型
        self._tree.bind("<Double-1>", self._on_double_click)
        for sequence in (
            "<Button-2>",
            "<Button-3>",
            "<Control-Button-1>",
            "<ButtonRelease-2>",
            "<ButtonRelease-3>",
            "<Control-ButtonRelease-1>",
        ):
            self._tree.bind(sequence, self._show_article_context_menu, add="+")

        # ── 底部提示 ──────────────────────────────────────────────────
        hint = tk.Label(
            self._win,
            text="双击「媒体类型」列或右键文章，可切换 权威媒体 / 自媒体（同主域名站点的历史与后续文章会同步记忆）",
            bg=_BG, fg=_GRAY, font=("PingFang SC", 10),
            anchor=tk.W, padx=14,
        )
        hint.pack(fill=tk.X, pady=(0, 6))

        # 记忆 article_id 映射
        self._id_map: dict[str, str] = {}   # tree iid → article_id

    # ------------------------------------------------------------------
    # 数据操作
    # ------------------------------------------------------------------

    def _build_brand_options(self) -> list[str]:
        brand_names = []
        for task in (self._config or {}).get("tasks", []) or []:
            name = str(task.get("name") or "").strip()
            if name and name not in brand_names:
                brand_names.append(name)
        for article in get_articles():
            for matched in article.get("matched_tasks") or []:
                matched_name = str(matched or "").strip()
                if matched_name and matched_name not in brand_names:
                    brand_names.append(matched_name)
        return ["全部品牌"] + brand_names

    def _current_brand_name(self) -> str:
        if self._fixed_brand:
            return self._task_name
        return str(self._brand_var.get() or "全部品牌").strip()

    def _apply_range_preset(self) -> None:
        preset = str(self._range_var.get() or "最近30天").strip()
        today = date.today()
        if preset == "最近7天":
            start = today - timedelta(days=6)
            end = today
        elif preset == "最近30天":
            start = today - timedelta(days=29)
            end = today
        elif preset == "最近90天":
            start = today - timedelta(days=89)
            end = today
        elif preset == "全部":
            articles = self._articles_cache or get_articles()
            dates = [d for d in (_parse_date_text(a.get("ts", "")) for a in articles) if d]
            if dates:
                start = min(dates)
                end = max(dates)
            else:
                start = today - timedelta(days=29)
                end = today
        else:
            return
        self._start_var.set(start.isoformat())
        self._end_var.set(end.isoformat())
        self._refresh_view()

    def _open_date_picker(self, target: str) -> None:
        current = _parse_date_text(self._start_var.get() if target == "start" else self._end_var.get()) or date.today()

        def _apply(selected: date) -> None:
            if target == "start":
                self._start_var.set(selected.isoformat())
            else:
                self._end_var.set(selected.isoformat())
            self._range_var.set("自定义")
            self._refresh_view()

        if self._date_popup is not None:
            try:
                self._date_popup._win.destroy()
            except Exception:
                pass
        self._date_popup = _CalendarPopup(self._win, current, _apply)

    def _sync_range_controls(self) -> None:
        if self._range_var.get() == "自定义":
            return

    def _refresh_list(self) -> None:
        self._refresh_view()

    def _get_filtered_articles(self) -> list[dict]:
        articles = list(self._articles_cache or get_articles())
        self._articles_cache = articles

        brand = self._current_brand_name()
        if brand and brand != "全部品牌":
            articles = [a for a in articles if brand in (a.get("matched_tasks") or [])]

        start = _parse_date_text(self._start_var.get())
        end = _parse_date_text(self._end_var.get())
        if start and end:
            if end < start:
                start, end = end, start
            articles = [
                a for a in articles
                if (ts_date := _parse_date_text(a.get("ts", ""))) and start <= ts_date <= end
            ]

        media_filter = self._media_filter
        if media_filter in ("authority", "selfmedia"):
            articles = [a for a in articles if a.get("media_type", "selfmedia") == media_filter]

        return articles

    def _refresh_view(self) -> None:
        self._articles_cache = get_articles()
        if self._brand_combo is not None:
            self._brand_combo["values"] = self._build_brand_options()
            if self._brand_var.get() not in self._brand_combo["values"]:
                self._brand_var.set("全部品牌")
        if not self._start_var.get() or not self._end_var.get():
            self._apply_range_preset()
            return

        self._sync_media_buttons()
        self._id_map.clear()
        for item in self._tree.get_children():
            self._tree.delete(item)

        articles = self._get_filtered_articles()
        self._draw_chart(articles)
        self._update_meta(articles)

        for article in articles:
            ts         = article.get("ts", "")[:16]
            display_url = resolve_article_display_url(article)
            title      = article.get("title") or display_url[:60]
            platform   = resolve_article_source(article) or article.get("platform", "")
            media_type = article.get("media_type", "selfmedia")
            tasks      = "、".join(article.get("matched_tasks") or [])
            label      = "权威媒体" if media_type == "authority" else "自媒体"

            iid = self._tree.insert(
                "", tk.END,
                values=(ts, title, platform, label, tasks),
            )
            # 用颜色区分媒体类型
            tag = "authority" if media_type == "authority" else "selfmedia"
            self._tree.item(iid, tags=(tag,))
            self._id_map[iid] = article.get("id", "")

        self._tree.tag_configure("authority", foreground=_AUTHORITY_COLOR)
        self._tree.tag_configure("selfmedia",  foreground=_SELFMEDIA_COLOR)

    def _update_meta(self, articles: list[dict]) -> None:
        authority = sum(1 for a in articles if a.get("media_type", "selfmedia") == "authority")
        selfmedia = sum(1 for a in articles if a.get("media_type", "selfmedia") == "selfmedia")
        total = len(articles)
        brand = self._current_brand_name()
        start = self._start_var.get().strip() or "未知"
        end = self._end_var.get().strip() or "未知"
        media = "全部类别" if self._media_filter == "all" else ("权威媒体" if self._media_filter == "authority" else "自媒体")
        self._chart_title_var.set(f"{brand} 的每日文章统计")
        self._chart_meta_var.set(f"时间范围：{start} ~ {end}  ·  当前类别：{media}  ·  文章数：{total}  (权威 {authority} / 自媒体 {selfmedia})")

    def _draw_chart(self, articles: list[dict] | None = None) -> None:
        if articles is None:
            articles = self._get_filtered_articles()

        canvas = self._chart_canvas
        canvas.delete("all")
        self._bar_items.clear()

        W = max(600, canvas.winfo_width() or 900)
        H = max(180, canvas.winfo_height() or 220)
        left, right, top, bottom = 52, 18, 20, 34
        plot_w = max(1, W - left - right)
        plot_h = max(1, H - top - bottom)

        start = _parse_date_text(self._start_var.get())
        end = _parse_date_text(self._end_var.get())
        if not start or not end:
            canvas.create_text(W // 2, H // 2, text="请选择有效的日期范围", fill=_GRAY, font=("PingFang SC", 11))
            return
        if end < start:
            start, end = end, start

        days = _date_range(start, end)
        if not days:
            canvas.create_text(W // 2, H // 2, text="暂无数据", fill=_GRAY, font=("PingFang SC", 11))
            return

        by_date = defaultdict(lambda: {"authority": 0, "selfmedia": 0})
        for article in articles:
            ts_date = _parse_date_text(article.get("ts", ""))
            if not ts_date or ts_date < start or ts_date > end:
                continue
            media_type = article.get("media_type", "selfmedia")
            if media_type not in ("authority", "selfmedia"):
                media_type = "selfmedia"
            by_date[ts_date.isoformat()][media_type] += 1

        max_value = max((max(v["authority"], v["selfmedia"]) for v in by_date.values()), default=0)
        max_value = max(max_value, 1)
        colors = {"authority": _AUTHORITY_COLOR, "selfmedia": _SELFMEDIA_COLOR}
        active_types = [self._media_filter] if self._media_filter in ("authority", "selfmedia") else ["authority", "selfmedia"]

        # 轴
        canvas.create_line(left, top, left, H - bottom, fill=_BAR_GRID)
        canvas.create_line(left, H - bottom, W - right, H - bottom, fill=_BAR_GRID)

        # Y 轴刻度
        for ratio, label in ((1.0, str(max_value)), (0.5, str(max_value // 2)), (0.0, "0")):
            y = top + plot_h - plot_h * ratio
            canvas.create_line(left - 4, y, W - right, y, fill=_BAR_GRID if ratio else _BAR_GRID, dash=(3, 4))
            canvas.create_text(left - 8, y, text=label, anchor="e", fill=_BAR_TEXT, font=("PingFang SC", 8))

        slot_w = plot_w / len(days)
        bar_gap = 4
        if self._media_filter in ("authority", "selfmedia"):
            bar_width = max(6, slot_w * 0.52)
        else:
            bar_width = max(4, slot_w * 0.36)

        def bar_height(value: int) -> float:
            return (value / max_value) * (plot_h - 8)

        for idx, day in enumerate(days):
            x_center = left + slot_w * idx + slot_w / 2
            day_key = day.isoformat()
            day_data = by_date.get(day_key, {"authority": 0, "selfmedia": 0})

            if self._media_filter in ("authority", "selfmedia"):
                media_types = [self._media_filter]
            else:
                media_types = ["authority", "selfmedia"]

            if len(media_types) == 2:
                offsets = {"authority": -bar_width / 2 - bar_gap / 2, "selfmedia": bar_width / 2 + bar_gap / 2}
            else:
                offsets = {media_types[0]: 0}

            for media_type in media_types:
                value = int(day_data.get(media_type, 0) or 0)
                bh = bar_height(value)
                x0 = x_center + offsets[media_type] - bar_width / 2
                x1 = x_center + offsets[media_type] + bar_width / 2
                y1 = H - bottom
                y0 = y1 - bh
                fill = colors[media_type] if value > 0 else _BAR_EMPTY
                rect = canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")
                canvas.create_text(
                    (x0 + x1) / 2,
                    y0 - 8,
                    text=str(value) if value > 0 else "",
                    fill=_BAR_TEXT,
                    font=("PingFang SC", 8, "bold"),
                )
                canvas.tag_bind(rect, "<Button-1>", lambda _e, mt=media_type: self._toggle_media_filter(mt))
                self._bar_items[rect] = {"media_type": media_type}

            canvas.create_text(
                x_center,
                H - 16,
                text=day.strftime("%m-%d"),
                fill=_BAR_TEXT,
                font=("PingFang SC", 8),
            )

    def _toggle_media_filter(self, media_type: str) -> None:
        media_type = str(media_type or "all").strip()
        if media_type == self._media_filter:
            self._media_filter = "all"
        else:
            self._media_filter = media_type if media_type in ("authority", "selfmedia") else "all"
        self._sync_media_buttons()
        self._refresh_view()

    def _sync_media_buttons(self) -> None:
        for key, btn in self._media_btns.items():
            if key == self._media_filter or (self._media_filter == "all" and key == "all"):
                btn.configure(bg=_ACTIVE_BG)
            elif key == "all":
                btn.configure(bg=_ACTIVE_BG if self._media_filter == "all" else _INACTIVE_BG)
            else:
                btn.configure(bg=_INACTIVE_BG)

    def _on_chart_click(self, event: tk.Event) -> None:
        items = self._chart_canvas.find_overlapping(event.x, event.y, event.x, event.y)
        for item in items:
            meta = self._bar_items.get(item)
            if not meta:
                continue
            self._toggle_media_filter(meta.get("media_type", "all"))
            return

    def _export_current_view(self) -> None:
        articles = self._get_filtered_articles()
        if not articles:
            messagebox.showinfo("导出 Excel", "当前筛选条件下没有可导出的文章。")
            return

        brand = self._current_brand_name()
        brand_slug = re.sub(r"[^\w\u4e00-\u9fff]+", "_", brand).strip("_") or "品牌"
        start = self._start_var.get().strip() or "开始"
        end = self._end_var.get().strip() or "结束"
        media_label = "全部" if self._media_filter == "all" else ("权威媒体" if self._media_filter == "authority" else "自媒体")
        default_name = f"{brand_slug}_文章汇总_{start}_{end}_{media_label}.xlsx"

        export_dir = resolve_app_path("exports")
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        file_path = filedialog.asksaveasfilename(
            parent=self._win,
            title="导出 Excel",
            initialdir=str(export_dir),
            initialfile=default_name,
            defaultextension=".xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx")],
        )
        if not file_path:
            return

        export_rows = []
        include_brand = brand == "全部品牌"
        show_keyword_category = bool(
            ((self._config or {}).get("article_export", {}) or {}).get("show_keyword_category", False)
        )
        show_selfmedia_account = bool(
            ((self._config or {}).get("article_export", {}) or {}).get("show_selfmedia_account", True)
        )
        keyword_task_name = "" if include_brand else brand
        keyword_order = build_article_export_keyword_order(self._config or {}, keyword_task_name)
        keyword_plan = build_article_export_keyword_plan(self._config or {}, keyword_task_name)
        keyword_config_signature = build_article_export_keyword_signature(self._config or {})
        for article in articles:
            title = str(article.get("title") or "").strip()
            platform = (
                resolve_article_export_source(
                    article,
                    show_selfmedia_account=show_selfmedia_account,
                )
                or extract_domain(resolve_article_display_url(article))
                or "未知来源"
            )
            matched_brands = [
                str(name or "").strip()
                for name in (article.get("matched_tasks") or [])
                if str(name or "").strip()
            ]
            export_rows.append({
                "id": str(article.get("id") or ""),
                "brand": "、".join(matched_brands) or "未归类",
                "keyword_categories": resolve_article_export_keywords(
                    article,
                    self._config or {},
                    keyword_task_name,
                    keyword_plan=keyword_plan,
                    keyword_config_signature=keyword_config_signature,
                ),
                "platform": platform,
                "title": title,
                "url": resolve_article_display_url(article),
                "published_at": article.get("published_at", ""),
                "imported_at": article.get("imported_at", article.get("created_at", "")),
                "ts": str(article.get("ts") or "").strip(),
                "media_type": str(article.get("media_type") or "selfmedia").strip(),
            })

        sheet_name = _sanitize_sheet_name(brand if brand != "全部品牌" else "全部品牌")
        try:
            _build_articles_xlsx(
                Path(file_path),
                sheet_name,
                export_rows,
                include_brand=include_brand,
                show_keyword_category=show_keyword_category,
                keyword_order=keyword_order,
            )
        except Exception as exc:
            messagebox.showerror("导出失败", f"生成 Excel 失败：{exc}")
            return

        self._set_status(f"已导出 {len(export_rows)} 篇文章：{file_path}")
        self._send_export_file_to_brand_webhook(Path(file_path), brand, len(export_rows))
        messagebox.showinfo("导出完成", f"已导出 {len(export_rows)} 篇文章。")

    def _resolve_brand_webhook(self, brand: str) -> tuple[str, str]:
        brand = str(brand or "").strip()
        if not brand or brand == "全部品牌":
            return "", ""

        tasks = (self._config or {}).get("tasks", []) or []

        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_name = str(task.get("name") or "").strip()
            brand_names = get_task_brand_names(task)
            if brand != task_name and brand not in brand_names:
                continue

            return str(task.get("webhook_url") or "").strip(), (task_name or brand)

        return "", ""

    def _send_export_file_to_brand_webhook(self, file_path: Path, brand: str, article_count: int) -> None:
        webhook_url, resolved_brand = self._resolve_brand_webhook(brand)
        if not webhook_url or "YOUR_KEY_HERE" in webhook_url:
            self._set_status(f"已导出 {article_count} 篇文章：{file_path}（未配置 {resolved_brand or brand} 的 webhook，未自动发送）")
            return

        def _worker():
            notifier = WeComNotifier(webhook_url=webhook_url, cooldown_minutes=0, send_interval=1)
            ok = notifier.send_file_message(str(file_path), file_name=file_path.name)
            if ok:
                self._win.after(
                    0,
                    lambda: self._set_status(
                        f"已导出并发送 {article_count} 篇文章到 {resolved_brand} 的企业微信：{file_path}"
                    ),
                )
            else:
                err = notifier.last_error or "发送失败"
                self._win.after(
                    0,
                    lambda: self._set_status(
                        f"已导出 {article_count} 篇文章，但发送到 {resolved_brand} 企业微信失败：{err[:80]}",
                        error=True,
                    ),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_add(self) -> None:
        url = self._url_var.get().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            self._set_status("请输入完整 URL（以 http:// 或 https:// 开头）", error=True)
            return

        self._add_btn.state(["disabled"])
        self._set_status("正在识别文章信息…")

        def _worker():
            try:
                info = fetch_article_info(url, self._config)
                entry = {
                    "url":          url,
                    "title":        info.get("title", ""),
                    "platform":     info.get("platform", ""),
                    "media_name":   info.get("media_name", ""),
                    "media_type":   info.get("media_type", "selfmedia"),
                    "excerpt":      info.get("excerpt", ""),
                    "fetch_method": info.get("fetch_method", "failed"),
                }
                analyzed = analyze_article_matches(entry.get("title", ""), self._config, article=entry)
                entry["matched_tasks"] = analyzed.get("matched_tasks") or []
                entry["match_reasons"] = analyzed.get("match_reasons") or {}
                entry["unmatched_reason"] = analyzed.get("unmatched_reason", "") or ""
                add_article(entry)
                self._win.after(0, self._after_add_success, info)
            except Exception as exc:
                self._win.after(0, self._after_add_error, str(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _after_add_success(self, info: dict) -> None:
        title    = info.get("title") or "（标题未识别）"
        mt_label = "权威媒体" if info.get("media_type") == "authority" else "自媒体"
        self._set_status(f"已录入：{title[:30]}  [{mt_label}]")
        self._url_var.set("")
        self._add_btn.state(["!disabled"])
        self._refresh_view()

    def _after_add_error(self, msg: str) -> None:
        self._set_status(f"录入失败：{msg[:60]}", error=True)
        self._add_btn.state(["!disabled"])

    def _on_double_click(self, event: tk.Event) -> None:
        """双击媒体类型列时切换 authority ↔ selfmedia。"""
        region = self._tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self._tree.identify_column(event.x)
        # 第 4 列（#4）= media_type
        if col != "#4":
            return
        iid = self._tree.identify_row(event.y)
        if not iid:
            return

        article_id = self._id_map.get(iid, "")
        if not article_id:
            return

        current_values = self._tree.item(iid, "values")
        current_label  = current_values[3] if len(current_values) > 3 else ""
        new_type  = "selfmedia" if current_label == "权威媒体" else "authority"
        self._apply_media_type_change(article_id, new_type)

    def _apply_media_type_change(self, article_id: str, media_type: str) -> None:
        article_id = str(article_id or "").strip()
        media_type = str(media_type or "").strip().lower()
        if not article_id or media_type not in {"authority", "selfmedia"}:
            return

        changed = update_media_type(article_id, media_type)
        if not changed:
            self._set_status("媒体类型更新失败，请重试。", error=True)
            return

        new_label = "权威媒体" if media_type == "authority" else "自媒体"
        self._refresh_view()
        self._set_status(f"已更新媒体类型 → {new_label}（同主域名站点历史与后续文章同步）")

    def _show_article_context_menu(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(event.y)
        if not iid:
            return

        article_id = self._id_map.get(iid, "")
        if not article_id:
            return

        try:
            self._tree.selection_set(iid)
            self._tree.focus(iid)
        except Exception:
            pass

        current_values = self._tree.item(iid, "values")
        current_label = current_values[3] if len(current_values) > 3 else ""

        menu = tk.Menu(self._win, tearoff=0)
        menu.add_command(
            label="设为权威媒体",
            state=tk.DISABLED if current_label == "权威媒体" else tk.NORMAL,
            command=lambda aid=article_id: self._apply_media_type_change(aid, "authority"),
        )
        menu.add_command(
            label="设为自媒体",
            state=tk.DISABLED if current_label == "自媒体" else tk.NORMAL,
            command=lambda aid=article_id: self._apply_media_type_change(aid, "selfmedia"),
        )
        menu.post(event.x_root, event.y_root)
        return "break"

    def _set_status(self, msg: str, *, error: bool = False) -> None:
        self._status_var.set(msg)
        # 通过 Label 的 fg 无法直接改，改用 configure
        color = _RED if error else _GRAY
        for widget in self._win.winfo_children():
            if isinstance(widget, tk.Frame):
                for child in widget.winfo_children():
                    if isinstance(child, tk.Label) and child.cget("textvariable") == str(self._status_var):
                        child.configure(fg=color)

    def _on_close(self) -> None:
        self._win.destroy()

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def lift(self) -> None:
        """将窗口提到最前。"""
        self._win.lift()
        self._win.focus_force()
