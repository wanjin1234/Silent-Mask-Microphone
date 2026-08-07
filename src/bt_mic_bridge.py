#!/usr/bin/env python3
"""
ReSpeaker 降噪 → 蓝牙 SCO (v15)
使用 pybluez 实现 SCO 套接字，alsaaudio 新版 API
"""

import sys
import time

import numpy as np
import torch
from df import enhance, init_df

try:
    import bluetooth
    from bluetooth import SCO, BluetoothSocket
except ImportError:
    print("请先安装 pybluez：pip install pybluez")
    sys.exit(1)

import alsaaudio

# ===== 配置 =====
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 320  # 20ms @ 16kHz
BT_ADDR = "C4:FF:99:AC:A6:6A"  # 电脑 MAC 地址
SCO_PACKET_SIZE = 64  # SCO MTU: 64
RESPEAKER_DEVICE = "hw:3,0"  # arecord -l 显示的 ReSpeaker 设备

# ===== 初始化降噪模型 =====
model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
df_model, df_state, _ = init_df(model_path)

# ===== 打开 ReSpeaker ALSA 设备（新版 API） =====
rec = alsaaudio.PCM(
    type=alsaaudio.PCM_CAPTURE,
    mode=alsaaudio.PCM_NORMAL,
    device=RESPEAKER_DEVICE,
    channels=CHANNELS,
    rate=SAMPLE_RATE,
    format=alsaaudio.PCM_FORMAT_S16_LE,
    periodsize=FRAME_SIZE,
)


# ===== 创建 SCO socket =====
def create_sco_socket(bt_addr):
    """建立到电脑的 SCO 语音连接"""
    sock = BluetoothSocket(SCO)
    try:
        sock.connect((bt_addr, 0))  # SCO 连接，端口固定为 0
    except Exception as e:
        print(f"❌ SCO 连接失败: {e}")
        print("请确认蓝牙已连接，且电脑已选择树莓派为音频输入设备")
        sys.exit(1)
    return sock


sco = create_sco_socket(BT_ADDR)
print("✅ SCO 套接字已建立")


# ===== 降噪并发送 =====
def process():
    while True:
        # 读取 ReSpeaker 音频
        length, data = rec.read()
        if length <= 0:
            continue
        # 转换为浮点
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        # 降噪
        tensor = torch.from_numpy(audio).unsqueeze(0)
        enhanced = enhance(df_model, df_state, tensor)
        enhanced = enhanced.squeeze(0).numpy()
        out_int = (enhanced * 32767).clip(-32768, 32767).astype(np.int16)
        # 通过 SCO 发送（每包 64 字节）
        pcm = out_int.tobytes()
        while len(pcm) >= SCO_PACKET_SIZE:
            sco.send(pcm[:SCO_PACKET_SIZE])
            pcm = pcm[SCO_PACKET_SIZE:]
        # 剩余不足一包的数据忽略（SCO 不允许短帧）


import threading

thread = threading.Thread(target=process, daemon=True)
thread.start()
print("🎤 降噪音频正在通过蓝牙发送到电脑，按 Ctrl+C 停止")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    sco.close()
    print("🛑 已停止")
