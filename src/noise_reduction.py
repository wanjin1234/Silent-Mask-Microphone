"""
实时麦克风降噪演示（基于 RNNoise + CFFI）
已修复帧不匹配、指针安全、多线程退出等问题。
使用前请戴好耳机，避免啸叫。
"""

import threading
import time
from collections import deque

import numpy as np
import pyaudio

# 从自定义 CFFI 模块导入
from rnnoise_cffi import RNNoise

# ========== 参数配置 ==========
SAMPLE_RATE = 48000  # RNNoise 使用 48kHz
FRAME_SIZE = 480  # 10ms 帧长（采样点数）
CHANNELS = 1  # 单声道
DEVICE_INDEX_IN = None  # 输入设备索引，None 为系统默认
DEVICE_INDEX_OUT = None  # 输出设备索引

# ========== 初始化 RNNoise ==========
denoiser = RNNoise()
denoiser_lock = threading.Lock()  # 线程锁，保护滤镜状态

# ========== 初始化 PyAudio ==========
p = pyaudio.PyAudio()

# 打印可选设备列表（方便调试）
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    print(
        f"{i}: {dev['name']} (in: {dev['maxInputChannels']}, out: {dev['maxOutputChannels']})"
    )

try:
    # 注意：frames_per_buffer 只是个建议值，实际读取可能返回任意数量
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

# 音频帧队列（每个元素是 bytes，长度恰好为 FRAME_SIZE*2 字节）
audio_in_queue = deque(maxlen=20)
running = True


def capture_thread():
    """音频采集线程：不断读取原始 PCM 数据，按固定帧长切分后入队"""
    global running
    buf = b""  # 字节缓冲区，用于拼凑完整帧
    while running:
        try:
            data = stream_in.read(FRAME_SIZE, exception_on_overflow=False)
            buf += data
            # 只要缓冲区足够一个完整帧（480 采样点 × 2 字节 = 960 字节），就切出一帧
            while len(buf) >= FRAME_SIZE * 2:
                frame = buf[: FRAME_SIZE * 2]
                buf = buf[FRAME_SIZE * 2 :]
                audio_in_queue.append(frame)
        except Exception as e:
            print(f"采集错误: {e}")
            break


# 启动采集线程
t = threading.Thread(target=capture_thread, daemon=True)
t.start()

try:
    while running:
        if audio_in_queue:
            # 从队列取出一帧 bytes
            frame_bytes = audio_in_queue.popleft()
        else:
            time.sleep(0.001)  # 队列空时短暂休眠，避免忙等待
            continue

        # 转换为 float32 数组并归一化
        audio_int16 = np.frombuffer(frame_bytes, dtype=np.int16)
        audio_float = audio_int16.astype(np.float32) / 32768.0

        # 降噪（加锁确保线程安全，尽管当前只有主线程调用）
        with denoiser_lock:
            denoised_float = denoiser.filter(audio_float.copy())

        # 转换回 int16 并播放
        denoised_int16 = (denoised_float * 32767).astype(np.int16)
        stream_out.write(denoised_int16.tobytes())

except KeyboardInterrupt:
    print("\n停止降噪...")
    running = False
finally:
    # 等待采集线程结束
    t.join(timeout=1)

    # 清理音频资源
    stream_in.stop_stream()
    stream_out.stop_stream()
    stream_in.close()
    stream_out.close()
    p.terminate()

    # 释放 RNNoise 状态（重要！避免段错误）
    denoiser.close()

    print("资源已释放")
