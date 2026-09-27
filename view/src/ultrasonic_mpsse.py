"""基于 FT232H 的超声波测距模块。

引脚约定（与 test_ultrasonic.py 一致）：
  TRIG = D0/D2/D4（输出），ECHO = D1/D3/D5（输入）
  方向位：pyftdi 约定 1=输出, 0=输入 -> 输出掩码 0b00010101

实现说明：
  采用 GpioController（异步 bitbang）。FT232H 在 MPSSE 模式下的硬件流式采样
  (READ_BYTES) 只能读取 ADBUS2(D2) 一路，无法同时采三路 ECHO，故不可用。
  异步 bitbang 的 read() 可一次性读回全部 8 个引脚，是 FT232H 上唯一能同时
  读三路 ECHO 的方式。回波脉宽用 perf_counter 计时 + 滤波降低 USB 抖动。
"""

import os
import time
from collections import deque
from pyftdi.gpio import GpioController

TRIG_PINS = [0, 2, 4]   # D0, D2, D4
ECHO_PINS = [1, 3, 5]   # D1, D3, D5
ANGLES = [-45, 0, 45]

DIR_MASK = 0b00010101   # D0/D2/D4 为输出，D1/D3/D5 为输入

# 声速 343 m/s，往返：距离(cm) = 时间(s) * 34300 / 2 = 时间(s) * 17150
CM_PER_SECOND = 34300.0 / 2.0


class MpsseUltrasonic:
    """共享的超声波测距后端：一个 FT232H（异步 bitbang）驱动三只传感器。"""
    def __init__(self, url='ftdi://ftdi:232h/1', echo_timeout_s=None):
        if echo_timeout_s is None:
            echo_timeout_s = float(os.getenv('ULTRA_TIMEOUT_S', '0.05'))
        self.echo_timeout_s = echo_timeout_s
        self.gpio = GpioController()
        self.gpio.open_from_url(url)
        # 方向：D0/D2/D4(TRIG) 输出，D1/D3/D5(ECHO) 输入
        self.gpio.set_direction(0b00111111, DIR_MASK)
        self.gpio.write(0x00)
        # 关键：降低 USB 读超时（默认 5000ms），设备忙时 read_pins 控制传输可能
        # 长时间不返回，降到 100ms 让它快速失败，交给测距超时逻辑处理。
        try:
            self.gpio.ftdi.timeouts = (100, 5000)
        except Exception:
            pass

    def _read_pins(self):
        """读 ADBUS 低 8 位瞬时状态（read_pins，控制传输）。"""
        return self.gpio.read() & 0xFF

    def measure(self, trig, echo):
        """测量单个传感器距离，返回 distance(cm)，无回波/超时返回 None。"""
        echo_mask = 1 << echo

        # 触发前清空 RX FIFO，避免设备因 FIFO 满进入忙状态拖慢 read_pins
        try:
            self.gpio.ftdi.purge_rx_buffer()
        except Exception:
            pass

        # 触发脉冲：低 -> 高(约20us，忙等保证最短脉宽) -> 低
        self.gpio.write(0x00)
        self.gpio.write(1 << trig)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 20e-6:
            pass
        self.gpio.write(0x00)

        # 跳过换能器振铃窗（约 300us），避免误采振铃导致的异常大/小值
        skip_until = time.perf_counter() + 0.0003
        while time.perf_counter() < skip_until:
            pass

        deadline = time.perf_counter() + self.echo_timeout_s
        # 等 ECHO 上升沿
        start = None
        while time.perf_counter() < deadline:
            if self._read_pins() & echo_mask:
                start = time.perf_counter()
                break
        if start is None:
            return None
        # 等 ECHO 下降沿
        while time.perf_counter() < deadline:
            if not (self._read_pins() & echo_mask):
                return (time.perf_counter() - start) * CM_PER_SECOND
        return None

    def read_all(self, min_cm=20.0, max_cm=400.0):
        """依次测量三个方向，返回 [{'angle','distance','valid'}...]。"""
        results = []
        for trig, echo, angle in zip(TRIG_PINS, ECHO_PINS, ANGLES):
            dist = self.measure(trig, echo)
            if dist is not None and min_cm <= dist <= max_cm:
                results.append({'angle': angle, 'distance': dist / 100.0,
                                'presence': 0, 'valid': True,
                                'timestamp': time.time()})
            else:
                results.append({'angle': angle, 'distance': 0.0,
                                'presence': 0, 'valid': False,
                                'timestamp': time.time()})
        return results

    def close(self):
        self.gpio.close()


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


class DistanceFilter:
    """超声波距离滤波：滑动窗口中位数去噪 + 突变剔除 + EMA 平滑 + 短暂无回波保持。

    目的：抑制多径反射/瞬时干扰导致的单次测量跳变，并在偶发"无回波"时保持
    上次有效距离，避免显示闪烁（数据一闪就消失）。
    """
    def __init__(self, window=None, spike_cm=None, spike_hold=None, alpha=None,
                 miss_clear=None):
        self.window = window if window is not None else _env_int('ULTRA_WINDOW', 5)
        self.spike_cm = spike_cm if spike_cm is not None else _env_float('ULTRA_SPIKE_CM', 50.0)
        self.spike_hold = spike_hold if spike_hold is not None else _env_int('ULTRA_SPIKE_HOLD', 3)
        self.alpha = alpha if alpha is not None else _env_float('ULTRA_EMA_ALPHA', 0.5)
        self.miss_clear = miss_clear if miss_clear is not None else _env_int('ULTRA_MISS_CLEAR', 6)
        self.history = deque(maxlen=max(1, self.window))
        self.last_ema = None
        self.last_valid = None
        self.hold_count = 0
        self.miss_count = 0

    def update(self, raw_cm):
        """输入一次原始距离(cm)。

        正常测量返回平滑距离(cm)。连续 miss_clear 次内出现 None 时，保持返回上次
        有效距离；超过后清空并返回 None（表示目标确实离开）。
        """
        if raw_cm is None:
            self.miss_count += 1
            if self.last_valid is not None and self.miss_count <= self.miss_clear:
                return self.last_valid
            self.history.clear()
            self.last_ema = None
            self.last_valid = None
            self.hold_count = 0
            self.miss_count = 0
            return None
        self.miss_count = 0
        self.history.append(raw_cm)
        med = sorted(self.history)[len(self.history) // 2]
        if self.last_valid is not None and abs(med - self.last_valid) > self.spike_cm:
            self.hold_count += 1
            if self.hold_count <= self.spike_hold:
                med = self.last_valid
            else:
                self.hold_count = 0
        else:
            self.hold_count = 0
        if self.last_ema is None:
            self.last_ema = med
        else:
            self.last_ema = self.alpha * med + (1 - self.alpha) * self.last_ema
        self.last_valid = self.last_ema
        return self.last_ema

    def reset(self):
        self.history.clear()
        self.last_ema = None
        self.last_valid = None
        self.hold_count = 0
        self.miss_count = 0


class UltrasonicSensor:
    """单个方向超声波：共享 MPSSE 后端，独立滤波，read_data() 返回融合层所需 dict。"""
    def __init__(self, backend, trig, echo, angle, min_cm=20.0, max_cm=400.0, filt=None):
        self.backend = backend
        self.trig = trig
        self.echo = echo
        self.angle = angle
        self.min_cm = min_cm
        self.max_cm = max_cm
        self.filt = filt if filt is not None else DistanceFilter()

    def read_data(self):
        raw = self.backend.measure(self.trig, self.echo)
        if raw is None or not (self.min_cm <= raw <= self.max_cm):
            # 偶发无回波：由滤波器决定是保持上次距离还是清空
            held = self.filt.update(None)
            if held is not None:
                return {'angle': self.angle, 'distance': held / 100.0, 'presence': 0,
                        'valid': True, 'timestamp': time.time()}
            return {'angle': self.angle, 'distance': 0.0, 'presence': 0,
                    'valid': False, 'timestamp': time.time()}
        smoothed = self.filt.update(raw)
        return {'angle': self.angle, 'distance': smoothed / 100.0, 'presence': 0,
                'valid': True, 'timestamp': time.time()}


def main():
    backend = MpsseUltrasonic()
    print("FT232H 已打开（MPSSE 模式）")
    sensors = [UltrasonicSensor(backend, trig, echo, angle)
               for trig, echo, angle in zip(TRIG_PINS, ECHO_PINS, ANGLES)]
    print("请把障碍物分别放到探头正前方/左/右，观察距离变化，Ctrl+C 停止\n")
    try:
        while True:
            for s in sensors:
                r = s.read_data()
                if r['valid']:
                    print(f"角度{r['angle']:+d}°: {r['distance']*100:.1f} cm")
                else:
                    print(f"角度{r['angle']:+d}°: 无回波")
            print("-" * 40)
            time.sleep(0.3)
    except KeyboardInterrupt:
        backend.close()
        print("\n测试结束")


if __name__ == "__main__":
    main()
