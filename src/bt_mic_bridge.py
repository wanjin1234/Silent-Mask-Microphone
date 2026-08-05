#!/usr/bin/env python3
"""
树莓派 ReSpeaker 降噪 → 蓝牙输出到电脑（v2 修复版）
修复内容：
  1. 蓝牙节点检测改用 pactl list sinks short + pw-cli 双重回退
  2. 增加 VAD（语音活动检测）+ 噪声门限，抑制回声和底噪
  3. 增加麦克风增益调节（amixer），从源头降低底噪
  4. 增加音频电平监控（调试模式），便于排查
"""

import atexit
import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time

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
FRAME_SIZE = 512  # @16kHz = 32ms
CHANNELS = 1
BT_DEVICE_NAME = "RaspberryPi-Mic"

# 噪声门限参数（可通过命令行或此处调优）
NOISE_GATE_THRESHOLD = 0.005  # RMS 能量阈值，低于此值静音（0~1，越小越敏感）
VAD_AGGRESSIVENESS = 2  # WebRTC VAD 激进程度 0~3，越大越严格
ENABLE_VAD = True  # 是否启用 VAD（需 pip install webrtcvad）
DEBUG_AUDIO_LEVELS = True  # 是否打印音频电平（调试用）

# ReSpeaker 麦克风增益（amixer 控件名，根据实际型号调整）
RESPEAKER_AMIXER_CONTROL = "Mic"  # 常见：Mic / Capture / PCM
RESPEAKER_AMIXER_VALUE = 60  # 0~100，建议从 50 起步调优


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

        # ---------- VAD 初始化 ----------
        self.vad = None
        if ENABLE_VAD:
            try:
                import webrtcvad

                self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
                # VAD 要求 10/20/30ms 帧，这里用 20ms = 320 样本 @16kHz
                self.vad_frame_size = int(sample_rate * 0.02)  # 320
                print(f"✅ WebRTC VAD 已启用 (激进度={VAD_AGGRESSIVENESS})")
            except ImportError:
                print("⚠️ 未安装 webrtcvad，VAD 禁用。安装: pip install webrtcvad")
                self.vad = None

        # ---------- 加载 DeepFilterNet ----------
        print("正在加载 DeepFilterNet2_ll 降噪模型...")
        model_path = os.path.join(
            os.path.dirname(__file__),
            "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2",
        )
        self.df_model, self.df_state, _ = init_df(model_path)
        print("✅ 模型加载完成")

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

    # ===================== 音频设备识别（修复重点） =====================
    def _find_respeaker_index(self):
        """在 PyAudio 中查找 ReSpeaker 输入设备"""
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

    def _find_bt_node_pactl(self):
        """方法一：通过 pactl list sinks short 查找蓝牙输出节点（最可靠）"""
        try:
            result = subprocess.run(
                ["pactl", "list", "sinks", "short"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                # 格式: <index>\t<name>\t<driver>\t<sample_spec>\t<state>
                if "bluez_output" in line or "bluez_sink" in line:
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        print(f"   [pactl] 找到蓝牙 sink: {parts[1]}")
                        return parts[1]
        except Exception as e:
            print(f"   [pactl] 查询失败: {e}")
        return None

    def _find_bt_node_pwcli(self):
        """方法二：通过 pw-cli ls Node 查找（回退方案）"""
        try:
            result = subprocess.run(
                ["pw-cli", "ls", "Node"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                if "bluez_output" in line or "bluez_sink" in line:
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        print(f"   [pw-cli] 找到蓝牙节点: {match.group(1)}")
                        return match.group(1)
        except Exception as e:
            print(f"   [pw-cli] 查询失败: {e}")
        return None

    def _find_bt_node_pwdump(self):
        """方法三：通过 pw-dump JSON 解析（最全面，但较重）"""
        try:
            result = subprocess.run(
                ["pw-dump"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            data = json.loads(result.stdout)
            for obj in data:
                props = obj.get("props", {})
                node_name = props.get("node.name", "")
                if "bluez_output" in node_name or "bluez_sink" in node_name:
                    print(f"   [pw-dump] 找到蓝牙节点: {node_name}")
                    return node_name
        except Exception as e:
            print(f"   [pw-dump] 查询失败: {e}")
        return None

    def _find_bt_node(self):
        """
        多重回退策略查找蓝牙输出节点。
        增加重试次数至 20 次（约 20 秒），适应蓝牙 profile 协商延迟。
        """
        print("🔍 正在查找蓝牙输出节点...")
        for attempt in range(20):
            # 方法一：pactl（最快最准）
            node = self._find_bt_node_pactl()
            if node:
                return node

            # 方法二：pw-cli
            node = self._find_bt_node_pwcli()
            if node:
                return node

            # 方法三：pw-dump（深度搜索）
            node = self._find_bt_node_pwdump()
            if node:
                return node

            # 诊断信息（每 5 次输出一次，避免刷屏）
            if attempt % 5 == 0 and attempt > 0:
                print(f"   ⏳ 已等待 {attempt} 秒，仍未找到蓝牙节点，继续重试...")
                print(
                    f"   提示：请确保电脑已连接到此树莓派蓝牙，并且音频 profile 已激活"
                )
                print(f"   可手动验证: pactl list sinks short | grep bluez")

            time.sleep(1)

        print("❌ 经过 20 次重试仍未找到蓝牙输出节点")
        print("   请手动运行以下命令排查：")
        print("   1. bluetoothctl devices Connected    # 确认设备已连接")
        print("   2. pactl list sinks short             # 查看所有音频输出")
        print("   3. pw-cli ls Node | grep bluez         # 查看 PipeWire 节点")
        return None

    # ===================== 麦克风增益调节 =====================
    def _set_mic_gain(self):
        """通过 amixer 调节 ReSpeaker 麦克风模拟增益，从源头降低底噪"""
        try:
            # 列出所有控件，查找麦克风相关
            result = subprocess.run(
                ["amixer", "scontrols"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            controls = result.stdout

            # 尝试多个常见的 ReSpeaker 控件名
            candidates = [RESPEAKER_AMIXER_CONTROL, "Mic", "Capture", "PGA", "ADC"]
            found = None
            for ctrl in candidates:
                if ctrl in controls:
                    found = ctrl
                    break

            if found:
                subprocess.run(
                    ["amixer", "sset", found, f"{RESPEAKER_AMIXER_VALUE}%"],
                    check=True,
                    capture_output=True,
                )
                print(f"✅ 麦克风增益已设置: {found} = {RESPEAKER_AMIXER_VALUE}%")
            else:
                print(f"⚠️ 未找到麦克风增益控件，可用控件列表:\n{controls}")
        except Exception as e:
            print(f"⚠️ 设置麦克风增益失败: {e}")

    # ===================== VAD + 降噪处理线程 =====================
    def _is_speech_vad(self, audio_int16):
        """使用 WebRTC VAD 判断是否为语音"""
        if self.vad is None:
            return True  # 无 VAD 时默认全部通过
        # VAD 需要 20ms 帧
        frame_bytes = audio_int16.tobytes()
        try:
            return self.vad.is_speech(frame_bytes, self.sample_rate)
        except Exception:
            return True

    def _is_speech_energy(self, audio_float, threshold=NOISE_GATE_THRESHOLD):
        """基于能量的简单噪声门限（回退方案）"""
        rms = np.sqrt(np.mean(audio_float**2))
        return rms > threshold

    def _audio_processing_loop(self):
        """实时采集 → VAD/噪声门限 → DeepFilterNet 降噪 → 蓝牙"""
        print("🎤 音频处理线程启动（VAD + DeepFilterNet）")
        level_log_interval = 50  # 每 50 帧输出一次电平
        frame_count = 0

        try:
            while self.running:
                # 1. 读取一帧原始 PCM
                data = self.stream_in.read(self.frame_size, exception_on_overflow=False)
                audio_int16 = np.frombuffer(data, dtype=np.int16)
                audio_float = audio_int16.astype(np.float32) / 32768.0

                # 2. 噪声门限 / VAD 检测（减少回声和底噪）
                is_speech = True
                if self.vad is not None:
                    is_speech = self._is_speech_vad(audio_int16)
                else:
                    is_speech = self._is_speech_energy(audio_float)

                # 调试：周期性地输出音频电平
                if DEBUG_AUDIO_LEVELS:
                    frame_count += 1
                    if frame_count % level_log_interval == 0:
                        rms = np.sqrt(np.mean(audio_float**2))
                        status = "🔊 语音" if is_speech else "🔇 静音"
                        print(
                            f"   [{status}] RMS={rms:.4f} (阈值={NOISE_GATE_THRESHOLD})"
                        )

                if not is_speech:
                    # 非语音：输出静音帧（避免传输底噪和回声）
                    silence = np.zeros(self.frame_size, dtype=np.int16)
                    self.pwcat_proc.stdin.write(silence.tobytes())
                    self.pwcat_proc.stdin.flush()
                    continue

                # 3. DeepFilterNet AI 降噪（仅对语音帧处理，节省 CPU）
                enhanced_float = enhance(self.df_model, self.df_state, audio_float)

                # 4. 转回 int16 并输出
                enhanced_int16 = (
                    (enhanced_float * 32767).clip(-32768, 32767).astype(np.int16)
                )
                self.pwcat_proc.stdin.write(enhanced_int16.tobytes())
                self.pwcat_proc.stdin.flush()
        except Exception as e:
            print(f"音频处理错误: {e}")
        finally:
            print("音频处理线程退出")

    # ===================== 桥接控制 =====================
    def start_bridge(self):
        """建立降噪桥接"""
        # 0. 设置麦克风增益
        self._set_mic_gain()

        # 1. 查找蓝牙节点
        bt_node = self._find_bt_node()
        if not bt_node:
            print("❌ 未找到蓝牙输出节点")
            return False
        print(f"✅ 蓝牙输出节点: {bt_node}")

        # 2. 打开 ReSpeaker 麦克风
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
                channels=CHANNELS,
                rate=self.sample_rate,
                input=True,
                input_device_index=idx,
                frames_per_buffer=self.frame_size,
            )
        except Exception as e:
            print(f"❌ 无法打开 ReSpeaker: {e}")
            p.terminate()
            return False

        # 3. 启动 pw-cat
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
            print(f"✅ pw-cat 已启动 → {bt_node}")
        except Exception as e:
            print(f"❌ pw-cat 启动失败: {e}")
            self.stream_in.close()
            p.terminate()
            return False

        # 4. 启动处理线程
        self.running = True
        self.audio_thread = threading.Thread(
            target=self._audio_processing_loop, daemon=True
        )
        self.audio_thread.start()

        print("✅ AI 降噪桥接已建立！")
        if ENABLE_VAD:
            print("   💡 VAD 已启用：仅语音时传输，可有效减少回声传播")
        else:
            print("   💡 噪声门限已启用：低于阈值的信号将被静音")
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
    print("树莓派降噪蓝牙麦克风 v2（VAD + DeepFilterNet）")
    print("=" * 50)

    bridge = DenoiseBTBridge(sample_rate=SAMPLE_RATE, frame_size=FRAME_SIZE)
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
        time.sleep(3)  # 等待 PipeWire 创建蓝牙节点

        if not bridge.start_bridge():
            print("⚠️ 桥接建立失败，下次循环重试...")
            last_addr = None
        else:
            print("🎤 电脑端现在应能收到降噪后的麦克风信号")


if __name__ == "__main__":
    main()
