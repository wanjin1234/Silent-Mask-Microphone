#!/usr/bin/env python3
"""
Raspberry Pi 作为蓝牙麦克风（BlueALSA + DeepFilterNet）

修正点：
- 不是把“无法连接 SCO”当作致命错误；Windows 已连接时，HFP/HSP 路由可能已生效
- 自动检测并执行 Linux 配置命令，不要求手工输入
- 断言服务/蓝牙状态，避免假设 hci0 固定存在
- 退出时恢复原来的 systemd override 和蓝牙配置
- 在找不到 exact SCO PCM 时，继续探测常见 BlueALSA profile 名称
"""

import glob
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MIC_DEVICE = os.environ.get("MIC_DEVICE", "hw:0,0")
SAMPLE_RATE = 16000
CHANNELS = 1
FORMAT = "S16_LE"
CONNECTION_TIMEOUT = 180

BACKUP_DIR = "/tmp/bt_mic_backup"
BT_OVERRIDE = "/etc/systemd/system/bluetooth.service.d/override.conf"
BLUEALSA_OVERRIDE = "/etc/systemd/system/bluealsa.service.d/override.conf"
BLUETOOTH_CONF = "/etc/bluetooth/main.conf"


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


def backup_system_state():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for path in [BLUETOOTH_CONF, BT_OVERRIDE, BLUEALSA_OVERRIDE]:
        if os.path.exists(path):
            dst = os.path.join(BACKUP_DIR, os.path.basename(path) + ".bak")
            shutil.copy2(path, dst)


def restore_default():
    print_status("恢复系统默认设置")
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


def setup_packages():
    print_status("检查并安装依赖")
    run("apt-get update -y --allow-releaseinfo-change", check=False, verbose=False)
    run(
        "apt-get install -y bluez bluez-tools bluez-alsa bluez-alsa-utils bluetooth libasound2-dev libasound2-plugins",
        check=False,
        verbose=False,
    )


def disable_audio_servers():
    print_status("禁用 PulseAudio / PipeWire / oFono")
    for service in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "ofono",
        "hsphfpd",
    ]:
        run(f"systemctl stop {service} 2>/dev/null || true", check=False, verbose=False)
        run(
            f"systemctl disable {service} 2>/dev/null || true",
            check=False,
            verbose=False,
        )
    # 某些发行版中这些进程是 user/session 级，systemctl stop 不一定覆盖。
    run("pkill -x pulseaudio 2>/dev/null || true", check=False, verbose=False)
    run("pkill -x pipewire 2>/dev/null || true", check=False, verbose=False)
    run("pkill -x wireplumber 2>/dev/null || true", check=False, verbose=False)
    run("pkill -x hsphfpd 2>/dev/null || true", check=False, verbose=False)


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
    """检查 org.bluealsa 是否已经注册到 system bus。"""
    result = run(
        "dbus-send --system --print-reply --dest=org.freedesktop.DBus "
        "/org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner "
        "string:org.bluealsa",
        check=False,
        timeout=8,
        verbose=False,
    )
    if not result:
        return False
    return "boolean true" in (result.stdout or "")


def ensure_bluealsa_dbus(hci):
    """确保 BlueALSA 守护进程在 D-Bus 上提供 org.bluealsa。"""
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
        f"{ba_path} --dbus=org.bluealsa -i {hci} -p hsp-hs -p hfp-hf >/tmp/bluealsa.log 2>&1 &",
        check=False,
        verbose=False,
    )
    time.sleep(2)
    return has_bluealsa_dbus_name()


def resolve_uuid_conflict(hci):
    """尝试释放已被注册的 HFP/HSP UUID：停止冲突服务、重启 bluetooth/bluealsa 并重试注册。"""
    print_status("尝试解决 UUID 冲突: 停止冲突服务并重启 bluetooth/bluealsa")
    # 停止 bluealsa / bluetooth 并关掉常见会占用 UUID 的守护进程
    run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl stop bluetooth 2>/dev/null || true", check=False, verbose=False)
    for svc in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "ofono",
        "hsphfpd",
    ]:
        run(f"systemctl stop {svc} 2>/dev/null || true", check=False, verbose=False)
    run("pkill -x pulseaudio 2>/dev/null || true", check=False, verbose=False)
    run("pkill -x pipewire 2>/dev/null || true", check=False, verbose=False)
    run("pkill -x wireplumber 2>/dev/null || true", check=False, verbose=False)
    run("pkill -x hsphfpd 2>/dev/null || true", check=False, verbose=False)

    # reload systemd & start bluetooth with our override
    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl start bluetooth", check=False, verbose=False)
    time.sleep(2)

    # 检查 bluetoothd 是否带有 --noplugin=audio,headset
    check = run(
        "ps -ef | grep bluetoothd | grep -v grep", check=False, timeout=8, verbose=False
    )
    if not (check and "--noplugin=audio,headset" in (check.stdout or "")):
        print(
            "  !! 注意：bluetoothd 运行时未检测到 --noplugin=audio,headset，可能无法释放 UUID"
        )

    # 重启 bluealsa 并查看 journal 中是否仍报 UUID 冲突
    run("systemctl restart bluealsa", check=False, verbose=False)
    time.sleep(2)
    journal = run(
        "journalctl -u bluealsa -n 80 --no-pager 2>&1",
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
    print(
        "  !! UUID 冲突仍然存在，请检查正在运行的进程（如 pipewire/hsphfpd/ofono/pulseaudio）或手动重启系统以清除注册"
    )
    # 额外打印当前 journal 帮助诊断
    if journal and (journal.stdout or "").strip():
        print("  -- bluealsa 最近日志 --")
        print((journal.stdout or "").strip())
    return False


def configure_bluetoothd():
    print_status("配置 BlueZ，禁用内置 audio/headset 插件")
    bt_bin = find_bluetoothd()
    # 必须同时禁用 audio + headset，否则会与 BlueALSA 的 HFP/HSP UUID 冲突。
    new_exec = f"{bt_bin} --experimental --noplugin=audio,headset"
    os.makedirs(os.path.dirname(BT_OVERRIDE), exist_ok=True)
    with open(BT_OVERRIDE, "w", encoding="utf-8") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl restart bluetooth", check=False, verbose=False)
    time.sleep(2)

    # 诊断：确认 bluetoothd 参数生效。
    check = run(
        "ps -ef | grep bluetoothd | grep -v grep", check=False, timeout=8, verbose=False
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
    # 作为“蓝牙麦克风/耳机”接入 Windows，树莓派应扮演 HF/HS 侧。
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

    conflict = run(
        "journalctl -u bluealsa -n 80 --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    if conflict and "UUID already registered" in (conflict.stdout or ""):
        print("  !! 检测到 'UUID already registered'，说明仍有组件占用了 HFP/HSP UUID")
        print("  !! 尝试自动清理冲突并重试注册 BlueALSA HFP/HSP")
        if resolve_uuid_conflict(hci):
            # 重新读取 journal 以确认状态
            conflict2 = run(
                "journalctl -u bluealsa -n 80 --no-pager 2>&1",
                check=False,
                timeout=10,
                verbose=False,
            )
            if (
                conflict2
                and "UUID already registered" not in (conflict2.stdout or "")
                and "Couldn't register hands-free profile"
                not in (conflict2.stdout or "")
            ):
                print("  -> 冲突已解决，继续后续流程")
            else:
                print(
                    "  !! 自动重试未能清除 UUID 冲突，请手动检查 bluetoothd/plugin 或重启系统"
                )
        else:
            print("  !! 自动清理失败：请手动检查 bluetoothd/plugin 或重启系统")

    # 诊断：确认 ALSA 端可见 bluealsa 设备字符串
    alsa_list = run("aplay -L", check=False, timeout=8, verbose=False)
    if not alsa_list or "bluealsa" not in (alsa_list.stdout or ""):
        print(
            "  !! aplay -L 未发现 bluealsa 设备，可能是 ALSA bluealsa 插件缺失或服务未注册"
        )
        print("  !! 请检查: bluealsa-aplay -L / bluealsa-cli list-pcms")


def prepare_bt_state():
    print_status("设置蓝牙可见性与电源")
    run("bluetoothctl power on", check=False, verbose=False)
    run("bluetoothctl agent NoInputNoOutput", check=False, verbose=False)
    run("bluetoothctl default-agent", check=False, verbose=False)
    run("bluetoothctl pairable on", check=False, verbose=False)
    run("bluetoothctl pairable-timeout 0", check=False, verbose=False)
    run("bluetoothctl discoverable on", check=False, verbose=False)
    run("bluetoothctl discoverable-timeout 0", check=False, verbose=False)
    run("hciconfig hci0 class 0x240404 2>/dev/null || true", check=False, verbose=False)
    time.sleep(2)


def wait_for_connection(timeout=CONNECTION_TIMEOUT):
    print_status(f"等待 Windows 连接（最长 {timeout} 秒）")
    print("  请在 Windows 蓝牙设置中连接 'RaspberryPi-Mic' 或选择耳机模式")
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
            print(f"  -> 已连接设备: {devices[0]}")
            return devices[0]
        time.sleep(3)
    return None


def get_device_info(device):
    result = run(f"bluetoothctl info {device}", check=False, verbose=False)
    if result:
        print(result.stdout)
    return result


def trust_device(device):
    run(f"bluetoothctl trust {device}", check=False, verbose=False)


def connect_hfp_hsp(device):
    """优先请求 HFP/HSP profile，但失败不视为致命错误。Windows 已经连上时，可能已完成 SCO 握手。"""
    print_status("请求 HFP/HSP profile")
    hci = get_hci_name()
    dev_path = f"/org/bluez/{hci}/dev_{device.replace(':', '_').upper()}"
    for uuid in [
        "0000111f-0000-1000-8000-00805f9b34fb",
        "00001112-0000-1000-8000-00805f9b34fb",
    ]:
        cmd = (
            f"dbus-send --system --print-reply --dest=org.bluez "
            f"{dev_path} org.bluez.Device1.ConnectProfile string:{uuid}"
        )
        res = run(cmd, check=False, timeout=10, verbose=False)
        if res and res.returncode == 0:
            print(f"  -> profile {uuid} 连接成功")
            return True
    print("  !! 连接 HFP/HSP profile 失败，但若 Windows 已连接且音频已流入，可继续执行")
    return False


def test_pcm(pcm, verbose=False):
    cmd = f"timeout 2 aplay -D '{pcm}' -f {FORMAT} -r {SAMPLE_RATE} -c {CHANNELS} /dev/zero 2>&1"
    result = run(cmd, check=False, timeout=5, verbose=False)
    if result is None:
        return False
    if result.returncode in (0, 124):
        return True
    if verbose and result.stdout:
        lines = result.stdout.strip().splitlines()
        if lines:
            print(f"    -> {pcm}: {lines[-1]}")
    return False


def list_bluealsa_pcm_candidates(device):
    """优先从 BlueALSA 工具读取真实 PCM，再回退到规则生成。"""
    candidates = []
    mac_patterns = {
        device.lower(),
        device.upper(),
        device.lower().replace(":", ""),
        device.upper().replace(":", ""),
    }

    # 1) bluealsa-aplay -L 的输出通常直接给出可用 PCM 字符串
    if shutil.which("bluealsa-aplay"):
        res = run("bluealsa-aplay -L", check=False, timeout=8, verbose=False)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                s = line.strip()
                if not s.startswith("bluealsa:"):
                    continue
                lower_s = s.lower()
                if any(m in lower_s for m in mac_patterns):
                    candidates.append(s)

    # 2) bluealsa-cli list-pcms 可用于确认 profile（不同发行版输出格式略有差异）
    if shutil.which("bluealsa-cli"):
        res = run("bluealsa-cli list-pcms", check=False, timeout=8, verbose=False)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                low = line.lower()
                if not any(m in low for m in mac_patterns):
                    continue
                prof = "sco"
                m = re.search(r"\b(a2dp|sco|hfp|hsp)\b", low)
                if m:
                    prof = m.group(1)
                # 统一构造成 ALSA 蓝牙设备字符串
                candidates.append(f"bluealsa:DEV={device.upper()},PROFILE={prof}")

    # 3) 再尝试 aplay -L 暴露的 bluealsa 模板
    if not candidates:
        res = run("aplay -L", check=False, timeout=8, verbose=False)
        if res and res.returncode == 0:
            for line in res.stdout.splitlines():
                s = line.strip()
                # bare "bluealsa" 只是模板名，不能直接 open。
                if s.startswith("bluealsa:"):
                    candidates.append(s)

    # 4) 兜底规则生成（即便已有候选，也补齐带 DEV/PROFILE 的显式候选）
    profiles = ["sco", "hfp", "hsp", "a2dp", "default"]
    mac_variants = [
        device.upper(),
        device.lower(),
        device.upper().replace(":", ""),
        device.lower().replace(":", ""),
    ]
    for mac in mac_variants:
        for prof in profiles:
            if prof == "default":
                candidates.append(f"bluealsa:DEV={mac}")
                candidates.append(f"bluealsa:SRV=org.bluealsa,DEV={mac}")
                candidates.append(f"bluealsa:DEV={mac},SRV=org.bluealsa")
            else:
                candidates.append(f"bluealsa:DEV={mac},PROFILE={prof}")
                candidates.append(f"bluealsa:SRV=org.bluealsa,DEV={mac},PROFILE={prof}")
                candidates.append(f"bluealsa:DEV={mac},PROFILE={prof},SRV=org.bluealsa")

    seen = set()
    ordered = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def find_working_pcm(device):
    print_status("查找 BlueALSA PCM 设备")
    ordered = list_bluealsa_pcm_candidates(device)
    if not ordered:
        print("  !! 当前未读到 BlueALSA 候选 PCM")
        return None

    for pcm in ordered:
        print(f"  测试: {pcm}")
        if test_pcm(pcm, verbose=True):
            print(f"  ** 找到可用 PCM: {pcm}")
            return pcm

    # 额外诊断，帮助确认 BlueALSA 实际导出的 PCM。
    print("  !! 所有候选均失败，输出 BlueALSA 诊断信息：")
    diag1 = run("bluealsa-aplay -L 2>&1", check=False, timeout=8, verbose=False)
    if diag1 and (diag1.stdout or "").strip():
        print("  -- bluealsa-aplay -L --")
        print((diag1.stdout or "").strip())
    diag2 = run("bluealsa-cli list-pcms 2>&1", check=False, timeout=8, verbose=False)
    if diag2 and (diag2.stdout or "").strip():
        print("  -- bluealsa-cli list-pcms --")
        print((diag2.stdout or "").strip())
    diag3 = run(
        "systemctl status bluealsa --no-pager -n 30 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    if diag3 and (diag3.stdout or "").strip():
        print("  -- systemctl status bluealsa --")
        print((diag3.stdout or "").strip())
    return None


def get_denoise_python():
    candidates = [
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


def drain_stream(stream, label, sink):
    try:
        for raw_line in iter(stream.readline, b""):
            if not raw_line:
                break
            line = raw_line.decode(errors="replace").rstrip()
            sink.append(line)
            print(f"    [{label}] {line}")
    finally:
        try:
            stream.close()
        except (OSError, ValueError) as exc:
            logger.debug("关闭 %s stderr 流失败: %s", label, exc)


def start_audio_forwarding(bluealsa_pcm):
    print_status("启动音频转发")
    venv_python = get_denoise_python()
    result = run(
        f"{venv_python} -c 'import df; print(\"ok\")'", check=False, verbose=False
    )
    use_df = bool(result and result.returncode == 0)

    if not use_df:
        print("  -> 未检测到 DeepFilterNet，使用普通 arecord -> aplay 转发")
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
                bluealsa_pcm,
                "-f",
                FORMAT,
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
            ],
        ]
    else:
        print("  -> 检测到 DeepFilterNet，启用降噪转发")
        denoise_script = Path("/tmp/df_denoise.py")
        denoise_script.write_text(
            """
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
""".strip()
        )
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
                bluealsa_pcm,
                "-f",
                FORMAT,
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
            ],
        ]

    def launch_pipeline():
        processes = []
        stderr_logs = {"arecord": [], "denoise": [], "aplay": []}
        prev = None
        for index, cmd in enumerate(pipeline):
            label = (
                "arecord"
                if index == 0
                else ("aplay" if index == len(pipeline) - 1 else "denoise")
            )
            if prev is None:
                p = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                )
            else:
                p = subprocess.Popen(
                    cmd,
                    stdin=prev.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                )
                if prev.stdout:
                    prev.stdout.close()
            processes.append((label, p))
            thread = threading.Thread(
                target=drain_stream,
                args=(p.stderr, label, stderr_logs[label]),
                daemon=True,
            )
            thread.start()
            prev = p
        return processes, stderr_logs

    restart_count = 0
    try:
        while True:
            processes, stderr_logs = launch_pipeline()
            print("  -> 音频转发已启动，按 Ctrl+C 停止")
            while True:
                time.sleep(1)
                dead = [(label, p) for label, p in processes if p.poll() is not None]
                if dead:
                    print("  !! 检测到转发子进程退出，准备重启整个音频管道")
                    for label, p in processes:
                        code = p.poll()
                        print(f"    -> {label} 退出码: {code}")
                    for label, p in processes:
                        if p.poll() is None:
                            p.terminate()
                    time.sleep(1)
                    for label, logs in stderr_logs.items():
                        if logs:
                            print(f"  -- {label} stderr tail --")
                            for line in logs[-10:]:
                                print(f"    {line}")
                    restart_count += 1
                    print(f"  -> 将在 2 秒后重启转发管道（第 {restart_count} 次）")
                    time.sleep(2)
                    break
    except KeyboardInterrupt:
        print("\n  -> 正在停止转发...")
        raise


def main():
    ensure_root()
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    print("=== Raspberry Pi 蓝牙麦克风（BlueALSA + DeepFilterNet）===")
    print(
        "核心：Windows 已经连接并开始接收音频时，不应因为 SCO 测试失败中止；应继续探测 BlueALSA PCM"
    )

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

        device = wait_for_connection()
        if not device:
            raise RuntimeError("未检测到 Windows 连接，脚本退出")

        get_device_info(device)
        trust_device(device)
        connect_hfp_hsp(device)

        # 无 PIN 场景下，确保设备保持可信并再次连接。
        run(f"bluetoothctl connect {device}", check=False, timeout=12, verbose=False)

        pcm = None
        start = time.time()
        retry_count = 0
        while time.time() - start < 90:
            pcm = find_working_pcm(device)
            if pcm:
                break
            retry_count += 1
            if retry_count % 3 == 0:
                print("  -> 仍未找到可用 PCM，尝试再次触发 HFP/HSP 协商")
                connect_hfp_hsp(device)
                run(
                    f"bluetoothctl connect {device}",
                    check=False,
                    timeout=12,
                    verbose=False,
                )
            time.sleep(3)

        if not pcm:
            raise RuntimeError(
                "未找到可用的 BlueALSA PCM。说明 Windows 连接已建立，但 SCO/HFP 设备还未完成音频通道协商；请确认系统已安装 bluez-alsa 且 Windows 选择了耳机/音频设备。"
            )

        print(f"  -> 最终使用 PCM: {pcm}")
        start_audio_forwarding(pcm)

    except KeyboardInterrupt:
        print("\n  用户中断程序")
    except (RuntimeError, FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        print(f"\n[错误] {e}")
        sys.exit(1)
    finally:
        restore_default()


if __name__ == "__main__":
    main()
