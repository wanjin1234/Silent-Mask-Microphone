# -*- coding: utf-8 -*-
"""
RespirationDetector —— 基于呼吸周期微动的纯物理人体存在检测器。

背景
----
C4002 等毫米波雷达的 `presence` / `target_status` 是固件在"已学习固定场景"
下训练出来的分类结果，换到陌生室内环境后不可靠。本模块完全不使用这些字段，
只依赖雷达的原始物理量——多普勒速度 `move_target_speed`（单位 cm/s）。

物理依据
--------
- 静止的人体仍然在呼吸：胸腔以约 0.1~0.6 Hz（典型 0.2~0.5 Hz）周期性起伏，
  雷达测到的多普勒速度会呈现同频率的低速周期性微变（幅度远小于走动，但 > 0）。
- 静止的墙面 / 天花板 / 家具：多普勒速度恒为 0，无周期、无幅值。
- 缓慢漂移或随机噪声：速度可能非零，但不具备稳定的呼吸频段周期。

检测流程（对速度时间序列，无需场景训练）
-----------------------------------------
1. 线性去趋势（去除慢漂移 / 佩戴者自身缓慢移动）。
2. 归一化自相关：在呼吸周期范围（period_min ~ period_max）内找峰值，估计周期。
3. 幅值校验（RMS > min_amplitude）：有真实微动，而非纯噪声。
4. 过零率校验（至少 2 次过零）：确实是振荡，而非单向漂移。
5. 综合打分 -> 输出 detected / confidence / breath_rate。

已知局限（诚实说明）
--------------------
- 若某个慢速周期干扰源（例如转速恰好 ~0.3 Hz 的吊扇）落在呼吸频段内，
  仅靠单点速度无法 100% 区分人与非人，需要幅度、周期稳定性、多帧一致性共同抑制。
- 呼吸检测需要至少 2~3 个完整呼吸周期，故建议扫描窗口 >= 6 s（约 0.3 Hz 时）。
"""

import math
import os
import time
from collections import deque

# 呼吸频段（周期，单位秒）与判定阈值，均可通过环境变量覆盖
PERIOD_MIN = float(os.getenv('C4002_BREATH_PERIOD_MIN', '1.6'))     # 呼吸频率上限 ~0.625 Hz
PERIOD_MAX = float(os.getenv('C4002_BREATH_PERIOD_MAX', '10.0'))    # 呼吸频率下限 ~0.1 Hz
CORR_THRESHOLD = float(os.getenv('C4002_BREATH_CORR_THRESHOLD', '0.5'))  # 自相关峰值最低（保守，抑制空场噪声）
MIN_AMPLITUDE = float(os.getenv('C4002_BREATH_MIN_AMPLITUDE', '1.5'))    # 去趋势后 RMS 最低 cm/s（保守，高于空场噪声底）
MIN_SAMPLES = int(os.getenv('C4002_BREATH_MIN_SAMPLES', '20'))           # 最少样本数
MAX_AGE = float(os.getenv('C4002_BREATH_MAX_AGE', '12.0'))               # 样本最大保留时长 s
MAX_SAMPLES = int(os.getenv('C4002_BREATH_MAX_SAMPLES', '400'))


class RespirationDetector:
    """对单路雷达的多普勒速度时间序列做呼吸周期检测。"""

    def __init__(self,
                 period_min=PERIOD_MIN,
                 period_max=PERIOD_MAX,
                 corr_threshold=CORR_THRESHOLD,
                 min_amplitude=MIN_AMPLITUDE,
                 min_samples=MIN_SAMPLES,
                 max_age=MAX_AGE,
                 max_samples=MAX_SAMPLES):
        self.period_min = float(period_min)
        self.period_max = float(period_max)
        self.corr_threshold = float(corr_threshold)
        self.min_amplitude = float(min_amplitude)
        self.min_samples = int(min_samples)
        self.max_age = float(max_age)
        self.max_samples = int(max_samples)
        # 每个元素为 (timestamp, speed_cm_s)
        self.buffer = deque(maxlen=self.max_samples)

    def reset(self):
        self.buffer.clear()

    def add(self, timestamp, speed_cm_s):
        """喂入一帧原始多普勒速度。speed_cm_s 为 None 时按 0 处理（无目标/静止）。"""
        if timestamp is None:
            timestamp = time.time()
        try:
            s = float(speed_cm_s)
        except (TypeError, ValueError):
            s = 0.0
        self.buffer.append((float(timestamp), s))
        self._prune(timestamp)

    def _prune(self, now=None):
        if now is None:
            now = time.time()
        # 去掉过老的样本（保持时间窗口，避免陈旧数据污染周期估计）
        while self.buffer and (now - self.buffer[0][0]) > self.max_age:
            self.buffer.popleft()

    # ------------------------------------------------------------------
    # 信号处理
    # ------------------------------------------------------------------
    @staticmethod
    def _detrend(y):
        """最小二乘线性去趋势。"""
        n = len(y)
        if n < 2:
            return list(y)
        sx = sum(range(n))
        sy = sum(y)
        sxx = sum(i * i for i in range(n))
        sxy = sum(i * y[i] for i in range(n))
        denom = n * sxx - sx * sx
        if denom == 0:
            return list(y)
        b = (n * sxy - sx * sy) / denom
        a = (sy - b * sx) / n
        return [y[i] - (a + b * i) for i in range(n)]

    @staticmethod
    def _autocorr(y):
        """归一化自相关，r[0] 恒为 1。纯 Python O(n^2)，n 很小（数十~数百）可接受。"""
        n = len(y)
        if n == 0:
            return []
        mean = sum(y) / n
        var = sum((v - mean) ** 2 for v in y)
        if var <= 0:
            return [0.0] * n
        r = [0.0] * n
        for k in range(n):
            num = 0.0
            for i in range(n - k):
                num += (y[i] - mean) * (y[i + k] - mean)
            r[k] = num / var
        return r

    @staticmethod
    def _zero_crossings(y):
        zc = 0
        prev = y[0]
        for v in y[1:]:
            if (prev < 0 <= v) or (prev > 0 >= v):
                zc += 1
            prev = v
        return zc

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def detect(self):
        """返回 dict；样本不足或无法估计时返回 None。

        返回字段：
          detected       bool   是否判定为「存在呼吸的人」
          confidence     float  0~1 置信度
          breath_rate_hz float  呼吸频率估计
          period_s       float  呼吸周期估计（秒）
          amplitude_cms  float  去趋势后速度 RMS（cm/s）
          corr_peak      float  归一化自相关峰值（-1~1）
          zero_crossings int    过零次数
          samples        int    参与计算的样本数
          fs_hz          float  有效采样率
        """
        # 相对最新样本的时间戳做清理（兼容绝对/相对时间戳），而非依赖真实墙钟
        if self.buffer:
            self._prune(self.buffer[-1][0])
        n = len(self.buffer)
        if n < self.min_samples:
            return None

        ts = [p[0] for p in self.buffer]
        sp = [p[1] for p in self.buffer]
        span = ts[-1] - ts[0]
        if span <= 0:
            return None
        fs = (n - 1) / span

        # 1) 去趋势
        y = self._detrend(sp)

        # 2) 幅值
        rms = math.sqrt(sum(v * v for v in y) / n)

        # 3) 自相关 + 周期搜索
        r = self._autocorr(y)
        lag_min = max(1, int(round(self.period_min * fs)))
        lag_max = min(n - 1, int(round(self.period_max * fs)))
        best_lag = None
        best_r = -1.0
        if lag_max >= lag_min:
            for k in range(lag_min, lag_max + 1):
                if r[k] > best_r:
                    best_r = r[k]
                    best_lag = k

        # 4) 过零
        zc = self._zero_crossings(y)

        if best_lag is None or best_lag <= 0:
            return None
        period_s = best_lag / fs
        breath_rate_hz = 1.0 / period_s

        # 判定：周期足够显著 + 幅值足够大 + 确有振荡（至少一次完整周期 = 2 次过零）
        corr_ok = best_r >= self.corr_threshold
        amp_ok = rms >= self.min_amplitude
        osc_ok = zc >= 2
        detected = corr_ok and amp_ok and osc_ok

        # 置信度：自相关峰值与幅值归一化后加权
        conf_corr = max(0.0, min(1.0, (best_r - 0.0) / 1.0))
        conf_amp = max(0.0, min(1.0, rms / max(self.min_amplitude * 4.0, 1e-6)))
        confidence = 0.6 * conf_corr + 0.4 * conf_amp

        return {
            'detected': bool(detected),
            'confidence': round(confidence, 3),
            'breath_rate_hz': round(breath_rate_hz, 3),
            'period_s': round(period_s, 3),
            'amplitude_cms': round(rms, 3),
            'corr_peak': round(best_r, 3),
            'zero_crossings': int(zc),
            'samples': int(n),
            'fs_hz': round(fs, 3),
        }


# ----------------------------------------------------------------------
# 自检：合成数据验证（无硬件也可运行）
# ----------------------------------------------------------------------
def _self_test():
    import random
    random.seed(0)
    fs = 10.0  # 10 Hz

    def synth(dur, speed_fn, noise=0.4):
        out = []
        n = int(dur * fs)
        for i in range(n):
            t = i / fs
            out.append((t, speed_fn(t) + random.uniform(-noise, noise)))
        return out

    cases = {
        'breathing human (0.3Hz)': lambda t: 8.0 * math.sin(2 * math.pi * 0.3 * t),
        'static wall (zero)':      lambda t: 0.0,
        'slow drift (aperiodic)':  lambda t: 0.5 * t,
        'noise only':              lambda t: 0.0,
    }
    print('=== RespirationDetector self-test ===')
    for name, fn in cases.items():
        det = RespirationDetector(min_samples=30)
        for t, s in synth(8.0, fn):
            det.add(t, s)
        r = det.detect()
        if r is None:
            print(f'{name:30s} -> insufficient samples')
        else:
            print(f'{name:30s} -> detected={r["detected"]} conf={r["confidence"]} '
                  f'rate={r["breath_rate_hz"]}Hz amp={r["amplitude_cms"]}cm/s '
                  f'corr={r["corr_peak"]} zc={r["zero_crossings"]}')
    print('Expected: only "breathing human" detected=True; others False.')


if __name__ == '__main__':
    _self_test()
