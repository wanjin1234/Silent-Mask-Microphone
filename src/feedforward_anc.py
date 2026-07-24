# feedforward_anc.py —— 前馈 ANC（静音口罩基础版）
import sys
import time

import numpy as np
import pyaudio

# ========== 参数（可调）==========
CHUNK = 64  # 越小延迟越低，64 对应约 4ms @ 16kHz
RATE = 16000  # 采样率
FORMAT = pyaudio.paInt16
GAIN = 0.6  # 输出增益（控制反相强度，小于1.0避免啸叫）


def find_device(p, is_input=True, min_channels=1):
    """查找 ReSpeaker 设备"""
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        name = info["name"].lower()
        if "seeed" in name:
            if is_input and info["maxInputChannels"] >= min_channels:
                return i, info
            if not is_input and info["maxOutputChannels"] > 0:
                return i, info
    return None, None


# ========== 初始化 ==========
p = pyaudio.PyAudio()

in_idx, in_info = find_device(p, is_input=True, min_channels=2)
out_idx, out_info = find_device(p, is_input=False)

if in_idx is None or out_idx is None:
    print("❌ 未找到 ReSpeaker 设备")
    print("可用输入设备：")
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(f"  [{i}] {info['name']} (max inputs: {info['maxInputChannels']})")
    sys.exit(1)

print(
    f"✅ 输入设备: [{in_idx}] {in_info['name']} (channels: {in_info['maxInputChannels']})"
)
print(
    f"✅ 输出设备: [{out_idx}] {out_info['name']} (channels: {out_info['maxOutputChannels']})"
)
print(f"   CHUNK={CHUNK}, RATE={RATE}, 理论延迟={CHUNK / RATE * 1000:.1f}ms")

# 打开立体声输入（左声道=参考麦克风，右声道=误差麦克风预留）
stream_in = p.open(
    format=FORMAT,
    channels=2,
    rate=RATE,
    input=True,
    input_device_index=in_idx,
    frames_per_buffer=CHUNK,
)

# 打开单声道输出
stream_out = p.open(
    format=FORMAT,
    channels=1,
    rate=RATE,
    output=True,
    output_device_index=out_idx,
    frames_per_buffer=CHUNK,
)


# ========== 简单陷波滤波器（防止啸叫）==========
# 预先计算一个简单的 IIR 陷波器，抑制 2-4kHz 附近容易啸叫的频段
class NotchFilter:
    """二阶陷波滤波器"""

    def __init__(self, freq, q, sample_rate):
        omega = 2 * np.pi * freq / sample_rate
        self.b0 = 1.0
        self.b1 = -2 * np.cos(omega)
        self.b2 = 1.0
        alpha = np.sin(omega) / (2 * q)
        a0 = 1 + alpha
        self.a1 = -2 * np.cos(omega) / a0
        self.a2 = (1 - alpha) / a0
        self.b0 /= a0
        self.b1 /= a0
        self.b2 /= a0
        # 状态
        self.x1 = self.x2 = 0.0
        self.y1 = self.y2 = 0.0

    def process(self, x):
        y = (
            self.b0 * x
            + self.b1 * self.x1
            + self.b2 * self.x2
            - self.a1 * self.y1
            - self.a2 * self.y2
        )
        self.x2, self.x1 = self.x1, x
        self.y2, self.y1 = self.y1, y
        return y


# 创建几个陷波器，抑制容易啸叫的共振频率
notch_filters = [
    NotchFilter(1500, 5.0, RATE),
    NotchFilter(3000, 5.0, RATE),
    NotchFilter(4500, 8.0, RATE),
]

print("🔇 前馈 ANC 已启动（静音口罩模式），按 Ctrl+C 停止...")
print("   请对着参考麦克风说话，喇叭将播放反相声音")

frame_count = 0
start_time = time.time()

try:
    while True:
        # 1. 读取双声道数据
        raw_data = stream_in.read(CHUNK, exception_on_overflow=False)
        samples_interleaved = np.frombuffer(raw_data, dtype=np.int16)

        # 2. 提取左声道（参考麦克风）
        ref_signal = samples_interleaved[0::2].astype(np.float32)

        # 3. 反相
        anti_noise = -ref_signal * GAIN

        # 4. 陷波滤波（抑制啸叫频段）
        for nf in notch_filters:
            for i in range(len(anti_noise)):
                anti_noise[i] = nf.process(anti_noise[i])

        # 5. 裁剪并转换为 int16
        anti_noise = np.clip(anti_noise, -32767, 32767).astype(np.int16)

        # 6. 播放
        stream_out.write(anti_noise.tobytes())

        frame_count += 1
        if frame_count % 100 == 0:
            elapsed = time.time() - start_time
            print(
                f"   运行中... {frame_count} 帧, {elapsed:.1f}s, "
                f"输入峰值={np.max(np.abs(ref_signal)):.0f}"
            )

except KeyboardInterrupt:
    print("\n正在停止...")

finally:
    stream_in.stop_stream()
    stream_in.close()
    stream_out.stop_stream()
    stream_out.close()
    p.terminate()
    print("✅ 已清理资源")
