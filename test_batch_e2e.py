#!/usr/bin/env python3
"""
批量测试功能端到端测试
"""

import sys
import time
import json

sys.path.insert(0, '.')

from web_backend import AppRuntime

def test_full_workflow():
    print("=" * 60)
    print("批量测试完整流程测试")
    print("=" * 60)

    runtime = AppRuntime()

    # 1. 启动批量测试
    print("\n[1] 启动批量测试...")
    result = runtime.start_batch_test({
        'brand': '华为',
        'keywords': [
            {
                'keyword': '手机推荐',
                'platforms': ['doubao'],
                'query_count': 1
            }
        ]
    })

    if not result.get('ok'):
        print(f"❌ 启动失败: {result.get('message')}")
        return False

    batch_id = result['batch_id']
    print(f"✓ 启动成功，batch_id: {batch_id[:8]}...")

    # 2. 轮询进度
    print("\n[2] 监控进度...")
    max_wait = 60  # 最多等待60秒
    start_time = time.time()

    while time.time() - start_time < max_wait:
        progress_result = runtime.get_batch_test_progress(batch_id)

        if not progress_result.get('ok'):
            print(f"❌ 获取进度失败: {progress_result.get('message')}")
            return False

        progress = progress_result.get('progress', {})
        status = progress.get('status', 'pending')
        completed = progress.get('completed_queries', 0)
        total = progress.get('total_queries', 0)
        status_text = progress.get('status_text', '')

        print(f"  状态: {status} | 进度: {completed}/{total} | {status_text}")

        if status == 'completed':
            print("✓ 测试完成")
            break
        elif status == 'failed':
            print("❌ 测试失败")
            return False
        elif status == 'cancelled':
            print("⚠ 测试已取消")
            return False

        time.sleep(2)
    else:
        print("⚠ 超时，但测试可能仍在后台运行")

    # 3. 获取报告
    print("\n[3] 获取测试报告...")
    time.sleep(1)  # 等待报告生成

    report_result = runtime.get_batch_test_report(batch_id)

    if not report_result.get('ok'):
        print(f"❌ 获取报告失败: {report_result.get('message')}")
        return False

    report = report_result.get('report', {})
    summary = report.get('summary', {})

    print(f"✓ 报告生成成功")
    print(f"  品牌: {report.get('brand')}")
    print(f"  总查询: {summary.get('total_queries')}")
    print(f"  成功: {summary.get('success_queries')}")
    print(f"  展示率: {summary.get('overall_display_rate', 0) * 100:.1f}%")

    # 4. 验证数据结构
    print("\n[4] 验证数据结构...")

    # 验证进度数据结构
    if 'progress' not in progress_result:
        print("❌ 进度数据缺少 'progress' 字段")
        return False

    required_progress_fields = ['status', 'total_queries', 'completed_queries', 'status_text']
    for field in required_progress_fields:
        if field not in progress:
            print(f"❌ 进度数据缺少 '{field}' 字段")
            return False

    # 验证报告数据结构
    if 'report' not in report_result:
        print("❌ 报告数据缺少 'report' 字段")
        return False

    required_report_fields = ['batch_id', 'brand', 'summary', 'keyword_stats', 'platform_stats']
    for field in required_report_fields:
        if field not in report:
            print(f"❌ 报告数据缺少 '{field}' 字段")
            return False

    print("✓ 数据结构验证通过")

    print("\n" + "=" * 60)
    print("✅ 完整流程测试通过")
    print("=" * 60)

    return True

if __name__ == "__main__":
    try:
        success = test_full_workflow()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
