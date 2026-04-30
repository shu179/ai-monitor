#!/bin/bash
echo "========================================"
echo " Surfaced"
echo "========================================"
echo

# 检查 Python
if ! command -v python3 &>/dev/null; then
    echo "[错误] 未检测到 Python3"
    echo
    echo "请先安装 Python 3.10 或以上版本："
    echo "https://www.python.org/downloads/"
    echo
    read -p "按回车退出..."
    exit 1
fi

PYVER=$(python3 --version 2>&1)
echo "[OK] $PYVER"

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 安装依赖
echo
echo "[1/3] 安装依赖包..."
python3 -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple -q
if [ $? -ne 0 ]; then
    echo "[WARN] 批量安装失败，逐个安装..."
    python3 -m pip install patchright pystray Pillow pyyaml watchdog requests openai markdown -i https://pypi.tuna.tsinghua.edu.cn/simple
    if [ $? -ne 0 ]; then
        echo "[错误] 依赖安装失败，请检查网络连接"
        read -p "按回车退出..."
        exit 1
    fi
fi
echo "[OK] 依赖安装完成"

# 安装 Chromium
echo
echo "[2/3] 安装 Chromium 浏览器（首次约需几分钟）..."
export PATCHRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
python3 -m patchright install chromium
if [ $? -ne 0 ]; then
    echo "[WARN] 镜像下载失败，尝试官方源..."
    unset PATCHRIGHT_DOWNLOAD_HOST
    python3 -m patchright install chromium
    if [ $? -ne 0 ]; then
        echo "[错误] Chromium 安装失败"
        read -p "按回车退出..."
        exit 1
    fi
fi
echo "[OK] Chromium 安装完成"

# 启动程序
echo
echo "[3/3] 启动 Surfaced..."
echo
python3 main.py
