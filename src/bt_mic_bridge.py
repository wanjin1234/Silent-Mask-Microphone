#!/usr/bin/env python3
"""
树莓派 ReSpeaker 降噪 → 蓝牙输出到电脑
版本: v4.0 (稳定版)
修复:
  - webrtcvad 导入错误（需 setuptools<81 或手动修补 webrtcvad.py）
  - 蓝牙节点检测改用 MAC 地址直接构造，100% 可靠
  - 集成瞬态噪声抑制与自适应门限
"""

import atexit
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque

# ---------- AI 降噪 ----------
try:
    from df import enhance, init_df
except ImportError:
    print("请先安装 deepfilternet：pip install deepfilternet")
    sys.exit(1)

import numpy as np
import pyaudio

# ========== 配置 ==========
RESPEAKER_DEVICE_NAME = "seeed"
SAMPLE_RATE = 16000
CHANNELS = 1
BT_DEVICE_NAME = "RaspberryPi-Mic"

# 降噪与门限
NOISE_GATE_THRESHOLD = 0.005
VAD_AGGRESSIVENESS = 2
ENABLE_VAD = True
TRANSIENT_DETECTION = True
TRANSIENT_ENERGY_RATIO = 3.0
TRANSIENT_HIGH_FREQ_RATIO = 0.6
DEBUG_AUDIO_LEVELS = True

TARGET_MIC_GAIN = 50  # 0-100，建议 40-60


class DenoiseBTBridge:
    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.vad_frame_size = 320  # 20ms@16kHz
        self.frame_size = self.vad_frame_size
        self.channels = CHANNELS

        self.agent_process = None
        self.pyaudio_instance = None
        self.stream_in = None
        self.pwcat_proc = None
        self.running = True
        self.audio_thread = None

        self.noise_floor_rms = 0.0
        self.noise_floor_alpha = 0.95

        # ---------- VAD ----------
        self.vad = None
        if ENABLE_VAD:
            try:
                import webrtcvad

                self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
                print(f"✅ WebRTC VAD 已启用 (激进度={VAD_AGGRESSIVENESS})")
            except ImportError as e:
                print(f"❌ webrtcvad 不可用: {e}")
                print("   修复方法一：pip install 'setuptools<81'")
                print("   修复方法二：手动编辑 webrtcvad.py 删除 pkg_resources 导入")
                self.vad = None

        # ---------- DeepFilterNet ----------
        print("正在加载 DeepFilterNet2_ll 降噪模型...")
        model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
        self.df_model, self.df_state, _ = init_df(model_path)
        print("✅ 模型加载完成")

        self.hi_bin_start = int(4000 / (sample_rate / self.frame_size))

    # ===================== 蓝牙管理 =====================
    def start_agent(self):
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
        print("🔵 初始化蓝牙...")
        subprocess.run(["sudo", "systemctl", "restart", "bluetooth"], check=False)
        time.sleep(2)
        subprocess.run(["bluetoothctl", "system-alias", BT_DEVICE_NAME], check=True)
        subprocess.run(["bluetoothctl", "power", "on"], check=True)
        subprocess.run(["bluetoothctl", "discoverable", "on"], check=True)
        subprocess.run(["bluetoothctl", "pairable", "on"], check=True)
        print(f"✅ 蓝牙可发现: {BT_DEVICE_NAME}")
        self.start_agent()

    # ===================== 音频设备识别 =====================
    def _find_respeaker_index(self):
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
        """根据已连接蓝牙的 MAC 地址直接构造 PipeWire 节点名（最高可靠性）"""
        print("🔍 查找蓝牙输出节点...")
        try:
            result = subprocess.run(
                ["bluetoothctl", "devices", "Connected"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "Device":
                    mac = parts[1]
                    # PipeWire 节点命名规则：bluez_output.<MAC替换冒号为下划线>.a2dp-sink
                    expected_node = f"bluez_output.{mac.replace(':', '_')}.a2dp-sink"
                    print(f"   预期节点: {expected_node}")

                    # 快速验证：检查 pw-dump 中是否存在（即便不存在也可尝试使用）
                    try:
                        data = subprocess.check_output(
                            ["pw-dump"], text=True, timeout=5
                        )
                        nodes = json.loads(data)
                        for obj in nodes:
                            if expected_node in obj.get("props", {}).get(
                                "node.name", ""
                            ):
                                print(f"   ✅ 节点已确认存在")
                                return expected_node
                    except Exception as e:
                        print(f"   pw-dump 验证跳过: {e}")

                    print("   ⚠️ 未通过 pw-dump 验证，但将直接使用构造的节点名")
                    return expected_node
            print("   ❌ 未发现已连接的蓝牙设备")
            return None
        except Exception as e:
            print(f"   bluetoothctl 查询失败: {e}")
            return None

    # ===================== 麦克风增益 =====================
    def _set_mic_gain(self):
        try:
            result = subprocess.run(
                ["amixer", "scontrols"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = result.stdout.splitlines()
            control_name = None
            for line in lines:
                for keyword in ["Mic", "Capture", "PGA", "ADC"]:
                    if keyword in line:
                        match = re.search(r"'([^']+)'", line)
                        if match:
                            control_name = match.group(1)
                            break
                if control_name:
                    break
            if control_name:
                subprocess.run(
                    ["amixer", "sset", control_name, f"{TARGET_MIC_GAIN}%"],
                    check=True,
                    capture_output=True,
                )
                print(f"✅ 麦克风增益: {control_name} = {TARGET_MIC_GAIN}%")
            else:
                print("⚠️ 未找到麦克风增益控件，跳过")
        except Exception as e:
            print(f"⚠️ 设置增益失败: {e}")

    # ===================== 瞬态检测 =====================
    def _is_transient(self, audio_float, prev_rms):
        if not TRANSIENT_DETECTION:
            return False
        cur_rms = np.sqrt(np.mean(audio_float**2))
        if prev_rms > 0:
            energy_jump = cur_rms / (prev_rms + 1e-9)
            if energy_jump > TRANSIENT_ENERGY_RATIO:
                return True
        spectrum = np.abs(np.fft.rfft(audio_float * np.hanning(self.frame_size)))
        total = np.sum(spectrum)
        if total > 0:
            hi = np.sum(spectrum[self.hi_bin_start :])
            if (hi / total) > TRANSIENT_HIGH_FREQ_RATIO:
                return True
        return False

    # ===================== 音频处理主循环 =====================
    def _audio_processing_loop(self):
        print("🎤 音频处理线程启动 (VAD + 瞬态抑制 + DeepFilterNet)")
        level_log_interval = 30
        frame_count = 0
        rms_history = deque([0.0] * 5, maxlen=5)

        try:
            while self.running:
                data = self.stream_in.read(self.frame_size, exception_on_overflow=False)
                audio_int16 = np.frombuffer(data, dtype=np.int16)
                audio_float = audio_int16.astype(np.float32) / 32768.0
                cur_rms = np.sqrt(np.mean(audio_float**2))

                # 更新背景噪声估计
                if cur_rms < self.noise_floor_rms * 1.2:
                    self.noise_floor_rms = (
                        self.noise_floor_alpha * self.noise_floor_rms
                        + (1 - self.noise_floor_alpha) * cur_rms
                    )
                else:
                    self.noise_floor_rms *= 0.999

                # VAD
                vad_speech = True
                if self.vad is not None:
                    try:
                        vad_speech = self.vad.is_speech(
                            audio_int16.tobytes(), self.sample_rate
                        )
                    except Exception:
                        vad_speech = True

                # 能量门限（自适应）
                adaptive_threshold = max(
                    NOISE_GATE_THRESHOLD, self.noise_floor_rms * 2.0
                )
                energy_speech = cur_rms > adaptive_threshold

                # 瞬态检测
                prev_rms = np.mean(rms_history) if rms_history else 0.0
                is_transient = self._is_transient(audio_float, prev_rms)

                is_speech = vad_speech and energy_speech and (not is_transient)
                rms_history.append(cur_rms)

                if DEBUG_AUDIO_LEVELS:
                    frame_count += 1
                    if frame_count % level_log_interval == 0:
                        status = "🔊" if is_speech else "🔇"
                        print(
                            f"   [{status}] RMS={cur_rms:.4f} 门限={adaptive_threshold:.4f} "
                            f"VAD={vad_speech} 瞬态={is_transient}"
                        )

                if not is_speech:
                    silence = np.zeros(self.frame_size, dtype=np.int16)
                    self.pwcat_proc.stdin.write(silence.tobytes())
                    self.pwcat_proc.stdin.flush()
                    continue

                # AI 降噪
                enhanced = enhance(self.df_model, self.df_state, audio_float)
                enhanced_int16 = (enhanced * 32767).clip(-32768, 32767).astype(np.int16)
                self.pwcat_proc.stdin.write(enhanced_int16.tobytes())
                self.pwcat_proc.stdin.flush()
        except Exception as e:
            print(f"音频处理错误: {e}")
        finally:
            print("音频处理线程退出")

    # ===================== 桥接控制 =====================
    def start_bridge(self):
        self._set_mic_gain()

        bt_node = self._find_bt_node()
        if not bt_node:
            print("❌ 未找到蓝牙输出节点，桥接终止")
            return False
        print(f"✅ 蓝牙输出节点: {bt_node}")

        p = pyaudio.PyAudio()
        self.pyaudio_instance = p
        idx, dev = self._find_respeaker_index()
        if idx is None:
            print("❌ 未找到 ReSpeaker 设备")
            p.terminate()
            return False
        print(f"✅ ReSpeaker: {dev['name']} (索引 {idx})")

        try:
            self.stream_in = p.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=idx,
                frames_per_buffer=self.frame_size,
            )
        except Exception as e:
            print(f"❌ 无法打开 ReSpeaker: {e}")
            p.terminate()
            return False

        cmd = [
            "pw-cat",
            "--playback",
            "--rate",
            str(self.sample_rate),
            "--channels",
            str(self.channels),
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
            print(f"✅ pw-cat 已启动 → {bt_node}")
        except Exception as e:
            print(f"❌ pw-cat 启动失败: {e}")
            self.stream_in.close()
            p.terminate()
            return False

        self.running = True
        self.audio_thread = threading.Thread(
            target=self._audio_processing_loop, daemon=True
        )
        self.audio_thread.start()
        print("✅ 桥接已建立！")
        return True

    def stop_bridge(self):
        self.running = False
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=2)
        if self.pwcat_proc and self.pwcat_proc.poll() is None:
            self.pwcat_proc.stdin.close()
            self.pwcat_proc.terminate()
            self.pwcat_proc.wait()
        if self.stream_in:
            self.stream_in.stop_stream()
            self.stream_in.close()
            self.stream_in = None
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()
            self.pyaudio_instance = None
        print("⏹️ 音频桥接已停止")

    def cleanup(self):
        self.stop_bridge()
        if self.agent_process:
            self.agent_process.terminate()
            self.agent_process.wait()


def main():
    print("=" * 50)
    print("树莓派降噪蓝牙麦克风 v4.0")
    print("=" * 50)

    bridge = DenoiseBTBridge(sample_rate=SAMPLE_RATE)
    atexit.register(bridge.cleanup)

    def handle_exit(signum, frame):
        print("\n🛑 正在退出...")
        bridge.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    bridge.init_bluetooth()

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

        print(f"🔗 检测到设备: {current_addr}")
        last_addr = current_addr
        time.sleep(3)

        if not bridge.start_bridge():
            print("⚠️ 桥接建立失败，3 秒后重试...")
            last_addr = None
        else:
            print("🎤 电脑端应能收到纯净语音")


if __name__ == "__main__":
    main()
