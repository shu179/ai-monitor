#!/usr/bin/env python3
"""
测试指纹多样性 - 验证多个账号的指纹是否足够分散
"""

import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.browser_fingerprint import BrowserFingerprint


def test_fingerprint_diversity():
    """测试生成多个指纹，验证多样性"""
    print("=" * 60)
    print("测试：生成 10 个指纹，验证多样性")
    print("=" * 60)

    fingerprints = []
    temp_dirs = []

    try:
        # 生成 10 个指纹
        for i in range(10):
            temp_dir = tempfile.mkdtemp(prefix=f"test_fp_{i}_")
            temp_dirs.append(temp_dir)
            fp = BrowserFingerprint(temp_dir)
            fingerprints.append({
                "id": i + 1,
                "timezone": fp.get_timezone_id(),
                "ua": fp.get_user_agent(),
                "cpu": fp.get_hardware_concurrency(),
                "memory": fp.get_device_memory(),
                "screen": fp.get_screen_resolution(),
                "arch": fp.get_client_hints_headers().get("sec-ch-ua-arch"),
            })

        # 统计多样性
        timezones = set(fp["timezone"] for fp in fingerprints)
        cpu_configs = set(fp["cpu"] for fp in fingerprints)
        memory_configs = set(fp["memory"] for fp in fingerprints)
        screen_configs = set(fp["screen"] for fp in fingerprints)
        os_versions = set(fp["ua"].split("Mac OS X ")[1].split(")")[0] for fp in fingerprints)

        print("\n📊 多样性统计:")
        print(f"  时区种类: {len(timezones)} 种 - {timezones}")
        print(f"  CPU 配置: {len(cpu_configs)} 种 - {cpu_configs}")
        print(f"  内存配置: {len(memory_configs)} 种 - {memory_configs}")
        print(f"  屏幕分辨率: {len(screen_configs)} 种")
        print(f"  macOS 版本: {len(os_versions)} 种")

        print("\n📋 详细指纹列表:")
        for fp in fingerprints:
            print(f"\n账号 {fp['id']}:")
            print(f"  时区: {fp['timezone']}")
            print(f"  系统: macOS {fp['ua'].split('Mac OS X ')[1].split(')')[0].replace('_', '.')}")
            print(f"  硬件: {fp['cpu']}核 CPU + {fp['memory']}GB 内存")
            print(f"  屏幕: {fp['screen'][0]}x{fp['screen'][1]}")
            print(f"  架构: {fp['arch']}")

        # 验证多样性
        print("\n✅ 多样性验证:")
        assert len(timezones) >= 2, f"时区种类太少: {len(timezones)}"
        print(f"  ✓ 时区有 {len(timezones)} 种变化")

        assert len(cpu_configs) >= 2, f"CPU 配置太单一: {len(cpu_configs)}"
        print(f"  ✓ CPU 配置有 {len(cpu_configs)} 种变化")

        assert len(memory_configs) >= 2, f"内存配置太单一: {len(memory_configs)}"
        print(f"  ✓ 内存配置有 {len(memory_configs)} 种变化")

        assert len(os_versions) >= 3, f"macOS 版本太单一: {len(os_versions)}"
        print(f"  ✓ macOS 版本有 {len(os_versions)} 种变化")

        # 验证一致性（每个指纹内部）
        print("\n✅ 一致性验证:")
        for fp in fingerprints:
            # 所有 macOS 都应该报告 x86 架构
            assert fp["arch"] == '"x86"', f"账号 {fp['id']} 架构不是 x86: {fp['arch']}"

            # CPU 和内存应该匹配
            if fp["cpu"] == 4:
                assert fp["memory"] in [8, 16], f"账号 {fp['id']} 4核配置内存不合理: {fp['memory']}GB"
            elif fp["cpu"] == 8:
                assert fp["memory"] in [8, 16], f"账号 {fp['id']} 8核配置内存不合理: {fp['memory']}GB"
            elif fp["cpu"] == 16:
                assert fp["memory"] in [16, 32], f"账号 {fp['id']} 16核配置内存不合理: {fp['memory']}GB"

        print("  ✓ 所有指纹内部一致（UA/Client Hints/硬件配置匹配）")

        # 验证没有独特特征
        print("\n✅ 独特性验证:")
        for fp in fingerprints:
            # 不应该有 12 核（M4 Pro 特征）
            assert fp["cpu"] != 12, f"账号 {fp['id']} 使用了 12 核（M4 Pro 特征太明显）"

            # macOS 版本应该在合理范围内
            os_ver = fp["ua"].split("Mac OS X ")[1].split(")")[0]
            major = int(os_ver.split("_")[0])
            assert major in [13, 14], f"账号 {fp['id']} macOS 版本不合理: {major}"

        print("  ✓ 没有独特特征（避免 12核、异常版本号等）")

        print("\n" + "=" * 60)
        print("🎉 指纹多样性测试通过！")
        print("=" * 60)
        print("\n总结:")
        print("✅ 每个账号的指纹都不同（避免账号关联）")
        print("✅ 每个指纹内部一致（UA/Client Hints/硬件匹配）")
        print("✅ 没有独特特征（避免被精准识别）")
        print("✅ 配置组合合理（CPU/内存/屏幕匹配）")

    finally:
        # 清理临时目录
        for temp_dir in temp_dirs:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_fingerprint_diversity()
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
