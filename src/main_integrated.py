#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集成主程序：雷达透视视图 + 热成像，GPIO 按钮切换模式。

数据链路（雷达）用 radarpi（scan start + keepalive），热成像用 MLX90640，
超声波（可选）提供底部距离条。按 GPIO 按钮（或键盘 T）在两种显示间切换：

    radar   —— 透视点云 + 顶部角度刻度 + 底部三弧形距离条（超声波）
    thermal —— 32x24 热成像色块铺满屏幕

运算压力控制：当前模式不用的传感器不做数据处理。
  - 雷达串口保持常开 + 后台 keepalive（否则雷达会挂死），但仅在 radar 模式
    下才做聚类/跟踪/累积等重计算；thermal 模式下只排空缓冲、不处理。
  - 热成像仅在 thermal 模式下读帧；radar 模式下休眠。
  - 超声波仅在 radar 模式下测距；thermal 模式下休眠。

用法（树莓派，需系统 python3 已装 pygame/numpy/smbus2/pigpio）::

    # 真机（雷达 + 热成像 + 超声波）
    python3 src/main_integrated.py

    # 无硬件时模拟（雷达模拟点云 + 热成像合成帧）
    RADAR_SIMULATE=1 THERMAL_SIMULATE=1 python3 src/main_integrated.py

按键：T 切换模式（等价 GPIO 按钮），ESC 退出。
环境变量：
    RADAR_SIMULATE        1=雷达用模拟点云
    THERMAL_SIMULATE      1=热成像用合成帧
    DISABLE_ULTRASONIC    1=禁用超声波（无 pigpiod 时）
    BUTTON_GPIO           切换按钮 GPIO（默认 4）
    其余雷达/跟踪/过滤环境变量见 radar_view.py
"""

import os
import sys
import time
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
# radarpi 包路径
for _c in (
    os.path.join(_HERE, '..', 'radarpi', 'src'),
    os.path.join(_HERE, '..', 'radarpi', 'radarpi', 'src'),
):
    _c = os.path.abspath(_c)
    if os.path.isdir(os.path.join(_c, 'radarpi')):
        sys.path.insert(0, _c)
        break
# 热成像包路径（兼容 mlx90640-thermal 与 thermal_ai 两种目录）
for _td in ('mlx90640-thermal', 'thermal_ai'):
    _THERMAL_DIR = os.path.abspath(os.path.join(_HERE, '..', _td))
    if os.path.isdir(_THERMAL_DIR):
        sys.path.insert(0, _THERMAL_DIR)
        break

from radarpi.link import RadarLink, SimulatedLink      # noqa: E402
from stereo_ar_display import StereoARDisplay           # noqa: E402
from radar_tracker import TargetTracker, PointAccumulator  # noqa: E402
from radar_filter import ClutterFilter                  # noqa: E402
from radar_view import classify, _frame_stats, GROUP_TO_CLS, \
    MICRO_GROUPS, MOVING_V_MIN, CLUSTER_EPS, CLUSTER_MIN_POINTS  # noqa: E402

try:
    from gpio_button import GpioButton
except Exception as exc:
    print("[button] GPIO 按钮不可用: %s" % exc)
    GpioButton = None

# 热成像（可选依赖，模拟时不需要）
try:
    from mlx90640_thermal_display import (
        Mlx90640Sensor, TemperatureScale, synthetic_frame, center_temperature)
except Exception as exc:
    print("[thermal] 热成像模块导入失败: %s" % exc)
    Mlx90640Sensor = TemperatureScale = synthetic_frame = center_temperature = None


# --------------------------------------------------------------------------
# 雷达采集线程（radar 模式处理数据，thermal 模式只排空缓冲）
# --------------------------------------------------------------------------
def radar_worker(link, state, lock, stop, mode):
    tracker = TargetTracker(
        match_dist=float(os.getenv('R4D_TRACK_MATCH_DIST', '0.8')),
        pos_alpha=float(os.getenv('R4D_TRACK_POS_ALPHA', '0.4')),
        confirm_frames=int(os.getenv('R4D_TRACK_CONFIRM', '2')),
        lost_frames=int(os.getenv('R4D_TRACK_LOST', '5')),
    )
    accumulator = PointAccumulator(
        max_frames=int(os.getenv('R4D_ACCUM_FRAMES', '4')),
    )
    filt = ClutterFilter(
        z_range=(float(os.getenv('R4D_FILTER_Z_MIN', '0.15')),
                 float(os.getenv('R4D_FILTER_Z_MAX', '2.6'))),
        y_range=(float(os.getenv('R4D_FILTER_Y_MIN', '0.3')),
                 float(os.getenv('R4D_FILTER_Y_MAX', '9.0'))),
        x_range=(-float(os.getenv('R4D_FILTER_X_RANGE', '4.0')),
                 float(os.getenv('R4D_FILTER_X_RANGE', '4.0'))),
        min_points=int(os.getenv('R4D_FILTER_MIN_POINTS', '3')),
        snr_enabled=os.getenv('R4D_FILTER_SNR', '0') == '1',
        snr_floor=int(os.getenv('R4D_FILTER_SNR_FLOOR', '0')),
    )
    try:
        for frame in link.frames():
            if stop.is_set():
                break
            # 非 radar 模式：只排空（frames() 内部已读串口 + 后台 keepalive），不处理
            if mode['value'] != 'radar':
                continue
            points, humans, obstacles = classify(frame, filt)
            humans, obstacles = tracker.update(humans, obstacles)
            points = accumulator.update(points)
            with lock:
                state['points'] = points
                state['humans'] = humans
                state['obstacles'] = obstacles
                state['stats'] = _frame_stats(frame)
    except Exception as exc:
        print("[radar_worker] 异常: %s" % exc)


# --------------------------------------------------------------------------
# 超声波线程（radar 模式测距，thermal 模式休眠）
# --------------------------------------------------------------------------
def ultrasonic_worker(state, lock, stop, mode):
    if os.getenv('DISABLE_ULTRASONIC', '0') == '1':
        return
    try:
        from ultrasonic_rpigpio import RpiUltrasonic, UltrasonicSensor, TRIG_PINS, ECHO_PINS, ANGLES
        backend = RpiUltrasonic()
        sensors = [UltrasonicSensor(backend, t, e, a)
                   for t, e, a in zip(TRIG_PINS, ECHO_PINS, ANGLES)]
    except Exception as exc:
        print("[ultrasonic] 初始化失败（禁用）: %s" % exc)
        return
    try:
        while not stop.is_set():
            if mode['value'] != 'radar':
                time.sleep(0.1)
                continue
            out = []
            for s in sensors:
                r = s.read_data()
                if r.get('valid'):
                    out.append({'angle': r['angle'], 'distance': r['distance']})
            with lock:
                state['ultrasonic'] = out
            time.sleep(0.05)
    except Exception as exc:
        print("[ultrasonic] 异常: %s" % exc)
    finally:
        try:
            backend.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 热成像线程（thermal 模式读帧，radar 模式休眠）
# --------------------------------------------------------------------------
def thermal_worker(state, lock, stop, mode):
    simulate = os.getenv('THERMAL_SIMULATE', '0') == '1'
    sensor = None
    scale = None
    # 树莓派 4B 硬件 I2C 对 MLX90640 的 clock stretching 容忍过短，读 RAM 会
    # 报 Input/output error。改用软件 I2C（i2c-gpio bus=3，需 dtoverlay 配置），
    # 由 MLX90640_I2C_BUS 环境变量控制；未配置软件 I2C 时回退默认 bus=1。
    os.environ.setdefault('MLX90640_I2C_BUS',
                          os.getenv('MLX90640_I2C_BUS', '3'))
    if simulate:
        if synthetic_frame is None:
            print("[thermal] 缺少热成像模块，禁用")
            return
        scale = TemperatureScale(None, None)
    else:
        if Mlx90640Sensor is None:
            print("[thermal] 缺少热成像驱动，禁用")
            return
        rate = float(os.getenv('MLX90640_RATE', '8.0'))
        try:
            sensor = Mlx90640Sensor(rate, 800000, 0x33)
            scale = TemperatureScale(None, None)
        except Exception as exc:
            print("[thermal] 传感器初始化失败（禁用）: %s" % exc)
            return

    t0 = time.monotonic()
    try:
        while not stop.is_set():
            if mode['value'] != 'thermal':
                time.sleep(0.1)
                continue
            try:
                temps = synthetic_frame(time.monotonic() - t0) if simulate \
                    else sensor.read_frame()
                low, high = scale.update(temps)
                center = center_temperature(temps)
                peak = max(temps)
                with lock:
                    state['thermal'] = {
                        'temps': temps, 'low': low, 'high': high,
                        'label': f"Center {center:.1f} C   Peak {peak:.1f} C",
                    }
            except Exception as exc:
                print("[thermal] 帧读取失败: %s" % exc)
                time.sleep(0.1)
            # 节拍：模拟模式按 rate 取帧；真机帧率由传感器 refresh_rate 决定，
            # 这里只做短暂让步，避免空转抢 CPU。
            if simulate:
                time.sleep(1.0 / max(rate, 0.5))
            else:
                time.sleep(0.02)
    finally:
        if sensor is not None:
            try:
                sensor.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------
def main():
    import pygame

    simulate = os.getenv('RADAR_SIMULATE', '0') == '1'
    link = SimulatedLink(fps=10.0) if simulate else RadarLink(device='auto', auto_start=True, keepalive=5.0)

    display = StereoARDisplay(1920, 1080)

    lock = threading.Lock()
    mode = {'value': 'radar'}          # 'radar' | 'thermal'
    state = {'points': [], 'humans': [], 'obstacles': [], 'ultrasonic': [],
             'stats': {}, 'thermal': None}
    stop = threading.Event()

    threads = [
        threading.Thread(target=radar_worker, args=(link, state, lock, stop, mode), daemon=True),
        threading.Thread(target=ultrasonic_worker, args=(state, lock, stop, mode), daemon=True),
        threading.Thread(target=thermal_worker, args=(state, lock, stop, mode), daemon=True),
    ]
    for t in threads:
        t.start()

    # GPIO 按钮切换模式
    button = None
    if GpioButton is not None:
        try:
            b = GpioButton()
            if b.enabled:
                button = b
        except Exception:
            button = None

    clock = pygame.time.Clock()
    running = True
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        running = False
                    elif event.key in (pygame.K_t, pygame.K_TAB):
                        mode['value'] = 'thermal' if mode['value'] == 'radar' else 'radar'
                        print("[mode] 切换到 %s" % mode['value'])

            if button is not None and button.poll():
                mode['value'] = 'thermal' if mode['value'] == 'radar' else 'radar'
                print("[mode] 切换到 %s" % mode['value'])

            with lock:
                if mode['value'] == 'radar':
                    points = state['points']
                    humans = state['humans']
                    obstacles = state['obstacles']
                    stats = state['stats']
                    ultrasonic = state['ultrasonic']
                    display.draw_pointcloud_mono(points, humans, obstacles, stats, ultrasonic)
                else:
                    th = state['thermal']
                    if th is not None:
                        display.draw_thermal(th['temps'], th['low'], th['high'], th['label'])
                    else:
                        display.screen.fill((0, 0, 0))

            pygame.display.flip()
            clock.tick(30)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=3.0)
        link.close()
        if button is not None:
            button.close()
        pygame.quit()


if __name__ == '__main__':
    main()
