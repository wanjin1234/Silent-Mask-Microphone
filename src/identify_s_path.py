import sounddevice as sd
import numpy as np

fs = 16000
T = 10
L = 64
dev_name = "seeed2micvoicec"

print("生成白噪声...")
noise = np.random.randn(fs * T).astype(np.float32) * 0.3

print("播放并录音中...")
# 关键：录制双声道（ch0=参考麦，ch1=误差麦），与设备实际通道数一致
rec = sd.playrec(noise, samplerate=fs, channels=2, blocking=True, device=dev_name)

# 检查形状并取误差麦（右声道）
print(f"录音形状: {rec.shape}")
e = rec[:, 1]  # 误差麦克风

# 对齐长度，确保 noise 和 e 长度一致
min_len = min(len(noise), len(e))
noise = noise[:min_len]
e = e[:min_len]

# 构造观测矩阵 X，形状为 (M, L)
M = min_len - L
if M <= 0:
    raise ValueError(f"录音长度不足，需要至少 {L+1} 个样本，实际 {min_len}")
X = np.array([noise[i:i+L] for i in range(M)])  # 不转置，保持 (M, L)

# 最小二乘拟合
S_est = np.linalg.lstsq(X, e[:M], rcond=None)[0]

# 保存头文件
with open('S_coeffs.h', 'w') as f:
    f.write('#ifndef S_COEFFS_H\n#define S_COEFFS_H\n\n')
    f.write(f'#define S_LEN {L}\n')
    f.write('static const float S[S_LEN] = {\n    ')
    f.write(',\n    '.join(f'{v:.6f}f' for v in S_est))
    f.write('\n};\n\n#endif\n')
print("成功生成 S_coeffs.h")