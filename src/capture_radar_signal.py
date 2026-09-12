#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集 C4002 雷达原始物理量到 CSV，用于标定呼吸检测阈值。

为什么需要它：
  呼吸检测器在空场（桌上静止、无人）时误触发，根因是真实传感器的 move_speed
  并非干净为 0，而是带 1/f 噪声 / 电源纹波 / 风扇空调 / 桌面微振动造成的
  低频周期起伏。只有先抓一段空场的真实信号，才能把阈值定在"噪声之上、呼吸之下"。

用法（在 src 目录运行）：
  export RADAR_PORTS=/dev/ttyUSB0,/dev/ttyUSB1,/dev/ttyUSB2
  export RADAR_ANGLES=-45,0,45
  python3 capture_radar_signal.py [时长秒]

环境变量：
  CAPTURE_DURATION   采集时长 s，默认 20
  CAPTURE_OUT        输出 CSV 路径，默认 radar_signal.csv

输出：
  radar_signal.csv   逐帧：timestamp,angle,move_speed,move_distance,move_energy,exist_distance,exist_energy
  末尾打印每个雷达 move_speed 的 RMS / 峰值 / 非零占比，用于定 C4002_BREATH_MIN_AMPLITUDE。
"""

import csv
import os
import sys
import time
from collections import defaultdict

try:
    from c4002_parser import RealSensorHub
except ImportError:
    print("请在 src 目录下运行，或先 import c4002_parser")
    raise SystemExit(1)


def _ports():
    env = os.getenv('RADAR_PORTS')
    if env:
        return [p.strip() for p in env.split(',') if p.strip()]
    if os.name != 'nt':
        return ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2']
    return []


def _angles():
    env = os.getenv('RADAR_ANGLES')
    if env:
        try:
            return [float(a.strip()) for a in env.split(',') if a.strip()]
        except Exception:
            pass
    return [-45, 0, 45]


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else float(os.getenv('CAPTURE_DURATION', '20'))
    out_path = os.getenv('CAPTURE_OUT', 'radar_signal.csv')
    ports = _ports()
    angles = _angles()

    if not ports:
        print("未设置 RADAR_PORTS，且当前非 Linux，无法打开真实串口。")
        raise SystemExit(1)

    hub = RealSensorHub(ports=ports, angles=angles)
    print(f"采集 {duration:.0f}s，输出到 {out_path}")
    print("映射：")
    for i, (p, a) in enumerate(zip(ports, angles)):
        print(f"  sensor {i} = {a:>5}° -> {p}")

    acc = defaultdict(list)   # angle -> list of move_speed
    t0 = time.time()
    with open(out_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['timestamp', 'angle', 'move_speed', 'move_distance',
                    'move_energy', 'exist_distance', 'exist_energy'])
        while time.time() - t0 < duration:
            for r in hub.radars:
                d = r.read_data()
                if not d:
                    continue
                # 无效帧返回 {'valid': False}，没有 angle 键；跳过以免产生 None 键
                if not d.get('valid'):
                    continue
                ang = d.get('angle')
                if ang is None:
                    continue
                sp = d.get('move_speed', 0)
                acc[ang].append(sp)
                w.writerow([
                    round(d.get('timestamp', time.time()), 4), ang,
                    sp, d.get('move_distance'), d.get('move_energy'),
                    d.get('exist_distance'), d.get('exist_energy'),
                ])
        f.flush()

    print("\n=== move_speed 统计（用于定 C4002_BREATH_MIN_AMPLITUDE）===")
    for ang in sorted(acc):
        sp = acc[ang]
        if not sp:
            print(f"{ang:>5}°: 无数据")
            continue
        n = len(sp)
        rms = (sum(v * v for v in sp) / n) ** 0.5
        peak = max(abs(v) for v in sp)
        nonzero = sum(1 for v in sp if v != 0) / n
        print(f"{ang:>5}°: n={n:4d}  rms={rms:6.2f} cm/s  peak={peak:6.2f} cm/s  "
              f"非零占比={nonzero:.0%}")
    print("\n提示：空场时若 rms/peak 明显非零，说明有慢周期噪声，")
    print("      C4002_BREATH_MIN_AMPLITUDE 应设在 空场peak 之上、真实呼吸peak 之下。")


if __name__ == '__main__':
    main()
