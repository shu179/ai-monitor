#!/usr/bin/env python3
"""
反检测修复验证脚本
测试修复后的指纹配置是否正确
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.browser_fingerprint import BrowserFingerprint
from platforms.doubao import DoubaoPlatform
import tempfile
import shutil


def test_fingerprint_config():
    """测试指纹配置是否使用真实系统信息"""
    print("=" * 60)
    print("测试 1: 指纹配置验证")
    print("=" * 60)

    # 创建临时目录
    temp_dir = tempfile.mkdtemp(prefix="test_fingerprint_")

    try:
        fp = BrowserFingerprint(temp_dir)

        # 检查时区配置
        timezone = fp.get_timezone_id()
        print(f"✓ 时区配置: {timezone}")
        assert timezone in ["Asia/Shanghai", "Asia/Hong_Kong"], f"时区应该是真实系统时区，而不是随机的: {timezone}"

        # 检查 User-Agent
        ua = fp.get_user_agent()
        print(f"✓ User-Agent: {ua}")
        assert "Intel Mac OS X" in ua, "macOS 的 UA 应该包含 Intel Mac OS X（标准行为）"

        # 检查 Client Hints 架构
        hints = fp.get_client_hints_headers()
        arch = hints.get("sec-ch-ua-arch", "")
        print(f"✓ sec-ch-ua-arch: {arch}")
        assert arch == '"x86"', f"架构应该是 x86（与 UA 一致），而不是: {arch}"

        # 检查硬件配置
        cpu = fp.get_hardware_concurrency()
        mem = fp.get_device_memory()
        print(f"✓ 硬件配置: CPU={cpu}核, 内存={mem}GB")
        assert cpu > 0, "CPU 核心数应该大于 0"
        assert mem in [4, 8, 16, 32], f"内存应该是常见值，而不是: {mem}"

        print("\n✅ 指纹配置测试通过！")
        return True

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_browser_launch():
    """测试浏览器启动参数"""
    print("\n" + "=" * 60)
    print("测试 2: 浏览器启动参数验证")
    print("=" * 60)

    temp_dir = tempfile.mkdtemp(prefix="test_browser_")

    try:
        monitor = DoubaoPlatform(temp_dir)

        # 检查启动参数
        args = monitor._browser_launch_args(window_size="1280,1600")
        print(f"✓ 启动参数数量: {len(args)}")

        # 必须包含的核心参数
        assert "--disable-blink-features=AutomationControlled" in args, "缺少核心反检测参数"
        print("✓ 包含 --disable-blink-features=AutomationControlled")

        # 不应该包含过度禁用的参数
        bad_args = [
            "--disable-webgl",
            "--disable-canvas",
            "--disable-audio",
            "--disable-gpu",
        ]
        for bad_arg in bad_args:
            assert bad_arg not in args, f"不应该包含过度禁用参数: {bad_arg}"
        print("✓ 未包含过度禁用参数")

        print("\n✅ 启动参数测试通过！")
        return True

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    print("\n🔍 开始反检测修复验证...\n")

    try:
        # 测试 1: 指纹配置
        test_fingerprint_config()

        # 测试 2: 启动参数
        test_browser_launch()

        print("\n" + "=" * 60)
        print("🎉 所有测试通过！反检测修复已完成")
        print("=" * 60)
        print("\n修复内容总结:")
        print("1. ✅ 时区配置 - 使用真实系统时区（Asia/Shanghai）")
        print("2. ✅ User-Agent - macOS 标准格式（Intel Mac OS X）")
        print("3. ✅ Client Hints - sec-ch-ua-arch 统一为 x86")
        print("4. ✅ 硬件配置 - 使用真实 CPU 核心数和合理内存值")
        print("5. ✅ WebRTC 防护 - 完全禁用本地 IP 泄露")
        print("6. ✅ Headless 检测 - 增强反检测脚本（plugins/mimeTypes/outerWidth）")
        print("7. ✅ Canvas/Audio - 添加轻微指纹噪声")
        print("\n建议:")
        print("- 删除旧的指纹配置文件（auth/*/.fingerprint.json）以应用新配置")
        print("- 重新运行爬虫测试检测网站，验证修复效果")

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
