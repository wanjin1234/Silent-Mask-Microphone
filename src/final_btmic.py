#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Pi 作为蓝牙麦克风（BlueALSA + DeepFilterNet）— 完成版

- 适配 Seeed Studio reSpeaker 2-Mics HAT v2：plughw 自动转换声道
- 不再主动连接 Windows，改为等待 Windows 发起 HFP/HSP 连接
- 修掉 HFP UUID 冲突（停止 pipewire/pulseaudio/ModemManager 等）
- 只等待 BlueALSA 真正发布的 SCO PCM，不再盲目测试不存在的设备
- 退出时自动断开蓝牙连接并恢复系统设置
"""

import atexit
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# 可被环境变量覆盖
MIC_DEVICE = os.environ.get("MIC_DEVICE", "")  # 空 = 自动探测
SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", "16000"))
CHANNELS = int(os.environ.get("CHANNELS", "1"))
FORMAT = "S16_LE"
CONNECTION_TIMEOUT = int(os.environ.get("CONNECTION_TIMEOUT", "180"))
MAX_RESTARTS = int(os.environ.get("MAX_RESTARTS", "5"))

BACKUP_DIR = "/tmp/bt_mic_backup"
BT_OVERRIDE = "/etc/systemd/system/bluetooth.service.d/override.conf"
BLUEALSA_OVERRIDE = "/etc/systemd/system/bluealsa.service.d/override.conf"
BLUETOOTH_CONF = "/etc/bluetooth/main.conf"
DENOISE_SCRIPT = Path("/tmp/df_denoise.py")
DOWNMIX_SCRIPT = Path("/tmp/df_downmix.py")


def run(cmd, check=False, timeout=60, verbose=True):
    if verbose:
        print(f"$ {cmd}")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            raise RuntimeError(f"命令失败(returncode={result.returncode}): {cmd}")
        return result
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"命令超时: {cmd}")
        return None


def print_status(msg):
    print(f"\n=== {msg} ===")


def ensure_root():
    if os.geteuid() != 0:
        print("当前不是 root，尝试自动用 sudo 重新启动...")
        os.execvp("sudo", ["sudo", "python3", *sys.argv])


# ---------------- 系统状态备份 / 恢复 ----------------


def backup_system_state():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for path in [BLUETOOTH_CONF, BT_OVERRIDE, BLUEALSA_OVERRIDE]:
        if os.path.exists(path):
            dst = os.path.join(BACKUP_DIR, os.path.basename(path) + ".bak")
            shutil.copy2(path, dst)


_RESTORED = False


def disconnect_bluetooth_devices():
    """退出时断开所有已连接的蓝牙设备，避免音频通道残留。"""
    result = run(
        "bluetoothctl devices Connected", check=False, timeout=8, verbose=False
    )
    if not result or result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            mac = parts[1]
            print(f"  -> 断开蓝牙设备 {mac}")
            run(f"bluetoothctl disconnect {mac}", check=False, timeout=8, verbose=False)


def restore_default():
    global _RESTORED
    if _RESTORED:
        return
    _RESTORED = True
    print_status("恢复系统默认设置并断开蓝牙")

    disconnect_bluetooth_devices()

    for path in [BT_OVERRIDE, BLUEALSA_OVERRIDE]:
        if os.path.exists(path):
            os.remove(path)

    for d in [
        "/etc/systemd/system/bluetooth.service.d",
        "/etc/systemd/system/bluealsa.service.d",
    ]:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)

    main_conf_bak = os.path.join(BACKUP_DIR, "main.conf.bak")
    if os.path.exists(main_conf_bak):
        shutil.copy2(main_conf_bak, BLUETOOTH_CONF)

    for service in ["pulseaudio", "pipewire", "pipewire-pulse", "ofono"]:
        run(
            f"systemctl enable {service} 2>/dev/null || true",
            check=False,
            verbose=False,
        )
        run(
            f"systemctl start {service} 2>/dev/null || true", check=False, verbose=False
        )

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl restart bluetooth 2>/dev/null || true", check=False, verbose=False)
    run("pkill -f 'arecord.*aplay' || true", check=False, verbose=False)
    run("pkill -f deepfilter_denoise || true", check=False, verbose=False)

    if os.path.exists("/tmp/asound.conf.removed"):
        shutil.move("/tmp/asound.conf.removed", "/etc/asound.conf")


atexit.register(restore_default)


# ---------------- 依赖 / 服务清理 ----------------


def setup_packages():
    print_status("检查并安装依赖")
    run("apt-get update -y --allow-releaseinfo-change", check=False, verbose=False)
    run(
        "apt-get install -y bluez bluez-tools bluez-alsa bluez-alsa-utils bluetooth "
        "libasound2-dev libasound2-plugins",
        check=False,
        verbose=False,
    )


def disable_audio_servers():
    print_status("禁用 PulseAudio / PipeWire / oFono / ModemManager / hsphfpd")

    # 系统服务
    for service in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "ofono",
        "hsphfpd",
        "ModemManager",
    ]:
        run(f"systemctl stop {service} 2>/dev/null || true", check=False, verbose=False)
        run(
            f"systemctl disable {service} 2>/dev/null || true",
            check=False,
            verbose=False,
        )

    # 用户会话服务（例如 wanjin1234 的 pipewire）
    run(
        "systemctl --user stop pipewire.socket pipewire-pulse.socket "
        "pipewire pipewire-pulse wireplumber pulseaudio.socket pulseaudio "
        "2>/dev/null || true",
        check=False,
        verbose=False,
    )

    # 进程级清理（最关键）
    for proc in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "hsphfpd",
        "ofono",
        "ModemManager",
        "modemmanager",
    ]:
        run(f"pkill -x {proc} 2>/dev/null || true", check=False, verbose=False)

    time.sleep(1)
    leftover = run(
        "ps -ef | grep -iE 'pipewire|pulseaudio|ofono|ModemManager|hsphfpd' | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if leftover and (leftover.stdout or "").strip():
        print("  !! 仍有冲突进程残留，请手动检查：")
        print((leftover.stdout or "").strip())
    else:
        print("  -> 未发现残留的音频/电话服务进程")


# ---------------- BlueZ / BlueALSA ----------------


def find_bluetoothd():
    for p in [
        "/usr/libexec/bluetooth/bluetoothd",
        "/usr/lib/bluetooth/bluetoothd",
        "/usr/local/libexec/bluetooth/bluetoothd",
    ]:
        if os.path.exists(p):
            return p
    found = shutil.which("bluetoothd")
    if found:
        return found
    raise FileNotFoundError("找不到 bluetoothd")


def get_hci_name():
    candidates = sorted(glob.glob("/sys/class/bluetooth/hci*"))
    if candidates:
        return os.path.basename(candidates[0])
    for name in ["hci0", "hci1"]:
        if os.path.exists(f"/sys/class/bluetooth/{name}"):
            return name
    return "hci0"


def has_bluealsa_dbus_name():
    result = run(
        "dbus-send --system --print-reply --dest=org.freedesktop.DBus "
        "/org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner string:org.bluealsa",
        check=False,
        timeout=8,
        verbose=False,
    )
    if not result:
        return False
    return "boolean true" in (result.stdout or "")


def ensure_bluealsa_dbus(hci):
    if has_bluealsa_dbus_name():
        return True
    print("  !! 未检测到 org.bluealsa，尝试重启 bluealsa 服务")
    run("systemctl restart bluealsa", check=False, verbose=False)
    time.sleep(2)
    if has_bluealsa_dbus_name():
        print("  -> org.bluealsa 已恢复")
        return True
    ba_path = shutil.which("bluealsa")
    if not ba_path:
        return False
    print("  !! 继续尝试前台参数直启 bluealsa 守护进程")
    run("pkill -x bluealsa 2>/dev/null || true", check=False, verbose=False)
    run(
        f"{ba_path} --dbus=org.bluealsa -i {hci} -p hsp-hs -p hfp-hf "
        ">/tmp/bluealsa.log 2>&1 &",
        check=False,
        verbose=False,
    )
    time.sleep(2)
    return has_bluealsa_dbus_name()


def diagnose_uuid_conflicts():
    print("  -- 可能占用 HFP/HSP UUID 的进程 --")
    result = run(
        "ps -ef | grep -iE 'pipewire|pulseaudio|ofono|ModemManager|hsphfpd|bluealsa|bluetoothd' "
        "| grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if result and (result.stdout or "").strip():
        print((result.stdout or "").strip())
    else:
        print("    （未发现相关进程）")

    result = run(
        "ps -ef | grep bluetoothd | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if result and (result.stdout or "").strip():
        if "--noplugin=audio,headset" in (result.stdout or ""):
            print("  -> bluetoothd 已禁用内置 audio/headset 插件")
        else:
            print(
                "  !! bluetoothd 未带 --noplugin=audio,headset，请检查 override.conf 是否生效"
            )

    names = run(
        "dbus-send --system --print-reply --dest=org.freedesktop.DBus "
        "/org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>/dev/null "
        "| grep -iE 'blue|ofono|modem|pulse|pipewire|phone'",
        check=False,
        timeout=8,
        verbose=False,
    )
    if names and (names.stdout or "").strip():
        print("  -- D-Bus 上相关服务名 --")
        print((names.stdout or "").strip())


def resolve_uuid_conflict(hci):
    print_status("尝试解决 UUID 冲突: 停止冲突服务并重启 bluetooth/bluealsa")
    run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl stop bluetooth 2>/dev/null || true", check=False, verbose=False)

    for svc in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "ofono",
        "hsphfpd",
        "ModemManager",
    ]:
        run(f"systemctl stop {svc} 2>/dev/null || true", check=False, verbose=False)
        run(
            f"systemctl --user stop {svc} 2>/dev/null || true",
            check=False,
            verbose=False,
        )

    for proc in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "hsphfpd",
        "ofono",
        "ModemManager",
        "modemmanager",
    ]:
        run(f"pkill -x {proc} 2>/dev/null || true", check=False, verbose=False)

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl start bluetooth", check=False, verbose=False)
    time.sleep(2)

    check = run(
        "ps -ef | grep bluetoothd | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if not (check and "--noplugin=audio,headset" in (check.stdout or "")):
        print("  !! 注意：bluetoothd 未带 --noplugin=audio,headset，可能无法释放 UUID")

    run("systemctl restart bluealsa", check=False, verbose=False)
    time.sleep(3)

    journal = run(
        "journalctl -u bluealsa -n 120 --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    if (
        journal
        and "Couldn't register hands-free profile" not in (journal.stdout or "")
        and "UUID already registered" not in (journal.stdout or "")
    ):
        print("  -> UUID 冲突已清除，bluealsa 可注册 HFP/HSP")
        return True

    print("  !! UUID 冲突仍然存在，可能占用 UUID 的进程/服务如下：")
    diagnose_uuid_conflicts()
    if journal and (journal.stdout or "").strip():
        print("  -- bluealsa 最近日志 --")
        print((journal.stdout or "").strip())
    return False


def configure_bluetoothd():
    print_status("配置 BlueZ，禁用内置 audio/headset 插件")
    bt_bin = find_bluetoothd()
    new_exec = f"{bt_bin} --experimental --noplugin=audio,headset"
    os.makedirs(os.path.dirname(BT_OVERRIDE), exist_ok=True)
    with open(BT_OVERRIDE, "w", encoding="utf-8") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl restart bluetooth", check=False, verbose=False)
    time.sleep(2)

    check = run(
        "ps -ef | grep bluetoothd | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if check and "--noplugin=audio,headset" in (check.stdout or ""):
        print("  -> bluetoothd 已禁用 audio/headset 插件")
    else:
        print("  !! 警告：未检测到 --noplugin=audio,headset，可能仍存在插件冲突")


def configure_bluealsa():
    print_status("配置 BlueALSA")
    ba_path = shutil.which("bluealsa")
    if not ba_path:
        raise FileNotFoundError("找不到 bluealsa，请确认已安装 bluez-alsa")

    hci = get_hci_name()
    new_exec = f"{ba_path} -i {hci} -p hsp-hs -p hfp-hf"
    print(f"  -> 使用参数: {new_exec}")
    os.makedirs(os.path.dirname(BLUEALSA_OVERRIDE), exist_ok=True)
    with open(BLUEALSA_OVERRIDE, "w", encoding="utf-8") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl enable bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl start bluealsa", check=False, verbose=False)
    time.sleep(2)

    result = run("systemctl is-active bluealsa", check=False, verbose=False)
    if result and result.stdout.strip() == "active":
        print("  -> BlueALSA 服务状态: active")
    else:
        print("  !! BlueALSA 服务未 active，尝试直接启动蓝牙 HF/HS 模式")
        run(
            f"{ba_path} -i {hci} -p hsp-hs -p hfp-hf >/tmp/bluealsa.log 2>&1 &",
            check=False,
            verbose=False,
        )
        time.sleep(2)

    if ensure_bluealsa_dbus(hci):
        print("  -> BlueALSA D-Bus 就绪（org.bluealsa）")
    else:
        print(
            "  !! BlueALSA D-Bus 仍未就绪，请查看 /tmp/bluealsa.log 与 systemctl status bluealsa"
        )

    journal = run(
        "journalctl -u bluealsa -n 120 --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    if journal and "UUID already registered" in (journal.stdout or ""):
        # 区分 A2DP UUID 冲突和 HFP/HSP UUID 冲突
        if "Couldn't register hands-free profile" in (journal.stdout or ""):
            print("  !! 检测到 HFP/HSP UUID 冲突，尝试清理...")
            resolve_uuid_conflict(hci)
        else:
            # 只有 A2DP UUID 冲突，不影响 HFP/HSP 麦克风功能
            print("  -> 仅 A2DP UUID 冲突，不影响 HFP/HSP 麦克风通道，可继续")
    else:
        print("  -> 未检测到 HFP/HSP 注册冲突")


def prepare_bt_state():
    print_status("设置蓝牙可见性与电源")
    run("bluetoothctl power on", check=False, verbose=False)
    run("bluetoothctl agent NoInputNoOutput", check=False, verbose=False)
    run("bluetoothctl default-agent", check=False, verbose=False)
    run("bluetoothctl pairable on", check=False, verbose=False)
    run("bluetoothctl pairable-timeout 0", check=False, verbose=False)
    run("bluetoothctl discoverable on", check=False, verbose=False)
    run("bluetoothctl discoverable-timeout 0", check=False, verbose=False)
    hci = get_hci_name()
    run(
        f"hciconfig {hci} class 0x240404 2>/dev/null || true",
        check=False,
        verbose=False,
    )
    time.sleep(2)


# ---------------- 等待 Windows 主动连接 ----------------


def wait_for_connection(timeout=CONNECTION_TIMEOUT):
    print_status(f"等待 Windows 主动连接（最长 {timeout} 秒）")
    print("  请在 Windows 蓝牙设置中配对并连接树莓派")
    print("  连接后，请到 Windows『声音设置 → 输入』中选择 Hands-Free/Headset 设备，")
    print("  此时 BlueALSA 才会建立 HFP/SCO 麦克风通道。")
    start = time.time()
    while time.time() - start < timeout:
        result = run("bluetoothctl devices Connected", check=False, verbose=False)
        devices = []
        if result:
            for line in result.stdout.splitlines():
                if line.startswith("Device "):
                    parts = line.split()
                    if len(parts) >= 2:
                        devices.append(parts[1])
        if devices:
            print(f"  -> 已检测到设备连接: {devices[0]}")
            return devices[0]
        time.sleep(3)
    return None


def get_device_info(device):
    run(f"bluetoothctl info {device}", check=False, verbose=False)


def trust_device(device):
    run(f"bluetoothctl trust {device}", check=False, verbose=False)


# ---------------- BlueALSA PCM 发现 ----------------


def get_bluealsa_pcm_candidates(device):
    """
    只收集 BlueALSA 自己报告的真实 PCM 名。
    当 HFP/HSP SCO 通道存在时，bluealsa-aplay 或 bluealsa-cli
    会给出形如 bluealsa:DEV=...,PROFILE=sco 的设备。
    """
    candidates = []
    mac_variants = [
        device.lower(),
        device.upper(),
        device.lower().replace(":", ""),
        device.upper().replace(":", ""),
    ]

    if shutil.which("bluealsa-aplay"):
        res = run("bluealsa-aplay -L", check=False, timeout=8, verbose=False)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                s = line.strip()
                if s.lower().startswith("bluealsa:"):
                    if any(m in s.lower() for m in mac_variants):
                        candidates.append(s)

    if shutil.which("bluealsa-cli"):
        res = run("bluealsa-cli list-pcms", check=False, timeout=8, verbose=False)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                s = line.strip()
                if s.lower().startswith("bluealsa:"):
                    if any(m in s.lower() for m in mac_variants):
                        candidates.append(s)

    # 去重，保持顺序
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def test_pcm(pcm):
    cmd = (
        f"timeout 2 aplay -D '{pcm}' -f {FORMAT} -r {SAMPLE_RATE} "
        f"-c {CHANNELS} /dev/zero 2>&1"
    )
    result = run(cmd, check=False, timeout=5, verbose=False)
    if result is None:
        return False
    if result.returncode in (0, 124):
        return True
    return False


def find_working_pcm(device, timeout=90):
    print_status("等待 BlueALSA SCO PCM 出现")
    start = time.time()
    print_count = 0
    while time.time() - start < timeout:
        candidates = get_bluealsa_pcm_candidates(device)
        if not candidates:
            print_count += 1
            if print_count % 5 == 0:
                print(
                    "  尚未发现 HFP/SCO PCM，请确认 Windows 已选择 Hands-Free/Headset 输入设备..."
                )
            time.sleep(3)
            continue

        for pcm in candidates:
            print(f"  测试: {pcm}")
            if test_pcm(pcm):
                print(f"  ** 找到可用 PCM: {pcm}")
                return pcm
        time.sleep(3)

    # 额外诊断
    print("  !! 未找到可用 PCM，尝试强制建立 HFP 连接（仅作为备用）")
    # 这里不主动连接整机，而是请求已有链路上的 HFP profile，
    # 能帮部分 Windows 版本从 A2DP 切到 Hands-Free。
    dev_path = f"/org/bluez/{get_hci_name()}/dev_{device.replace(':', '_').upper()}"
    run(
        f"dbus-send --system --print-reply --dest=org.bluez {dev_path} "
        "org.bluez.Device1.ConnectProfile string:0000111f-0000-1000-8000-00805f9b34fb",
        check=False,
        timeout=10,
        verbose=False,
    )
    time.sleep(5)

    candidates = get_bluealsa_pcm_candidates(device)
    for pcm in candidates:
        if test_pcm(pcm):
            return pcm

    print("  !! 仍未找到 PCM。请尝试：")
    print("     1. 在 Windows 蓝牙设置中删除该设备，重新配对；")
    print("     2. 配对后点开『声音设置』，把输入设备选为 Hands-Free/Headset；")
    print("     3. 打开任意录音软件，让 Windows 真正启用麦克风通道；")
    print("     4. 检查 Windows 是否把树莓派识别为“耳机”而不是“音箱”。")
    return None


# ---------------- 麦克风自动探测 ----------------


def probe_input_devices():
    devices = []
    result = run("arecord -l", check=False, timeout=8, verbose=False)
    if not result or result.returncode != 0:
        return devices

    for line in result.stdout.splitlines():
        line = line.strip()
        m_card = re.match(r"^card\s+(\d+):\s*(.*)$", line)
        if not m_card:
            continue
        try:
            card = int(m_card.group(1))
        except ValueError:
            continue
        rest = m_card.group(2)
        m_dev = re.search(r",\s*device\s+(\d+):\s*(.*?)(?:\s*\[[^\]]*\])?\s*$", rest)
        if not m_dev:
            continue
        try:
            dev = int(m_dev.group(1))
        except ValueError:
            continue
        card_part = rest[: m_dev.start()].strip()
        card_name = re.sub(r"\s*\[.*?\]\s*$", "", card_part).strip()
        dev_name = m_dev.group(2).strip()
        label = f"{card_name} / {dev_name}".strip(" /")
        devices.append((card, dev, label))
    return devices


def verify_mic(dev, channels=CHANNELS):
    print(f"  测试录音设备 {dev}（{channels} 通道）...")
    cmd = (
        f"arecord -t raw -D '{dev}' -f {FORMAT} -r {SAMPLE_RATE} "
        f"-c {channels} -d 1 /dev/null 2>&1"
    )
    result = run(cmd, check=False, timeout=10, verbose=False)
    if result and result.returncode == 0:
        print(f"  -> 麦克风 {dev} 可用（{channels} 通道）")
        return True
    if result:
        err = (result.stdout or "").strip().splitlines()
        tail = err[-1] if err else "未知错误"
        print(f"  !! {dev}（{channels} 通道）不可用: {tail}")
    else:
        print(f"  !! {dev} 测试超时")
    return False


def select_and_verify_mic():
    print_status("探测麦克风输入设备")
    devices = probe_input_devices()
    candidates = []

    env_dev = os.environ.get("MIC_DEVICE")
    if env_dev:
        candidates.append((env_dev, CHANNELS))
        print(f"  -> 使用环境变量 MIC_DEVICE={env_dev}")

    usb = [d for d in devices if "usb" in d[2].lower() or "microphone" in d[2].lower()]
    other = [d for d in devices if d not in usb]
    for card, dev, name in usb + other:
        d = f"hw:{card},{dev}"
        print(f"  -> 发现录音设备: {d} ({name})")
        candidates.append((f"plughw:{card},{dev}", 1))
        candidates.append((d, 1))
        candidates.append((f"plughw:{card},{dev}", 2))
        candidates.append((d, 2))

    candidates.append(("plughw:0,0", 1))
    candidates.append(("hw:0,0", 1))

    seen = set()
    for dev, ch in candidates:
        key = (dev, ch)
        if key in seen:
            continue
        seen.add(key)
        if verify_mic(dev, ch):
            return dev, ch
    return None, None


# ---------------- DeepFilterNet 降噪 ----------------

DENOISE_SCRIPT_CONTENT = r"""
import sys
import numpy as np
import df

SR = __SR__
CHANNELS = __CHANNELS__   # 采集声道数：1 或 2


def main():
    print("[denoise] loading DeepFilterNet ...", file=sys.stderr, flush=True)
    try:
        model, df_state, _ = df.init_model(sr=SR, channels=1)
    except Exception as exc:
        print("[denoise] init_model failed: %s" % exc, file=sys.stderr, flush=True)
        return 2

    if "--preload-only" in sys.argv:
        _ = int(df_state.block_len)
        print("[denoise] preload ok", file=sys.stderr, flush=True)
        return 0

    try:
        block_len = int(df_state.block_len)
    except Exception:
        block_len = 160
    block_bytes = block_len * CHANNELS * 2
    print("[denoise] model loaded, block_len=%d, capture_channels=%d"
          % (block_len, CHANNELS), file=sys.stderr, flush=True)

    buf = b""
    while True:
        raw = sys.stdin.buffer.read(65536)
        if not raw:
            break
        buf += raw
        complete = len(buf) // block_bytes
        if complete == 0:
            continue
        n_bytes = complete * block_bytes
        chunk = buf[:n_bytes]
        buf = buf[n_bytes:]

        raw_int = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if CHANNELS == 2:
            audio = raw_int.reshape(-1, 2).mean(axis=1)
        else:
            audio = raw_int
        audio = audio.reshape(1, -1)

        try:
            processed, df_state = df.process(model, df_state, audio)
        except Exception as exc:
            print("[denoise] process failed: %s" % exc, file=sys.stderr, flush=True)
            return 3

        out = np.asarray(processed).reshape(-1)
        in_len = audio.shape[-1]
        if out.shape[0] < in_len:
            out = np.pad(out, (0, in_len - out.shape[0]))
        elif out.shape[0] > in_len:
            out = out[:in_len]
        out = (out * 32768.0).astype(np.int16)
        try:
            sys.stdout.buffer.write(out.tobytes())
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def write_denoise_script(path, channels):
    content = DENOISE_SCRIPT_CONTENT.replace("__SR__", str(SAMPLE_RATE)).replace(
        "__CHANNELS__", str(channels)
    )
    path.write_text(content)


def get_denoise_python():
    candidates = [
        os.environ.get("DENOISE_PYTHON"),
        "/home/wanjin1234/denoise_mic/venv/bin/python",
        os.path.expanduser("~/denoise_mic/venv/bin/python"),
        os.path.expanduser("~/venv/bin/python"),
        "/usr/bin/python3",
        sys.executable,
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return sys.executable


# ---------------- 音频管道 ----------------

DOWNMIX_SCRIPT_CONTENT = r"""
import array
import sys


def main():
    while True:
        data = sys.stdin.buffer.read(8192)
        if not data:
            break
        n = len(data) // 4
        data = data[:n * 4]
        samples = array.array("h")
        samples.frombytes(data)
        out = array.array("h")
        for i in range(0, len(samples), 2):
            out.append((samples[i] + samples[i + 1]) // 2)
        sys.stdout.buffer.write(out.tobytes())
        sys.stdout.flush()


if __name__ == "__main__":
    main()
"""


def drain_stream(stream, label, sink):
    try:
        for raw_line in iter(stream.readline, b""):
            if not raw_line:
                break
            line = raw_line.decode(errors="replace").rstrip()
            sink.append(line)
            print(f"    [{label}] {line}", flush=True)
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def build_pipeline(mic_device, bluealsa_pcm, venv_python, use_df, mic_channels=1):
    out_channels = 1
    rec_cmd = [
        "arecord",
        "-t",
        "raw",
        "-D",
        mic_device,
        "-f",
        FORMAT,
        "-r",
        str(SAMPLE_RATE),
        "-c",
        str(mic_channels),
    ]
    play_cmd = [
        "aplay",
        "-t",
        "raw",
        "-D",
        bluealsa_pcm,
        "-f",
        FORMAT,
        "-r",
        str(SAMPLE_RATE),
        "-c",
        str(out_channels),
    ]
    stages = [("arecord", rec_cmd)]
    if use_df:
        stages.append(("denoise", [venv_python, str(DENOISE_SCRIPT)]))
    elif mic_channels == 2:
        DOWNMIX_SCRIPT.write_text(DOWNMIX_SCRIPT_CONTENT)
        stages.append(("downmix", [venv_python, str(DOWNMIX_SCRIPT)]))
    stages.append(("aplay", play_cmd))
    return stages


def launch_pipeline(pipeline):
    procs = []
    logs = {label: [] for label in set(l for l, _ in pipeline)}
    prev = None
    for label, cmd in pipeline:
        if prev is None:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            p = subprocess.Popen(
                cmd, stdin=prev.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            prev.stdout.close()
        procs.append((label, p))
        threading.Thread(
            target=drain_stream, args=(p.stderr, label, logs[label]), daemon=True
        ).start()
        prev = p
    return procs, logs


def terminate_all(procs):
    for label, p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
    time.sleep(1)
    for label, p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass


def print_stderr_tails(logs, n=12):
    for label, lines in logs.items():
        if lines:
            print(f"  -- {label} stderr tail --")
            for line in lines[-n:]:
                print(f"    {line}")


def check_pipeline_startup(procs, logs):
    reasons = []
    time.sleep(3)
    for label, p in procs:
        if p.poll() is not None:
            reasons.append(f"{label} 启动后立即退出（退出码 {p.returncode}）")
    if not reasons:
        time.sleep(5)
        for label, p in procs:
            if p.poll() is not None:
                reasons.append(f"{label} 启动后退出（退出码 {p.returncode}）")
    if reasons:
        print_stderr_tails(logs)
        return False, reasons
    print("  -> 管道健康检查通过：所有子进程均在运行")
    return True, []


def start_audio_forwarding(bluealsa_pcm, mic_device, mic_channels=1):
    print_status("启动音频转发")
    venv_python = get_denoise_python()
    use_df = False

    check = run(
        f"{venv_python} -c 'import df, numpy; print(\"df_ok\")'",
        check=False,
        timeout=120,
        verbose=False,
    )
    if check and check.returncode == 0:
        write_denoise_script(DENOISE_SCRIPT, mic_channels)
        print("  -> 正在预加载 DeepFilterNet 模型（首次运行会下载权重，请耐心等待）...")
        pre = run(
            f"{venv_python} {DENOISE_SCRIPT} --preload-only",
            check=False,
            timeout=300,
            verbose=False,
        )
        if pre and pre.returncode == 0:
            use_df = True
            print("  -> DeepFilterNet 预加载成功，启用降噪转发")
        else:
            err_text = ""
            if pre:
                err_text = ((pre.stdout or "") + (pre.stderr or "")).strip()
            tail = err_text.splitlines()[-1] if err_text else "预加载超时或失败"
            print(f"  !! DeepFilterNet 预加载失败（{tail}），降级为无降噪转发")
    else:
        print("  -> 未检测到 DeepFilterNet，使用普通 arecord -> aplay 转发")

    pipeline = build_pipeline(
        mic_device, bluealsa_pcm, venv_python, use_df, mic_channels
    )
    print("  管道命令:")
    for label, cmd in pipeline:
        print(f"    {label}: {' '.join(cmd)}")

    restart_count = 0
    procs = []
    try:
        while restart_count <= MAX_RESTARTS:
            procs, logs = launch_pipeline(pipeline)
            print("  -> 音频转发已启动，按 Ctrl+C 停止")

            healthy, reasons = check_pipeline_startup(procs, logs)
            if not healthy:
                restart_count += 1
                print(f"  !! 启动失败: {'; '.join(reasons)}")
                if restart_count > MAX_RESTARTS:
                    print("  !! 连续多次启动失败，放弃重试")
                    raise RuntimeError("音频转发管道启动失败: " + "; ".join(reasons))
                print(f"  -> 将在 3 秒后重试（第 {restart_count}/{MAX_RESTARTS} 次）")
                terminate_all(procs)
                time.sleep(3)
                continue

            while True:
                time.sleep(1)
                dead = [(label, p) for label, p in procs if p.poll() is not None]
                if not dead:
                    continue
                print("  !! 检测到转发子进程退出，准备重启整个音频管道")
                for label, p in dead:
                    print(f"    -> {label} 退出码: {p.returncode}")
                print_stderr_tails(logs)
                restart_count += 1
                if restart_count > MAX_RESTARTS:
                    print("  !! 超过最大重启次数，退出")
                    raise RuntimeError("音频转发管道反复退出，已停止")
                terminate_all(procs)
                print(
                    f"  -> 将在 2 秒后重启转发管道（第 {restart_count}/{MAX_RESTARTS} 次）"
                )
                time.sleep(2)
                break
    except KeyboardInterrupt:
        print("\n  -> 正在停止转发...")
        raise
    finally:
        terminate_all(procs)


# ---------------- 主流程 ----------------


def main():
    ensure_root()
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    print("=== Raspberry Pi 蓝牙麦克风（BlueALSA + DeepFilterNet）完成版 ===")
    print("等待 Windows 主动连接，不主动连接；退出时自动断开蓝牙")

    backup_system_state()
    try:
        setup_packages()
        disable_audio_servers()

        if os.path.exists("/etc/asound.conf"):
            shutil.move("/etc/asound.conf", "/tmp/asound.conf.removed")
            print("  -> 已移走 /etc/asound.conf")

        configure_bluetoothd()
        configure_bluealsa()
        prepare_bt_state()

        mic_result = select_and_verify_mic()
        if not mic_result[0]:
            raise RuntimeError(
                "未找到可用的录音设备（麦克风）。请连接麦克风，"
                "或用 MIC_DEVICE=plughw:X,Y 环境变量指定设备后重试。"
            )
        mic_device, mic_channels = mic_result
        print(f"  -> 最终使用麦克风: {mic_device}（{mic_channels} 通道）")

        device = wait_for_connection()
        if not device:
            raise RuntimeError("未检测到 Windows 连接，脚本退出")

        get_device_info(device)
        trust_device(device)

        # 不主动连接设备，也不主动连 A2DP。
        # 只等待 Windows 建立 HFP/HSP/SCO 通道。
        pcm = find_working_pcm(device)

        if not pcm:
            raise RuntimeError(
                "没有找到可用的 HFP/SCO PCM。"
                "请在 Windows 声音设置中选择 Hands-Free/Headset 作为输入设备，然后重新运行。"
            )

        print(f"  -> 最终使用 PCM: {pcm}")
        start_audio_forwarding(pcm, mic_device, mic_channels)

    except KeyboardInterrupt:
        print("\n  用户中断程序")
    except (RuntimeError, FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        print(f"\n[错误] {e}")
        sys.exit(1)
    finally:
        restore_default()


if __name__ == "__main__":
    main()
