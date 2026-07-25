import sounddevice as sd
import numpy as np
import sys

fs = 16000
T = 10
L = 64
dev_name = "seeed2micvoicec"

print("生成白噪声...")
noise = np.random.randn(fs * T).astype(np.float32) * 0.3

print("播放并录音中...")
try:
    rec = sd.playrec(noise, samplerate=fs, channels=1,
                     blocking=True, device=dev_name)
except Exception as e:
    print(f"设备错误: {e}")
    sys.exit(1)

e = rec[:, 0]

# ---------- 对齐长度 ----------
min_len = min(len(noise), len(e))
noise = noise[:min_len]
e = e[:min_len]

# 如果录音开头有延迟，可以跳过前 N 个样本（通常不需要）
# skip = 0  # 若需要可调整，例如 skip = 800 (50ms)
# noise = noise[skip:]
# e = e[skip:]

# 构造矩阵
if len(noise) <= L:
    print("录音长度不足，请增加 T 或检查声卡")
    sys.exit(1)

X = np.array([noise[i:i+L] for i in range(len(noise)-L)]).T
S_est, residuals, rank, s = np.linalg.lstsq(X, e[:len(noise)-L], rcond=None)

# 保存头文件
with open('S_coeffs.h', 'w') as f:
    f.write('#ifndef S_COEFFS_H\n#define S_COEFFS_H\n\n')
    f.write(f'#define S_LEN {L}\n')
    f.write('static const float S[S_LEN] = {\n    ')
    f.write(',\n    '.join(f'{v:.6f}f' for v in S_est))
    f.write('\n};\n\n#endif\n')
print("成功生成 S_coeffs.h")