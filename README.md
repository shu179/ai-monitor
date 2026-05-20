# Surfaced

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

如果要运行仓库内的自动化测试，建议额外安装开发依赖：

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

推荐使用 `Python 3.12` 或 `Python 3.13`。

在 macOS 上，如果使用 `Python 3.14` 启动时出现 `zsh: abort python3 main.py`，通常不是业务代码异常，而是当前 `tkinter/Tk` 运行时在创建 GUI 窗口时直接崩溃。此时请切换到 `Python 3.12/3.13` 后重新安装依赖再运行。

## Web 桌面模式

如果你想用 Figma Make 导出的 Web UI 作为桌面端界面，可以启动新的 Web 模式：

```bash
cd web-ui
npm install
npm run build
cd ..
python main.py --web
```

这个模式会优先启动本地 Python 后端并打开桌面壳；如果本地服务无法启动，界面会退回到静态页面和默认数据。

## 版本信息

当前程序版本统一维护在 `core/version.py`。

```bash
python main.py --version
```

后续如果你打包后继续迭代，只需要先更新这里的版本号，就能同步影响启动信息、Web 接口返回和打包产物名称来源。

如果你准备做“已安装版本升级到新版本”，建议配合一个升级清单 JSON。仓库里已经放了示例文件：

- `release_manifest.example.json`

系统设置里的“应用更新”已经支持保存更新通道、清单地址和下载页地址，并可手动检查更新。推荐实际升级时使用“外部安装包”或“独立 updater”在程序退出后替换文件，不要让正在运行的主程序直接覆盖自己。

仓库里也已经提供了独立更新器原型：

- `updater.py`
- `updater.spec`

它的职责是等待主程序退出后，把“已解压的新版本目录”替换到当前程序目录，并可选重启程序。典型流程是：

1. 主程序先下载并解压新版本到临时目录
2. 主程序启动独立 updater
3. 主程序退出
4. updater 完成替换并重启

源码模式下可直接测试：

```bash
python updater.py \
  --source-dir /path/to/new-app-dir \
  --target-dir /path/to/current-app-dir \
  --wait-pid 12345 \
  --cleanup-source
```

现在系统设置里的“应用更新”也已经支持：

1. 填写“已解压的新版本目录”
2. 预览安装计划
3. 启动独立 updater
4. 自动退出当前程序并完成替换

以后如果接云端更新，只需要把“本地目录来源”替换成“下载后解压到临时目录”，底层 updater 不需要重写。

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

浏览器模式下也支持按平台覆盖反检测/代理参数，写在 `browser_automation.<平台名>` 下：

```yaml
browser_automation:
  doubao:
    browser_locale: zh-CN
    browser_accept_language: zh-CN,zh;q=0.9,en;q=0.8
    browser_timezone_id: Asia/Shanghai
    browser_user_agent: ""
    browser_proxy_server: ""
    browser_proxy_username: ""
    browser_proxy_password: ""
    browser_extra_args: "--disable-features=Translate"
    page_stabilize_wait_min_ms: 1200
    page_stabilize_wait_max_ms: 2600
    failure_backoff_base_seconds: 3
    failure_backoff_max_seconds: 12
```

调度告警也支持单独控制连续失败提醒节奏：

```yaml
default_notification:
  failure_alert_threshold: 7
  failure_alert_cooldown_minutes: 5
```

## 首次运行

1. **启动程序**：
```bash
python main.py
```

如果你本机同时装有多个 Python 版本，macOS 建议优先使用：

```bash
python3.13 -m pip install -r requirements.txt
python3.13 main.py
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

# 如果要把本地 Gemma 4 E2B 一起随包分发，先准备 third_party 资源
python3 scripts/fetch_ollama_bundle_binary.py --bundle-platform darwin-arm64
python3 scripts/prepare_local_model_bundle.py

# 打包前先检查资源是否齐全
python3 scripts/check_local_model_bundle.py

# 打包（目录模式，启动更快）
pyinstaller build.spec

# 输出在 dist/Surfaced/ 目录
```

如果你希望把本机 Ollama 里的所有模型都一起打包，可以改用：

```bash
python3 scripts/prepare_local_model_bundle.py --all-local-models
```

如果你还要准备 Windows 包的内置引擎，可以直接拉官方 release 资产到随包目录：

```bash
python3 scripts/fetch_ollama_bundle_binary.py --bundle-platform windows-amd64
python3 scripts/check_local_model_bundle.py --bundle-platform windows-amd64
```

注意：`PyInstaller` 不能跨平台打包，Windows 包最终仍然需要在 Windows 环境里执行 `pyinstaller build.spec`。

如果你希望在 Windows 打包机上一条命令走完“装依赖、构建前端、准备内置引擎、拉取模型、验包、打包”，可以直接运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows_bundle.ps1
```

如果你希望安装完成后自动把本地模型运行环境准备好，现在可以直接让打包后的程序执行：

```bash
Surfaced.exe --prepare-local-runtime
```

如果安装阶段不想联网拉模型，可以改成：

```bash
Surfaced.exe --prepare-local-runtime --prepare-local-runtime-no-model-pull
```

源码模式下可先验证这条链路：

```bash
python3 scripts/prepare_local_runtime.py
```

仓库里也附了一个 Inno Setup 示例：

- `scripts/inno_setup_local_runtime_example.iss`

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
