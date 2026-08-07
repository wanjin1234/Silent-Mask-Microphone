#!/usr/bin/env python3
"""
ReSpeaker 降噪 → 蓝牙麦克风 (混合架构 v16)
降噪处理在 pyenv Python 中运行，SCO 发送由系统 Python 子进程负责
"""

import subprocess
import sys
import time

import alsaaudio
import numpy as np
import torch
from df import enhance, init_df

# ===== 配置 =====
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 320
RESPEAKER_DEVICE = "hw:3,0"
SYSTEM_PYTHON = "/usr/bin/python3"
SCO_SENDER_SCRIPT = (
    "/home/wanjin1234/Desktop/silentmask/Silent-Mask-Microphone/src/sco_sender.py"
)

# ===== 启动 SCO 发送子进程 =====
print("🚀 启动 SCO 发送后端...")
try:
    sco_proc = subprocess.Popen(
        [SYSTEM_PYTHON, SCO_SENDER_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # 等待它完成连接并输出提示
    line = sco_proc.stdout.readline().decode().strip()
    print(line)
    if "失败" in line:
        sys.exit(1)
    line = sco_proc.stdout.readline().decode().strip()
    print(line)
except Exception as e:
    print(f"❌ 无法启动 SCO 后端: {e}")
    sys.exit(1)

# ===== 加载降噪模型 =====
print("加载 DeepFilterNet2 模型...")
model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
df_model, df_state, _ = init_df(model_path)
print("✅ 模型就绪")

# ===== 打开 ReSpeaker =====
rec = alsaaudio.PCM(
    type=alsaaudio.PCM_CAPTURE,
    mode=alsaaudio.PCM_NORMAL,
    device=RESPEAKER_DEVICE,
    channels=CHANNELS,
    rate=SAMPLE_RATE,
    format=alsaaudio.PCM_FORMAT_S16_LE,
    periodsize=FRAME_SIZE,
)
print("🎤 ReSpeaker 已打开")

# ===== 降噪循环 =====
print("🎧 开始降噪传输，按 Ctrl+C 停止")
try:
    while True:
        length, data = rec.read()
        if length <= 0:
            continue

        # 转为浮点
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        # 降噪
        tensor = torch.from_numpy(audio).unsqueeze(0)
        enhanced = enhance(df_model, df_state, tensor)
        enhanced = enhanced.squeeze(0).numpy()
        out_int = (enhanced * 32767).clip(-32768, 32767).astype(np.int16)

        # 发送到 SCO 子进程
        try:
            sco_proc.stdin.write(out_int.tobytes())
            sco_proc.stdin.flush()
        except BrokenPipeError:
            print("❌ SCO 连接断开")
            break
except KeyboardInterrupt:
    pass
finally:
    sco_proc.terminate()
    print("🛑 已停止")
