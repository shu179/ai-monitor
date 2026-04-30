from openpyxl import load_workbook

from web_backend import _build_article_export_xlsx


def test_article_export_xlsx_includes_keyword_category_and_account(tmp_path):
    output_path = tmp_path / "articles.xlsx"
    config = {
        "tasks": [
            {
                "name": "品牌A",
                "brand": "品牌A",
                "keywords": [
                    {"keyword": "融资"},
                    {"keyword": "新品"},
                ],
            }
        ]
    }
    articles = [
        {
            "id": "selfmedia",
            "title": "品牌A 新品 发布",
            "url": "https://example.com/self",
            "media_name": "今日头条",
            "media_type": "selfmedia",
            "account_name": "作者甲",
            "published_at": "2024-01-02",
            "matched_tasks": ["品牌A"],
        },
        {
            "id": "authority",
            "title": "品牌A 融资 新闻",
            "url": "https://example.com/news",
            "media_name": "财新",
            "media_type": "authority",
            "published_at": "2024/01/01 09:30",
            "matched_tasks": ["品牌A"],
        },
    ]

    _build_article_export_xlsx(
        brand_name="品牌A",
        start_date="2024-01-01",
        end_date="2024-01-31",
        articles=articles,
        output_path=output_path,
        show_keyword_category=True,
        show_selfmedia_account=True,
        config=config,
        task_name="品牌A",
    )

    workbook = load_workbook(output_path, read_only=True, data_only=True)
    sheet = workbook.active

    assert sheet["A1"].value == "品牌A 文章汇总"
    assert [sheet.cell(row=5, column=col).value for col in range(1, 7)] == [
        "关键词大类",
        "来源",
        "类型",
        "文章标题",
        "文章链接",
        "发布时间",
    ]
    assert [sheet.cell(row=6, column=col).value for col in range(1, 7)] == [
        "融资",
        "财新",
        "权威媒体",
        "品牌A 融资 新闻",
        "https://example.com/news",
        "2024-01-01",
    ]
    assert [sheet.cell(row=7, column=col).value for col in range(1, 7)] == [
        "新品",
        "今日头条（作者甲）",
        "自媒体",
        "品牌A 新品 发布",
        "https://example.com/self",
        "2024-01-02",
    ]
