#!/usr/bin/env python3
"""
树莓派 ReSpeaker 降噪 → 蓝牙输出到电脑
降噪引擎：DeepFilterNet2_ll（轻量 AI 模型，实时处理）
整合了蓝牙管理、音频采集、降噪、pw-cat 输出。
"""

import atexit
import re
import signal
import subprocess
import sys
import threading
import time

# ---------- AI 降噪核心（替代 noisereduce） ----------
try:
    from df import enhance, init_df
except ImportError:
    print("请先安装 deepfilternet：pip install deepfilternet")
    sys.exit(1)

import numpy as np
import pyaudio

# ========== 配置 ==========
RESPEAKER_DEVICE_NAME = "seeed"  # 用于识别 ReSpeaker 的关键词
SAMPLE_RATE = 16000  # 16 kHz（推荐，性能与效果均衡）
FRAME_SIZE = 512  # 帧长（@16kHz ≈ 32 ms）
CHANNELS = 1  # 单声道
BT_DEVICE_NAME = "RaspberryPi-Mic"  # 蓝牙设备名称
BT_NODE_PATTERN = "bluez_output"  # 蓝牙输出节点标识


class DenoiseBTBridge:
    def __init__(self, sample_rate=16000, frame_size=512):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.agent_process = None
        self.pyaudio_instance = None
        self.stream_in = None
        self.pwcat_proc = None
        self.running = True
        self.audio_thread = None

        # ---------- 加载 DeepFilterNet 模型（轻量版，降低 CPU 占用）----------
        print("正在加载 DeepFilterNet2_ll 降噪模型...")
        self.df_model, self.df_state, _ = init_df("DeepFilterNet2_ll")
        print("✅ 模型加载完成，AI 降噪引擎已就绪")

    # ---------- 蓝牙管理（与之前完全一致） ----------
    def start_agent(self):
        """启动 bt-agent 作为配对代理（自动接受配对）"""
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

        self.start_agent()

    # ---------- 音频设备识别 ----------
    def _find_respeaker_index(self):
        """在 PyAudio 中查找 ReSpeaker 输入设备索引"""
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if (
                RESPEAKER_DEVICE_NAME in dev["name"].lower()
                and dev["maxInputChannels"] > 0
            ):
                p.terminate()
                return i, dev
        p.terminate()
        return None, None

    def _find_bt_node(self):
        """查找蓝牙输出节点名（bluez_output.*）"""
        for _ in range(10):
            try:
                result = subprocess.run(
                    ["pw-cli", "ls", "Node"], capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    if BT_NODE_PATTERN in line:
                        match = re.search(r'"([^"]+)"', line)
                        if match:
                            return match.group(1)
            except Exception as e:
                print(f"⚠️ 查找节点失败: {e}")
            time.sleep(1)
        return None

    # ---------- AI 降噪处理线程 ----------
    def _audio_processing_loop(self):
        """实时采集 → DeepFilterNet 降噪 → 写入蓝牙管道"""
        print("🎤 AI 降噪线程启动（DeepFilterNet）")
        try:
            while self.running:
                # 1. 读取一帧原始 PCM 数据
                data = self.stream_in.read(self.frame_size, exception_on_overflow=False)
                audio_int16 = np.frombuffer(data, dtype=np.int16)

                # 2. 转换为 float32，归一化到 [-1, 1]（DeepFilterNet 要求）
                audio_float = audio_int16.astype(np.float32) / 32768.0

                # 3. AI 降噪
                #    enhance() 内部会维护状态，支持连续流式处理
                enhanced_float = enhance(self.df_model, self.df_state, audio_float)

                # 4. 转回 int16，写入管道送到蓝牙
                enhanced_int16 = (
                    (enhanced_float * 32767).clip(-32768, 32767).astype(np.int16)
                )
                self.pwcat_proc.stdin.write(enhanced_int16.tobytes())
                self.pwcat_proc.stdin.flush()
        except Exception as e:
            print(f"音频处理错误: {e}")
        finally:
            print("音频处理线程退出")

    # ---------- 桥接控制 ----------
    def start_bridge(self):
        """建立降噪桥接：打开麦克风 → 启动 pw-cat → 启动处理线程"""
        # 1. 找到蓝牙节点
        bt_node = self._find_bt_node()
        if not bt_node:
            print("❌ 未找到蓝牙输出节点")
            return False
        print(f"✅ 蓝牙输出节点: {bt_node}")

        # 2. 打开 ReSpeaker 麦克风
        p = pyaudio.PyAudio()  # 注意：这里实例化后要在退出时释放
        self.pyaudio_instance = p  # 保存以便后续关闭
        idx, dev = self._find_respeaker_index()
        if idx is None:
            print("❌ 未找到 ReSpeaker 设备")
            p.terminate()
            return False
        print(f"✅ ReSpeaker 设备: {dev['name']} (索引 {idx})")

        try:
            self.stream_in = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=self.sample_rate,
                input=True,
                input_device_index=idx,
                frames_per_buffer=self.frame_size,
            )
        except Exception as e:
            print(f"❌ 无法打开 ReSpeaker 输入流: {e}")
            p.terminate()
            return False

        # 3. 启动 pw-cat，将降噪后数据播放到蓝牙节点
        cmd = [
            "pw-cat",
            "--playback",
            "--rate",
            str(self.sample_rate),
            "--channels",
            str(CHANNELS),
            "--format",
            "s16",
            "--target",
            bt_node,
        ]
        try:
            self.pwcat_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"✅ pw-cat 已启动，目标：{bt_node}")
        except Exception as e:
            print(f"❌ 启动 pw-cat 失败: {e}")
            self.stream_in.close()
            p.terminate()
            return False

        # 4. 启动音频处理线程（无需噪声基线采集）
        self.running = True
        self.audio_thread = threading.Thread(
            target=self._audio_processing_loop, daemon=True
        )
        self.audio_thread.start()

        print("✅ AI 降噪桥接已建立！电脑端收到的是降噪后的清晰语音。")
        return True

    def stop_bridge(self):
        """停止桥接"""
        self.running = False
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=2)
        if self.pwcat_proc and self.pwcat_proc.poll() is None:
            self.pwcat_proc.stdin.close()
            self.pwcat_proc.terminate()
            self.pwcat_proc.wait()
            print("⏹️ pw-cat 已停止")
        if self.stream_in:
            self.stream_in.stop_stream()
            self.stream_in.close()
            self.stream_in = None
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()
            self.pyaudio_instance = None
        print("⏹️ 音频桥接已停止")

    def cleanup(self):
        """退出清理"""
        self.stop_bridge()
        if self.agent_process:
            self.agent_process.terminate()
            self.agent_process.wait()
            print("⏹️ bt-agent 已退出")


def main():
    print("=" * 50)
    print("树莓派降噪蓝牙麦克风（DeepFilterNet AI 版）")
    print("=" * 50)

    # 初始化桥接对象（只需采样率和帧长，不再需要降噪参数）
    bridge = DenoiseBTBridge(sample_rate=SAMPLE_RATE, frame_size=FRAME_SIZE)
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
            print("🎤 电脑端现在应能收到 AI 降噪后的麦克风信号")


if __name__ == "__main__":
    main()
