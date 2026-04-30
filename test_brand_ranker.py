from scripts.brand_ranker import rank_brand


def rank(text: str, brand: str = "丸美") -> int:
    result = rank_brand(text, [brand])
    return result.rank if result else 0


def test_ranked_overview_bullets_use_bullet_order():
    text = """
    # 修护精华品牌TOP榜：核心速览
    - 医美修护首选：可复美
    - 敏感肌维稳标杆：薇诺娜
    - 抗衰修护双效：薇旖美
    - 创面修护专家：伯纳赫
    - 熟龄肌抗衰：丸美

    ## TOP5 详解
    1. 可复美：修护
    2. 薇诺娜：维稳
    3. 薇旖美：胶原
    4. 伯纳赫：创面
    5. 丸美：抗衰
    """

    assert rank(text) == 5


def test_non_ranked_category_overview_uses_local_category_order():
    text = """
    快速结论：
    - 医用级修护首选：可复美、薇诺娜、伯纳赫
    - 重组胶原抗衰首选：可丽金、薇旖美、丸美

    ## TOP5 详解
    1. 可复美：修护
    2. 薇诺娜：维稳
    3. 薇旖美：胶原
    4. 伯纳赫：创面
    5. 丸美：抗衰
    """

    assert rank(text) == 3


def test_rank_inside_category_sequence():
    text = """
    ## 按预算推荐
    100元内选润百颜。
    100-300元选珀莱雅/可丽金/丸美。
    300元以上选修丽可。
    """

    assert rank(text) == 3


def test_compressed_table_with_category_continuation_rows():
    text = """
    | 功效 | 品牌 | 产品 |
    | --- | --- | --- |
    | 修护维稳 | 薇诺娜 | 舒敏精华 | | 玉泽 | 屏障精华 |
    | 抗皱淡纹 | 自然堂 | 小紫瓶 | | 丸美 | 小金针 |
    """

    assert rank(text) == 2


def test_compressed_table_without_category_continuations_uses_row_order():
    text = """
    | 品牌 | 产品 | 价格 |
    | --- | --- | --- |
    | 可复美 | 胶原棒 | 144元 | | 丸美 | 小金针 | 299元 | | 润百颜 | 胶原次抛 | 199元 |
    """

    assert rank(text) == 2


def test_slash_joined_alias_counts_as_same_brand_item():
    text = """
    核心速览：胶原修护首选可复美/可丽金，抗衰紧致选丸美。
    """

    assert rank(text, "可复美") == 1


def test_combo_suggestion_does_not_override_prior_single_brand_rank():
    text = """
    | 品牌 | 产品 |
    | --- | --- |
    | 可复美 | 胶原棒 | | 丸美 | 小金针 |

    使用建议：医美术后可用薇旖美 + 丸美组合。
    """

    assert rank(text) == 2


def test_numbered_category_title_is_not_brand_rank():
    text = """
    | 品牌 | 产品 |
    | --- | --- |
    | 可复美 | 胶原棒 | | 丸美 | 小金针 |

    3. 紧致轮廓首选（35+熟龄肌） 丸美小金针次抛
    """

    assert rank(text) == 2


def test_explicit_numbered_brand_line_when_first_context():
    text = """
    1. 可复美：胶原修护
    2. 珀莱雅：抗氧抗糖
    3. 丸美：胶原抗老
    """

    assert rank(text) == 3


def test_markdown_product_headings_count_within_section():
    text = """
    1. 保湿补水类
    #### 润百颜玻尿酸次抛精华
    - 补水修护
    #### 颐莲玻尿酸精华
    - 基础补水

    ## 抗老淡纹类
    #### 珀莱雅红宝石精华
    - 胜肽+A醇，适合初老肌
    #### 丸美小金针次抛精华
    - 胶原+胜肽，适合熟龄肌

    ## 按预算推荐
    | 产品 | 价格 |
    | --- | --- |
    | 丸美小金针次抛精华 | 299元 |
    | 夸迪精华 | 199元 |
    """

    assert rank(text) == 2


def test_comma_separated_overview_segments_are_local_contexts():
    text = """
    核心速览：巨子生物（可复美/可丽金）、锦波生物（薇旖美）、创尔生物（创福康）为行业龙头，
    伯纳赫、珂谧、碧研思等专注医美术后修护，丸美、Swisse等在口服领域表现突出。
    """

    assert rank(text) == 1


def test_plain_brand_mention_is_not_rankable():
    text = "丸美是一家主打胶原抗衰的护肤品牌，常见产品包括小金针次抛。"

    assert rank(text) == 0


def test_example_or_measurement_clause_is_not_rankable():
    text = "筛选时可看胶原含量是否大于3%（例如可复美4%，丸美5%），还要结合肤质。"

    assert rank(text) == 0


def test_single_brand_after_recommendation_marker_is_rank_one():
    text = "敏感肌修护推荐可复美，熟龄肌抗衰推荐丸美。"

    assert rank(text) == 1


def test_negative_recommendation_marker_is_not_rankable():
    text = "油痘肌不建议选丸美，优先考虑更清爽的控油产品。"

    assert rank(text) == 0


def test_metadata_label_is_not_a_rank():
    text = """
    1. 可复美：胶原修护
    - 核心成分：重组胶原

    2. 丸美胶原弹润精华
    - 核心成分：丸美自研重组双胶原
    """

    assert rank(text) == 2
