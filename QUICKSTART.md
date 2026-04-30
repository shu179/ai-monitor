# DOM文本渲染模式快速开始

## 一键启动

```bash
./start_dom_mode.sh
```

这个脚本会：
1. 检查并启用DOM文本模式
2. 运行测试验证
3. 提供启动指南

## 手动启动

### 1. 启用DOM文本模式

编辑 `config.yaml`，设置：

```yaml
recognition:
  dom_render_mode: true
```

### 2. 运行测试

```bash
python3 test_dom_render.py
```

### 3. 启动应用

```bash
python3 main.py
```

### 4. 使用

1. 切换到识别模式
2. 从AI对话页面复制文本（Cmd+C）
3. 观察日志和企业微信通知

## 测试文本

复制以下文本进行测试：

```markdown
# AI平台对比

## DeepSeek
DeepSeek是一家专注于大语言模型研发的人工智能公司。

## Kimi
Kimi是月之暗面推出的智能助手。

## 豆包
豆包是字节跳动推出的AI助手。
```

## 预期结果

日志输出：
```
[Recognition] 文本已识别品牌 ['DeepSeek', 'Kimi', '豆包']，截图已渲染: text_0422_143025_abc12345.jpg
[Recognition] 使用文本匹配结果: ['DeepSeek', 'Kimi', '豆包']
```

截图文件：`screenshots/recognition/text_*.jpg`

## 更多信息

- 详细使用指南：[docs/DOM_TEXT_MODE_GUIDE.md](docs/DOM_TEXT_MODE_GUIDE.md)
- 实现总结：[DOM_TEXT_MODE_SUMMARY.md](DOM_TEXT_MODE_SUMMARY.md)
- 单元测试：`python3 test_dom_render.py`
- 集成测试：`python3 test_dom_integration.py`

## 切换回截图模式

编辑 `config.yaml`：

```yaml
recognition:
  dom_render_mode: false
```

## 故障排查

### 问题：文本没有被识别

**检查：**
1. `config.yaml` 中 `dom_render_mode: true`
2. 文本长度在 10-50000 字符之间
3. 文本包含配置的品牌词
4. 查看日志：`tail -f logs/monitor.log`

### 问题：渲染失败

**检查：**
1. 字体文件：`ls assets/fonts/`
2. 模板文件：`ls assets/templates/`
3. Playwright安装：`python3 -c "from patchright.sync_api import sync_playwright"`

## 支持

如有问题，请查看：
- 日志文件：`logs/monitor.log`
- 测试脚本：`test_dom_render.py`
- 使用指南：`docs/DOM_TEXT_MODE_GUIDE.md`
