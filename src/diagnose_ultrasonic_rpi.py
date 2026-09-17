#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单路超声波诊断（pigpio 后端）：定位"某个方向测不到距离"的问题。

针对当前 ultrasonic_rpigpio.py 的接线（TRIG/ECHO 均为 BCM 编号）：
  左 -45°：TRIG=22(物理15), ECHO=26(物理37)  # 原 23 损坏，已换
  中  0° ：TRIG=24(物理18), ECHO=25(物理22)
  右 +45°：TRIG=5 (物理29), ECHO=6 (物理31)

用法：
  sudo pigpiod                                    # 先确保 pigpiod 运行
  python3 diagnose_ultrasonic_rpi.py              # 三路都测
  python3 diagnose_ultrasonic_rpi.py -45          # 只测左路（重点排查）

诊断输出分三步：
  1. ECHO 引脚空闲电平：正常应为 0；若恒为 1，多半是接线/分压/传感器异常
  2. 触发后是否出现 ECHO 高电平：200ms 内无高电平 = 无回波（接线或探头坏）
  3. 脉宽换算距离：有脉宽但距离离谱 = 分压电阻问题或探头损坏
"""

import os
import sys
import time
import argparse

try:
    import pigpio
except ImportError:
    print("缺少 pigpio，请执行：pip install pigpio")
    raise SystemExit(1)

# 与 ultrasonic_rpigpio.py 保持一致
TRIG_PINS = { -45: 22, 0: 24, 45: 5 }
ECHO_PINS = { -45: 26, 0: 25, 45: 6 }   # 左路 ECHO 原为 23(物理16)损坏，改到 26(物理37)
US_PER_CM = 2.0 / 34300.0 * 1e6   # ≈58.3 us/cm


def diagnose_one(pi, angle, trig, echo, rounds=5):
    print(f"\n=== 角度 {angle:+d}°  TRIG=BCM{trig}(物理{_phys(trig)})  "
          f"ECHO=BCM{echo}(物理{_phys(echo)}) ===")

    # 1. 空闲电平
    lvl = pi.read(echo)
    print(f"[1] ECHO 空闲电平 = {lvl}  {'正常(0)' if lvl == 0 else '!!! 异常(恒为1)'}")
    if lvl != 0:
        print("     可能原因：ECHO 分压电阻缺失/接反、悬空、或 GPIO 已损坏")

    # 2. 触发并测脉宽
    ok = 0
    dists = []
    for r in range(rounds):
        pi.gpio_trigger(trig, 20, 1)     # 20us 触发
        t0 = time.time()
        start = None
        end = None
        while time.time() - t0 < 0.2:
            if pi.read(echo):
                start = time.time()
                while pi.read(echo) and time.time() - t0 < 0.2:
                    pass
                end = time.time()
                break
            time.sleep(0.0001)
        if start and end:
            dur_us = (end - start) * 1e6
            cm = dur_us / US_PER_CM
            ok += 1
            dists.append(cm)
        time.sleep(0.05)

    print(f"[2] {rounds} 次触发中 {ok} 次有回波")
    if dists:
        print(f"[3] 距离(cm) = {['%.1f' % d for d in dists]}")
        print(f"     平均 = {sum(dists)/len(dists):.1f} cm")
    else:
        print("[3] 完全无回波 -> 检查 TRIG/ECHO 接线、探头正前方是否有障碍物(20~600cm)、探头是否损坏")


def _phys(bcm):
    """BCM -> 物理排针号（仅 40pin 常用脚）。"""
    m = {
        2: 3, 3: 5, 4: 7, 5: 29, 6: 31,
        17: 11, 18: 12, 22: 15, 23: 16, 24: 18, 25: 22,
        27: 13,
    }
    return m.get(bcm, '?')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("angle", nargs="?", type=int, default=None,
                    help="只测一个角度，如 -45 / 0 / 45")
    args = ap.parse_args()

    pi = pigpio.pi()
    if not pi.connected:
        print("无法连接 pigpiod，请先执行：sudo pigpiod")
        return

    for trig, echo in zip(TRIG_PINS.values(), ECHO_PINS.values()):
        pi.set_mode(trig, pigpio.OUTPUT)
        pi.set_mode(echo, pigpio.INPUT)
        pi.set_pull_up_down(echo, pigpio.PUD_OFF)
        pi.write(trig, 0)

    angles = [args.angle] if args.angle is not None else list(TRIG_PINS)
    for a in angles:
        if a not in TRIG_PINS:
            print(f"无效角度 {a}，可选 {-45, 0, 45}")
            continue
        diagnose_one(pi, a, TRIG_PINS[a], ECHO_PINS[a])

    pi.stop()


if __name__ == "__main__":
    main()
