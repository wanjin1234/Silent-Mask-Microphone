#!/usr/bin/env python3
"""
ReSpeaker 降噪 → 蓝牙 SCO (终极版 v17)
自动使用 ALSA bt-sco 设备，无需手动处理 socket
"""

import re
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
RESPEAKER_DEVICE = "hw:3,0"  # ReSpeaker 设备
SCO_ALSA_DEVICE = None  # 自动查找


def find_sco_device():
    """查找 ALSA 中的 SCO 设备"""
    out = subprocess.run(["aplay", "-l"], capture_output=True, text=True).stdout
    match = re.search(r"card (\d+):.*bt-sco.*device (\d+):", out, re.IGNORECASE)
    if match:
        return f"hw:{match.group(1)},{match.group(2)}"
    # 备用：直接根据名称查找
    for line in out.splitlines():
        if "bt-sco" in line.lower():
            # 提取 card 编号
            card_match = re.search(r"card (\d+)", line)
            if card_match:
                return f"hw:{card_match.group(1)},0"
    return None


# ===== 加载降噪模型 =====
model_path = "/home/wanjin1234/.pyenv/versions/3.10.14/lib/python3.10/site-packages/pretrained_models/DeepFilterNet2"
df_model, df_state, _ = init_df(model_path)

# ===== 确定输出设备 =====
# 确保 snd-bt-sco 已加载
subprocess.run(["sudo", "modprobe", "snd-bt-sco"], check=False)
time.sleep(1)
SCO_ALSA_DEVICE = find_sco_device()
if SCO_ALSA_DEVICE:
    print(f"✅ 找到 ALSA SCO 设备: {SCO_ALSA_DEVICE}")
    # 使用 aplay 子进程
    aplay_proc = subprocess.Popen(
        [
            "aplay",
            "-D",
            SCO_ALSA_DEVICE,
            "-r",
            str(SAMPLE_RATE),
            "-c",
            str(CHANNELS),
            "-f",
            "S16_LE",
            "-",
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
else:
    print("❌ 未找到 ALSA SCO 设备，尝试使用原始 socket 模式...")
    # 备用：使用 struct 打包的 SCO socket（需要 sudo）
    import socket
    import struct

    BT_ADDR = "C4:FF:99:AC:A6:6A"

    def mac_to_bytes(mac):
        return bytes.fromhex(mac.replace(":", ""))

    try:
        sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, 2
        )  # BTPROTO_SCO
        # 绑定本地地址（需要 struct）
        local_addr = struct.pack("6s", mac_to_bytes(BT_ADDR))
        sock.bind((BT_ADDR, 0))  # 尝试传统格式，如果失败则用下面的
    except:
        # 使用底层 HCI sockaddr_hci 结构
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, 0)  # HCI raw
        # 构建 SCO 连接...
        # 这种方式过于复杂，暂跳过，建议使用 ALSA 方法
        print("SCO socket 不可用，请确保 snd-bt-sco 模块已加载")
        sys.exit(1)

    print("✅ SCO socket 已建立")
    sco_sock = sock

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

# ===== 降噪循环 =====
print("🎤 开始降噪传输...")
try:
    while True:
        length, data = rec.read()
        if length <= 0:
            continue
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(audio).unsqueeze(0)
        enhanced = enhance(df_model, df_state, tensor)
        enhanced = enhanced.squeeze(0).numpy()
        out_int = (enhanced * 32767).clip(-32768, 32767).astype(np.int16)
        pcm = out_int.tobytes()

        if SCO_ALSA_DEVICE:
            aplay_proc.stdin.write(pcm)
            aplay_proc.stdin.flush()
        else:
            # SCO socket 发送
            sco_sock.send(pcm)
except KeyboardInterrupt:
    pass
finally:
    print("🛑 停止")
