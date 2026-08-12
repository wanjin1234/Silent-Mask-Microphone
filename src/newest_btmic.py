#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝牙麦克风（BlueALSA + DeepFilterNet）自动修复版
- 主动请求 HFP profile
- 超时后自动重启 BlueALSA 重试
- 退出时自动恢复系统设置
"""

import glob
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ==================== 配置 ====================
MIC_DEVICE = os.environ.get("MIC_DEVICE", "hw:0,0")
SAMPLE_RATE = 16000
CHANNELS = 1
FORMAT = "S16_LE"

BT_OVERRIDE = "/etc/systemd/system/bluetooth.service.d/override.conf"
BLUEALSA_OVERRIDE = "/etc/systemd/system/bluealsa.service.d/override.conf"
BT_OVERRIDE_BAK = "/tmp/bluetooth.service.override.bak"
BLUEALSA_OVERRIDE_BAK = "/tmp/bluealsa.service.override.bak"

CONNECTION_TIMEOUT = 180
HFP_PROFILE_TIMEOUT = 120
BLUEALSA_WAIT_ATTEMPTS = 3  # 每 30 秒重启 bluealsa，共尝试 3 次

# ==================== 工具函数 ====================


def run(cmd, check=False, timeout=60, capture=True):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=capture, text=True, timeout=timeout
        )
        if check and result.returncode != 0:
            print(f"命令失败: {cmd}")
            if capture:
                print(result.stdout)
                print(result.stderr)
            sys.exit(1)
        return result
    except subprocess.TimeoutExpired:
        if check:
            print(f"命令超时: {cmd}")
            sys.exit(1)
        return None


def print_status(msg):
    print(f"\n=== {msg} ===")


def check_root():
    if os.geteuid() != 0:
        sys.exit("请使用 sudo 运行此脚本")


# ==================== 备份与恢复 ====================


def backup_settings():
    print_status("备份系统配置")
    for src, dst in [
        (BT_OVERRIDE, BT_OVERRIDE_BAK),
        (BLUEALSA_OVERRIDE, BLUEALSA_OVERRIDE_BAK),
    ]:
        if os.path.exists(src):
            shutil.copy2(src, dst)
        else:
            Path(dst).write_text("# NO_FILE\n")

    for svc in ["pulseaudio", "pipewire", "pipewire-pulse", "bluealsa"]:
        state = subprocess.run(
            f"systemctl is-enabled {svc} 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        Path(f"/tmp/{svc}.state").write_text(state if state else "disabled")


def restore_settings():
    print_status("恢复系统设置")

    # 恢复 override 文件
    for bak, target in [
        (BT_OVERRIDE_BAK, BT_OVERRIDE),
        (BLUEALSA_OVERRIDE_BAK, BLUEALSA_OVERRIDE),
    ]:
        if not os.path.exists(bak):
            continue
        content = Path(bak).read_text()
        if content == "# NO_FILE\n":
            if os.path.exists(target):
                os.remove(target)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(bak, target)

    run("systemctl daemon-reload")

    # 恢复服务启用状态
    for svc in ["pulseaudio", "pipewire", "pipewire-pulse", "bluealsa"]:
        state_file = f"/tmp/{svc}.state"
        if os.path.exists(state_file):
            state = Path(state_file).read_text().strip()
            if state == "enabled":
                run(f"systemctl enable {svc} 2>/dev/null || true", check=False)
            else:
                run(f"systemctl disable {svc} 2>/dev/null || true", check=False)

    run("systemctl restart bluetooth", check=False)
    run("systemctl stop bluealsa 2>/dev/null || true", check=False)
    time.sleep(2)

    for f in [
        BT_OVERRIDE_BAK,
        BLUEALSA_OVERRIDE_BAK,
        "/tmp/pulseaudio.state",
        "/tmp/pipewire.state",
        "/tmp/pipewire-pulse.state",
        "/tmp/bluealsa.state",
    ]:
        if os.path.exists(f):
            os.remove(f)
    print("  -> 已恢复原始配置")


# ==================== 系统配置 ====================


def setup_packages():
    print_status("检查依赖")
    run("apt-get update -y --allow-releaseinfo-change", check=False)
    run("apt-get install -y bluez bluez-tools bluez-alsa-utils", check=True)


def disable_audio_servers():
    print_status("禁用 PulseAudio / PipeWire")
    for service in ["pulseaudio", "pipewire", "pipewire-pulse"]:
        run(f"systemctl stop {service} 2>/dev/null || true")
        run(f"systemctl disable {service} 2>/dev/null || true")


def find_bluetoothd():
    for p in ["/usr/libexec/bluetooth/bluetoothd", "/usr/lib/bluetooth/bluetoothd"]:
        if os.path.exists(p):
            return p
    return shutil.which("bluetoothd") or sys.exit("找不到 bluetoothd")


def configure_bluetooth():
    print_status("配置 BlueZ")
    bt_bin = find_bluetoothd()
    new_exec = f"{bt_bin} --noplugin=audio,headset --experimental"

    os.makedirs(os.path.dirname(BT_OVERRIDE), exist_ok=True)
    with open(BT_OVERRIDE, "w") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl daemon-reload")
    run("systemctl restart bluetooth", check=True)
    time.sleep(2)

    result = run("ps aux | grep bluetoothd | grep -v grep")
    if result and "--noplugin=audio,headset" in result.stdout:
        print("  -> BlueZ 已禁用内置 audio/headset")
    else:
        print("  !! 警告：BlueZ 参数未生效")


def configure_bluealsa():
    print_status("配置 BlueALSA")
    ba_path = shutil.which("bluealsa")
    if not ba_path:
        sys.exit("找不到 bluealsa")

    candidates = glob.glob("/sys/class/bluetooth/hci*")
    interface = os.path.basename(candidates[0]) if candidates else "hci0"

    new_exec = f"{ba_path} -i {interface} -p hfp-ag -p hsp-ag"
    print(f"  -> 启动参数: {new_exec}")

    os.makedirs(os.path.dirname(BLUEALSA_OVERRIDE), exist_ok=True)
    with open(BLUEALSA_OVERRIDE, "w") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl reset-failed bluealsa 2>/dev/null || true")
    run("systemctl daemon-reload")
    run("systemctl stop bluealsa 2>/dev/null || true")
    run("systemctl enable bluealsa")

    result = run("systemctl restart bluealsa")
    if result and result.returncode != 0:
        print("  !! BlueALSA 启动失败，日志：")
        run("journalctl -u bluealsa -n 20 --no-pager")
        sys.exit(1)
    time.sleep(2)
    print("  -> BlueALSA 已启动")


# ==================== 蓝牙设备管理 ====================


def get_connected_devices():
    result = run("bluetoothctl devices Connected", check=False)
    devices = []
    if result:
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Device "):
                parts = line.split()
                if len(parts) >= 2:
                    devices.append(parts[1])
    return devices


def wait_for_connection(timeout=CONNECTION_TIMEOUT):
    print_status(f"等待 Windows 连接（最长 {timeout} 秒）")
    print("  请在 Windows 蓝牙设置中连接 'RaspberryPi-Mic'")
    start = time.time()
    while time.time() - start < timeout:
        devices = get_connected_devices()
        if devices:
            print(f"  -> 已连接设备: {devices[0]}")
            return devices[0]
        time.sleep(3)
    print("  !! 错误：未检测到设备")
    return None


def ensure_hfp(device):
    print_status("确保 HFP 模式并激活语音通道")

    info = run(f"bluetoothctl info {device}")
    if info and "Handsfree" in info.stdout:
        print("  -> 设备支持 HFP，主动请求 HFP profile")

        # 通过 D-Bus 强制连接 HFP（UUID 0x111f 是 Handsfree Audio Gateway）
        dev_path = "/org/bluez/hci0/dev_" + device.replace(":", "_")
        uuid_hf = "0000111e-0000-1000-8000-00805f9b34fb"  # Handsfree
        uuid_ag = "0000111f-0000-1000-8000-00805f9b34fb"  # Handsfree Audio Gateway

        # 先请求 AG role（树莓派作为网关）
        result = run(
            f"dbus-send --system --print-reply --dest=org.bluez "
            f"{dev_path} org.bluez.Device1.ConnectProfile string:{uuid_ag}",
            check=False,
            timeout=10,
        )
        if not result or result.returncode != 0:
            # 如果 AG 失败，尝试 HF role（允许 Windows 作为网关？）
            run(
                f"dbus-send --system --print-reply --dest=org.bluez "
                f"{dev_path} org.bluez.Device1.ConnectProfile string:{uuid_hf}",
                check=False,
                timeout=10,
            )

        time.sleep(5)
        info = run(f"bluetoothctl info {device}")
        return True

    print("  ! 设备不支持 HFP，请检查 Windows 连接方式")
    return False


def find_bluealsa_device(device):
    """在 aplay/arecord 输出中查找 BlueALSA 设备"""
    mac_pattern = f"bluealsa:DEV={device},"
    for cmd in ["arecord -L", "aplay -L"]:
        result = run(cmd, check=False)
        if result:
            for line in result.stdout.splitlines():
                if mac_pattern in line:
                    return line.strip()
    return None


def wait_for_bluealsa_device(
    device, timeout=HFP_PROFILE_TIMEOUT, attempts=BLUEALSA_WAIT_ATTEMPTS
):
    print_status(f"等待 SCO 语音通道（最长 {timeout * attempts} 秒）")

    for attempt in range(1, attempts + 1):
        print(f"  -- 尝试 {attempt}/{attempts} --")
        start = time.time()
        found = False

        while time.time() - start < timeout:
            dev = find_bluealsa_device(device)
            if dev:
                print(f"  -> BlueALSA 设备已就绪: {dev}")
                return dev

            # 提示用户操作
            if int(time.time() - start) % 30 == 0:
                print("  -> 请在 Windows 声音控制面板中确认蓝牙麦克风正在使用")
                print("     如果已经启用，请尝试对麦克风说话或调整音量")

            # 设备断开了？
            if device not in get_connected_devices():
                print("  !! 设备已断开")
                return None

            time.sleep(2)

        # 当前尝试超时，重启 BlueALSA 再试
        if attempt < attempts:
            print("  超时，重启 BlueALSA 后重试...")
            run("systemctl restart bluealsa", check=False)
            time.sleep(5)
        else:
            print("  !! 多次尝试仍无 BlueALSA 设备")
            print("  !! 请手动运行以下命令获取诊断信息：")
            print("       journalctl -u bluealsa -f")
            print("       bluetoothctl info " + device)
            return None


# ==================== 音频转发 ====================


def start_audio_forwarding(bluealsa_device):
    print_status("启动音频转发")

    venv_python = "/home/wanjin1234/denoise_mic/venv/bin/python"
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    result = run(f"{venv_python} -c 'import df; print(\"ok\")'")
    use_df = result and result.returncode == 0

    if not use_df:
        print("  -> 未启用 DeepFilterNet，直接转发")
        pipeline = [
            [
                "arecord",
                "-D",
                MIC_DEVICE,
                "-f",
                FORMAT,
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
            ],
            [
                "aplay",
                "-D",
                bluealsa_device,
                "-f",
                FORMAT,
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
            ],
        ]
    else:
        print("  -> 启用 DeepFilterNet 降噪")
        denoise_script = Path("/tmp/df_denoise.py")
        denoise_script.write_text("""
import sys
import numpy as np
import df

model, state, _ = df.init_model(16000, channels=1)
while True:
    data = sys.stdin.buffer.read(1024)
    if not data:
        break
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    processed, state = df.process(model, state, audio)
    out = (processed * 32768.0).astype(np.int16)
    sys.stdout.buffer.write(out.tobytes())
    sys.stdout.flush()
""")
        pipeline = [
            [
                "arecord",
                "-D",
                MIC_DEVICE,
                "-f",
                FORMAT,
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
            ],
            [venv_python, str(denoise_script)],
            [
                "aplay",
                "-D",
                bluealsa_device,
                "-f",
                FORMAT,
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
            ],
        ]

    processes = []
    prev = None
    try:
        for cmd in pipeline:
            if prev is None:
                p = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            else:
                p = subprocess.Popen(
                    cmd,
                    stdin=prev.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                prev.stdout.close()
            processes.append(p)
            prev = p

        print("  -> 音频转发已启动，按 Ctrl+C 停止")
        for p in processes:
            p.wait()
    except KeyboardInterrupt:
        print("\n  -> 正在停止...")
        for p in processes:
            p.terminate()
        raise


# ==================== 主流程 ====================


def main():
    check_root()
    backup_settings()

    try:
        print("=== 蓝牙麦克风（BlueALSA + DeepFilterNet）自动修复版 ===")

        setup_packages()
        disable_audio_servers()

        configure_bluetooth()
        configure_bluealsa()

        print_status("设置蓝牙类别")
        run("hciconfig hci0 class 0x240404 2>/dev/null || true", check=False)
        run("bluetoothctl power on", check=False)
        run("bluetoothctl discoverable on", check=False)
        run("bluetoothctl pairable on", check=False)

        device_mac = wait_for_connection()
        if not device_mac:
            sys.exit("未检测到设备")

        ensure_hfp(device_mac)

        bluealsa_dev = wait_for_bluealsa_device(device_mac)
        if not bluealsa_dev:
            sys.exit("SCO 语音通道未建立")

        start_audio_forwarding(bluealsa_dev)

    except KeyboardInterrupt:
        print("\n  用户中断程序")
    except SystemExit as e:
        print(f"  程序退出: {e}")
    finally:
        restore_settings()


if __name__ == "__main__":
    main()
