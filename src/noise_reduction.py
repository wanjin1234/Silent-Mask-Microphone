"""
实时麦克风降噪（基于 noisereduce）
纯 Python 实现，无需任何 C 库，树莓派/PC 均可直接运行。

原理：频谱减法（非深度学习），通过采集一段“噪声基线”，
      从后续音频中减去该噪声的频谱分量，达到降噪效果。

使用前请戴好耳机，避免啸叫。
"""

import argparse
import time

import noisereduce as nr
import numpy as np
import pyaudio

# ========== 参数配置 ==========
SAMPLE_RATE = 48000  # 采样率（可改为 16000 以降低计算量）
FRAME_SIZE = 512  # 每次处理的帧长（采样点数）
# 512@48kHz ≈ 10.7ms，延迟完全可接受
CHANNELS = 1  # 单声道
NOISE_DURATION = 1.0  # 噪声基线采集时长（秒）
PROP_DECREASE = 0.85  # 降噪强度：0（无降噪）～ 1（最大降噪）
# 建议 0.7～0.95，根据噪声环境调整
STATIONARY = False  # True：稳态噪声（如风扇）；False：非稳态噪声
DEVICE_INDEX_IN = None  # 输入设备索引（None = 系统默认）
DEVICE_INDEX_OUT = None  # 输出设备索引

# ========== 音频设备初始化 ==========
p = pyaudio.PyAudio()

# 打印可用设备列表
print("可用音频设备：")
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    print(
        f"  {i}: {dev['name']} (输入: {dev['maxInputChannels']}ch, 输出: {dev['maxOutputChannels']}ch)"
    )

# 打开流
try:
    stream = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        output=True,
        input_device_index=DEVICE_INDEX_IN,
        output_device_index=DEVICE_INDEX_OUT,
        frames_per_buffer=FRAME_SIZE,
    )
except Exception as e:
    print(f"音频设备打开失败: {e}")
    p.terminate()
    exit(1)

# ========== 采集噪声基线 ==========
print(f"\n正在采集 {NOISE_DURATION} 秒环境噪声，请保持安静...")
noise_frames = []
num_frames = int(SAMPLE_RATE * NOISE_DURATION / FRAME_SIZE)
for i in range(num_frames):
    data = stream.read(FRAME_SIZE, exception_on_overflow=False)
    noise_frames.append(np.frombuffer(data, dtype=np.int16))
    # 显示进度
    if i % 10 == 0:
        print(f"  采集进度: {i}/{num_frames} 帧", end="\r")
noise_signal = np.concatenate(noise_frames)
print(f"\n噪声基线采集完成（共 {len(noise_signal)} 个采样点）")

# ========== 实时降噪循环 ==========
print(f"开始实时降噪（降噪强度={PROP_DECREASE}），按 Ctrl+C 停止...")
print("请对着麦克风说话，戴上耳机监听效果。")

# 用于统计的变量
frame_count = 0
start_time = time.time()

try:
    while True:
        # 读取一帧音频
        data = stream.read(FRAME_SIZE, exception_on_overflow=False)
        audio = np.frombuffer(data, dtype=np.int16)

        # 降噪
        reduced = nr.reduce_noise(
            y=audio,
            y_noise=noise_signal,
            sr=SAMPLE_RATE,
            stationary=STATIONARY,
            prop_decrease=PROP_DECREASE,
        )

        # 输出降噪后的音频
        stream.write(reduced.astype(np.int16).tobytes())

        # 定期打印运行状态
        frame_count += 1
        if frame_count % 100 == 0:
            elapsed = time.time() - start_time
            print(f"  已运行 {elapsed:.1f} 秒，处理 {frame_count} 帧", end="\r")

except KeyboardInterrupt:
    print("\n\n收到停止信号，正在退出...")
finally:
    stream.stop_stream()
    stream.close()
    p.terminate()
    print("音频设备已关闭，资源已释放。")
