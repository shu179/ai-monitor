Param(
    [string]$PythonCmd = "",
    [string]$BundlePlatform = "windows-amd64",
    [string]$Model = "gemma4:e2b",
    [switch]$SkipFrontend,
    [switch]$SkipModelPull,
    [switch]$SkipPyInstaller
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host ("[STEP] " + $Message) -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host ("[INFO] " + $Message) -ForegroundColor Gray
}

function Write-Ok {
    param([string]$Message)
    Write-Host ("[OK] " + $Message) -ForegroundColor Green
}

function Resolve-PythonCommand {
    param([string]$Requested)

    if ($Requested) {
        return $Requested
    }

    $candidates = @(
        "py -3.13",
        "py -3.12",
        "python",
        "python3"
    )

    foreach ($candidate in $candidates) {
        try {
            $parts = $candidate -split '\s+'
            if ($parts.Length -gt 1) {
                & $parts[0] $parts[1..($parts.Length - 1)] --version *> $null
            } else {
                & $parts[0] --version *> $null
            }
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        } catch {
        }
    }

    throw "未找到可用 Python，请安装 Python 3.12 或 3.13。"
}

function Invoke-Python {
    param(
        [string]$Python,
        [string[]]$Arguments
    )

    $commandLine = "$Python $($Arguments -join ' ')"
    Write-Info $commandLine
    $parts = $Python -split '\s+'
    if ($parts.Length -gt 1) {
        & $parts[0] $parts[1..($parts.Length - 1)] @Arguments
    } else {
        & $Python @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败: $commandLine"
    }
}

function Wait-OllamaReady {
    param(
        [string]$BaseUrl,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-RestMethod -Uri ($BaseUrl.TrimEnd("/") + "/api/tags") -Method Get -TimeoutSec 3 | Out-Null
            return
        } catch {
            Start-Sleep -Seconds 2
        }
    }

    throw "等待 Ollama 服务就绪超时: $BaseUrl"
}

function Stop-ProcessSafe {
    param([System.Diagnostics.Process]$Process)
    if ($null -eq $Process) {
        return
    }
    try {
        if (-not $Process.HasExited) {
            $Process.Kill()
            $Process.WaitForExit()
        }
    } catch {
    }
}

$projectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $projectRoot

$python = Resolve-PythonCommand -Requested $PythonCmd
$ollamaBinary = Join-Path $projectRoot "third_party\ollama\$BundlePlatform\ollama.exe"
$modelsDir = Join-Path $projectRoot "third_party\ollama-models"
$ollamaHost = "127.0.0.1:11434"
$ollamaBaseUrl = "http://$ollamaHost"
$serveProcess = $null

Write-Info "项目目录: $projectRoot"
Write-Info "Python 命令: $python"

try {
    Write-Step "安装 Python 依赖"
    Invoke-Python -Python $python -Arguments @("-m", "pip", "install", "-r", "requirements.txt")
    Invoke-Python -Python $python -Arguments @("-m", "pip", "install", "pyinstaller")
    Write-Ok "Python 依赖已安装"

    if (-not $SkipFrontend) {
        Write-Step "构建前端"
        Push-Location (Join-Path $projectRoot "web-ui")
        try {
            npm install
            if ($LASTEXITCODE -ne 0) {
                throw "npm install 失败"
            }
            npm run build
            if ($LASTEXITCODE -ne 0) {
                throw "npm run build 失败"
            }
        } finally {
            Pop-Location
        }
        Write-Ok "前端已构建"
    }

    Write-Step "准备 Windows 内置 Ollama 引擎"
    Invoke-Python -Python $python -Arguments @(
        "scripts/fetch_ollama_bundle_binary.py",
        "--bundle-platform", $BundlePlatform
    )
    Write-Ok "Windows 内置引擎已准备"

    if (-not $SkipModelPull) {
        Write-Step "拉取本地模型到随包模型目录"
        if (-not (Test-Path $ollamaBinary)) {
            throw "未找到内置 Ollama 引擎: $ollamaBinary"
        }

        New-Item -ItemType Directory -Path $modelsDir -Force | Out-Null
        $env:OLLAMA_MODELS = $modelsDir
        $env:OLLAMA_HOST = $ollamaHost

        $serveProcess = Start-Process -FilePath $ollamaBinary -ArgumentList "serve" -PassThru -WindowStyle Hidden
        Wait-OllamaReady -BaseUrl $ollamaBaseUrl -TimeoutSeconds 90

        & $ollamaBinary pull $Model
        if ($LASTEXITCODE -ne 0) {
            throw "拉取模型失败: $Model"
        }
        Write-Ok "模型已拉取: $Model"
    }

    Write-Step "检查随包资源"
    Invoke-Python -Python $python -Arguments @(
        "scripts/check_local_model_bundle.py",
        "--bundle-platform", $BundlePlatform,
        "--model", $Model
    )
    Write-Ok "随包资源检查通过"

    if (-not $SkipPyInstaller) {
        Write-Step "执行 PyInstaller 打包"
        Invoke-Python -Python $python -Arguments @("-m", "PyInstaller", "build.spec")
        Write-Ok "Windows 打包完成"
    }

    Write-Host ""
    Write-Host "[DONE] Windows 打包链路执行完成" -ForegroundColor Green
    Write-Host "[INFO] 产物目录: dist\Surfaced" -ForegroundColor Gray
} finally {
    Stop-ProcessSafe -Process $serveProcess
}
