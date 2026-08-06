#!/usr/bin/env python3
"""
树莓派 ReSpeaker 降噪 → 蓝牙输出 v6.0
完全基于 PipeWire，避免 ALSA 设备冲突。
使用管道: pw-cat --record -> Python 降噪 -> pw-cat --playback
"""

import argparse
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

warnings.filterwarnings("ignore")

try:
    from df import enhance, init_df
except ImportError:
    print("请先安装 deepfilternet：pip install deepfilternet")
    sys.exit(1)

import numpy as np
import torch

# ====== 可调参数 ======
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 320  # 20ms @ 16kHz
BT_DEVICE_NAME = "RaspberryPi-Mic"

NOISE_GATE_THRESHOLD = 0.005
VAD_AGGRESSIVENESS = 2
ENABLE_VAD = True
DEBUG_LEVELS = False  # 设为 True 可查看音频电平


def run_cmd(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


class PipeWireDenoiseBridge:
    def __init__(self):
        # 降噪模型
        print("加载 DeepFilterNet2 模型...")
        model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
        self.df_model, self.df_state, _ = init_df(model_path)
        print("✅ 模型加载完成")

        # VAD
        self.vad = None
        if ENABLE_VAD:
            try:
                import webrtcvad

                self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
            except ImportError:
                pass

        # 进程管理
        self.rec_proc = None  # pw-cat --record
        self.play_proc = None  # pw-cat --playback
        self.agent_proc = None
        self.running = True
        self.bt_sink = None
        self.respeaker_source = None

    # ==================== 蓝牙初始化 ====================
    def _init_bluetooth(self):
        print("🔵 初始化蓝牙...")
        subprocess.run(
            ["sudo", "systemctl", "restart", "bluetooth"],
            check=False,
            capture_output=True,
        )
        time.sleep(3)
        subprocess.run(
            ["sudo", "rfkill", "unblock", "bluetooth"], check=False, capture_output=True
        )
        subprocess.run(
            ["bluetoothctl", "system-alias", BT_DEVICE_NAME],
            check=False,
            capture_output=True,
        )

        for _ in range(10):
            if "Powered: yes" in run_cmd(["bluetoothctl", "show"]):
                print("✅ 蓝牙已上电")
                break
            subprocess.run(
                ["bluetoothctl", "power", "on"], check=False, capture_output=True
            )
            time.sleep(2)

        subprocess.run(
            ["bluetoothctl", "discoverable", "on"], check=False, capture_output=True
        )
        subprocess.run(
            ["bluetoothctl", "pairable", "on"], check=False, capture_output=True
        )

        # bt-agent
        subprocess.run(["killall", "bt-agent"], capture_output=True, check=False)
        if subprocess.run(["which", "bt-agent"], capture_output=True).returncode == 0:
            self.agent_proc = subprocess.Popen(
                ["bt-agent", "-c", "NoInputNoOutput"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("✅ bt-agent 已启动")
        else:
            print("⚠️  bt-agent 未安装 (sudo apt install bluez-tools)")

    # ==================== 节点查找 ====================
    def _find_bt_sink(self):
        """查找蓝牙输出 sink（优先 pactl）"""
        # 等待 A2DP
        mac = None
        out = run_cmd(["bluetoothctl", "devices", "Connected"])
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device":
                mac = parts[1]
                break
        if mac:
            print(f"   已连接 MAC: {mac}")
            # 等待 A2DP 配置
            for _ in range(10):
                if "a2dp" in run_cmd(["pactl", "list", "cards"], 5).lower():
                    break
                time.sleep(1)

        # pactl sinks
        out = run_cmd(["pactl", "list", "short", "sinks"])
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and "bluez" in parts[1]:
                name = parts[1]
                print(f"   ✅ 蓝牙 sink: {name}")
                return name

        # pw-cli 备用
        out = run_cmd(["pw-cli", "ls", "Node"])
        m = re.findall(r'node\.name\s*=\s*"(bluez[^"]+)"', out)
        for name in m:
            if "output" in name or "sink" in name:
                print(f"   ✅ 蓝牙 sink (pw-cli): {name}")
                return name

        print("❌ 未找到蓝牙输出 sink")
        print("   请确保电脑已连接并选择为音频输出设备")
        return None

    def _find_respeaker_source(self):
        """查找 ReSpeaker 输入节点（PipeWire）"""
        out = run_cmd(["pw-cli", "ls", "Node"])
        # 尝试多种正则
        patterns = [
            rf'node\.description\s*=\s*"({re.escape("seeed2micvoicec")}[^"]*)"',
            rf'node\.name\s*=\s*"(alsa_input[^"]*seeed[^"]*)"',
            rf'node\.name\s*=\s*"(alsa_input[^"]*tlv320aic3x[^"]*)"',
            rf'node\.description\s*=\s*"(.*ReSpeaker.*)"',
        ]
        for pat in patterns:
            m = re.search(pat, out, re.IGNORECASE)
            if m:
                name = m.group(1)
                # 需要取 node.name 而不是 description
                # 若匹配到 description，需要再找到对应的 node.name
                # 简化：我们已经匹配到语句，可模糊提取 node.name
                # 这里改进：直接获取所有 node.name 并判断是否包含关键词
                pass

        # 更简单的方法：用 pactl sources
        out = run_cmd(["pactl", "list", "short", "sources"])
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and "seeed" in parts[1].lower():
                name = parts[1]
                print(f"   ✅ ReSpeaker 输入源: {name}")
                return name

        # 最后用 pw-cli 列出所有节点，选第一个包含 seeed 的
        lines = out.splitlines()
        collect = {}
        current = None
        for line in lines:
            if "node.name" in line:
                m = re.search(r'"([^"]+)"', line)
                if m:
                    current = m.group(1)
                    collect[current] = ""
            elif "node.description" in line and current:
                desc = re.search(r'"([^"]+)"', line)
                if desc:
                    collect[current] = desc.group(1)
        for name, desc in collect.items():
            if "seeed" in desc.lower() or "seeed" in name.lower():
                print(f"   ✅ ReSpeaker 节点: {name}")
                return name

        print("❌ 未找到 ReSpeaker 输入节点")
        print("   请检查驱动：arecord -l 及 pw-cli ls Node | grep -i seeed")
        return None

    # ==================== 音频处理核心 ====================
    def _start_pipewire_capture(self, source):
        """启动 pw-cat --record，捕获指定源，输出到 stdout"""
        cmd = [
            "pw-cat",
            "--record",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            str(CHANNELS),
            "--format",
            "s16",
            "--target",
            source,
            "-",
        ]
        print(f"🔄 启动录制: {' '.join(cmd)}")
        try:
            self.rec_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            # 等待开始
            time.sleep(1)
            if self.rec_proc.poll() is not None:
                err = self.rec_proc.stderr.read().decode()
                print(f"❌ pw-cat 录制失败: {err}")
                return False
            return True
        except Exception as e:
            print(f"❌ 无法启动录制: {e}")
            return False

    def _start_pipewire_playback(self, sink):
        """启动 pw-cat --playback，输出到蓝牙 sink"""
        cmd = [
            "pw-cat",
            "--playback",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            str(CHANNELS),
            "--format",
            "s16",
            "--target",
            sink,
            "-",
        ]
        print(f"🔄 启动播放: {' '.join(cmd)}")
        try:
            self.play_proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE
            )
            time.sleep(1)
            if self.play_proc.poll() is not None:
                err = self.play_proc.stderr.read().decode()
                print(f"❌ pw-cat 播放失败: {err}")
                return False
            return True
        except Exception as e:
            print(f"❌ 无法启动播放: {e}")
            return False

    def _process_loop(self):
        """读取录制数据，降噪，写入播放"""
        print("🎤 开始降噪处理...")
        frame_bytes = FRAME_SIZE * CHANNELS * 2  # s16 = 2 bytes
        rms_history = deque([0.0] * 5, maxlen=5)
        consecutive_errors = 0

        while self.running:
            # 检查进程健康
            if self.rec_proc and self.rec_proc.poll() is not None:
                print("录制进程退出，尝试重启...")
                if not self._start_pipewire_capture(self.respeaker_source):
                    break
            if self.play_proc and self.play_proc.poll() is not None:
                print("播放进程退出，尝试重启...")
                if not self._start_pipewire_playback(self.bt_sink):
                    break

            try:
                raw = self.rec_proc.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break  # EOF
            except Exception:
                break

            # 转为浮点
            audio_int = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            cur_rms = np.sqrt(np.mean(audio_int**2))

            # 简单 VAD
            is_speech = True
            if self.vad:
                try:
                    is_speech = self.vad.is_speech(raw, SAMPLE_RATE)
                except:
                    pass
            if cur_rms < NOISE_GATE_THRESHOLD:
                is_speech = False

            if not is_speech:
                silence = np.zeros(FRAME_SIZE, dtype=np.int16)
                self._write_playback(silence.tobytes())
                continue

            # DeepFilterNet 降噪
            audio_tensor = torch.from_numpy(audio_int).unsqueeze(0)
            enhanced = enhance(self.df_model, self.df_state, audio_tensor)
            if isinstance(enhanced, torch.Tensor):
                enhanced = enhanced.squeeze(0).numpy()
            else:
                enhanced = np.asarray(enhanced).flatten()
            if len(enhanced) > FRAME_SIZE:
                enhanced = enhanced[:FRAME_SIZE]
            elif len(enhanced) < FRAME_SIZE:
                enhanced = np.pad(enhanced, (0, FRAME_SIZE - len(enhanced)))

            enhanced_int = (enhanced * 32767).clip(-32768, 32767).astype(np.int16)
            self._write_playback(enhanced_int.tobytes())

        print("处理循环退出")

    def _write_playback(self, data):
        try:
            self.play_proc.stdin.write(data)
            self.play_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            print("播放管道断开")
            self.play_proc = None

    def start_bridge(self):
        # 查找节点
        self.bt_sink = self._find_bt_sink()
        if not self.bt_sink:
            return False
        self.respeaker_source = self._find_respeaker_source()
        if not self.respeaker_source:
            return False

        # 启动录制和播放
        if not self._start_pipewire_capture(self.respeaker_source):
            return False
        if not self._start_pipewire_playback(self.bt_sink):
            return False

        # 启动处理线程
        self.proc_thread = threading.Thread(target=self._process_loop, daemon=True)
        self.proc_thread.start()
        print("✅ 桥接成功！降噪音频正在发送到电脑。")
        return True

    def stop_bridge(self):
        self.running = False
        if self.rec_proc:
            self.rec_proc.terminate()
        if self.play_proc:
            self.play_proc.terminate()
        if self.agent_proc:
            self.agent_proc.terminate()
        print("🛑 桥接已停止")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bt-sink", help="手动指定蓝牙 sink 名称")
    parser.add_argument("--source", help="手动指定 ReSpeaker 输入源名称")
    args = parser.parse_args()

    bridge = PipeWireDenoiseBridge()
    atexit.register(bridge.stop_bridge)
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    # 蓝牙初始化
    bridge._init_bluetooth()

    # 如果手动指定节点，覆盖查找
    if args.bt_sink:
        bridge.bt_sink = args.bt_sink
    if args.source:
        bridge.respeaker_source = args.source

    last_addr = None
    fail_count = 0
    while True:
        out = run_cmd(["bluetoothctl", "devices", "Connected"])
        current_addr = None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device":
                current_addr = parts[1]
                break

        if not current_addr:
            if last_addr:
                print(f"🔌 设备 {last_addr} 断开")
                bridge.stop_bridge()
                last_addr = None
            time.sleep(3)
            continue

        if current_addr != last_addr or fail_count > 0:
            if current_addr != last_addr:
                print(f"🔗 新连接: {current_addr}")
                bridge.stop_bridge()
                last_addr = current_addr
                fail_count = 0
                time.sleep(3)

            if bridge.start_bridge():
                fail_count = 0
                # 保持运行，直到断开
                while last_addr == current_addr:
                    time.sleep(2)
                    out2 = run_cmd(["bluetoothctl", "devices", "Connected"])
                    if current_addr not in out2:
                        break
                print("设备断开，重新监听...")
            else:
                fail_count += 1
                backoff = min(fail_count * 5, 30)
                print(f"桥接失败，{backoff}秒后重试...")
                time.sleep(backoff)
        else:
            time.sleep(3)


if __name__ == "__main__":
    main()
