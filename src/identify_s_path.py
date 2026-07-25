import sounddevice as sd
import numpy as np

fs = 16000
T = 10
L = 64
dev_name = "seeed2micvoicec"

print("生成白噪声...")
noise = np.random.randn(fs * T).astype(np.float32) * 0.3

print("播放并录音中...")
# 录制双声道，误差麦克风通常是第二个通道 (ch1)
rec = sd.playrec(noise, samplerate=fs, channels=2, blocking=True, device=dev_name)

# 检查录音形状
print(f"录音形状: {rec.shape}")  # 应该是 (fs*T, 2)

# 取误差麦克风通道（索引1）
e = rec[:, 1]

# 对齐长度
min_len = min(len(noise), len(e))
noise = noise[:min_len]
e = e[:min_len]
print(f"对齐后 noise 长度: {len(noise)}, e 长度: {len(e)}")

# 构造矩阵
X = np.array([noise[i:i+L] for i in range(len(noise)-L)]).T
print(f"X shape: {X.shape}, e shape: {e[:len(noise)-L].shape}")

# 最小二乘辨识
S_est, *_ = np.linalg.lstsq(X, e[:len(noise)-L], rcond=None)

# 保存系数
with open('S_coeffs.h', 'w') as f:
    f.write('#ifndef S_COEFFS_H\n#define S_COEFFS_H\n\n')
    f.write(f'#define S_LEN {L}\n')
    f.write('static const float S[S_LEN] = {\n    ')
    f.write(',\n    '.join(f'{v:.6f}f' for v in S_est))
    f.write('\n};\n\n#endif\n')
print("成功生成 S_coeffs.h")