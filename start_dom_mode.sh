#!/bin/bash
# DOM文本渲染模式快速启动脚本

echo "=========================================="
echo "DOM文本渲染模式快速启动"
echo "=========================================="
echo ""

# 检查配置
echo "步骤1: 检查配置..."
if grep -q "dom_render_mode: true" config.yaml; then
    echo "✅ DOM文本模式已启用"
else
    echo "⚠️  DOM文本模式未启用"
    echo ""
    echo "是否启用DOM文本模式？(y/n)"
    read -r response
    if [[ "$response" == "y" ]]; then
        # 备份配置
        cp config.yaml config.yaml.backup
        # 启用DOM模式
        sed -i '' 's/dom_render_mode: false/dom_render_mode: true/' config.yaml
        echo "✅ 已启用DOM文本模式（原配置已备份为 config.yaml.backup）"
    else
        echo "❌ 用户取消，保持截图模式"
        exit 0
    fi
fi

echo ""
echo "步骤2: 运行测试..."
python3 test_dom_render.py

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ 测试通过！"
    echo ""
    echo "步骤3: 启动应用..."
    echo ""
    echo "请运行以下命令启动应用："
    echo "  python3 main.py"
    echo ""
    echo "然后："
    echo "  1. 切换到识别模式"
    echo "  2. 从AI对话页面复制包含品牌词的文本"
    echo "  3. 观察日志输出和企业微信通知"
    echo ""
    echo "查看使用指南："
    echo "  cat docs/DOM_TEXT_MODE_GUIDE.md"
    echo ""
else
    echo ""
    echo "❌ 测试失败，请检查错误信息"
    exit 1
fi
