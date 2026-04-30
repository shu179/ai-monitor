from io import BytesIO

from backend_lib.keyword_import import (
    KEYWORD_IMPORT_EXTENSIONS,
    _extract_keyword_import_items,
    _normalize_keyword_import_candidate,
    _split_keyword_import_candidates,
)


def test_keyword_import_normalizes_and_splits_text():
    assert ".docx" in KEYWORD_IMPORT_EXTENSIONS
    assert _normalize_keyword_import_candidate("1.  品牌声量  ") == "品牌声量"
    assert _normalize_keyword_import_candidate("https://example.com") == ""
    assert _normalize_keyword_import_candidate("关键词") == ""
    assert _split_keyword_import_candidates("关键词：品牌A、品牌B\n品牌A") == ["品牌A", "品牌B", "品牌A"]


def test_keyword_import_extracts_plain_text_and_dedupes():
    data = "关键词：品牌A、品牌B\n- 品牌A\nhttps://example.com\n".encode("utf-8")

    keywords, details = _extract_keyword_import_items("keywords.txt", data)

    assert keywords == ["品牌A", "品牌B"]
    assert details == {"line_count": 3, "source": "txt"}


def test_keyword_import_extracts_xlsx_header_columns():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "关键词表"
    sheet.append(["序号", "关键词", "搜索词", "数量"])
    sheet.append([1, "品牌A、品牌B", "新品", 2])
    sheet.append([2, "融资", "", 1])
    output = BytesIO()
    workbook.save(output)

    keywords, details = _extract_keyword_import_items("keywords.xlsx", output.getvalue())

    assert keywords == ["品牌A", "品牌B", "新品", "融资"]
    assert details == {
        "sheets": [
            {
                "sheet": "关键词表",
                "mode": "header",
                "columns": [1, 2],
                "count": 4,
            }
        ]
    }
