#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检脚本共用的工具：从 final_btmic.py 里取出网络桥脚本。

树莓派上真正跑的网络桥是 final_btmic.py 里的 NET_BRIDGE_SCRIPT_CONTENT
（运行时写到 /tmp/btmic_net_bridge.py），所以自检也用它当唯一来源——
另存一份副本迟早会和树莓派上跑的东西不一致。
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def load_btmic():
    """导入 final_btmic.py（不会执行 main：它只在 __main__ 下调用）。"""
    spec = importlib.util.spec_from_file_location(
        "btmic_under_test", os.path.join(ROOT, "final_btmic.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 退出时别让 atexit 里的 restore_default 在 Windows 上乱跑（那是树莓派的
    # 蓝牙恢复流程：会去调 systemctl/bluetoothctl）
    mod._RESTORED = True
    return mod


def bridge_path():
    """把网络桥脚本写到临时文件，返回路径（给子进程用）。"""
    mod = load_btmic()
    path = os.path.join(tempfile.gettempdir(), "btmic_net_bridge.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(mod.NET_BRIDGE_SCRIPT_CONTENT)
    return path


def load_bridge_module():
    """把网络桥脚本当模块导入（给测试进程内直接调函数用）。"""
    path = bridge_path()
    spec = importlib.util.spec_from_file_location("net_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gui_module():
    """导入 Windows 端界面模块（只建类，不开窗口）。"""
    win = os.path.join(ROOT, "windows")
    if win not in sys.path:
        sys.path.insert(0, win)
    import importlib.util as iu

    spec = iu.spec_from_file_location(
        "btmic_net_gui", os.path.join(win, "btmic_net_gui.py"))
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
