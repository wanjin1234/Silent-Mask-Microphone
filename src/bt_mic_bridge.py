#!/usr/bin/env python3
"""
树莓派 ReSpeaker 降噪 → 蓝牙输出到电脑
整合了蓝牙管理、降噪处理、pw-cat 输出。
"""

import atexit
import re
import signal
import subprocess
import sys
import threading
import time

import noisereduce as nr
import numpy as np
import pyaudio

# ========== 配置 ==========
RESPEAKER_DEVICE_NAME = "seeed"  # 用于识别 ReSpeaker 的关键词
SAMPLE_RATE = 16000  # 16 kHz（HFP 常用，A2DP 也可用 48kHz）
FRAME_SIZE = 512  # 帧长（@16kHz ≈ 32 ms）
CHANNELS = 1  # 单声道降噪
NOISE_DURATION = 2.0  # 噪声基线采集时长（秒）
PROP_DECREASE = 0.85  # 降噪强度 0~1
STATIONARY = False  # 非稳态噪声
BT_DEVICE_NAME = "RaspberryPi-Mic"  # 蓝牙设备名称
BT_NODE_PATTERN = "bluez_output"  # 蓝牙输出节点标识


class DenoiseBTBridge:
    def __init__(
        self, sample_rate=16000, frame_size=512, prop_decrease=0.85, stationary=False
    ):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.prop_decrease = prop_decrease
        self.stationary = stationary
        self.agent_process = None
        self.pyaudio_instance = None
        self.stream_in = None
        self.pwcat_proc = None
        self.running = True
        self.audio_thread = None
        self.noise_signal = None  # 采集的噪声基线
        self.noise_energy_thresh = None  # 噪声能量阈值
        self.noise_update_counter = 0

    # ---------- 蓝牙管理 ----------
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

    # ---------- 音频设备与降噪 ----------
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

    def _capture_noise_baseline(self):
        """采集初始噪声基线，并计算能量阈值"""
        print(f"正在采集 {NOISE_DURATION} 秒环境噪声...")
        frames = []
        num_frames = int(self.sample_rate * NOISE_DURATION / self.frame_size)
        for i in range(num_frames):
            data = self.stream_in.read(self.frame_size)
            frames.append(np.frombuffer(data, dtype=np.int16))
        self.noise_signal = np.concatenate(frames)
        # 计算噪声 RMS 作为 VAD 阈值
        self.noise_energy_thresh = (
            np.sqrt(np.mean(self.noise_signal.astype(np.float32) ** 2)) * 1.5
        )
        print(f"噪声基线采集完成，能量阈值={self.noise_energy_thresh:.1f}")

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


def _audio_processing_loop(self):
    print("🎤 音频处理线程启动")
    noise_update_frames = []  # 暂存潜在的噪声帧
    try:
        while self.running:
            data = self.stream_in.read(self.frame_size)
            audio = np.frombuffer(data, dtype=np.int16)
            # 简单能量 VAD
            rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
            if rms < self.noise_energy_thresh:
                # 认为是噪声帧，收集用于更新基线
                noise_update_frames.append(audio.copy())
                # 每积累约 0.5 秒（16帧 @ 512/16k ≈ 0.5s）更新一次基线
                if len(noise_update_frames) >= 16:
                    new_noise = np.concatenate(noise_update_frames)
                    # 与旧基线按比例混合（避免突变）
                    self.noise_signal = (
                        0.7 * self.noise_signal[-len(new_noise) :] + 0.3 * new_noise
                    ).astype(np.int16)
                    noise_update_frames.clear()
                    self.noise_energy_thresh = (
                        np.sqrt(np.mean(new_noise.astype(np.float32) ** 2)) * 1.5
                    )
            else:
                # 语音帧，清空积累
                noise_update_frames.clear()
            # 降噪（使用当前最新的 noise_signal）
            reduced = nr.reduce_noise(
                y=audio,
                y_noise=self.noise_signal,
                sr=self.sample_rate,
                stationary=False,
                prop_decrease=0.75,
                n_fft=512,
                hop_length=128,
                win_length=512,
                n_std_thresh_stationary=1.5,
                freq_mask_smooth_hz=200,
                time_mask_smooth_ms=50,
            )
            self.pwcat_proc.stdin.write(reduced.astype(np.int16).tobytes())
            self.pwcat_proc.stdin.flush()
    except Exception as e:
        print(f"音频处理错误: {e}")
    finally:
        print("音频处理线程退出")

    # ---------- 桥接控制 ----------
    def start_bridge(self):
        """建立降噪桥接：采集噪声 → 启动 pw-cat → 启动处理线程"""
        # 1. 找到蓝牙节点
        bt_node = self._find_bt_node()
        if not bt_node:
            print("❌ 未找到蓝牙输出节点")
            return False
        print(f"✅ 蓝牙输出节点: {bt_node}")

        # 2. 打开 ReSpeaker 麦克风
        p = pyaudio.PyAudio()
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

        # 3. 采集噪声基线（阻塞，但此时蓝牙刚刚连接，电脑端还没开始接收，所以无妨）
        self._capture_noise_baseline()

        # 4. 启动 pw-cat，将降噪后数据播放到蓝牙节点
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

        # 5. 启动音频处理线程
        self.running = True
        self.audio_thread = threading.Thread(
            target=self._audio_processing_loop, daemon=True
        )
        self.audio_thread.start()

        print("✅ 降噪桥接已建立！电脑现在收到的是降噪后的麦克风信号。")
        return True

    def stop_bridge(self):
        """停止桥接"""
        # 告知处理线程停止
        self.running = False
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=2)
        # 关闭 pw-cat
        if self.pwcat_proc and self.pwcat_proc.poll() is None:
            self.pwcat_proc.stdin.close()
            self.pwcat_proc.terminate()
            self.pwcat_proc.wait()
            print("⏹️ pw-cat 已停止")
        # 关闭音频输入流
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
    print("树莓派降噪蓝牙麦克风")
    print("=" * 50)

    bridge = DenoiseBTBridge(
        sample_rate=SAMPLE_RATE,
        frame_size=FRAME_SIZE,
        prop_decrease=PROP_DECREASE,
        stationary=STATIONARY,
    )
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
            print("🎤 电脑现在应能收到降噪后的立体声/单声道麦克风信号")


if __name__ == "__main__":
    main()
