#!/usr/bin/env python3
"""
ReSpeaker 蓝牙麦克风 — 一键成功版
==================================
严格遵循已验证的手动步骤：
① 停用 ofono
② 重启 PipeWire 和蓝牙服务
③ 清除旧配对（可选）
④ 设置蓝牙名称、开启可发现
⑤ 启动 bt-agent 代理
⑥ 等待 Windows 连接
⑦ 通过 pw-link 建立音频桥接

用法：
  sudo python3 bt_mic_success.py [--name "我的麦克风"] [--reset]
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time

DEFAULT_NAME = "RaspberryPi-Mic"

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def run(cmd, shell=True, check=False, capture=True, timeout=None):
    try:
        return subprocess.run(cmd, shell=shell, check=check,
                              capture_output=capture, text=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        log(f"  ⚠️ 命令返回非零: {cmd}\n     {e.stderr.strip() if e.stderr else ''}")
        return None
    except Exception as e:
        log(f"  ⚠️ 异常: {e}")
        return None

def is_root():
    return os.geteuid() == 0

# ---------- 环境准备 ----------
def prepare_environment():
    """执行手动操作的前三步"""
    log("1. 停止并禁用 oFono...")
    run("systemctl stop ofono 2>/dev/null", shell=True)
    run("systemctl disable ofono 2>/dev/null", shell=True)
    run("systemctl mask ofono 2>/dev/null", shell=True)

    log("2. 重启 PipeWire 用户服务...")
    user = os.environ.get('SUDO_USER', 'pi')
    run(f"sudo -u {user} systemctl --user restart pipewire pipewire-pulse wireplumber", shell=True)
    time.sleep(2)

    log("3. 重启蓝牙服务...")
    run("systemctl restart bluetooth", shell=True)
    time.sleep(3)

    show = run("bluetoothctl show", shell=True)
    if not show or "No default controller" in (show.stdout or ""):
        log("❌ 蓝牙适配器未就绪")
        return False
    log("✅ 蓝牙适配器就绪")
    return True

# ---------- 清除配对 ----------
def remove_all_devices():
    """移除所有已配对设备（相当于你执行的 bluetoothctl remove ...）"""
    log("移除所有已配对设备...")
    devs = run("bluetoothctl devices", shell=True)
    if devs and devs.stdout.strip():
        for line in devs.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device":
                mac = parts[1]
                run(f"bluetoothctl remove {mac}", shell=True)
    else:
        log("   没有已配对设备")

# ---------- 蓝牙初始化 ----------
def init_bluetooth(name, do_clear=False):
    if do_clear:
        remove_all_devices()

    log("4. 开启蓝牙并设置名称...")
    run("bluetoothctl power on", shell=True)
    # 设置蓝牙别名（广播名称）
    run(f"bluetoothctl system-alias '{name}'", shell=True)

    # 同时修改 main.conf 中的 Name，防止重启后名称丢失
    conf = "/etc/bluetooth/main.conf"
    if os.path.exists(conf):
        with open(conf, 'r') as f:
            content = f.read()
        if re.search(r'^Name\s*=', content, re.MULTILINE):
            content = re.sub(r'^Name\s*=.*', f'Name = {name}', content, flags=re.MULTILINE)
        else:
            content += f"\nName = {name}\n"
        with open(conf, 'w') as f:
            f.write(content)

    run("bluetoothctl discoverable on", shell=True)
    run("bluetoothctl pairable on", shell=True)

    log("5. 启动配对代理 bt-agent...")
    run("killall bt-agent 2>/dev/null", shell=True)
    time.sleep(0.5)
    agent = subprocess.Popen(["bt-agent", "-c", "NoInputNoOutput"])
    log(f"   bt-agent PID: {agent.pid}")

    time.sleep(1)
    check = run("pgrep -a bt-agent", shell=True)
    if check and check.stdout.strip():
        log("✅ bt-agent 运行中")
    else:
        log("⚠️ bt-agent 可能未启动")

    show = run("bluetoothctl show | grep -E 'Name|Alias'", shell=True)
    if show:
        log(f"当前蓝牙信息:\n{show.stdout.strip()}")

    log(f"\n蓝牙已就绪，名称: {name}")
    log("请在 Windows 上搜索并连接（若之前配对过，请先在 Windows 上删除旧设备）。")
    return agent

# ---------- 音频桥接 ----------
def find_node(pattern):
    res = run("pw-cli ls Node", shell=True, timeout=5)
    if not res: return None
    for line in res.stdout.splitlines():
        if pattern in line:
            m = re.search(r'"([^"]+)"', line)
            if m: return m.group(1)
    return None

def establish_bridge():
    log("等待 bluez_output 节点...")
    bt_node = None
    for _ in range(20):
        bt_node = find_node("bluez_output")
        if bt_node: break
        time.sleep(1)
    if not bt_node:
        log("❌ 未找到 bluez_output 节点（请确认 Windows 连接时启用了“免提耳机”服务）")
        return False

    # 查找 reSpeaker 输入节点
    resp_node = None
    res = run("pw-cli ls Node", shell=True, timeout=5)
    if res:
        for line in res.stdout.splitlines():
            if re.search(r"seeed.*input|alsa_input.*seeed|wm8960", line, re.I):
                m = re.search(r'"([^"]+)"', line)
                if m: resp_node = m.group(1); break
    if not resp_node:
        resp_node = find_node("alsa_input")
    if not resp_node:
        log("❌ 未找到 reSpeaker 录音节点")
        return False

    cap = f"{resp_node}:capture_1"
    play = f"{bt_node}:playback_1"
    log(f"连接: {cap} → {play}")
    r = run(f"pw-link {cap} {play}", shell=True, check=False)
    if r and r.returncode == 0:
        log("✅ 音频桥接成功！Windows 现在可以收到麦克风声音。")
        return True

    # 自动匹配备用方案
    ports = run("pw-link -l", shell=True, timeout=5)
    if ports:
        c = p = None
        for line in ports.stdout.splitlines():
            if resp_node in line and "capture" in line: c = line.split()[0]
            if bt_node in line and "playback" in line: p = line.split()[0]
        if c and p:
            log(f"自动匹配: {c} → {p}")
            r = run(f"pw-link {c} {p}", shell=True, check=False)
            if r and r.returncode == 0:
                log("✅ 桥接成功！")
                return True
    log("❌ 桥接失败")
    return False

# ---------- 主循环 ----------
def main_loop():
    last_addr = None
    log("\n等待 Windows 蓝牙连接... (Ctrl+C 退出)")
    while True:
        res = run("bluetoothctl devices Connected", shell=True)
        cur_addr = None
        if res and res.stdout.strip():
            for line in res.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "Device":
                    cur_addr = parts[1]
                    break
        if not cur_addr:
            if last_addr:
                log(f"设备断开: {last_addr}")
                last_addr = None
            time.sleep(3)
            continue
        if cur_addr != last_addr:
            log(f"设备已连接: {cur_addr}")
            last_addr = cur_addr
            time.sleep(3)
            establish_bridge()
        else:
            time.sleep(3)

# ---------- 主入口 ----------
def main():
    parser = argparse.ArgumentParser(description="ReSpeaker 蓝牙麦克风一键成功版")
    parser.add_argument("--name", default=DEFAULT_NAME, help="蓝牙名称")
    parser.add_argument("--reset", action="store_true", help="清除所有已配对设备（推荐首次使用）")
    args = parser.parse_args()

    if not is_root():
        print("请使用 sudo 运行。")
        sys.exit(1)

    if not prepare_environment():
        sys.exit(1)

    agent = init_bluetooth(args.name, do_clear=args.reset)

    def cleanup(sig=None, frame=None):
        log("退出...")
        agent.terminate()
        agent.wait()
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    main_loop()

if __name__ == "__main__":
    main()