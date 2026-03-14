# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

block_cipher = None

# 项目根目录
root = Path(SPECDIR).absolute()

a = Analysis(
    ['main.py'],
    pathex=[str(root)],
    binaries=[],
    datas=[
        # 包含配置文件
        ('config.yaml', '.'),
    ],
    hiddenimports=[
        # Playwright
        'playwright.sync_api',
        'playwright_stealth',
        # 平台模块
        'platforms.base',
        'platforms.doubao',
        'platforms.deepseek',
        'platforms.kimi',
        'platforms.yuanbao',
        'platforms.tongyi',
        'platforms.wenxin',
        # 核心模块
        'core.notifier',
        'core.scheduler',
        'core.config_watcher',
        # UI模块
        'ui.tray',
        # 托盘依赖
        'pystray._win32',
        'pystray._darwin',
        'PIL._tkinter_finder',
        # 其他
        'yaml',
        'requests',
        'PIL',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AI品牌监控',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='app.ico',  # 如果有图标文件可以取消注释
)

# 目录模式（推荐，启动更快）
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AI品牌监控'
)
