; Windows 安装器示例：安装完成后自动准备本地模型运行环境

[Run]
Filename: "{app}\Surfaced.exe"; Parameters: "--prepare-local-runtime"; Flags: runhidden waituntilterminated; StatusMsg: "正在准备本地模型运行环境..."
Filename: "{app}\Surfaced.exe"; Description: "启动 Surfaced"; Flags: nowait postinstall skipifsilent
