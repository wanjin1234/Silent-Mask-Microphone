#!/usr/bin/env python3
"""测试喇叭是否正常发声"""

import sys

import numpy as np
import sounddevice as sd


def find_output_device():
    """自动查找 ReSpeaker 播放设备"""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if "seeed" in dev["name"].lower() and dev["max_output_channels"] > 0:
            return i
    # 找不到时返回默认设备
    print("未找到 ReSpeaker 播放设备，使用系统默认播放设备")
    return sd.default.device[1]


# 参数
DURATION = 60  # 播放秒数（可自行停止）
SAMPLE_RATE = 44100  # 采样率（ReSpeaker支持）
FREQUENCY = 440.0  # 频率（A4音符）

# 生成正弦波
t = np.linspace(0, DURATION, int(SAMPLE_RATE * DURATION), endpoint=False)
wave = 0.3 * np.sin(2 * np.pi * FREQUENCY * t)  # 振幅 0.3 避免破音

# 设置输出设备
dev_idx = find_output_device()
sd.default.device[1] = dev_idx
print(f"使用输出设备: {sd.query_devices()[dev_idx]['name']}")
print("开始播放 440Hz 正弦波，按 Ctrl+C 停止...")

try:
    sd.play(wave, samplerate=SAMPLE_RATE, blocking=True)
except KeyboardInterrupt:
    sd.stop()
    print("\n播放已停止")
