# AI品牌监控系统

基于 Playwright 的多平台品牌排名监控工具，支持豆包、DeepSeek、Kimi、元宝、通义千问、文心一言六大AI平台。

## 功能特性

- **多平台支持**：豆包、DeepSeek、Kimi、腾讯元宝、阿里通义千问、百度文心一言
- **智能调度**：夜间休眠、随机间隔、星期控制
- **防封号**：模拟人类输入、30分钟去重、随机偏移
- **企业微信通知**：图文推送、频率控制
- **系统托盘**：后台运行、状态可视化
- **配置热重载**：修改配置无需重启

## 安装依赖

```bash
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium
```

## 配置文件

编辑 `config.yaml`：

```yaml
# 企业微信机器人 Webhook
notification:
  webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY"

# 监控任务
tasks:
  - platform: doubao
    keyword: "武汉geo优化公司"
    brand: "即搜AI"
    enabled: true
    weekdays: [0,1,2,3,4]  # 周一到周五

  - platform: deepseek
    keyword: "AI写作工具"
    brand: "DeepSeek"
    enabled: true
```

## 首次运行

1. **启动程序**：
```bash
python main.py
```

2. **登录各平台**：
   - 首次运行会自动打开浏览器
   - 在每个平台手动登录一次
   - 登录状态会自动保存到 `auth/` 目录
   - 后续无需再次登录

3. **系统托盘**：
   - 右键托盘图标查看状态
   - 点击"开始监控"启动定时任务

## 打包为可执行文件

```bash
# 安装 PyInstaller
pip install pyinstaller

# 打包（目录模式，启动更快）
pyinstaller build.spec

# 输出在 dist/AI品牌监控/ 目录
```

## 目录结构

```
ai-monitor/
├── main.py              # 主入口
├── config.yaml          # 配置文件
├── platforms/           # 平台适配器
│   ├── doubao.py
│   ├── deepseek.py
│   ├── kimi.py
│   └── ...
├── core/                # 核心模块
│   ├── notifier.py      # 企业微信通知
│   ├── scheduler.py     # 智能调度
│   └── config_watcher.py
├── ui/                  # 用户界面
│   └── tray.py          # 系统托盘
├── auth/                # 登录状态（自动创建）
├── screenshots/         # 截图保存（自动创建）
└── logs/                # 运行日志（自动创建）
```

## 注意事项

1. **首次登录**：每个平台首次需要手动登录，之后自动保持登录状态
2. **频率控制**：默认5分钟检查一次，夜间23:00-07:00自动休眠
3. **去重机制**：相同排名30分钟内不会重复通知
4. **截图清理**：自动保留7天，超过500MB删除旧文件

## License

MIT
