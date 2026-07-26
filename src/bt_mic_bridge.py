#!/usr/bin/env python3
"""
树莓派蓝牙麦克风桥接脚本
- 将 ReSpeaker 2-Mic 的音频通过 HFP/HSP 发送给连接的 Windows 电脑
- 依赖: PipeWire, WirePlumber (已配置 HFP-AG), pyaudio, dbus-python
"""

import os
import signal
import subprocess
import sys
import threading
import time

import dbus
import gi
from dbus.mainloop.glib import DBusGMainLoop

gi.require_version("GLib", "2.0")
from gi.repository import GLib

# ===================== 配置区 =====================
# ReSpeaker ALSA 设备名（用 arecord -l 查看）
RESPEAKER_DEVICE = "hw:seeed2micvoicec,0"
# 或者用 hw:0,0 等

# 音频参数（HFP/HSP 通常为 8kHz/16kHz 单声道，16-bit）
SAMPLE_RATE = 16000  # 16 kHz 是 HFP 宽频，8 kHz 是窄频
CHANNELS = 1
FORMAT = "s16le"  # 16-bit little-endian

# 蓝牙设备名称（Windows 上看到的名称）
BT_DEVICE_NAME = "RaspberryPi-Mic"


# ===================== 蓝牙管理类 =====================
class BluetoothManager:
    """通过 D-Bus 管理蓝牙配对与连接状态"""

    def __init__(self):
        self.bus = dbus.SystemBus()
        self.adapter_path = None
        self._find_adapter()

    def _find_adapter(self):
        """找到默认蓝牙适配器"""
        manager = dbus.Interface(
            self.bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager"
        )
        objects = manager.GetManagedObjects()
        for path, ifaces in objects.items():
            if "org.bluez.Adapter1" in ifaces:
                self.adapter_path = path
                break
        if not self.adapter_path:
            raise RuntimeError("未找到蓝牙适配器")

    def set_discoverable(self, timeout=0):
        """设置可发现模式"""
        adapter = dbus.Interface(
            self.bus.get_object("org.bluez", self.adapter_path),
            "org.freedesktop.DBus.Properties",
        )
        adapter.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(timeout))
        adapter.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
        adapter.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
        print("✅ 蓝牙已设为可发现 / 可配对")

    def set_alias(self, name):
        """设置蓝牙设备名称"""
        adapter = dbus.Interface(
            self.bus.get_object("org.bluez", self.adapter_path),
            "org.freedesktop.DBus.Properties",
        )
        adapter.Set("org.bluez.Adapter1", "Alias", name)
        print(f"✅ 蓝牙名称已设为: {name}")

    def get_connected_devices(self):
        """返回已连接的设备 MAC 列表"""
        manager = dbus.Interface(
            self.bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager"
        )
        objects = manager.GetManagedObjects()
        devices = []
        for path, ifaces in objects.items():
            if "org.bluez.Device1" in ifaces:
                props = ifaces["org.bluez.Device1"]
                if props.get("Connected", False):
                    devices.append(props.get("Address", "unknown"))
        return devices

    def wait_for_connection(self, timeout=None):
        """阻塞等待直到有设备连接，返回设备 MAC"""
        print("🔍 等待 Windows 电脑连接蓝牙...")
        start = time.time()
        while True:
            devices = self.get_connected_devices()
            if devices:
                print(f"🔗 已连接设备: {devices[0]}")
                return devices[0]
            if timeout and (time.time() - start) > timeout:
                raise TimeoutError("等待连接超时")
            time.sleep(1)


# ===================== 音频桥接类 =====================
class AudioBridge:
    """将 ReSpeaker ALSA 设备桥接到 PipeWire 蓝牙 AG source"""

    def __init__(self, alsa_device, sample_rate, channels):
        self.alsa_device = alsa_device
        self.sample_rate = sample_rate
        self.channels = channels
        self.running = False
        self.process = None

    def find_bluetooth_source(self):
        """查找蓝牙 AG 的音频源（即 Windows 听到的麦克风）"""
        try:
            result = subprocess.run(
                ["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=5
            )
            # 简化解析：寻找包含 "bluez" 且方向为 "input" 的节点
            lines = result.stdout.splitlines()
            current_id = None
            for line in lines:
                if line.startswith("id "):
                    current_id = line.split()[1].rstrip(",")
                if "bluez" in line.lower() and "input" in line.lower():
                    return current_id
        except Exception as e:
            print(f"⚠️ 查找蓝牙 source 失败: {e}")
        return None

    def start(self):
        """启动音频桥接"""
        self.running = True
        self.process = subprocess.Popen(
            [
                "pw-loopback",
                "--capture-props",
                f"node.target={self.alsa_device}",
                "--playback-props",
                "media.class=Audio/Source",
                "--rate",
                str(self.sample_rate),
                "--channels",
                str(self.channels),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        print(f"🔄 音频桥接已启动: {self.alsa_device} → Bluetooth AG")

    def stop(self):
        """停止音频桥接"""
        self.running = False
        if self.process:
            self.process.terminate()
            self.process.wait()
            print("⏹️ 音频桥接已停止")


# ===================== 主程序 =====================
def main():
    print("=" * 50)
    print("树莓派蓝牙麦克风桥接工具")
    print("=" * 50)

    # 1. 初始化蓝牙
    bt = BluetoothManager()
    bt.set_alias(BT_DEVICE_NAME)
    bt.set_discoverable(timeout=0)  # 0 = 始终可发现

    # 2. 注册信号处理（优雅退出）
    bridge = AudioBridge(RESPEAKER_DEVICE, SAMPLE_RATE, CHANNELS)

    def cleanup(signum=None, frame=None):
        print("\n🛑 正在退出...")
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # 3. 主循环：等待连接 → 建立音频桥 → 监控断开 → 重新等待
    while True:
        try:
            # 等待 Windows 连接
            device_mac = bt.wait_for_connection()

            # 短暂延迟，确保 PipeWire 创建了蓝牙节点
            time.sleep(2)

            # 启动音频桥接
            bridge.start()

            # 监控连接状态
            while True:
                time.sleep(2)
                connected = bt.get_connected_devices()
                if device_mac not in connected:
                    print("🔌 设备已断开，停止音频桥接...")
                    bridge.stop()
                    break

        except KeyboardInterrupt:
            cleanup()
        except Exception as e:
            print(f"❌ 错误: {e}")
            bridge.stop()
            time.sleep(5)  # 出错后等待 5 秒再重试


if __name__ == "__main__":
    main()
