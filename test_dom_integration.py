#!/usr/bin/env python3
"""
DOM文本渲染模式端到端集成测试
模拟完整的识别流程：文本复制 → 品牌匹配 → 渲染截图 → 路由发送
"""

import sys
import time
import subprocess
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

def set_clipboard_text(text: str):
    """设置剪贴板文本（macOS）"""
    if sys.platform == "darwin":
        process = subprocess.Popen(['pbcopy'], stdin=subprocess.PIPE)
        process.communicate(text.encode('utf-8'))
        return True
    elif sys.platform == "win32":
        process = subprocess.Popen(['powershell', '-command', f'Set-Clipboard -Value "{text}"'])
        process.wait()
        return True
    else:
        print("⚠️  当前平台不支持自动设置剪贴板")
        return False

def test_end_to_end():
    """端到端测试"""
    print("=" * 60)
    print("DOM文本渲染模式端到端测试")
    print("=" * 60)
    print()

    # 测试文本
    test_text = """# AI平台对比

## DeepSeek
DeepSeek是一家专注于大语言模型研发的人工智能公司。

**优势：**
- 推理能力强
- 模型：DeepSeek-V3
- 开源友好

## Kimi
Kimi是月之暗面推出的智能助手。

**优势：**
- 长文本处理
- 模型：Moonshot
- 上下文窗口大

## 豆包
豆包是字节跳动推出的AI助手。

**特点：**
- 多模态能力
- 集成抖音生态
- 企业级应用

## 对比表格
| 平台 | 公司 | 特点 |
|------|------|------|
| DeepSeek | DeepSeek | 推理强 |
| Kimi | 月之暗面 | 长文本 |
| 豆包 | 字节跳动 | 多模态 |
"""

    print("步骤1: 设置剪贴板文本")
    print("-" * 60)
    if not set_clipboard_text(test_text):
        print("❌ 无法设置剪贴板，测试终止")
        return False
    print(f"✅ 已将测试文本复制到剪贴板（{len(test_text)} 字符）")
    print()

    print("步骤2: 模拟识别模式处理")
    print("-" * 60)
    print("提示：现在可以启动识别模式（dom_render_mode: true）")
    print("      识别模式会自动检测到剪贴板文本并处理")
    print()
    print("预期行为：")
    print("  1. 检测到剪贴板文本变化")
    print("  2. 文本长度检查通过（{} 字符）".format(len(test_text)))
    print("  3. 品牌匹配：DeepSeek, Kimi, 豆包")
    print("  4. 跳过AI识别（已在文本态完成）")
    print("  5. 路由到对应任务批次")
    print("  6. 发送前使用最终关键词和品牌渲染通知截图")
    print("  7. 发送企业微信通知")
    print()

    print("步骤3: 验证配置")
    print("-" * 60)

    import yaml
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        print("❌ config.yaml 不存在")
        return False

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    recognition_config = config.get("recognition", {})
    dom_mode = recognition_config.get("dom_render_mode", False)
    min_length = recognition_config.get("dom_render_min_length", 10)
    max_length = recognition_config.get("dom_render_max_length", 50000)

    print(f"DOM渲染模式: {'✅ 已启用' if dom_mode else '❌ 未启用（需要设置为true）'}")
    print(f"文本长度限制: {min_length} - {max_length} 字符")
    print()

    if not dom_mode:
        print("⚠️  提示：需要在 config.yaml 中设置 recognition.dom_render_mode: true")
        print()

    print("步骤4: 手动测试指南")
    print("-" * 60)
    print("1. 确保 config.yaml 中 recognition.dom_render_mode: true")
    print("2. 启动应用：python3 main.py")
    print("3. 切换到识别模式")
    print("4. 从AI对话页面复制一段包含品牌词的回答")
    print("5. 观察日志输出：")
    print("   - [Recognition] 文本已识别品牌 [...]")
    print("   - [Recognition] 使用文本匹配结果: [...]")
    print("   - [html_renderer] Satori截图已保存: ...（或 Playwright fallback 截图已保存）")
    print("6. 检查 screenshots/recognition/decorated/ 目录下的 *_dom.jpg 文件")
    print("7. 验证企业微信是否收到通知")
    print()

    print("=" * 60)
    print("✅ 集成测试准备完成")
    print("=" * 60)
    print()
    print("下一步：")
    print("1. 启动应用进行实际测试")
    print("2. 复制包含品牌词的文本")
    print("3. 验证端到端流程")
    print()

    return True

def main():
    try:
        success = test_end_to_end()
        return 0 if success else 1
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
