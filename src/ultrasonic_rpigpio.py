"""基于树莓派板载 GPIO 的超声波测距模块（推荐方案）。

为什么不用 FT232H：
  FT232H 测回波脉宽需要反复 read() 轮询 ECHO 引脚，每次是 USB 控制传输，
  往返延迟约 0.2-0.5ms。近距离回波脉宽只有 1-2ms，会被 USB 延迟淹没导致
  漏采或误差巨大。树莓派板载 GPIO 用 pigpio 硬件定时器 + 边沿回调，
  时间戳分辨率 1us（毫米级），近距离也能准确测量。

接线（BCM 编号，避开声卡 I2C 2/3、I2S 18-21、SPI 8-11、UART 14/15、EEPROM 0/1）：
  左 -45°：TRIG=22, ECHO=23
  中  0° ：TRIG=24, ECHO=25
  右 +45°：TRIG=5,  ECHO=6
  所有传感器 GND 接树莓派 GND，VCC 接 5V（Pin 2/4）。

  !!! 重要：ECHO 是 5V 输出，树莓派 GPIO 只耐 3.3V，必须分压 !!!
  每个 ECHO 引脚接线：
    ECHO ──[1kΩ]──┬── GPIO
                  [2kΩ]
                   │
                  GND
  （5V × 2k/(1k+2k) ≈ 3.3V）

  依赖：pip install pigpio；sudo pigpiod
"""

import os
import time
from collections import deque
import pigpio

TRIG_PINS = [22, 24, 5]
ECHO_PINS = [23, 25, 6]
ANGLES = [-45, 0, 45]

# 声速 343 m/s，往返：距离(cm) = 时间(us) / 58.3
US_PER_CM = 2.0 / 34300.0 * 1e6   # ≈ 58.3 us/cm


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


class _Sensor:
    """单个传感器：pigpio 边沿回调记录 ECHO 脉宽（微秒）。"""

    def __init__(self, pi, trig, echo):
        self.pi = pi
        self.trig = trig
        self.echo = echo
        self._rise = None
        self._pulse_us = None
        pi.set_mode(trig, pigpio.OUTPUT)
        pi.set_mode(echo, pigpio.INPUT)
        pi.set_pull_up_down(echo, pigpio.PUD_OFF)
        self.cb = pi.callback(echo, pigpio.EITHER_EDGE, self._edge)

    def _edge(self, gpio, level, tick):
        if level == 1:                      # 上升沿
            self._rise = tick
        elif self._rise is not None:        # 下降沿
            self._pulse_us = pigpio.tickDiff(self._rise, tick)
            self._rise = None

    def measure(self, timeout_s=0.08):
        self._rise = None
        self._pulse_us = None
        self.pi.gpio_trigger(self.trig, 20, 1)   # 20us 触发脉冲
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            if self._pulse_us is not None:
                return self._pulse_us / US_PER_CM
            time.sleep(0.0002)
        return None


class RpiUltrasonic:
    """三方向超声波 hub，接口与 ultrasonic_mpsse.MpsseUltrasonic 兼容。"""

    def __init__(self):
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError(
                "无法连接 pigpiod 守护进程。请先运行: sudo pigpiod")
        self.sensors = {}
        for trig, echo, angle in zip(TRIG_PINS, ECHO_PINS, ANGLES):
            self.sensors[angle] = _Sensor(self.pi, trig, echo)

    def measure(self, trig, echo):
        """兼容旧接口：按 trig/echo 查找对应角度。"""
        for angle, s in self.sensors.items():
            if s.trig == trig and s.echo == echo:
                return s.measure()
        return None

    def measure_by_angle(self, angle):
        return self.sensors[angle].measure()

    def close(self):
        for s in self.sensors.values():
            s.cb.cancel()
        self.pi.stop()


class DistanceFilter:
    """中位数去噪 + 突变剔除 + EMA 平滑 + 短暂无回波保持。"""

    def __init__(self, window=None, spike_cm=None, spike_hold=None, alpha=None,
                 miss_clear=None):
        self.window = window if window is not None else _env_int('ULTRA_WINDOW', 5)
        self.spike_cm = spike_cm if spike_cm is not None else _env_float('ULTRA_SPIKE_CM', 30.0)
        self.spike_hold = spike_hold if spike_hold is not None else _env_int('ULTRA_SPIKE_HOLD', 3)
        self.alpha = alpha if alpha is not None else _env_float('ULTRA_EMA_ALPHA', 0.5)
        self.miss_clear = miss_clear if miss_clear is not None else _env_int('ULTRA_MISS_CLEAR', 6)
        self.history = deque(maxlen=max(1, self.window))
        self.last_ema = None
        self.last_valid = None
        self.hold_count = 0
        self.miss_count = 0

    def update(self, raw_cm):
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


class UltrasonicSensor:
    """单个方向：read_data() 返回 data_fusion 所需 dict。"""

    def __init__(self, backend, trig, echo, angle, min_cm=20.0, max_cm=600.0,
                 filt=None):
        self.backend = backend
        self.trig = trig
        self.echo = echo
        self.angle = angle
        self.min_cm = min_cm
        self.max_cm = max_cm
        self.filt = filt if filt is not None else DistanceFilter()

    def read_data(self):
        raw = self.backend.measure_by_angle(self.angle)
        if raw is None or not (self.min_cm <= raw <= self.max_cm):
            held = self.filt.update(None)
            if held is not None:
                return {'angle': self.angle, 'distance': held / 100.0,
                        'presence': 0, 'valid': True, 'timestamp': time.time()}
            return {'angle': self.angle, 'distance': 0.0, 'presence': 0,
                    'valid': False, 'timestamp': time.time()}
        smoothed = self.filt.update(raw)
        return {'angle': self.angle, 'distance': smoothed / 100.0,
                'presence': 0, 'valid': True, 'timestamp': time.time()}


def main():
    try:
        backend = RpiUltrasonic()
    except RuntimeError as e:
        print(f"错误: {e}")
        return
    sensors = [UltrasonicSensor(backend, trig, echo, angle)
               for trig, echo, angle in zip(TRIG_PINS, ECHO_PINS, ANGLES)]
    print("树莓派 GPIO 超声波已就绪（pigpio 硬件定时，微秒级精度）")
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
            time.sleep(0.2)
    except KeyboardInterrupt:
        backend.close()
        print("\n测试结束")


if __name__ == "__main__":
    main()
