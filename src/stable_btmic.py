#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Pi as Bluetooth Microphone for Windows
通过蓝牙将ReSpeaker麦克风音频传输到Windows，使Windows将其识别为耳机（麦克风）。
运行前请确保ReSpeaker驱动已安装。
程序退出时会恢复所有修改的系统设置。
"""

import atexit
import os
import shutil
import signal
import subprocess
import sys
import time

# 强制所有pactl客户端连接系统PulseAudio的socket
os.environ["PULSE_SERVER"] = "unix:/run/pulse/native"

# ---------- 全局变量 ----------
BACKUP_DIR = "/tmp/bt_mic_backup"
BLUETOOTH_CONF = "/etc/bluetooth/main.conf"
PULSE_SYSTEM_PA = "/etc/pulse/system.pa"
BT_OVERRIDE = "/etc/systemd/system/bluetooth.service.d/override.conf"


# ---------- 工具函数 ----------
def run(cmd, check=False):
    """执行shell命令，打印输出"""
    print("[执行]", cmd)
    result = subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if result.stdout:
        print(result.stdout)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败: {cmd}\n输出: {result.stdout}")
    return result


def backup_config():
    """备份需要修改的配置文件"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for path in [BLUETOOTH_CONF, PULSE_SYSTEM_PA, BT_OVERRIDE]:
        if os.path.exists(path):
            base = os.path.basename(path) + ".bak"
            shutil.copy2(path, os.path.join(BACKUP_DIR, base))


def restore_config():
    """恢复系统原始配置"""
    print("正在恢复系统配置...")
    src = os.path.join(BACKUP_DIR, os.path.basename(BLUETOOTH_CONF) + ".bak")
    if os.path.exists(src):
        shutil.copy2(src, BLUETOOTH_CONF)
    else:
        run(f"rm -f {BLUETOOTH_CONF}", check=False)

    src = os.path.join(BACKUP_DIR, os.path.basename(PULSE_SYSTEM_PA) + ".bak")
    if os.path.exists(src):
        shutil.copy2(src, PULSE_SYSTEM_PA)
    else:
        run(f"rm -f {PULSE_SYSTEM_PA}", check=False)

    if os.path.exists(BT_OVERRIDE):
        src = os.path.join(BACKUP_DIR, os.path.basename(BT_OVERRIDE) + ".bak")
        if os.path.exists(src):
            shutil.copy2(src, BT_OVERRIDE)
        else:
            run(f"rm -rf {os.path.dirname(BT_OVERRIDE)}", check=False)

    run("pkill -f pulseaudio || true", check=False)
    run("pkill -f bt-agent || true", check=False)
    run("systemctl daemon-reload", check=False)
    run("systemctl restart bluetooth", check=False)
    print("系统配置已恢复。")


# ---------- 安装依赖 ----------
def install_dependencies():
    run("apt-get update -y", check=True)
    run(
        "apt-get install -y bluez bluez-tools pulseaudio pulseaudio-module-bluetooth",
        check=True,
    )


# ---------- 配置蓝牙 ----------
def enable_bluetooth_profiles():
    """
    启用HSP/HFP配置，并设置设备类别为音频/耳机（0x240404）。
    """
    content = ""
    if os.path.exists(BLUETOOTH_CONF):
        with open(BLUETOOTH_CONF, "r") as f:
            content = f.read()

    lines = content.splitlines()
    new_lines = []
    in_general = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_general = stripped == "[General]"
            new_lines.append(line)
            continue
        if in_general and (
            stripped.startswith("Enable=") or stripped.startswith("Class=")
        ):
            continue
        new_lines.append(line)

    idx_general = None
    for i, line in enumerate(new_lines):
        if line.strip() == "[General]":
            idx_general = i
            break
    if idx_general is None:
        new_lines.append("[General]")
        idx_general = len(new_lines) - 1

    new_lines.insert(idx_general + 1, "Enable=Headset,Gateway")
    new_lines.insert(idx_general + 2, "Class = 0x240404")

    with open(BLUETOOTH_CONF, "w") as f:
        f.write("\n".join(new_lines) + "\n")
    print("已修改蓝牙配置：仅启用Headset/Gateway，Class=0x240404 (耳机)")


def get_original_bluetooth_execstart():
    """从 systemd 单元文件中读取原始的 ExecStart 命令行"""
    result = run(
        "systemctl show bluetooth.service -p FragmentPath --value", check=False
    )
    unit_file = result.stdout.strip()
    if not unit_file or not os.path.isfile(unit_file):
        for path in [
            "/lib/systemd/system/bluetooth.service",
            "/etc/systemd/system/bluetooth.service",
            "/usr/lib/systemd/system/bluetooth.service",
        ]:
            if os.path.isfile(path):
                unit_file = path
                break
    if not unit_file:
        raise RuntimeError("找不到 bluetooth.service 单元文件")

    with open(unit_file, "r") as f:
        lines = f.readlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            return stripped[len("ExecStart=") :].strip()
    raise RuntimeError("在 bluetooth.service 中未找到 ExecStart")


def configure_bluetoothd():
    """启用BlueZ实验性功能（HFP必需）"""
    os.makedirs(os.path.dirname(BT_OVERRIDE), exist_ok=True)
    original_exec = get_original_bluetooth_execstart()

    if "--experimental" not in original_exec and "-E" not in original_exec:
        new_exec = original_exec + " --experimental"
    else:
        new_exec = original_exec

    with open(BT_OVERRIDE, "w") as f:
        f.write("[Service]\n")
        f.write("ExecStart=\n")
        f.write(f"ExecStart={new_exec}\n")

    run("systemctl daemon-reload", check=True)
    run("systemctl restart bluetooth", check=True)
    print("BlueZ已配置并重启。")


# ---------- 配置PulseAudio ----------
def configure_pulseaudio():
    """
    配置系统级PulseAudio，确保蓝牙模块正确加载并允许匿名访问。
    """
    if os.path.exists(PULSE_SYSTEM_PA):
        with open(PULSE_SYSTEM_PA, "r") as f:
            content = f.read()
    else:
        content = ""

    lines = content.splitlines() if content else []
    new_lines = []
    found_native = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("load-module module-native-protocol-unix"):
            found_native = True
            if "auth-anonymous" not in stripped:
                line = line + " auth-anonymous=1"
            new_lines.append(line)
        else:
            new_lines.append(line)

    if not found_native:
        new_lines.append("load-module module-native-protocol-unix auth-anonymous=1")

    bluetooth_modules = [l for l in new_lines if "bluetooth" in l]
    if not any("module-bluetooth-policy" in l for l in bluetooth_modules):
        new_lines.append("load-module module-bluetooth-policy")
    if not any("module-bluetooth-discover" in l for l in bluetooth_modules):
        new_lines.append("load-module module-bluetooth-discover")

    with open(PULSE_SYSTEM_PA, "w") as f:
        f.write("\n".join(new_lines) + "\n")

    run("pulseaudio --kill 2>/dev/null || true", check=False)
    time.sleep(1)
    run("rm -f /run/pulse/native /run/pulse/pid", check=False)
    run("pulseaudio --system --daemonize --high-priority --disallow-exit", check=False)

    for _ in range(20):
        if os.path.exists("/run/pulse/native"):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("PulseAudio socket 未创建，请检查配置")

    # 确保蓝牙模块已加载
    result = run("pactl list modules short", check=False)
    if result.returncode == 0:
        if "module-bluetooth-discover" not in result.stdout:
            print("PulseAudio 未加载 module-bluetooth-discover，手动加载...")
            run("pactl load-module module-bluetooth-discover", check=False)
        if "module-bluetooth-policy" not in result.stdout:
            run("pactl load-module module-bluetooth-policy", check=False)
    print("PulseAudio (系统模式) 配置完成。")


# ---------- 设置蓝牙可见和自动配对 ----------
def make_discoverable():
    """启用可发现、可配对"""
    commands = (
        "power on\n"
        "agent on\n"
        "default-agent\n"
        "discoverable on\n"
        "discoverable-timeout 0\n"
        "pairable on\n"
        "pairable-timeout 0\n"
        "quit\n"
    )
    p = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p.communicate(commands, timeout=10)
    run("bluetoothctl show", check=False)
    print("蓝牙已设为可见，等待Windows配对连接。")


# ---------- 音频路由 ----------
def find_mic_source():
    """返回ReSpeaker麦克风对应的ALSA输入源名称"""
    result = run("pactl list short sources", check=False)
    if result.returncode != 0:
        return None

    preferred_keywords = ["seeed2micvoicec", "soc_sound", "aic3x"]
    candidates = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            name = parts[1]
            if "monitor" in name or "bluez_source" in name:
                continue
            candidates.append(name)

    for keyword in preferred_keywords:
        for name in candidates:
            if keyword in name:
                print(f"选择麦克风源: {name}")
                return name

    if candidates:
        print(f"选择麦克风源: {candidates[0]}")
        return candidates[0]
    return None


def find_bt_card():
    """在PulseAudio中查找蓝牙card，返回card名称或None"""
    result = run("pactl list cards", check=False)
    if result.returncode != 0:
        return None

    in_card = False
    current_name = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Card #"):
            in_card = True
            current_name = None
            continue
        if in_card:
            if line == "":
                in_card = False
                continue
            if line.startswith("Name:"):
                current_name = line.split(":", 1)[1].strip()
            elif "device.bus" in line and "bluetooth" in line.lower():
                if current_name:
                    print(f"检测到蓝牙card: {current_name}")
                    return current_name
    return None


def find_bt_sink(card_name):
    """根据card名称查找对应sink"""
    result = run("pactl list sinks", check=False)
    if result.returncode != 0:
        return None

    in_sink = False
    current_sink = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("Sink #"):
            in_sink = True
            current_sink = {}
            continue
        if in_sink:
            if line == "":
                in_sink = False
                continue
            if line.startswith("Name:"):
                current_sink["name"] = line.split(":", 1)[1].strip()
            elif "device.card.name" in line:
                current_sink["card"] = line.split("=", 1)[1].strip().strip('"')
            elif "device.profile.name" in line:
                current_sink["profile"] = line.split("=", 1)[1].strip().strip('"')
            if current_sink.get("card") == card_name and "headset" in current_sink.get(
                "profile", ""
            ):
                return current_sink.get("name")
    return None


def find_connected_bt_mac():
    """返回已连接的蓝牙设备MAC地址"""
    result = run("bluetoothctl devices Connected", check=False)
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    # 格式: Device MAC NAME
    parts = lines[0].split()
    if len(parts) >= 2:
        return parts[1]
    return None


def force_connect_bt(mac):
    """强制建立蓝牙音频连接"""
    print(f"强制连接蓝牙设备 {mac}...")
    p = subprocess.Popen(
        ["bluetoothctl", "connect", mac],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        out, _ = p.communicate(timeout=15)
        print(out)
    except subprocess.TimeoutExpired:
        p.kill()
        print("连接命令超时")
    time.sleep(3)


def attempt_route_audio(card_name):
    """尝试设置HFP profile并建立音频路由"""
    # 先尝试设置 profile
    print(f"尝试设置 card {card_name} 为 HFP profile...")
    for profile in ["headset_head_unit", "handsfree"]:
        cmd = f"pactl set-card-profile {card_name} {profile}"
        result = run(cmd, check=False)
        if result.returncode == 0:
            print(f"profile 设置成功: {profile}")
            time.sleep(2)
            sink_name = find_bt_sink(card_name)
            if sink_name:
                mic_source = find_mic_source()
                if mic_source:
                    print(f"建立音频路由: {mic_source} -> {sink_name}")
                    run(
                        "pactl list short modules | grep module-loopback | awk '{print $1}' | xargs -I{} pactl unload-module {} || true",
                        check=False,
                    )
                    result = run(
                        f"pactl load-module module-loopback source={mic_source} sink={sink_name} latency_msec=20",
                        check=False,
                    )
                    return result.returncode == 0
    return False


# ---------- 主流程 ----------
def main():
    if os.geteuid() != 0:
        print("程序需要root权限，正在尝试通过sudo重新启动...")
        os.execvp("sudo", ["sudo", "python3", sys.argv[0]])

    backup_config()
    atexit.register(restore_config)

    print("===== 开始配置蓝牙麦克风 =====")
    install_dependencies()
    enable_bluetooth_profiles()
    configure_bluetoothd()
    configure_pulseaudio()
    make_discoverable()

    print("\n请现在在Windows电脑上打开蓝牙设置，搜索并连接设备（应该显示为“耳机”）。")
    print("连接成功后，程序将自动配置音频路由。\n")

    loopback_loaded = False
    last_bt_state = False

    try:
        while True:
            # 1. 检查本地麦克风
            mic_source = find_mic_source()
            if not mic_source:
                print("[警告] 未找到本地麦克风源，请检查ReSpeaker是否连接。")
                time.sleep(2)
                continue

            # 2. 检查蓝牙是否已配对连接
            bt_mac = find_connected_bt_mac()
            if bt_mac:
                # 3. 检查PulseAudio中是否存在蓝牙card
                card_name = find_bt_card()
                if card_name:
                    # 蓝牙card存在，尝试建立路由
                    if not loopback_loaded:
                        success = attempt_route_audio(card_name)
                        if success:
                            print(
                                "音频路由已建立，ReSpeaker麦克风现在通过蓝牙发送到Windows。"
                            )
                            loopback_loaded = True
                        else:
                            print("音频路由建立失败，继续尝试...")
                    else:
                        # 检查路由是否仍然有效（可选）
                        pass
                else:
                    # 蓝牙card不存在，说明音频连接未建立，主动尝试
                    print("已检测到蓝牙设备，但PulseAudio未创建card，尝试强制连接...")
                    force_connect_bt(bt_mac)
                    # 等待PulseAudio识别
                    time.sleep(2)
                    card_name = find_bt_card()
                    if card_name:
                        print("蓝牙card已出现！")
                        if not loopback_loaded:
                            success = attempt_route_audio(card_name)
                            if success:
                                loopback_loaded = True
                    else:
                        print(
                            "PulseAudio仍未能识别蓝牙音频设备，可能Windows未切换到耳机模式。"
                        )
            else:
                # 无蓝牙连接
                if loopback_loaded:
                    print("蓝牙连接已断开，停止路由。")
                    run("pactl unload-module module-loopback || true", check=False)
                    loopback_loaded = False

            time.sleep(3)
    except KeyboardInterrupt:
        print("\n程序终止，正在退出...")
    finally:
        if loopback_loaded:
            run("pactl unload-module module-loopback || true", check=False)


def handle_sigterm(signum, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    main()
