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
    subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], check=False)
    time.sleep(2)

    # 设置名称和上电（这两个可以单独执行，因为会持久化）
    subprocess.run(["bluetoothctl", "system-alias", BT_DEVICE_NAME], check=True)
    subprocess.run(["bluetoothctl", "power", "on"], check=True)

    # 关键：在同一会话中注册代理并设为默认
    agent_commands = "agent NoInputNoOutput\ndefault-agent\n"
    subprocess.run(["bluetoothctl"], input=agent_commands, text=True, check=True)

    # 开启发现和配对（也可以在同一次会话中，但分开更清晰）
    subprocess.run(["bluetoothctl", "discoverable", "on"], check=True)
    subprocess.run(["bluetoothctl", "pairable", "on"], check=True)

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
    def __init__(self, alsa_device, sample_rate, channels):
        self.alsa_device = alsa_device
        self.sample_rate = sample_rate
        self.channels = channels
        self.link_id = None
        self.capture_node = None

    def _find_node(self, pattern):
        """查找匹配 pattern 的 PipeWire 节点名"""
        try:
            result = subprocess.run(
                ["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if pattern in line:
                    # 取该行的最后一列（节点名）
                    return line.split('"')[1]
        except Exception as e:
            print(f"⚠️ 查找节点失败: {e}")
        return None

    def _find_port(self, node_name, direction):
        """查找节点的一个端口（capture 或 playback）"""
        try:
            # 获取节点 ID
            result = subprocess.run(
                ["pw-cli", "info", node_name], capture_output=True, text=True, timeout=5
            )
            # 简单解析端口，这里用 pw-link -l 更可靠
        except:
            pass
        # 用 pw-link 列端口并 grep
        try:
            result = subprocess.run(
                ["pw-link", "-l"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if node_name in line and direction in line:
                    return line.split()[0]
        except:
            pass
        return None

    def start(self):
        """建立音频桥接"""
        print("🔄 查找蓝牙输出节点...")
        # 等待 bluez_output 出现（最多等 10 秒）
        for _ in range(10):
            bt_sink = self._find_node("bluez_output")
            if bt_sink:
                break
            time.sleep(1)
        if not bt_sink:
            print("❌ 未找到 bluez_output 节点，请确认蓝牙已连接且 HFP-AG 已激活")
            return

        print(f"✅ 找到蓝牙输出节点: {bt_sink}")

        # 查找 ReSpeaker 节点
        alsa_node = self._find_node("seeed")
        if not alsa_node:
            alsa_node = self._find_node("alsa_input")
        if not alsa_node:
            print("❌ 未找到 ReSpeaker 录音节点")
            return

        print(f"✅ 找到 ReSpeaker 节点: {alsa_node}")

        # 构建连接命令（使用 pw-loopback 更简单，但需要确保 target 正确）
        # 这里改用 pw-link 直接连接，更可靠
        try:
            # 获取两个节点的端口
            subprocess.run(["pw-link", "-l"], capture_output=True, text=True)
            # 直接尝试连接（假设端口名规则）
            capture_port = f"{alsa_node}:capture_1"
            playback_port = f"{bt_sink}:playback_1"

            print(f"🔗 连接 {capture_port} → {playback_port}")
            subprocess.run(
                ["pw-link", capture_port, playback_port],
                check=True,
                capture_output=True,
                text=True,
            )
            print("✅ 音频桥接已建立")
        except subprocess.CalledProcessError as e:
            print(f"⚠️ pw-link 连接失败: {e.stderr}")
            # 备用方案：使用 pw-loopback
            print("尝试备选方案: pw-loopback")
            self._start_loopback(bt_sink)

    def _start_loopback(self, bt_sink):
        """备选：使用 pw-loopback 连接"""
        self.process = subprocess.Popen(
            [
                "pw-loopback",
                "--capture-props",
                f"node.target={self.alsa_device}",
                "--playback-props",
                f"node.target={bt_sink}",
                "--rate",
                str(self.sample_rate),
                "--channels",
                str(self.channels),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"🔄 pw-loopback 已启动")

    def stop(self):
        """断开桥接"""
        if self.process:
            self.process.terminate()
            self.process.wait()
            print("⏹️ 音频桥接已停止")
        # 如果用的是 pw-link，则断开连接（可选）


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
