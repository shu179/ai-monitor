#!/usr/bin/env python3
"""
反检测能力测试脚本

访问常见的爬虫检测网站，验证反检测配置的有效性：
1. https://bot.sannysoft.com/ - 综合检测（WebDriver、Chrome、权限等）
2. https://arh.antoinevastel.com/bots/areyouheadless - Headless 检测
3. https://pixelscan.net/ - 指纹和自动化检测
4. https://abrahamjuliot.github.io/creepjs/ - 深度指纹分析
"""

import sys
import os
import time
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from platforms.doubao import DoubaoPlatform
from core.app_paths import resolve_app_dir

# 测试网站列表
TEST_SITES = [
    {
        "name": "Sannysoft_Bot_Detector",
        "url": "https://bot.sannysoft.com/",
        "wait": 5,
        "description": "综合检测 WebDriver、Chrome、权限等特征"
    },
    {
        "name": "Are_You_Headless",
        "url": "https://arh.antoinevastel.com/bots/areyouheadless",
        "wait": 3,
        "description": "Headless 模式检测"
    },
    {
        "name": "PixelScan",
        "url": "https://pixelscan.net/",
        "wait": 8,
        "description": "指纹和自动化检测（需要较长加载时间）"
    },
    {
        "name": "CreepJS",
        "url": "https://abrahamjuliot.github.io/creepjs/",
        "wait": 10,
        "description": "深度指纹分析（最全面，需要最长加载时间）"
    }
]


def main():
    """运行反检测测试"""
    # 创建测试目录
    test_dir = resolve_app_dir("temp_test_detection")
    os.makedirs(test_dir, exist_ok=True)

    # 创建截图目录
    screenshot_dir = os.path.join(os.path.dirname(__file__), "reports", "anti_detection_test")
    os.makedirs(screenshot_dir, exist_ok=True)

    print("=" * 70)
    print("反检测能力测试".center(70))
    print("=" * 70)
    print()
    print(f"测试目录: {test_dir}")
    print(f"截图目录: {screenshot_dir}")
    print()

    platform = DoubaoPlatform(test_dir)
    platform.inspect = True  # 前台显示浏览器，方便观察

    try:
        print("正在启动浏览器...")
        platform.start()
        print("✅ 浏览器已启动")
        print()

        results = []

        for i, site in enumerate(TEST_SITES, 1):
            print(f"[{i}/{len(TEST_SITES)}] 🌐 测试: {site['name']}")
            print(f"    URL: {site['url']}")
            print(f"    说明: {site['description']}")

            try:
                # 访问网站
                print(f"    → 正在访问...")
                platform.page.goto(site['url'], wait_until="domcontentloaded", timeout=30000)

                # 等待页面加载和检测完成
                print(f"    → 等待 {site['wait']} 秒让检测完成...")
                time.sleep(site['wait'])

                # 截图
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = os.path.join(screenshot_dir, f"{timestamp}_{site['name']}.png")
                platform.page.screenshot(path=screenshot_path, full_page=True)
                print(f"    ✅ 截图已保存: {screenshot_path}")

                results.append({
                    "name": site['name'],
                    "url": site['url'],
                    "screenshot": screenshot_path,
                    "status": "success"
                })

            except Exception as e:
                print(f"    ❌ 测试失败: {e}")
                results.append({
                    "name": site['name'],
                    "url": site['url'],
                    "status": "failed",
                    "error": str(e)
                })

            print()

            # 短暂停顿
            if i < len(TEST_SITES):
                time.sleep(2)

        # 输出测试总结
        print("=" * 70)
        print("测试完成".center(70))
        print("=" * 70)
        print()
        print("测试结果：")
        for result in results:
            status_icon = "✅" if result['status'] == 'success' else "❌"
            print(f"  {status_icon} {result['name']}")
            if result['status'] == 'success':
                print(f"     截图: {result['screenshot']}")
            else:
                print(f"     错误: {result.get('error', 'Unknown')}")
        print()
        print(f"所有截图保存在: {screenshot_dir}")
        print()
        print("请查看截图分析反检测效果：")
        print("  - Sannysoft: 检查红色警告项数量（越少越好）")
        print("  - Are You Headless: 检查是否显示 'You are not Chrome-Headless'")
        print("  - PixelScan: 检查 Automation Detection 结果")
        print("  - CreepJS: 检查 Trust Score 和 Lies 数量")
        print()
        print("=" * 70)

        # 保持浏览器打开，方便手动查看
        print("\n🔍 浏览器将保持打开，请手动检查检测结果")
        print("   按 Ctrl+C 关闭浏览器并退出\n")
        time.sleep(3600)  # 保持 1 小时

    except KeyboardInterrupt:
        print("\n\n👋 用户中断，关闭浏览器")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n正在关闭浏览器...")
        try:
            platform.close()
        except:
            pass
        print("✅ 测试结束")


if __name__ == "__main__":
    main()
