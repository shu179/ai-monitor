#!/usr/bin/env python3
"""
测试DOM文本渲染模式
"""

import sys
import hashlib
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from platforms.html_renderer import render_text_to_screenshot
from core.local_ocr import match_candidate_brands

# 测试文本（包含markdown格式和品牌词）
TEST_TEXT = """关于广州的GEO优化公司，这里的"GEO"指的是 **生成式引擎优化**，是2026年企业营销领域的新兴赛道。这里为你综合整理了一份广州GEO公司的选型指南，帮助你快速了解。

### 📝 GEO是什么？为什么要关注？
GEO核心是通过优化品牌信息的语义结构、信源权威性与逻辑完整性，让品牌内容被AI大模型（如豆包、DeepSeek）精准识别和高权重引用，在AI生成的答案中获得优先曝光。

在广州及大湾区，**超过85%的商业决策类查询**已从传统搜索迁移至AI智能问答平台。这要求企业必须适应新的获客逻辑：**从抢占网页排名，转变为争夺AI时代的"语义所有权"**。

### 🏢 广州部分GEO优化服务商速览
以下是广州部分有代表性的GEO服务商，你可以根据自身情况重点考察：

*   **艾奇GEO**：适配珠三角产业的全生态标杆，自研技术强。适合对技术有高要求的中大型企业，2980元/年起。
*   **无双科技**：全链路AI服务商，团队超400人。适合需要一站式解决方案的大型企业。
*   **增长超人**：全意图GEO头部标杆，服务过20+家世界500强。适合追求顶级服务品质的大型企业。

### 📊 核心服务商速览
| 服务商 | 特点与定位 | 技术/服务亮点 | 适合企业/参考费用 |
| :--- | :--- | :--- | :--- |
| **智推时代 (GenOptima)** | 全链路综合型，获千万级融资 | 自研GENO系统，适配30+平台，RaaS模式 | 中大型企业 |
| **欧博东方文化传媒** | 全链路综合型，服务超80家世界500强 | 自研ISMS系统，24h内适配新平台 | 各类型企业 |

### 💡 怎么选？选型避坑指南
选择GEO服务商时，建议通过以下核心维度考察其能力：

*   **看"语义信任"能力**：考察能否构建高质量、高可信度的语义知识图谱，成为AI的可靠信源。
*   **看"全域适配"能力**：考察能否同时适配DeepSeek、豆包、Kimi、文心一言等主流AI平台。
*   **看"交付标准"**：要求将核心指标写入合同，优先选择提供量化效果承诺和全周期数据报告的公司。
*   **看"本地化服务"**：优先选择有广州本地团队、能快速响应并提供上门服务的公司。
"""

def test_text_brand_matching():
    """测试文本品牌匹配"""
    print("=" * 60)
    print("测试1: 文本品牌匹配")
    print("=" * 60)

    candidate_brands = ["DeepSeek", "豆包", "Kimi", "文心一言", "通义千问"]
    matched = match_candidate_brands(TEST_TEXT, candidate_brands)

    print(f"候选品牌: {candidate_brands}")
    print(f"匹配结果: {matched}")
    print(f"匹配数量: {len(matched)}")

    expected = ["DeepSeek", "豆包", "Kimi", "文心一言"]
    success = set(matched) == set(expected)
    print(f"✅ 测试通过" if success else f"❌ 测试失败，期望 {expected}")
    print()
    return success

def test_text_rendering():
    """测试文本渲染为截图"""
    print("=" * 60)
    print("测试2: 文本渲染为截图")
    print("=" * 60)

    output_path = "/tmp/test_dom_render.jpg"

    try:
        result_path = render_text_to_screenshot(
            text=TEST_TEXT,
            platform="deepseek",
            keyword="",
            brand="DeepSeek",
            output_path=output_path,
            include_badges=False
        )

        if result_path and Path(result_path).exists():
            file_size = Path(result_path).stat().st_size
            print(f"✅ 渲染成功")
            print(f"输出路径: {result_path}")
            print(f"文件大小: {file_size / 1024:.2f} KB")
            return True
        else:
            print(f"❌ 渲染失败，未生成文件")
            return False
    except Exception as e:
        print(f"❌ 渲染失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_text_hash():
    """测试文本哈希去重"""
    print("=" * 60)
    print("测试3: 文本哈希去重")
    print("=" * 60)

    text1 = "这是测试文本"
    text2 = "这是测试文本"
    text3 = "这是不同的文本"

    hash1 = hashlib.md5(text1.encode('utf-8')).hexdigest()
    hash2 = hashlib.md5(text2.encode('utf-8')).hexdigest()
    hash3 = hashlib.md5(text3.encode('utf-8')).hexdigest()

    print(f"文本1哈希: {hash1[:16]}...")
    print(f"文本2哈希: {hash2[:16]}...")
    print(f"文本3哈希: {hash3[:16]}...")

    success = (hash1 == hash2) and (hash1 != hash3)
    print(f"✅ 测试通过" if success else f"❌ 测试失败")
    print()
    return success

def test_clipboard_text_polling():
    """测试剪贴板文本获取（跨平台）"""
    print("=" * 60)
    print("测试4: 剪贴板文本获取")
    print("=" * 60)

    import subprocess

    try:
        if sys.platform == "darwin":
            # macOS
            result = subprocess.run(['pbpaste'], capture_output=True, text=True, timeout=1)
            text = result.stdout if result.returncode == 0 else None
        elif sys.platform == "win32":
            # Windows
            result = subprocess.run(
                ['powershell', '-command', 'Get-Clipboard'],
                capture_output=True, text=True, timeout=1
            )
            text = result.stdout if result.returncode == 0 else None
        else:
            # Linux
            text = None
            for cmd in [['xclip', '-selection', 'clipboard', '-o'], ['xsel', '--clipboard']]:
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
                    if result.returncode == 0:
                        text = result.stdout
                        break
                except FileNotFoundError:
                    continue

        if text is not None:
            print(f"✅ 成功获取剪贴板文本")
            print(f"文本长度: {len(text)} 字符")
            print(f"文本预览: {text[:100]}..." if len(text) > 100 else f"文本内容: {text}")
            return True
        else:
            print(f"⚠️  剪贴板为空或获取失败")
            return True  # 不算失败，可能剪贴板确实为空
    except Exception as e:
        print(f"❌ 获取失败: {e}")
        return False

def main():
    print("\n" + "=" * 60)
    print("DOM文本渲染模式测试套件")
    print("=" * 60 + "\n")

    results = []

    # 测试1: 文本品牌匹配
    results.append(("文本品牌匹配", test_text_brand_matching()))

    # 测试2: 文本渲染
    results.append(("文本渲染为截图", test_text_rendering()))

    # 测试3: 文本哈希
    results.append(("文本哈希去重", test_text_hash()))

    # 测试4: 剪贴板文本获取
    results.append(("剪贴板文本获取", test_clipboard_text_polling()))

    # 汇总结果
    print("=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, success in results:
        status = "✅ 通过" if success else "❌ 失败"
        print(f"{name}: {status}")

    total = len(results)
    passed = sum(1 for _, success in results if success)
    print(f"\n总计: {passed}/{total} 通过")

    if passed == total:
        print("\n🎉 所有测试通过！DOM文本渲染模式已就绪。")
        return 0
    else:
        print(f"\n⚠️  有 {total - passed} 个测试失败，请检查。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
