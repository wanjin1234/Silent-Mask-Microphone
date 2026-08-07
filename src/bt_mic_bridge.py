#!/usr/bin/env python3
"""
树莓派 ReSpeaker 降噪 → 蓝牙麦克风 (v8.0)
- 自动切换蓝牙卡为 HFP/HSP Profile
- 将降噪后音频发送到电脑（通过 bluez_source）
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
FRAME_SIZE = 320          # 20ms @ 16kHz
BT_DEVICE_NAME = "RaspberryPi-Mic"
NOISE_GATE_THRESHOLD = 0.005
VAD_AGGRESSIVENESS = 2
ENABLE_VAD = True


def run_cmd(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except:
        return ""


class PipeWireDenoiseBridge:
    def __init__(self):
        print("加载 DeepFilterNet2 模型...")
        model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
        self.df_model, self.df_state, _ = init_df(model_path)
        print("✅ 模型加载完成")

        self.vad = None
        if ENABLE_VAD:
            try:
                import webrtcvad
                self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
            except:
                pass

        self.rec_proc = None
        self.play_proc = None
        self.agent_proc = None
        self.running = False       # 由 start/stop 控制
        self.bt_source = None      # 蓝牙 source (电脑麦克风输入)
        self.respeaker_source = None

    # ==================== 蓝牙基础初始化（仅首次） ====================
    def init_bluetooth(self):
        """只做基本配置，不重启服务"""
        print("🔵 蓝牙基本初始化...")
        run_cmd(["sudo", "rfkill", "unblock", "bluetooth"])
        run_cmd(["bluetoothctl", "system-alias", BT_DEVICE_NAME])
        # 确保上电
        for _ in range(10):
            if "Powered: yes" in run_cmd(["bluetoothctl", "show"]):
                break
            run_cmd(["bluetoothctl", "power", "on"])
            time.sleep(1)
        run_cmd(["bluetoothctl", "discoverable", "on"])
        run_cmd(["bluetoothctl", "pairable", "on"])
        # 启动 bt-agent
        run_cmd(["killall", "bt-agent"], check=False)
        if run_cmd(["which", "bt-agent"]).strip():
            self.agent_proc = subprocess.Popen(
                ["bt-agent", "-c", "DisplayOnly"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            print("✅ bt-agent 已启动")
        else:
            print("⚠️  bt-agent 未安装 (sudo apt install bluez-tools)")

    # ==================== 获取蓝牙卡并切换 profile ====================
    def _find_bt_card(self):
        out = run_cmd(["pactl", "list", "cards", "short"])
        for line in out.splitlines():
            if "bluez_card" in line:
                return line.split()[1]
        return None

    def _activate_hfp(self, card):
        """激活 headset_head_unit (HFP) profile"""
        for attempt in range(5):
            if "headset_head_unit" in run_cmd(["pactl", "list", "cards", "short"]):
                run_cmd(["pactl", "set-card-profile", card, "headset_head_unit"])
                time.sleep(2)
                if "headset_head_unit" in run_cmd(["pactl", "list", "cards", "short"]):
                    print("✅ 已激活 headset_head_unit")
                    return True
            time.sleep(1)
        print("⚠️ 无法激活 HFP, 尝试 A2DP Source")
        run_cmd(["pactl", "set-card-profile", card, "a2dp_source"])
        return True

    def _find_bt_source(self, card):
        """查找蓝牙 source (电脑的麦克风输入)"""
        for _ in range(10):
            out = run_cmd(["pactl", "list", "sources", "short"])
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and "bluez_source" in parts[1]:
                    return parts[1]
            time.sleep(1)
        # 备用：pw-cli
        out = run_cmd(["pw-cli", "ls", "Node"])
        m = re.findall(r'node\.name\s*=\s*"(bluez_source[^"]+)"', out)
        if m:
            return m[0]
        return None

    # ==================== ReSpeaker 输入源 ====================
    def _find_respeaker_source(self):
        out = run_cmd(["pactl", "list", "sources", "short"])
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and "seeed" in parts[1].lower():
                return parts[1]
        # 备用 pw-cli
        out = run_cmd(["pw-cli", "ls", "Node"])
        m = re.findall(r'node\.name\s*=\s*"(alsa_input[^"]*seeed[^"]*)"', out, re.I)
        if m:
            return m[0]
        return None

    # ==================== 音频管道 ====================
    def _start_capture(self, source):
        cmd = [
            "pw-cat", "--record",
            "--rate", str(SAMPLE_RATE),
            "--channels", str(CHANNELS),
            "--format", "s16",
            "--target", source,
            "-"
        ]
        print(f"🔄 录制: {' '.join(cmd)}")
        self.rec_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(1)
        if self.rec_proc.poll() is not None:
            err = self.rec_proc.stderr.read().decode()
            print(f"❌ 录制失败: {err}")
            return False
        return True

    def _start_playback(self, bt_source):
        cmd = [
            "pw-cat", "--playback",
            "--rate", str(SAMPLE_RATE),
            "--channels", str(CHANNELS),
            "--format", "s16",
            "--target", bt_source,
            "-"
        ]
        print(f"🔄 发送到电脑: {' '.join(cmd)}")
        self.play_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(1)
        if self.play_proc.poll() is not None:
            err = self.play_proc.stderr.read().decode()
            print(f"❌ 发送失败: {err}")
            return False
        return True

    # ==================== 降噪处理循环 ====================
    def _process_loop(self):
        print("🎤 开始降噪...")
        frame_bytes = FRAME_SIZE * CHANNELS * 2
        while self.running:
            if self.rec_proc and self.rec_proc.poll() is not None:
                print("录制进程退出，尝试重启...")
                if not self._start_capture(self.respeaker_source):
                    break
            if self.play_proc and self.play_proc.poll() is not None:
                print("播放进程退出，尝试重启...")
                if not self._start_playback(self.bt_source):
                    break

            try:
                raw = self.rec_proc.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break
            except:
                break

            audio_int = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            rms = np.sqrt(np.mean(audio_int**2))

            is_speech = True
            if self.vad:
                try:
                    is_speech = self.vad.is_speech(raw, SAMPLE_RATE)
                except:
                    pass
            if rms < NOISE_GATE_THRESHOLD:
                is_speech = False

            if not is_speech:
                silence = np.zeros(FRAME_SIZE, dtype=np.int16)
                self._write(silence.tobytes())
                continue

            # 降噪
            t = torch.from_numpy(audio_int).unsqueeze(0)
            enhanced = enhance(self.df_model, self.df_state, t)
            enhanced = enhanced.squeeze(0).numpy() if hasattr(enhanced, 'numpy') else np.asarray(enhanced).flatten()
            if len(enhanced) > FRAME_SIZE:
                enhanced = enhanced[:FRAME_SIZE]
            elif len(enhanced) < FRAME_SIZE:
                enhanced = np.pad(enhanced, (0, FRAME_SIZE - len(enhanced)))

            out_int = (enhanced * 32767).clip(-32768, 32767).astype(np.int16)
            self._write(out_int.tobytes())

        print("处理循环退出")

    def _write(self, data):
        try:
            self.play_proc.stdin.write(data)
            self.play_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            print("播放管道断开")
            self.play_proc = None

    # ==================== 桥接启动/停止 ====================
    def start_bridge(self):
        card = self._find_bt_card()
        if not card:
            print("未找到蓝牙卡")
            return False
        self._activate_hfp(card)
        self.bt_source = self._find_bt_source(card)
        if not self.bt_source:
            print("未找到蓝牙 source")
            return False
        self.respeaker_source = self._find_respeaker_source()
        if not self.respeaker_source:
            print("未找到 ReSpeaker")
            return False

        if not self._start_capture(self.respeaker_source):
            return False
        if not self._start_playback(self.bt_source):
            return False

        self.running = True          # 关键：重置状态
        self.proc_thread = threading.Thread(target=self._process_loop, daemon=True)
        self.proc_thread.start()
        print("✅ 桥接成功！降噪音频正在发送到电脑。")
        return True

    def stop_bridge(self):
        self.running = False
        for p in [self.rec_proc, self.play_proc]:
            if p:
                p.terminate()
        print("🛑 桥接已停止")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bt-source", help="手动指定蓝牙 source 名称")
    parser.add_argument("--source", help="手动指定 ReSpeaker 输入源名称")
    args = parser.parse_args()

    bridge = PipeWireDenoiseBridge()
    bridge.init_bluetooth()

    if args.bt_source:
        bridge.bt_source = args.bt_source
    if args.source:
        bridge.respeaker_source = args.source

    last_addr = None
    fail_count = 0

    while True:
        # 获取当前连接设备
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
                time.sleep(2)
                last_addr = current_addr
                fail_count = 0

            if bridge.start_bridge():
                fail_count = 0
                # 等待断开
                while last_addr == current_addr:
                    time.sleep(2)
                    out2 = run_cmd(["bluetoothctl", "devices", "Connected"])
                    if current_addr not in out2:
                        break
                print("设备断开，重新监听...")
                bridge.stop_bridge()
                last_addr = None        # 重置，允许重新连接
            else:
                fail_count += 1
                backoff = min(fail_count * 5, 30)
                print(f"桥接失败，{backoff}s 后重试...")
                time.sleep(backoff)
        else:
            time.sleep(3)


if __name__ == "__main__":
    main()