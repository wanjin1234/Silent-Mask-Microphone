import numpy as np
import sounddevice as sd
from scipy.io import wavfile

fs = 16000
duration = 5             # 5秒白噪声
dev = 0

# 1. 生成白噪声（左声道播放，扬声器会响）
noise = np.random.randn(int(fs * duration)).astype(np.float32) * 0.7
playback = np.zeros((len(noise), 2), dtype=np.float32)
playback[:, 0] = noise

print("播放白噪声并录音...")
rec = sd.playrec(playback, samplerate=fs, channels=2, device=dev, blocking=True)
err = rec[:, 1]          # 误差麦克风（右声道）

# 保存录音供人工检查
wavfile.write('noise_rec.wav', fs, rec)

# 2. 互相关找最佳延迟（使用噪声自身作为参考，因为扬声器播放的就是这段噪声）
corr = np.correlate(err, noise, mode='full')
lag = np.argmax(corr) - (len(noise) - 1)
print(f"自动检测延迟: {lag} 样本 ({lag/fs*1000:.1f} ms)")

# 3. 根据延迟对齐
if lag >= 0:
    aligned = err[lag:]
else:
    aligned = np.concatenate([np.zeros(-lag), err])

# 4. 截取前64个样本作为脉冲响应
S = aligned[:64]
if len(S) < 64:
    S = np.pad(S, (0, 64 - len(S)), 'constant')

# 5. 归一化
max_val = np.max(np.abs(S))
if max_val > 1e-9:
    S = S / max_val
else:
    print("警告：信号极弱，检查扬声器和麦克风。")

# 6. 保存 S_coeffs.h
with open('S_coeffs.h', 'w') as f:
    f.write('#ifndef S_COEFFS_H\n#define S_COEFFS_H\n\n')
    f.write('#define S_LEN 64\n')
    f.write('static const float S[S_LEN] = {\n    ')
    f.write(',\n    '.join(f'{v:.6f}f' for v in S))
    f.write('\n};\n\n#endif\n')
print("S 系数已保存。前10个值:", S[:10])