from io import BytesIO

from backend_lib.keyword_import import (
    KEYWORD_IMPORT_EXTENSIONS,
    _extract_keyword_import_items,
    _normalize_keyword_import_candidate,
    _split_keyword_import_candidates,
)


def test_keyword_import_normalizes_text_and_splits_on_newlines_only():
    """规范化辅助函数 + 默认按换行切分，**不**按顿号 / 逗号无脑切 cell 内容。"""
    assert ".docx" in KEYWORD_IMPORT_EXTENSIONS
    assert _normalize_keyword_import_candidate("1.  品牌声量  ") == "品牌声量"
    assert _normalize_keyword_import_candidate("https://example.com") == ""
    assert _normalize_keyword_import_candidate("关键词") == ""
    # 单 cell 整体作为 1 个关键词；多行才按行各取一个。
    assert _split_keyword_import_candidates("关键词：品牌A、品牌B\n品牌A") == [
        "品牌A、品牌B",
        "品牌A",
    ]


def test_keyword_import_treats_single_cell_as_one_keyword():
    """每个 cell 一个关键词是核心场景：cell 内含顿号 / 斜杠等标点不再被无脑切碎。"""
    assert _split_keyword_import_candidates("品牌A、品牌B、品牌C") == ["品牌A、品牌B、品牌C"]
    assert _split_keyword_import_candidates("数字化转型、人工智能") == ["数字化转型、人工智能"]
    assert _split_keyword_import_candidates("控油洗发水推荐、防脱洗发水推荐") == [
        "控油洗发水推荐、防脱洗发水推荐",
    ]


def test_keyword_import_extracts_plain_text_and_dedupes():
    data = "关键词：品牌A、品牌B\n- 品牌A\nhttps://example.com\n".encode("utf-8")

    keywords, details = _extract_keyword_import_items("keywords.txt", data)

    # 每行一个关键词；URL 行被丢；"- 品牌A" 跟第 1 行的 "品牌A、品牌B" 没重复，所以两个都保留。
    assert keywords == ["品牌A、品牌B", "品牌A"]
    assert details == {"line_count": 3, "source": "txt"}


def test_keyword_import_keeps_long_tail_when_cell_is_overlong_compound():
    """单 cell 内塞了"长尾主词 + 一串短卖点"（整体超过关键词上限）时，
    才退回顿号切并只保留最长一段，避免把"适合新手""不飞粉"当独立关键词导入。"""
    assert _split_keyword_import_candidates(
        "不飞粉的大地色眼影推荐，适合新手，日常通勤，自然百搭"
    ) == ["不飞粉的大地色眼影推荐"]
    # 多行 cell 内每行独立处理：第 1 行短而均匀直接整体保留；
    # 第 2 行超长且 compound → 只取最长片段。
    assert _split_keyword_import_candidates(
        "数字化转型、人工智能\n"
        "眼唇脸三合一卸妆油推荐，温和不刺激，可卸防水彩妆，浓妆淡妆都能用"
    ) == ["数字化转型、人工智能", "眼唇脸三合一卸妆油推荐"]


def test_keyword_import_splits_on_newlines_within_cell():
    """从 Word / 网页 / AI 输出粘贴进单个 cell 时会带回车，按行切才能拿到独立关键词。"""
    assert _split_keyword_import_candidates(
        "不飞粉的大地色眼影推荐\n适合新手\n日常通勤\n自然百搭"
    ) == ["不飞粉的大地色眼影推荐", "适合新手", "日常通勤", "自然百搭"]


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

    # "品牌A、品牌B" 单 cell 现在整体作为 1 个关键词，不再被顿号切碎。
    assert keywords == ["品牌A、品牌B", "新品", "融资"]
    assert details == {
        "sheets": [
            {
                "sheet": "关键词表",
                "mode": "header",
                "columns": [1, 2],
                "count": 3,
            }
        ]
    }
