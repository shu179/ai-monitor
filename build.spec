# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.building.datastruct import Tree

block_cipher = None

# 项目根目录
root = Path(SPECDIR).absolute()
sys.path.insert(0, str(root))

from core.version import APP_NAME
from core.local_model_manager import current_bundle_platform

datas = [
    # 包含配置文件
    ('config.yaml', '.'),
    Tree('assets', prefix='assets'),
    Tree('web-ui/dist', prefix='web-ui/dist'),
]

platform_ollama_dir = root / 'third_party' / 'ollama' / current_bundle_platform()
bundled_ollama_dir = root / 'third_party' / 'ollama'
if platform_ollama_dir.exists():
    datas.append(Tree(str(platform_ollama_dir), prefix='third_party/ollama'))
elif bundled_ollama_dir.exists():
    datas.append(Tree(str(bundled_ollama_dir), prefix='third_party/ollama'))

bundled_ollama_models_dir = root / 'third_party' / 'ollama-models'
if bundled_ollama_models_dir.exists():
    datas.append(Tree(str(bundled_ollama_models_dir), prefix='third_party/ollama-models'))

platform_browser_dir = root / 'third_party' / 'browser' / current_bundle_platform()
bundled_browser_dir = root / 'third_party' / 'browser'
if platform_browser_dir.exists():
    datas.append(Tree(str(platform_browser_dir), prefix='third_party/browser'))
elif bundled_browser_dir.exists():
    datas.append(Tree(str(bundled_browser_dir), prefix='third_party/browser'))

a = Analysis(
    ['main.py'],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # Patchright
        'patchright.sync_api',
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
        'core.local_model_manager',
        # UI模块
        'ui.tray',
        'web_backend',
        'web_desktop',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
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
    name=APP_NAME,
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
    name=APP_NAME
)
