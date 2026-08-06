#!/usr/bin/env python3
"""
树莓派 ReSpeaker 降噪 → 蓝牙输出到电脑 v5.2
修复 v5.1 找不到 ReSpeaker 的问题：
  - 不再依赖 PyAudio 宿主内索引，直接全局枚举
  - 每次查找时新建独立 PyAudio 实例，避免状态污染
  - 打印完整设备列表用于诊断
  - 修复管道错误计数器的作用域问题
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
import warnings
from collections import deque

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

try:
    from df import enhance, init_df
except ImportError:
    print("请先安装 deepfilternet：pip install deepfilternet")
    sys.exit(1)

import numpy as np
import pyaudio
import torch

# ====== 可调参数 ======
RESPEAKER_DEVICE_KEYWORD = "seeed"  # 设备名中包含此关键词
SAMPLE_RATE = 16000
CHANNELS = 1  # 单声道
BT_DEVICE_NAME = "RaspberryPi-Mic"

NOISE_GATE_THRESHOLD = 0.005
VAD_AGGRESSIVENESS = 2
ENABLE_VAD = True
TRANSIENT_DETECTION = True
TRANSIENT_ENERGY_RATIO = 3.0
TRANSIENT_HIGH_FREQ_RATIO = 0.6
DEBUG_AUDIO_LEVELS = True
TARGET_MIC_GAIN = 30


def _run_cmd(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


class DenoiseBTBridge:
    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.vad_frame_size = 320
        self.frame_size = self.vad_frame_size
        self.channels = CHANNELS

        self.agent_process = None
        self.pyaudio_instance = None  # 用于打开音频流
        self.stream_in = None
        self.pwcat_proc = None
        self.running = True
        self.audio_thread = None
        self.bt_node = None
        self.respeaker_idx = None

        self.noise_floor_rms = 0.0
        self.noise_floor_alpha = 0.95
        self._consecutive_pipe_errors = 0  # 实例级管道错误计数器

        # VAD
        self.vad = None
        if ENABLE_VAD:
            try:
                import webrtcvad

                self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
                print(f"✅ WebRTC VAD 已启用 (激进度={VAD_AGGRESSIVENESS})")
            except ImportError:
                print("⚠️ webrtcvad 不可用，VAD 将被禁用")
                self.vad = None

        # DeepFilterNet
        print("正在加载 DeepFilterNet2_ll 降噪模型...")
        model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
        self.df_model, self.df_state, _ = init_df(model_path)
        print("✅ 模型加载完成")
        self.hi_bin_start = int(4000 / (sample_rate / self.frame_size))

    # ==================== 蓝牙管理 ====================
    def start_agent(self):
        subprocess.run(["killall", "bt-agent"], capture_output=True, check=False)
        if subprocess.run(["which", "bt-agent"], capture_output=True).returncode != 0:
            print("❌ bt-agent 未安装，请执行: sudo apt install bluez-tools")
            return
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
        time.sleep(3)
        subprocess.run(["sudo", "rfkill", "unblock", "bluetooth"], check=False)
        subprocess.run(["bluetoothctl", "system-alias", BT_DEVICE_NAME], check=False)

        print("⏳ 等待蓝牙上电...")
        for attempt in range(20):
            show = _run_cmd(["bluetoothctl", "show"])
            if "Powered: yes" in show:
                print("✅ 蓝牙已上电")
                break
            subprocess.run(
                ["bluetoothctl", "power", "on"],
                check=False,
                capture_output=True,
                timeout=5,
            )
            if attempt % 5 == 0:
                print(f"   重试 {attempt + 1}/20 ...")
            time.sleep(2)
        else:
            print("⚠️ 蓝牙未能上电，继续尝试...")

        subprocess.run(
            ["bluetoothctl", "discoverable", "on"], check=False, capture_output=True
        )
        subprocess.run(
            ["bluetoothctl", "pairable", "on"], check=False, capture_output=True
        )
        print(f"✅ 蓝牙初始化完成（名称: {BT_DEVICE_NAME}）")
        self.start_agent()

    # ==================== 音频设备查找（彻底修复） ====================
    def _find_respeaker_index(self):
        """
        每次调用都创建全新的 PyAudio 实例，全局枚举所有设备，
        打印完整列表，查找 ReSpeaker。
        """
        print("🔍 全局扫描音频设备（新建 PyAudio 实例）...")
        p = pyaudio.PyAudio()
        found = []
        target_idx = None
        for i in range(p.get_device_count()):
            try:
                dev = p.get_device_info_by_index(i)
                name = dev["name"]
                in_ch = dev["maxInputChannels"]
                found.append(f"  [{i}] {name} (in={in_ch})")
                if RESPEAKER_DEVICE_KEYWORD in name.lower() and in_ch > 0:
                    print(f"   ✅ 找到 ReSpeaker: {name} (索引 {i})")
                    target_idx = i
            except Exception as e:
                found.append(f"  [{i}] 查询异常: {e}")

        # 打印所有设备列表方便诊断
        print("📋 全部设备列表:")
        for line in found:
            print(line)

        p.terminate()
        return target_idx

    def _wait_a2dp_ready(self, mac, timeout=15):
        print("⏳ 等待 A2DP 协议激活...")
        start = time.time()
        while time.time() - start < timeout:
            out = _run_cmd(["pactl", "list", "cards"], timeout=5)
            if "a2dp" in out.lower() and mac.replace(":", "_") in out.replace(":", "_"):
                print("✅ A2DP 已激活 (pactl)")
                return True
            info = _run_cmd(["bluetoothctl", "info", mac], timeout=5)
            if "Audio Sink" in info or "A2DP" in info:
                print("✅ A2DP 已激活 (bluetoothctl)")
                return True
            time.sleep(1.5)
        print("⚠️ A2DP 等待超时，仍尝试查找...")
        return False

    def _find_bt_node(self):
        print("🔍 查找蓝牙输出节点...")
        mac = None
        out = _run_cmd(["bluetoothctl", "devices", "Connected"], timeout=5)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device":
                mac = parts[1]
                break
        if mac:
            print(f"   已连接设备 MAC: {mac}")
            self._wait_a2dp_ready(mac)

        for _ in range(15):
            try:
                data = subprocess.check_output(["pw-dump"], text=True, timeout=5)
                try:
                    nodes = json.loads(data)
                    for obj in nodes:
                        props = obj.get("props", {})
                        name = props.get("node.name", "")
                        if (
                            "bluez_output" in name or "bluez_sink" in name
                        ) and "a2dp" in name.lower():
                            print(f"   ✅ 找到蓝牙节点: {name}")
                            return name
                    for obj in nodes:
                        props = obj.get("props", {})
                        name = props.get("node.name", "")
                        if "bluez_output" in name or "bluez_sink" in name:
                            print(f"   ✅ 找到蓝牙节点: {name}")
                            return name
                except json.JSONDecodeError:
                    pass
                matches = re.findall(r'"node\.name":\s*"(bluez_output[^"]+)"', data)
                if matches:
                    preferred = [m for m in matches if "a2dp" in m or "sink" in m]
                    node = (preferred or matches)[0]
                    print(f"   ✅ 找到蓝牙节点: {node}")
                    return node
            except Exception as e:
                print(f"   pw-dump 失败: {e}")
            time.sleep(1)

        print("   尝试 pw-cli ...")
        out = _run_cmd(["pw-cli", "ls", "Node"], timeout=5)
        matches = re.findall(r'node\.name\s*=\s*"(bluez_output[^"]+)"', out)
        if matches:
            preferred = [m for m in matches if "a2dp" in m]
            node = (preferred or matches)[0]
            print(f"   ✅ 找到蓝牙节点: {node}")
            return node

        if mac:
            for suffix in [".1", ".a2dp-sink", ".2", ""]:
                candidate = f"bluez_output.{mac.replace(':', '_')}{suffix}"
                info = _run_cmd(["pw-cli", "info", candidate], timeout=3)
                if "node.name" in info:
                    print(f"   ✅ 找到蓝牙节点: {candidate}")
                    return candidate

        print("❌ 未找到蓝牙输出节点")
        return None

    # ==================== 麦克风增益 ====================
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
            keywords = [
                "Mic",
                "Capture",
                "PGA",
                "ADC",
                "PCM",
                "Digital",
                "Master",
                "Line",
            ]
            for line in lines:
                for kw in keywords:
                    if kw in line:
                        match = re.search(r"'([^']+)'", line)
                        if match:
                            control_name = match.group(1)
                            if "Playback" not in line:
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
                print("⚠️ 未找到麦克风增益控件")
        except Exception as e:
            print(f"⚠️ 设置增益失败: {e}")

    # ==================== 瞬态检测 ====================
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

    # ==================== pw-cat 管理 ====================
    def _ensure_pwcat_running(self):
        if self.pwcat_proc is not None and self.pwcat_proc.poll() is not None:
            _, err = self.pwcat_proc.communicate()
            print(f"⚠️ pw-cat 退出 (码={self.pwcat_proc.returncode})")
            if err:
                print(f"   stderr: {err.decode()}")
            self.pwcat_proc = None

        if self.pwcat_proc is None and self.bt_node:
            print("🔄 启动 pw-cat ...")
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
                self.bt_node,
                "-",
            ]
            try:
                self.pwcat_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                time.sleep(1.5)
                if self.pwcat_proc.poll() is not None:
                    _, err = self.pwcat_proc.communicate()
                    print(f"❌ pw-cat 启动失败: {err.decode() if err else '未知'}")
                    self.pwcat_proc = None
                    return False
                print("✅ pw-cat 已启动")
                return True
            except Exception as e:
                print(f"❌ pw-cat 异常: {e}")
                self.pwcat_proc = None
                return False
        return self.pwcat_proc is not None

    # ==================== 音频处理主循环 ====================
    def _audio_processing_loop(self):
        print("🎤 音频处理线程启动")
        level_log_interval = 30
        frame_count = 0
        rms_history = deque([0.0] * 5, maxlen=5)

        try:
            while self.running:
                if not self._ensure_pwcat_running():
                    time.sleep(2)
                    continue

                data = self.stream_in.read(self.frame_size, exception_on_overflow=False)
                audio_int16 = np.frombuffer(data, dtype=np.int16)
                audio_float = audio_int16.astype(np.float32) / 32768.0
                cur_rms = np.sqrt(np.mean(audio_float**2))

                if cur_rms < self.noise_floor_rms * 1.2:
                    self.noise_floor_rms = (
                        self.noise_floor_alpha * self.noise_floor_rms
                        + (1 - self.noise_floor_alpha) * cur_rms
                    )
                else:
                    self.noise_floor_rms *= 0.999

                vad_speech = True
                if self.vad:
                    try:
                        vad_speech = self.vad.is_speech(
                            audio_int16.tobytes(), self.sample_rate
                        )
                    except Exception:
                        vad_speech = True

                adaptive_threshold = max(
                    NOISE_GATE_THRESHOLD, self.noise_floor_rms * 2.0
                )
                energy_speech = cur_rms > adaptive_threshold

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
                    self._write_to_pwcat(silence.tobytes())
                    continue

                # 降噪处理
                audio_tensor = torch.from_numpy(audio_float).unsqueeze(0)
                enhanced_tensor = enhance(self.df_model, self.df_state, audio_tensor)
                if isinstance(enhanced_tensor, torch.Tensor):
                    enhanced_float = enhanced_tensor.squeeze(0).numpy()
                else:
                    enhanced_float = np.asarray(enhanced_tensor).flatten()

                if len(enhanced_float) > self.frame_size:
                    enhanced_float = enhanced_float[: self.frame_size]
                elif len(enhanced_float) < self.frame_size:
                    enhanced_float = np.pad(
                        enhanced_float, (0, self.frame_size - len(enhanced_float))
                    )

                enhanced_int16 = (
                    (enhanced_float * 32767).clip(-32768, 32767).astype(np.int16)
                )
                self._write_to_pwcat(enhanced_int16.tobytes())
        except Exception as e:
            print(f"音频处理错误: {e}")
            import traceback

            traceback.print_exc()
        finally:
            print("音频处理线程退出")

    def _write_to_pwcat(self, data_bytes):
        """写入 pw-cat 并处理管道错误"""
        try:
            if self.pwcat_proc and self.pwcat_proc.poll() is None:
                self.pwcat_proc.stdin.write(data_bytes)
                self.pwcat_proc.stdin.flush()
                self._consecutive_pipe_errors = 0
        except (BrokenPipeError, OSError):
            self.pwcat_proc = None
            self._consecutive_pipe_errors += 1
            if self._consecutive_pipe_errors > 5:
                self.running = False

    # ==================== 桥接控制 ====================
    def start_bridge(self):
        self._set_mic_gain()

        self.bt_node = self._find_bt_node()
        if not self.bt_node:
            print("❌ 未找到蓝牙输出节点，桥接失败")
            return False

        # 新建 PyAudio 实例用于音频采集（之前若有则先停掉）
        if self.pyaudio_instance:
            self.pyaudio_instance.terminate()
            self.pyaudio_instance = None

        # 查找 ReSpeaker（使用新的独立实例）
        idx = self._find_respeaker_index()
        if idx is None:
            print("❌ 未找到 ReSpeaker 设备")
            print(
                "   请检查: 1) 驱动安装  2) /boot/config.txt 禁用 1-Wire  3) arecord -l 输出"
            )
            return False

        # 用找到的索引打开音频流
        try:
            self.pyaudio_instance = pyaudio.PyAudio()
            self.stream_in = self.pyaudio_instance.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=idx,
                frames_per_buffer=self.frame_size,
            )
        except Exception as e:
            print(f"❌ 无法打开 ReSpeaker 音频流: {e}")
            if self.pyaudio_instance:
                self.pyaudio_instance.terminate()
                self.pyaudio_instance = None
            return False

        if not self._ensure_pwcat_running():
            print("❌ 未能启动 pw-cat 播放")
            self.stream_in.close()
            self.stream_in = None
            self.pyaudio_instance.terminate()
            self.pyaudio_instance = None
            return False

        self.running = True
        self._consecutive_pipe_errors = 0
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
            try:
                self.pwcat_proc.stdin.close()
            except Exception:
                pass
            self.pwcat_proc.terminate()
            try:
                self.pwcat_proc.wait(timeout=2)
            except Exception:
                self.pwcat_proc.kill()
            self.pwcat_proc = None
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
            try:
                self.agent_process.wait(timeout=2)
            except Exception:
                pass


def main():
    print("=" * 50)
    print("树莓派降噪蓝牙麦克风 v5.2")
    print("=" * 50)

    # 确保 PipeWire 用户服务运行
    if subprocess.run(["pgrep", "pipewire"], capture_output=True).returncode != 0:
        print("⚠️ PipeWire 未运行，尝试启动...")
        subprocess.run(
            ["systemctl", "--user", "start", "pipewire", "wireplumber"], check=False
        )
        time.sleep(2)

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
    fail_count = 0
    while True:
        try:
            res = subprocess.run(
                ["bluetoothctl", "devices", "Connected"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = res.stdout.splitlines()
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
                fail_count = 0
            time.sleep(3)
            continue

        if current_addr != last_addr or fail_count > 0:
            if current_addr != last_addr:
                print(f"🔗 检测到新设备: {current_addr}")
                bridge.stop_bridge()
                last_addr = current_addr
                fail_count = 0
                time.sleep(3)

            if bridge.start_bridge():
                print("🎤 电脑端应能收到纯净语音")
                fail_count = 0
            else:
                fail_count += 1
                backoff = min(fail_count * 5, 30)
                print(f"⚠️ 桥接建立失败 (第 {fail_count} 次)，{backoff} 秒后重试...")
                bridge.stop_bridge()
                time.sleep(backoff)
        else:
            time.sleep(3)


if __name__ == "__main__":
    main()
