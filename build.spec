# -*- mode: python ; coding: utf-8 -*-
# 机场链接管理器 — PyInstaller 打包配置
# 单文件 + 无控制台 + 内嵌 assets 图标资源
# 用法： python -m PyInstaller build.spec --noconfirm --clean
import os

HERE = os.path.abspath(os.getcwd())
if not os.path.isfile(os.path.join(HERE, "src", "main.py")):
    HERE = os.path.dirname(HERE)
SRC = os.path.join(HERE, "src")
ASSETS = os.path.join(SRC, "assets")

block_cipher = None

a = Analysis(
    [os.path.join(SRC, "main.py")],
    pathex=[SRC],
    binaries=[],
    datas=[(os.path.join(ASSETS, "*"), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
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
    name="机场链接管理器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ASSETS, "app.ico"),
)
