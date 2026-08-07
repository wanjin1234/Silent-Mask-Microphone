#!/usr/bin/env python3
"""
ReSpeaker 降噪 → 蓝牙 SCO (v14)
直接通过 SCO socket 发送音频，无需查找 bluez_source
"""

import socket
import sys
import threading
import time

import numpy as np
import torch
from df import enhance, init_df

# ===== 配置 =====
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 320  # 20ms @ 16kHz
BT_ADDR = "C4:FF:99:AC:A6:6A"  # 电脑的 MAC 地址（可运行 bluetoothctl devices 查看）
SCO_PACKET_SIZE = 64  # SCO 包大小（根据 hciconfig -a 中的 SCO MTU: 64:1）
DEVICE_INDEX = 0  # hci0 的索引
RESPEAKER_DEVICE = "hw:3,0"  # arecord 显示的 card 3, device 0

# ===== 初始化降噪模型 =====
model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
df_model, df_state, _ = init_df(model_path)

# ===== 打开 ReSpeaker ALSA 设备 =====
import alsaaudio

rec = alsaaudio.PCM(alsaaudio.PCM_CAPTURE, alsaaudio.PCM_NORMAL, RESPEAKER_DEVICE)
rec.setchannels(CHANNELS)
rec.setrate(SAMPLE_RATE)
rec.setformat(alsaaudio.PCM_FORMAT_S16_LE)
rec.setperiodsize(FRAME_SIZE)


# ===== 创建 SCO socket =====
def create_sco_socket():
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_SCO)
    sock.bind((BT_ADDR, 0))
    sock.connect((BT_ADDR, 0))
    return sock


sco = create_sco_socket()
print("SCO 套接字已建立")


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
        # 通过 SCO 发送（可能需要填充到 SCO 包大小）
        pcm = out_int.tobytes()
        while len(pcm) > 0:
            sco.send(pcm[:SCO_PACKET_SIZE])
            pcm = pcm[SCO_PACKET_SIZE:]


thread = threading.Thread(target=process, daemon=True)
thread.start()
print("✅ 降噪音频正在通过蓝牙发送到电脑")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("停止")
