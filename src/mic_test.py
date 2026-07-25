#!/usr/bin/env python3
"""实时监测两个麦克风的音量（dB）"""

import sys

import numpy as np
import sounddevice as sd


def find_input_device():
    """自动查找 ReSpeaker 录音设备（需要至少2个输入通道）"""
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        if "seeed" in dev["name"].lower() and dev["max_input_channels"] >= 2:
            return i
    print("未找到 ReSpeaker 双声道输入设备，使用默认输入设备")
    return sd.default.device[0]


# 参数
SAMPLE_RATE = 16000  # 采样率
BLOCK_DURATION = 0.1  # 每次读取的时间长度（秒）
BLOCKSIZE = int(SAMPLE_RATE * BLOCK_DURATION)

# 查找并设置输入设备
dev_idx = find_input_device()
sd.default.device[0] = dev_idx
dev_info = sd.query_devices()[dev_idx]
print(f"使用输入设备: {dev_info['name']}")
print(f"最大输入通道: {dev_info['max_input_channels']}")
print("开始监测音量（左声道 / 右声道），按 Ctrl+C 停止...")

# 打开双声道流
stream = sd.InputStream(
    device=dev_idx,
    channels=2,
    samplerate=SAMPLE_RATE,
    blocksize=BLOCKSIZE,
    dtype="float32",
)
stream.start()

try:
    while True:
        data, status = stream.read(BLOCKSIZE)  # data形状: (BLOCKSIZE, 2)
        if status:
            print(f"状态异常: {status}")

        left = data[:, 0]
        right = data[:, 1]

        # 计算 RMS 并转换为分贝（避免 log(0)）
        rms_left = np.sqrt(np.mean(left**2))
        rms_right = np.sqrt(np.mean(right**2))
        db_left = 20 * np.log10(rms_left + 1e-6)
        db_right = 20 * np.log10(rms_right + 1e-6)

        print(
            f"\r左声道: {db_left:6.1f} dB  右声道: {db_right:6.1f} dB",
            end="",
            flush=True,
        )

except KeyboardInterrupt:
    print("\n停止监测")
    stream.stop()
    stream.close()
