#!/usr/bin/env python3
"""
树莓派蓝牙麦克风桥接（无第三方依赖版）
通过 subprocess 调用 bluetoothctl 和 pw-loopback 实现
"""

import signal
import subprocess
import sys
import time

# ========== 配置 ==========
RESPEAKER_DEVICE = "hw:seeed2micvoicec,0"  # ReSpeaker ALSA 设备名
SAMPLE_RATE = "16000"  # HFP 宽频 16kHz
CHANNELS = "1"  # 单声道
BT_DEVICE_NAME = "RaspberryPi-Mic"  # 蓝牙名称


# ========== 蓝牙初始化 ==========
def bluetoothctl_cmd(*args):
    """执行 bluetoothctl 命令"""
    subprocess.run(["bluetoothctl"] + list(args), check=True)


def init_bluetooth():
    """初始化蓝牙：上电、可发现、可配对"""
    print("🔵 初始化蓝牙...")
    # 先确保蓝牙服务已启动
    subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], check=False)
    time.sleep(2)

    # 设置名称
    bluetoothctl_cmd("system-alias", BT_DEVICE_NAME)
    # 上电
    bluetoothctl_cmd("power", "on")
    # 设置代理（自动接受配对）
    bluetoothctl_cmd("agent", "NoInputNoOutput")
    bluetoothctl_cmd("default-agent")
    # 开启发现和配对
    bluetoothctl_cmd("discoverable", "on")
    bluetoothctl_cmd("pairable", "on")
    print("✅ 蓝牙已可被发现，名称：", BT_DEVICE_NAME)


def get_connected_devices():
    """获取已连接设备 MAC 列表"""
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        devices = []
        for line in result.stdout.splitlines():
            # 格式: Device XX:XX:XX:XX:XX:XX 名称
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device":
                devices.append(parts[1])
        return devices
    except Exception:
        return []


def wait_for_connection():
    """等待直到有设备连接，返回设备 MAC"""
    print("🔍 等待 Windows 电脑连接蓝牙...")
    while True:
        devices = get_connected_devices()
        if devices:
            print(f"🔗 已连接设备: {devices[0]}")
            return devices[0]
        time.sleep(2)


# ========== 音频桥接 ==========
class AudioBridge:
    def __init__(self):
        self.process = None

    def start(self):
        """启动 pw-loopback 桥接"""
        print(f"🔄 启动音频桥接: {RESPEAKER_DEVICE} → 蓝牙 AG")
        self.process = subprocess.Popen(
            [
                "pw-loopback",
                "--capture-props",
                f"node.target={RESPEAKER_DEVICE}",
                "--playback-props",
                "media.class=Audio/Source",
                "--rate",
                SAMPLE_RATE,
                "--channels",
                CHANNELS,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self):
        """停止桥接"""
        if self.process:
            self.process.terminate()
            self.process.wait()
            print("⏹️ 音频桥接已停止")


# ========== 主循环 ==========
def main():
    print("=" * 50)
    print("树莓派蓝牙麦克风桥接工具")
    print("=" * 50)

    init_bluetooth()
    bridge = AudioBridge()

    def cleanup(signum=None, frame=None):
        print("\n🛑 退出...")
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    while True:
        try:
            device_mac = wait_for_connection()
            time.sleep(2)  # 等待 PipeWire 创建蓝牙节点
            bridge.start()

            # 监控连接状态
            while True:
                time.sleep(3)
                if device_mac not in get_connected_devices():
                    print("🔌 设备断开")
                    bridge.stop()
                    break
        except KeyboardInterrupt:
            cleanup()
        except Exception as e:
            print(f"❌ 错误: {e}")
            bridge.stop()
            time.sleep(5)


if __name__ == "__main__":
    main()
