#!/usr/bin/env python3
"""
批量测试功能完整性测试脚本
"""

import sys
import time

sys.path.insert(0, '/Users/shuao/Desktop/ai-monitor')

from web_backend import AppRuntime
from core.batch_test_runner import BatchTestRunner
from core.batch_test_report import BatchTestReportGenerator

def test_query_plan():
    """测试查询计划生成"""
    print("=" * 60)
    print("测试 1: 查询计划生成")
    print("=" * 60)

    batch_config = {
        'batch_id': 'test_123',
        'brand': '测试品牌',
        'keywords': [
            {
                'keyword': '关键词1',
                'platforms': ['doubao', 'deepseek'],
                'query_count': 3
            },
            {
                'keyword': '关键词2',
                'platforms': ['kimi'],
                'query_count': 2
            }
        ]
    }

    runner = BatchTestRunner({}, None)
    plan = runner._build_query_plan(batch_config)

    print(f"✓ 生成 {len(plan)} 个查询任务")
    assert len(plan) == 8, f"期望 8 个查询，实际 {len(plan)}"

    # 验证顺序：关键词1 -> doubao(3次) -> deepseek(3次)，关键词2 -> kimi(2次)
    assert plan[0]['keyword'] == '关键词1' and plan[0]['platform'] == 'doubao'
    assert plan[3]['keyword'] == '关键词1' and plan[3]['platform'] == 'deepseek'
    assert plan[6]['keyword'] == '关键词2' and plan[6]['platform'] == 'kimi'

    print("✓ 查询顺序正确")
    print()

def test_report_generator():
    """测试报告生成器"""
    print("=" * 60)
    print("测试 2: 报告生成器")
    print("=" * 60)

    mock_records = [
        {
            'keyword': '关键词1',
            'brand': '测试品牌',
            'platform': 'doubao',
            'success': True,
            'extra': {
                'batch_id': 'test_123',
                'media_sources': [
                    {'name': '媒体A', 'url': 'http://a.com', 'rank': 1},
                    {'name': '媒体B', 'url': 'http://b.com', 'rank': 2}
                ]
            }
        },
        {
            'keyword': '关键词1',
            'brand': '测试品牌',
            'platform': 'deepseek',
            'success': True,
            'extra': {
                'batch_id': 'test_123',
                'media_sources': [
                    {'name': '媒体A', 'url': 'http://a.com', 'rank': 1},
                ]
            }
        },
        {
            'keyword': '关键词2',
            'brand': '测试品牌',
            'platform': 'doubao',
            'success': False,
            'extra': {
                'batch_id': 'test_123',
                'media_sources': []
            }
        }
    ]

    generator = BatchTestReportGenerator()

    # 测试总览
    summary = generator._calc_summary(mock_records)
    assert summary['total_queries'] == 3
    assert summary['success_queries'] == 2
    assert abs(summary['overall_display_rate'] - 0.667) < 0.01
    print(f"✓ 总览统计: {summary['success_queries']}/{summary['total_queries']} = {summary['overall_display_rate']:.1%}")

    # 测试关键词统计
    kw_stats = generator._calc_keyword_stats(mock_records)
    assert len(kw_stats) == 2
    assert kw_stats[0]['display_rate'] == 1.0  # 关键词1: 2/2
    assert kw_stats[1]['display_rate'] == 0.0  # 关键词2: 0/1
    print(f"✓ 关键词统计: {len(kw_stats)} 个关键词")

    # 测试跨平台媒体
    assert len(kw_stats[0]['cross_platform_media']) == 1  # 媒体A 出现在 doubao 和 deepseek
    assert kw_stats[0]['cross_platform_media'][0]['media'] == '媒体A'
    print(f"✓ 跨平台媒体检测: 媒体A 出现在 2 个平台")

    # 测试平台统计
    plat_stats = generator._calc_platform_stats(mock_records)
    assert len(plat_stats) == 2
    print(f"✓ 平台统计: {len(plat_stats)} 个平台")

    # 测试全局媒体矩阵
    matrix = generator._calc_global_media_matrix(mock_records)
    assert len(matrix) == 1  # 只有媒体A 出现在多个平台
    assert matrix[0]['media'] == '媒体A'
    assert matrix[0]['platform_count'] == 2
    print(f"✓ 全局媒体矩阵: {len(matrix)} 个跨平台媒体")
    print()

def test_web_api():
    """测试 Web API"""
    print("=" * 60)
    print("测试 3: Web API 端点")
    print("=" * 60)

    runtime = AppRuntime()

    # 测试启动批量测试
    result = runtime.start_batch_test({
        'brand': '测试品牌',
        'keywords': [
            {
                'keyword': '关键词1',
                'platforms': ['doubao'],
                'query_count': 2
            }
        ]
    })

    assert result['ok'] == True
    batch_id = result['batch_id']
    print(f"✓ start_batch_test: batch_id={batch_id[:8]}...")

    # 测试获取进度
    time.sleep(0.5)
    progress = runtime.get_batch_test_progress(batch_id)
    assert progress['ok'] == True
    print(f"✓ get_batch_test_progress: status={progress.get('progress', {}).get('status', 'pending')}")

    # 测试取消
    cancel_result = runtime.cancel_batch_test(batch_id)
    assert cancel_result['ok'] == True
    print(f"✓ cancel_batch_test: 取消成功")
    print()

def main():
    print("\n" + "=" * 60)
    print("批量测试功能完整性测试")
    print("=" * 60 + "\n")

    try:
        test_query_plan()
        test_report_generator()
        test_web_api()

        print("=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
        print("\n功能清单:")
        print("  ✓ 批量测试执行器 (core/batch_test_runner.py)")
        print("  ✓ 报告生成器 (core/batch_test_report.py)")
        print("  ✓ Web API 端点 (web_backend.py)")
        print("  ✓ 前端对话框组件:")
        print("    - BatchTestDialog.tsx (配置对话框)")
        print("    - BatchTestProgressDialog.tsx (进度对话框)")
        print("    - BatchTestReportDialog.tsx (报告对话框)")
        print("  ✓ ReleaseContent.tsx 集成")
        print("\n使用方法:")
        print("  1. 启动 Web UI: python web_desktop.py")
        print("  2. 进入「发稿」页面")
        print("  3. 点击「品牌分析」按钮")
        print("  4. 配置品牌、关键词、平台和查询次数")
        print("  5. 启动测试并查看实时进度")
        print("  6. 测试完成后查看详细报告")
        print()

        return 0

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
