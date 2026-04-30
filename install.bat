@echo off
chcp 65001 >/dev/null
title AI Monitor - Install and Start

echo ========================================
echo  AI Monitor
echo ========================================
echo.

:: ---- Find Python (skip Microsoft Store stub) ----
set PYTHON=

for %%C in (py python python3) do (
    if not defined PYTHON (
        for /f "delims=" %%F in ('where %%C 2^>nul') do (
            if not defined PYTHON (
                echo %%F | findstr /i "WindowsApps" >/dev/null
                if errorlevel 1 ( set PYTHON=%%C )
            )
        )
    )
)

if defined PYTHON goto check_version

for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "C:\Program Files\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Program Files\Python310\python.exe"
) do (
    if exist %%P ( set PYTHON=%%P & goto check_version )
)

:: ---- Auto-install Python (Huawei mirror) ----
echo [INFO] Python not found. Downloading Python 3.12 from Huawei mirror...
set PY_INSTALLER=%TEMP%\python_installer.exe
curl -L -o "%PY_INSTALLER%" "https://mirrors.huaweicloud.com/python/3.12.9/python-3.12.9-amd64.exe"
if errorlevel 1 (
    echo [WARN] Huawei mirror failed, trying official source...
    curl -L -o "%PY_INSTALLER%" "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe"
    if errorlevel 1 (
        echo [ERROR] Download failed. Please check your network.
        pause
        exit /b 1
    )
)
echo [INFO] Installing Python 3.12 silently...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1
if errorlevel 1 (
    echo [ERROR] Python install failed. Please install manually from https://www.python.org/downloads/
    pause
    exit /b 1
)
del "%PY_INSTALLER%"
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
set PYTHON=python

:check_version
for /f "tokens=2" %%i in ('%PYTHON% --version 2^>^&1') do set PYVER=%%i
echo [OK] Python %PYVER% (%PYTHON%)

:: ---- Upgrade pip (Tsinghua mirror) ----
echo.
echo [0/3] Upgrading pip...
%PYTHON% -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple -q
echo [OK] pip upgraded

:: ---- Install dependencies (Tsinghua mirror) ----
echo.
echo [1/3] Installing dependencies...
%PYTHON% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [WARN] Bulk install failed, trying one by one...
    %PYTHON% -m pip install patchright pystray Pillow pyyaml watchdog requests win11toast openai markdown -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo [OK] Dependencies installed

:: ---- Install Chromium ----
echo.
echo [2/3] Installing Chromium (first time may take a few minutes)...
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
%PYTHON% -m patchright install chromium
if errorlevel 1 (
    echo [WARN] Mirror failed, retrying with official source...
    set PLAYWRIGHT_DOWNLOAD_HOST=
    %PYTHON% -m patchright install chromium
    if errorlevel 1 (
        echo [ERROR] Chromium install failed. Check your network and retry.
        pause
        exit /b 1
    )
)
echo [OK] Chromium installed

:: ---- Start app ----
echo.
echo [3/3] Starting AI Monitor...
echo.
%PYTHON% main.py

pause
