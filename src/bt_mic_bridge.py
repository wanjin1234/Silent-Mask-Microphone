#!/usr/bin/env python3
"""
树莓派蓝牙麦克风桥接（立体声双声道 + 开机自启动）
通过 subprocess 调用 bt-agent 和 pw-link 实现
"""

import atexit
import re
import signal
import subprocess
import sys
import time

# ========== 配置 ==========
RESPEAKER_DEVICE = "hw:seeed2micvoicec,0"
SAMPLE_RATE = "16000"  # 若使用 A2DP 可改为 48000
CHANNELS = "2"  # 立体声
BT_DEVICE_NAME = "RaspberryPi-Mic"
BT_NODE_PATTERN = "bluez_output"  # 若实际为 bluez_input 请修改


class AudioBridge:
    def __init__(self, alsa_device, sample_rate, channels):
        self.alsa_device = alsa_device
        self.sample_rate = sample_rate
        self.channels = int(channels)
        self.loopback_process = None
        self.link_count = 0
        self.agent_process = None  # bt-agent 子进程

    def start_agent(self):
        """启动 bt-agent 作为后台配对代理（自动接受配对）"""
        # 先杀掉可能残留的 bt-agent
        subprocess.run(["killall", "bt-agent"], capture_output=True, check=False)
        try:
            self.agent_process = subprocess.Popen(
                ["bt-agent", "-c", "NoInputNoOutput"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("✅ bt-agent 配对代理已启动")
        except Exception as e:
            print(f"⚠️ 启动 bt-agent 失败: {e}")

    def init_bluetooth(self):
        """初始化蓝牙：上电、可发现、可配对"""
        print("🔵 初始化蓝牙...")
        subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], check=False)
        time.sleep(2)

        subprocess.run(["bluetoothctl", "system-alias", BT_DEVICE_NAME], check=True)
        subprocess.run(["bluetoothctl", "power", "on"], check=True)
        subprocess.run(["bluetoothctl", "discoverable", "on"], check=True)
        subprocess.run(["bluetoothctl", "pairable", "on"], check=True)
        print(f"✅ 蓝牙已可被发现，名称：{BT_DEVICE_NAME}")

        # 启动配对代理
        self.start_agent()

    def _find_node(self, pattern):
        """查找 PipeWire 节点名"""
        try:
            result = subprocess.run(
                ["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if pattern in line:
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        return match.group(1)
        except Exception as e:
            print(f"⚠️ 查找节点失败: {e}")
        return None

    def _get_ports(self, node_name, direction):
        """获取节点端口列表"""
        ports = []
        try:
            result = subprocess.run(
                ["pw-link", "-l"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if node_name in line and direction in line:
                    port_id = line.split()[0]
                    ports.append(port_id)
        except Exception as e:
            print(f"⚠️ 获取端口失败: {e}")
        return ports

    def start_bridge(self):
        """建立音频桥接"""
        print("🔄 查找蓝牙目标节点...")
        bt_node = None
        for _ in range(10):
            bt_node = self._find_node(BT_NODE_PATTERN)
            if bt_node:
                break
            time.sleep(1)
        if not bt_node:
            print(f"❌ 未找到包含 '{BT_NODE_PATTERN}' 的节点")
            return False

        print(f"✅ 蓝牙目标节点: {bt_node}")

        # 查找 ReSpeaker 节点
        alsa_node = self._find_node("seeed")
        if not alsa_node:
            alsa_node = self._find_node("alsa_input")
        if not alsa_node:
            print("❌ 未找到 ReSpeaker 录音节点")
            return False

        print(f"✅ ReSpeaker 节点: {alsa_node}")

        cap_ports = self._get_ports(alsa_node, "capture")
        pb_ports = self._get_ports(bt_node, "playback")
        if not cap_ports or not pb_ports:
            print("❌ 未找到有效端口")
            return False

        num_channels = min(self.channels, len(cap_ports), len(pb_ports))
        success = 0
        for i in range(num_channels):
            try:
                subprocess.run(
                    ["pw-link", cap_ports[i], pb_ports[i]],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                print(f"   已连接: {cap_ports[i]} -> {pb_ports[i]}")
                success += 1
            except subprocess.CalledProcessError as e:
                print(f"   ⚠️ 连接失败: {e.stderr.strip()}")

        if success > 0:
            self.link_count = success
            print(f"✅ 音频桥接已建立（{success}/{num_channels} 声道）")
            return True
        else:
            # 备选方案：pw-loopback
            cmd = [
                "pw-loopback",
                "--capture-props",
                f"node.target={self.alsa_device}",
                "--playback-props",
                f"node.target={bt_node}",
                "--rate",
                str(self.sample_rate),
                "--channels",
                str(self.channels),
            ]
            try:
                self.loopback_process = subprocess.Popen(cmd)
                print("🔄 备选方案 pw-loopback 已启动")
                return True
            except Exception as e:
                print(f"❌ pw-loopback 失败: {e}")
                return False

    def stop_bridge(self):
        """停止桥接"""
        if self.loopback_process and self.loopback_process.poll() is None:
            self.loopback_process.terminate()
            self.loopback_process.wait(timeout=2)
            print("⏹️ pw-loopback 已停止")
        self.link_count = 0

    def cleanup(self):
        """退出清理"""
        self.stop_bridge()
        if self.agent_process:
            self.agent_process.terminate()
            self.agent_process.wait()
            print("⏹️ bt-agent 已退出")


def main():
    print("=" * 50)
    print("树莓派蓝牙麦克风桥接（双声道）")
    print("=" * 50)

    bridge = AudioBridge(RESPEAKER_DEVICE, SAMPLE_RATE, CHANNELS)
    atexit.register(bridge.cleanup)

    def handle_exit(signum, frame):
        print("\n🛑 正在退出...")
        bridge.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # 初始化蓝牙（仅一次）
    bridge.init_bluetooth()

    # 循环监听连接
    last_addr = None
    while True:
        # 获取当前连接的第一个设备
        try:
            result = subprocess.run(
                ["bluetoothctl", "devices", "Connected"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = result.stdout.splitlines()
            current_addr = None
            for line in lines:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "Device":
                    current_addr = parts[1]
                    break
        except Exception:
            current_addr = None

        if not current_addr:
            if last_addr:
                print(f"🔌 设备 {last_addr} 已断开")
                bridge.stop_bridge()
                last_addr = None
            time.sleep(3)
            continue

        if current_addr == last_addr:
            time.sleep(3)
            continue

        # 新设备连接
        print(f"🔗 检测到设备: {current_addr}")
        last_addr = current_addr
        time.sleep(2)  # 等待 PipeWire 创建蓝牙节点

        if not bridge.start_bridge():
            print("再次尝试建立桥接...")
            last_addr = None  # 下次循环重试
        else:
            print("🎤 Windows 现在应该能收到立体声麦克风信号")


if __name__ == "__main__":
    main()
