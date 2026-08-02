"""
实时麦克风降噪演示（基于 RNNoise）
使用前请戴好耳机，避免啸叫。
"""

import threading
import time
from collections import deque

import numpy as np
import pyaudio

from rnnoise_cffi import RNNoise

# ========== 参数配置 ==========
SAMPLE_RATE = 48000  # RNNoise 要求 48kHz（也可以是 16kHz，但需对应训练模型）
FRAME_SIZE = 480  # 10ms @ 48kHz (RNNoise 要求帧长为 10ms 或 20ms 等)
CHANNELS = 1  # 单声道
DEVICE_INDEX_IN = None  # 输入设备索引，None 表示系统默认
DEVICE_INDEX_OUT = None  # 输出设备索引

# ========== 初始化 RNNoise ==========
denoiser = RNNoise()

# ========== 初始化 PyAudio ==========
p = pyaudio.PyAudio()

# 打印可用设备列表（可选）
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    print(
        f"{i}: {dev['name']} (in: {dev['maxInputChannels']}, out: {dev['maxOutputChannels']})"
    )

try:
    stream_in = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=DEVICE_INDEX_IN,
        frames_per_buffer=FRAME_SIZE,
    )

    stream_out = p.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        output=True,
        output_device_index=DEVICE_INDEX_OUT,
        frames_per_buffer=FRAME_SIZE,
    )
except Exception as e:
    print(f"音频设备打开失败: {e}")
    exit(1)

print("开始实时降噪，按 Ctrl+C 停止...")
# 使用缓冲区避免丢帧（多线程）
audio_in_queue = deque(maxlen=10)
running = True


def capture_thread():
    """音频采集线程"""
    global running
    while running:
        try:
            data = stream_in.read(FRAME_SIZE, exception_on_overflow=False)
            audio_in_queue.append(data)
        except Exception as e:
            print(f"采集错误: {e}")
            break


# 启动采集线程
t = threading.Thread(target=capture_thread)
t.daemon = True
t.start()

try:
    while running:
        if len(audio_in_queue) > 0:
            data = audio_in_queue.popleft()
            # 将原始 int16 数据转换为 float32 并归一化到 [-1, 1]
            audio_float = (
                np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            )

            # RNNoise 降噪（输入输出都是 float32 数组，长度相同）
            # 注意：filter 函数会原地修改数组！
            denoised_float = denoiser.filter(audio_float.copy())

            # 转换回 int16
            denoised_int16 = (denoised_float * 32767).astype(np.int16)

            # 播放降噪后的音频
            stream_out.write(denoised_int16.tobytes())
        else:
            time.sleep(0.001)  # 等待数据
except KeyboardInterrupt:
    print("停止降噪...")
    running = False
finally:
    t.join(timeout=1)
    stream_in.stop_stream()
    stream_out.stop_stream()
    stream_in.close()
    stream_out.close()
    p.terminate()
    print("资源已释放")
