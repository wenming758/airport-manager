# -*- coding: utf-8 -*-
"""机场链接管理器 —— 启动入口。

说明：原程序只有编译好的 exe，没有源码。为了 **100% 保留原有界面与风格**，
本入口直接加载从原 exe 中提取出来的字节码模块（orig/*.pyc），
再在其之上叠加新增的功能（见 overlay.py）。
原程序的界面代码一行都没有被改写。
"""
import os
import sys
import types
import marshal

# 让 PyInstaller 收集原程序运行所需的第三方依赖
import requests          # noqa: F401
import socks             # noqa: F401
import urllib3           # noqa: F401
import certifi           # noqa: F401
import charset_normalizer  # noqa: F401
import idna              # noqa: F401
import cryptography      # noqa: F401
import PySide6           # noqa: F401


def base_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS",
                       os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def load_module(name):
    """从 orig/<name>.pyc 载入模块（跳过 16 字节 pyc 头）。"""
    path = os.path.join(base_dir(), "orig", name + ".pyc")
    with open(path, "rb") as f:
        f.read(16)
        code = marshal.load(f)
    mod = types.ModuleType(name)
    mod.__file__ = path
    mod.__package__ = ""
    sys.modules[name] = mod
    exec(code, mod.__dict__)
    return mod


def main():
    cards = load_module("cards")
    store = load_module("store")
    proxy = load_module("proxy")
    checker = load_module("checker")
    main_mod = load_module("main")

    try:
        import overlay
        overlay.install(cards=cards, store=store, proxy=proxy,
                        checker=checker, main=main_mod)
    except Exception:
        import traceback
        traceback.print_exc()

    main_mod.main()


if __name__ == "__main__":
    main()
