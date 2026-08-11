#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Pi as Bluetooth Microphone with DeepFilterNet (BlueALSA方案)
- 使用 BlueALSA 绕过 PulseAudio，稳定支持 HFP。
- 自动确保 bluealsa 以 hfp-ag/hsp-ag 模式运行。
- 无控制台交互，全自动运行。
"""

import atexit
import os
import shutil
import signal
import subprocess
import sys
import time

# 配置
USE_DEEP_FILTER = True
DENOISE_PYTHON = os.path.expanduser("~/denoise_mic/venv/bin/python")

BACKUP_DIR = "/tmp/bt_mic_backup"
BLUETOOTH_CONF = "/etc/bluetooth/main.conf"
BT_OVERRIDE = "/etc/systemd/system/bluetooth.service.d/override.conf"
BLUEALSA_OVERRIDE = "/etc/systemd/system/bluealsa.service.d/override.conf"


# ---------- 工具 ----------
def run(cmd, timeout=20, check=False):
    print("[执行]", cmd)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[超时] {cmd}")
        return None
    if result.stdout:
        print(result.stdout)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败: {cmd}\n输出: {result.stdout}")
    return result


def backup_config():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for path in [BLUETOOTH_CONF, BT_OVERRIDE, BLUEALSA_OVERRIDE]:
        if os.path.exists(path):
            shutil.copy2(
                path, os.path.join(BACKUP_DIR, os.path.basename(path) + ".bak")
            )


def restore_config():
    print("正在恢复系统配置...")
    for fname, dest in [("main.conf.bak", BLUETOOTH_CONF)]:
        src = os.path.join(BACKUP_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, dest)
        else:
            run(f"rm -f {dest}", check=False)
    run(
        "rm -rf /etc/systemd/system/bluetooth.service.d /etc/systemd/system/bluealsa.service.d",
        check=False,
    )
    run("systemctl stop bluealsa 2>/dev/null || true", check=False)
    run("systemctl disable bluealsa 2>/dev/null || true", check=False)
    run("pkill -f deepfilter_denoise || pkill -f 'arecord.*aplay' || true", check=False)
    run("systemctl daemon-reload", check=False)
    run("systemctl restart bluetooth", check=False)
    print("系统配置已恢复。")


# ---------- 系统配置 ----------
def install_dependencies():
    run("apt-get update -y --allow-releaseinfo-change", timeout=60, check=True)
    run("apt-get install -y bluez bluez-tools bluez-alsa-utils", check=True)


def configure_bluetooth_class():
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
        if in_general and stripped.startswith("Class="):
            continue
        new_lines.append(line)
    idx = None
    for i, l in enumerate(new_lines):
        if l.strip() == "[General]":
            idx = i
            break
    if idx is None:
        new_lines.append("[General]")
        idx = len(new_lines) - 1
    new_lines.insert(idx + 1, "Class = 0x240404")
    with open(BLUETOOTH_CONF, "w") as f:
        f.write("\n".join(new_lines) + "\n")
    print("蓝牙类别已设为耳机（Class=0x240404）")


def get_original_bluetooth_execstart():
    result = run(
        "systemctl show bluetooth.service -p FragmentPath --value", check=False
    )
    unit_file = result.stdout.strip() if result else ""
    if not unit_file or not os.path.isfile(unit_file):
        for path in [
            "/lib/systemd/system/bluetooth.service",
            "/usr/lib/systemd/system/bluetooth.service",
        ]:
            if os.path.isfile(path):
                unit_file = path
                break
    if not unit_file:
        raise RuntimeError("找不到 bluetooth.service 单元文件")
    with open(unit_file, "r") as f:
        for line in f:
            if line.strip().startswith("ExecStart="):
                return line.strip().split("=", 1)[1].strip()
    raise RuntimeError("未找到 ExecStart 行")


def configure_bluetoothd():
    os.makedirs(os.path.dirname(BT_OVERRIDE), exist_ok=True)
    orig = get_original_bluetooth_execstart()
    if "--experimental" not in orig and "-E" not in orig:
        new_exec = orig + " --experimental"
    else:
        new_exec = orig
    with open(BT_OVERRIDE, "w") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")
    run("systemctl daemon-reload", check=True)
    run("systemctl restart bluetooth", check=True)
    print("BlueZ 已启用实验模式并重启")


def configure_bluealsa():
    """确保 bluealsa 以 hfp-ag/hsp-ag 模式运行"""
    os.makedirs(os.path.dirname(BLUEALSA_OVERRIDE), exist_ok=True)

    result = run("systemctl show bluealsa.service -p FragmentPath --value", check=False)
    unit_file = result.stdout.strip() if result else None
    if not unit_file or not os.path.isfile(unit_file):
        for path in [
            "/lib/systemd/system/bluealsa.service",
            "/usr/lib/systemd/system/bluealsa.service",
            "/etc/systemd/system/bluealsa.service",
        ]:
            if os.path.isfile(path):
                unit_file = path
                break
    if not unit_file:
        raise RuntimeError("找不到 bluealsa.service 单元文件")

    exec_start = None
    with open(unit_file, "r") as f:
        for line in f:
            if line.strip().startswith("ExecStart="):
                exec_start = line.strip().split("=", 1)[1].strip()
                break
    if exec_start is None:
        exec_start = "/usr/bin/bluealsa"

    new_exec = exec_start
    if "-p hfp-ag" not in new_exec:
        new_exec += " -p hfp-ag"
    if "-p hsp-ag" not in new_exec:
        new_exec += " -p hsp-ag"

    with open(BLUEALSA_OVERRIDE, "w") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl daemon-reload", check=True)
    run("systemctl stop bluealsa 2>/dev/null || true", check=False)
    run("systemctl enable bluealsa 2>/dev/null || true", check=False)
    run("systemctl restart bluealsa", check=True)
    time.sleep(2)

    result = run("systemctl is-active bluealsa", check=False)
    if result and result.stdout.strip() == "active":
        print("BlueALSA 已启动，支持 HFP/HSP AG")
        return True
    else:
        run("bluealsa -i hci0 -p hsp-ag -p hfp-ag &", check=False)
        time.sleep(2)
        return True


def make_discoverable():
    cmds = (
        "power on\nagent on\ndefault-agent\n"
        "discoverable on\ndiscoverable-timeout 0\npairable on\n"
        "pairable-timeout 0\nquit\n"
    )
    p = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p.communicate(cmds, timeout=10)
    run("bluetoothctl show", check=False)
    print("蓝牙已设为可发现，请在 Windows 端连接树莓派")


# ---------- 设备查找 ----------
def find_bluealsa_device():
    """查找 BlueALSA 创建的 HFP/HSP 设备"""
    result = run("aplay -L", check=False)
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("bluealsa:") and ("hfp_ag" in line or "hsp_ag" in line):
            print(f"检测到 BlueALSA 设备: {line}")
            return line
    return None


def wait_for_bluealsa_device(macs, timeout=30):
    """在已经连接的设备上等待 HFP/HSP 设备出现"""
    if not macs:
        return None
    mac = macs[0].lower()
    start = time.time()
    while time.time() - start < timeout:
        result = run("aplay -L", check=False)
        if result and result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if (
                    line.startswith("bluealsa:DEV=")
                    and mac in line.lower()
                    and ("hfp_ag" in line or "hsp_ag" in line)
                ):
                    print(f"找到 BlueALSA HFP 设备: {line}")
                    return line
        time.sleep(2)
    return None


def find_mic_alsa_name():
    result = run("arecord -L", check=False)
    if result is None or result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if (
            "seeed2micvoicec" in line
            or "soc_sound" in line
            or "ac108" in line
            or "tlv320aic3x" in line
        ):
            if (
                line.startswith("hw:")
                or line.startswith("plughw:")
                or line.startswith("sysdefault:")
            ):
                print(f"本地麦克风 ALSA 设备: {line}")
                return line
    return None


def find_connected_bt_macs():
    result = run("bluetoothctl devices Connected", check=False)
    if result is None or result.returncode != 0:
        return []
    macs = []
    for line in result.stdout.splitlines():
        if line.startswith("Device "):
            parts = line.split()
            if len(parts) >= 2:
                macs.append(parts[1])
    return macs


def trigger_bt_connect(macs):
    for mac in macs:
        print(f"确保蓝牙连接: {mac}")
        run(f"bluetoothctl connect {mac}", timeout=5, check=False)
        time.sleep(2)


# ---------- 音频转发 ----------
def get_denoise_python():
    if DENOISE_PYTHON and os.path.isfile(DENOISE_PYTHON):
        return DENOISE_PYTHON
    for p in [
        "/home/wanjin1234/venv/bin/python",
        os.path.expanduser("~/venv/bin/python"),
    ]:
        if os.path.isfile(p):
            return p
    return sys.executable


def check_sounddevice(python_interp):
    try:
        result = subprocess.run(
            [python_interp, "-c", "import sounddevice; print('ok')"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True
        else:
            print(f"虚拟环境缺少 sounddevice: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"检查 sounddevice 时出错: {e}")
        return False


def start_denoise_process(mic_alsa, bt_alsa):
    python_interp = get_denoise_python()
    worker_code = f"""
import os, sys, time
import numpy as np
import sounddevice as sd
import df

def main():
    print("初始化 DeepFilterNet...", flush=True)
    model, state = df.init(mode="streaming")
    print("模型初始化完成", flush=True)

    CH = 1
    RATE = 16000
    BLOCK = 512

    try:
        stream = sd.Stream(
            device=({mic_alsa!r}, {bt_alsa!r}),
            samplerate=RATE,
            blocksize=BLOCK,
            channels=CH,
            dtype='int16',
            latency='low'
        )
    except Exception as e:
        print(f"无法打开音频流: {{e}}", file=sys.stderr)
        sys.exit(1)

    stream.start()
    print("开始实时降噪...", flush=True)
    try:
        while True:
            data, overflowed = stream.read(BLOCK)
            if overflowed:
                continue
            pcm = data.astype(np.float32) / 32768.0
            enhanced = df.process_pcm(model, state, pcm)
            out = (np.clip(enhanced, -1, 1) * 32767).astype(np.int16)
            stream.write(out)
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        stream.close()

if __name__ == "__main__":
    main()
"""
    env = os.environ.copy()
    env["HOME"] = "/root"
    env["ALSA_CONFIG_PATH"] = "/usr/share/alsa/alsa.conf"
    try:
        proc = subprocess.Popen(
            [python_interp, "-c", worker_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        time.sleep(3)
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            print(f"降噪进程启动失败: {out}", file=sys.stderr)
            return None
        return proc
    except Exception as e:
        print(f"启动降噪异常: {e}", file=sys.stderr)
        return None


def start_plain_forward(mic_alsa, bt_alsa):
    """回退：使用 arecord | aplay 简单转发"""
    print("使用普通音频转发（无降噪）")
    cmd = f"arecord -D {mic_alsa} -f S16_LE -r 16000 -c 1 | aplay -D {bt_alsa} -f S16_LE -r 16000 -c 1"
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc
    except Exception as e:
        print(f"普通转发启动失败: {e}")
        return None


def stop_forward_proc(proc):
    if not proc:
        return
    print("停止音频转发...")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except:
        try:
            proc.kill()
        except:
            pass


# ---------- 主流程 ----------
def main():
    if os.geteuid() != 0:
        print("需要 root，尝试 sudo 重启...")
        os.execvp("sudo", ["sudo", "python3", sys.argv[0]])

    backup_config()
    atexit.register(restore_config)

    print("===== 蓝牙麦克风（BlueALSA + DeepFilterNet）=====")
    install_dependencies()
    configure_bluetooth_class()
    configure_bluetoothd()
    configure_bluealsa()
    make_discoverable()

    python_interp = get_denoise_python()
    if USE_DEEP_FILTER and not check_sounddevice(python_interp):
        print("警告：虚拟环境缺少 sounddevice，将使用普通转发（无降噪）。")
        deep_filter_enabled = False
    else:
        deep_filter_enabled = USE_DEEP_FILTER

    print("\n请在 Windows 蓝牙设置中连接树莓派（应显示为“耳机”）。\n")

    forward_proc = None
    route_active = False
    last_mac = set()
    waiting_bt = False

    try:
        while True:
            connected_macs = find_connected_bt_macs()
            if connected_macs:
                if set(connected_macs) != last_mac:
                    print(f"检测到已连接设备: {connected_macs}")
                    last_mac = set(connected_macs)
                    trigger_bt_connect(connected_macs)
                    waiting_bt = True

                if waiting_bt:
                    bt_alsa = wait_for_bluealsa_device(connected_macs, timeout=15)
                    if bt_alsa is None:
                        print("尚未出现 HFP 设备，继续等待并重新触发连接...")
                        trigger_bt_connect(connected_macs)
                else:
                    bt_alsa = find_bluealsa_device()

                if bt_alsa and not route_active:
                    mic_alsa = find_mic_alsa_name()
                    if not mic_alsa:
                        print("[警告] 未找到本地麦克风，重试...")
                        time.sleep(3)
                        continue

                    print(f"本地麦克风: {mic_alsa}")
                    print(f"蓝牙设备: {bt_alsa}")

                    if deep_filter_enabled:
                        forward_proc = start_denoise_process(mic_alsa, bt_alsa)
                        if forward_proc:
                            print("深度降噪已启动，音频发送中...")
                            route_active = True
                        else:
                            print("降噪启动失败，回退到普通转发...")
                            forward_proc = start_plain_forward(mic_alsa, bt_alsa)
                            if forward_proc:
                                route_active = True
                    else:
                        forward_proc = start_plain_forward(mic_alsa, bt_alsa)
                        if forward_proc:
                            route_active = True

                    if route_active:
                        waiting_bt = False
                elif bt_alsa:
                    pass
            else:
                if route_active:
                    print("蓝牙断开，停止转发...")
                    stop_forward_proc(forward_proc)
                    forward_proc = None
                    route_active = False
                last_mac.clear()
                waiting_bt = False

            time.sleep(2)

    except KeyboardInterrupt:
        print("\n退出中...")
    finally:
        if forward_proc:
            stop_forward_proc(forward_proc)


def handle_sigterm(signum, frame):
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    main()
