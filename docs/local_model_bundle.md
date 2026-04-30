# 本地模型随安装包分发

当前项目已经支持把 `Ollama` 二进制和本地模型仓库一起打进安装包。

## 推荐做法

优先使用仓库自带脚本自动准备资源：

```bash
python3 scripts/prepare_local_model_bundle.py
```

如果你本机还没有准备好 `Ollama` 可执行文件，也可以先直接拉官方 release 资产到随包目录：

```bash
python3 scripts/fetch_ollama_bundle_binary.py --bundle-platform darwin-arm64
python3 scripts/fetch_ollama_bundle_binary.py --bundle-platform windows-amd64
```

这一步只负责准备内置引擎，不会自动拉模型。

默认会做两件事：

- 自动收集本机 `Ollama` 可执行文件到 `third_party/ollama/<平台>/`
- 自动收集本机 `gemma4:e2b` 到 `third_party/ollama-models/`

准备完资源后，建议马上跑一次自检：

```bash
python3 scripts/check_local_model_bundle.py
```

如果看到“随包资源检查通过，可以进入打包流程”，就说明这份包的本地模型资源结构已经齐了。

如果你想把本机已经拉过的全部 Ollama 模型都一起打包：

```bash
python3 scripts/prepare_local_model_bundle.py --all-local-models
```

如果本机 `ollama` 或模型仓库不在默认位置，也可以手动指定：

```bash
python3 scripts/prepare_local_model_bundle.py \
  --ollama-bin /Applications/Ollama.app/Contents/Resources/ollama \
  --models-dir ~/.ollama/models
```

如果你已经手动下载了官方归档，也可以直接从本地归档提取：

```bash
python3 scripts/fetch_ollama_bundle_binary.py \
  --bundle-platform windows-amd64 \
  --archive /path/to/ollama-windows-amd64.zip
```

如果你是在一台机器上先准备 Windows 包的内置引擎，也可以手动指定目标平台：

```bash
python3 scripts/prepare_local_model_bundle.py \
  --bundle-platform windows-amd64 \
  --ollama-bin /path/to/ollama.exe \
  --skip-models
```

然后检查：

```bash
python3 scripts/check_local_model_bundle.py --bundle-platform windows-amd64
```

如果你是在 Windows 打包机上操作，也可以直接用一键脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows_bundle.ps1
```

它会按顺序完成：

1. 安装 Python 依赖
2. 构建 `web-ui`
3. 下载 Windows 内置 `ollama.exe`
4. 用这个内置 `ollama.exe` 直接把 `gemma4:e2b` 拉到 `third_party/ollama-models`
5. 执行资源检查
6. 调用 `pyinstaller build.spec`

## 目录约定

打包前，把资源放到项目根目录下：

```text
third_party/
  ollama/
    darwin-arm64/
      ollama
    windows-amd64/
      ollama.exe
  ollama-models/
    manifests/
    blobs/
```

说明：

- `third_party/ollama/<平台>/`
  - 按打包目标平台分别放 `ollama` 可执行文件
  - 例如：
    - `darwin-arm64/ollama`
    - `windows-amd64/ollama.exe`
- `third_party/ollama-models/`
  - 放完整的 Ollama 模型仓库
  - 目录内要能看到 `manifests/` 和 `blobs/`

推荐平台标识：

- mac Apple Silicon: `darwin-arm64`
- mac Intel: `darwin-amd64`
- Windows 64 位: `windows-amd64`

## 如何准备模型仓库

先在打包机上准备好模型：

```bash
ollama pull gemma4:e2b
```

然后把本机 Ollama 模型目录复制到项目里：

```bash
mkdir -p third_party/ollama-models
cp -R ~/.ollama/models/manifests third_party/ollama-models/
cp -R ~/.ollama/models/blobs third_party/ollama-models/
```

如果你用的是自定义 `OLLAMA_MODELS` 目录，就从那个目录复制。

上面这套手工步骤现在都可以交给 `scripts/prepare_local_model_bundle.py` 来做。

## 运行时行为

程序启动后会：

1. 读取安装包内的 `third_party/ollama-models`
2. 自动把缺失文件复制到用户数据目录下的 `ollama/models`
3. 用这个数据目录启动程序自管的 `Ollama`

默认数据目录：

- macOS: `~/Library/Application Support/AIBrandMonitor/ollama/models`
- Windows: `%APPDATA%/AIBrandMonitor/ollama/models`

这样做的好处：

- 安装包里的资源保持只读
- 第一次启动后模型可持续复用
- 后续如果你更新安装包，也只会补齐缺失文件，不会直接覆盖用户已有模型

## 安装阶段自动准备

如果你希望小白用户“装完就能直接点开用”，推荐在安装器结束前自动跑一次运行时准备：

```bash
Surfaced.exe --prepare-local-runtime
```

这个入口现在已经接进主程序，适合安装器、首启引导、升级后补资源时直接调用。

它会自动做这些事情：

- 读取安装后的 `config.yaml`
- 启动内置 `Ollama`
- 把随包模型补到用户数据目录
- 检查目标模型是否可用
- 如果允许，会自动拉取缺失模型

如果你只想做“轻量预热”，不希望安装器阶段联网拉模型，可以这样调用：

```bash
Surfaced.exe --prepare-local-runtime --prepare-local-runtime-no-model-pull
```

源码模式下，也可以直接测试同一套逻辑：

```bash
python3 scripts/prepare_local_runtime.py
```

如果要强制指定模型：

```bash
python3 scripts/prepare_local_runtime.py --model gemma4:e2b
```

## Windows 安装器示例

如果你用 Inno Setup，可以在安装完成后自动执行准备步骤，再决定是否启动主程序：

```ini
[Run]
Filename: "{app}\Surfaced.exe"; Parameters: "--prepare-local-runtime"; Flags: runhidden waituntilterminated; StatusMsg: "正在准备本地模型运行环境..."
Filename: "{app}\Surfaced.exe"; Description: "启动 Surfaced"; Flags: nowait postinstall skipifsilent
```

如果你担心首次安装时间太长，也可以改成轻量模式：

```ini
[Run]
Filename: "{app}\Surfaced.exe"; Parameters: "--prepare-local-runtime --prepare-local-runtime-no-model-pull"; Flags: runhidden waituntilterminated; StatusMsg: "正在初始化本地模型环境..."
Filename: "{app}\Surfaced.exe"; Description: "启动 Surfaced"; Flags: nowait postinstall skipifsilent
```

建议：

- 完全离线包：直接用 `--prepare-local-runtime`
- 在线轻量包：先用 `--prepare-local-runtime --prepare-local-runtime-no-model-pull`
- 如果准备失败，安装器可以继续完成安装，但首次启动页里要提示用户重新执行一次准备

## 打包

`build.spec` 已经支持自动打包以下目录：

- `third_party/ollama/<当前平台>`
- `third_party/ollama-models`

只要目录存在，执行原来的打包命令即可。

## 注意

- `gemma4:e2b` 模型体积不小，安装包会明显变大。
- macOS 下如果要分发给别人，建议在最终产物阶段重新做签名和公证。
- 如果希望“首次完全离线可用”，必须同时带上 `blobs` 和 `manifests`，不能只带模型名配置。
- `PyInstaller` 不能跨平台打包，Windows 包最终仍然需要在 Windows 环境里执行 `pyinstaller build.spec`。
