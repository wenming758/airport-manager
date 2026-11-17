# -*- mode: python ; coding: utf-8 -*-
# 机场链接管理器 — PyInstaller 打包配置（原版字节码 + 功能叠加）
import os

# 调试用：设置环境变量 WB_CONSOLE=1 可打包出带控制台的版本，便于查看报错
CONSOLE = os.environ.get("WB_CONSOLE", "0") == "1"

HERE = os.path.abspath(os.path.dirname(SPEC))  # noqa: F821
ASSETS = os.path.join(HERE, "assets")
ORIG = os.path.join(HERE, "orig")

block_cipher = None

a = Analysis(
    [os.path.join(HERE, "entry.py")],
    pathex=[HERE],
    binaries=[],
    datas=[
        (os.path.join(ASSETS, "*"), "assets"),
        (os.path.join(ORIG, "*.pyc"), "orig"),
    ],
    hiddenimports=[
        "socks", "requests", "urllib3", "certifi",
        "charset_normalizer", "idna", "cryptography",
        "yaml",   # 解析部分机场直接返回的完整 Clash 配置（YAML）
    ],
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
    name="机场链接管理器" + ("_debug" if CONSOLE else ""),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=CONSOLE,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ASSETS, "app.ico"),
)
