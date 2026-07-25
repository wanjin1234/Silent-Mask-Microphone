import sounddevice as sd
import numpy as np
import sys

fs = 16000
T = 10          # 白噪声时长
L = 64          # 次级路径阶数
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

e = rec[:, 0]   # 误差麦克风信号

# 最小二乘辨识
print("计算 S 系数...")
X = np.array([noise[i:i+L] for i in range(len(noise)-L)]).T
S_est, *_ = np.linalg.lstsq(X, e[:len(noise)-L], rcond=None)

# 保存头文件
with open('S_coeffs.h', 'w') as f:
    f.write('#ifndef S_COEFFS_H\n#define S_COEFFS_H\n\n')
    f.write(f'#define S_LEN {L}\n')
    f.write('static const float S[S_LEN] = {\n    ')
    f.write(',\n    '.join(f'{v:.6f}f' for v in S_est))
    f.write('\n};\n\n#endif\n')
print("成功生成 S_coeffs.h")