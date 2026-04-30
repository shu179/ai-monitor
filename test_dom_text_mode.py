#!/usr/bin/env python3
"""
DOM文本渲染模式功能测试脚本
"""

import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from core.recognition import ClipboardRecognitionManager


def test_clipboard_text_capture():
    """测试剪贴板文本捕获"""
    print("\n=== 测试1: 剪贴板文本捕获 ===")

    config = {'tasks': []}
    manager = ClipboardRecognitionManager(lambda: config)

    text = manager._poll_clipboard_text()
    if text is not None:
        print(f"✓ 剪贴板文本捕获成功")
        print(f"  当前剪贴板内容长度: {len(text)} 字符")
        if text:
            preview = text[:100].replace('\n', ' ')
            print(f"  预览: {preview}...")
    else:
        print("✗ 剪贴板文本捕获失败")
        return False

    return True


def test_brand_matching():
    """测试品牌匹配功能"""
    print("\n=== 测试2: 品牌匹配功能 ===")

    test_config = {
        'tasks': [
            {
                'name': '测试任务',
                'recognition_enabled': True,
                'recognition_brands': 'DeepSeek, Kimi, 豆包',
                'guide_keywords': [
                    {'keyword': '测试', 'brand': '文心一言'},
                    {'keyword': '测试2', 'brand': '通义千问'}
                ]
            }
        ]
    }

    manager = ClipboardRecognitionManager(lambda: test_config)

    # 测试文本
    test_text = '''
    # AI平台对比

    DeepSeek是一家人工智能公司，专注于大语言模型研发。
    Kimi是月之暗面推出的智能助手。
    豆包和文心一言也是不错的选择。
    '''

    matched = manager._match_brands_from_text(test_text)

    if len(matched) > 0:
        print(f"✓ 品牌匹配成功")
        print(f"  匹配到的品牌: {', '.join(matched)}")
        print(f"  匹配数量: {len(matched)}")

        # 验证关键品牌
        expected_brands = ['DeepSeek', 'Kimi', '豆包', '文心一言']
        found_brands = [b for b in expected_brands if b in matched]
        print(f"  预期品牌: {', '.join(expected_brands)}")
        print(f"  实际匹配: {', '.join(found_brands)}")

        if len(found_brands) >= 3:
            print(f"✓ 品牌匹配准确率良好 ({len(found_brands)}/{len(expected_brands)})")
            return True
        else:
            print(f"✗ 品牌匹配准确率不足 ({len(found_brands)}/{len(expected_brands)})")
            return False
    else:
        print("✗ 品牌匹配失败，未匹配到任何品牌")
        return False


def test_config_loading():
    """测试配置加载"""
    print("\n=== 测试3: 配置加载 ===")

    import yaml

    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        print("✗ config.yaml 不存在")
        return False

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    recognition_config = config.get('recognition', {})

    # 检查新配置项
    required_keys = [
        'dom_render_mode',
        'dom_render_default_platform',
        'dom_render_min_length',
        'dom_render_max_length'
    ]

    missing_keys = [key for key in required_keys if key not in recognition_config]

    if missing_keys:
        print(f"✗ 配置项缺失: {', '.join(missing_keys)}")
        return False

    print("✓ 配置加载成功")
    print(f"  dom_render_mode: {recognition_config['dom_render_mode']}")
    print(f"  dom_render_default_platform: {recognition_config['dom_render_default_platform']}")
    print(f"  dom_render_min_length: {recognition_config['dom_render_min_length']}")
    print(f"  dom_render_max_length: {recognition_config['dom_render_max_length']}")

    return True


def test_method_existence():
    """测试新方法是否存在"""
    print("\n=== 测试4: 方法完整性检查 ===")

    config = {'tasks': []}
    manager = ClipboardRecognitionManager(lambda: config)

    required_methods = [
        '_poll_clipboard_text',
        '_match_brands_from_text',
        '_render_text_to_screenshot',
        '_poll_once_text_mode'
    ]

    missing_methods = []
    for method_name in required_methods:
        if not hasattr(manager, method_name):
            missing_methods.append(method_name)
        else:
            print(f"✓ {method_name} 方法存在")

    if missing_methods:
        print(f"✗ 缺失方法: {', '.join(missing_methods)}")
        return False

    print("✓ 所有必需方法都已实现")
    return True


def main():
    """运行所有测试"""
    print("=" * 60)
    print("DOM文本渲染模式功能测试")
    print("=" * 60)

    tests = [
        ("方法完整性检查", test_method_existence),
        ("配置加载", test_config_loading),
        ("剪贴板文本捕获", test_clipboard_text_capture),
        ("品牌匹配功能", test_brand_matching),
    ]

    results = []
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"\n✗ {test_name} 测试异常: {e}")
            import traceback
            traceback.print_exc()
            results.append((test_name, False))

    # 汇总结果
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        print(f"{status} - {test_name}")

    print(f"\n总计: {passed}/{total} 测试通过")

    if passed == total:
        print("\n🎉 所有测试通过！DOM文本渲染模式已准备就绪。")
        return 0
    else:
        print(f"\n⚠️  有 {total - passed} 个测试失败，请检查。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
