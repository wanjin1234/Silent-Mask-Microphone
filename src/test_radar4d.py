#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4D 成像雷达串口调试：打印帧统计、点云与分类结果（不依赖 pygame）。

用法::

    python3 test_radar4d.py [端口] [选项]
    python3 test_radar4d.py --simulate       # 模拟点云，验证解析/分类（无需硬件）
    python3 test_radar4d.py --selftest       # radar_4d 内置回环自测
    python3 test_radar4d.py --raw            # 原始字节探测：只打印 hex，不解析（排查波特率/接线）
    python3 test_radar4d.py --config         # 发完整默认配置 + scanstart（默认只发 scanstart）
    python3 test_radar4d.py --no-start       # 不发任何指令，直接读（诊断用）

说明::
    雷达默认不输出数据，必须先下发 scanstart 才上报点云，因此本脚本
    默认会先发送 scanstart（除非 --no-start）。若 --raw 模式能看到字节、
    但解析不出帧，多半是波特率或协议不匹配。

环境变量::

    R4D_PORT     串口路径（默认 /dev/ttyACM0）
    R4D_BAUD     波特率（默认 3000000）
    R4D_CMD_EOL  指令换行符（默认 \\r\\n，可改 \\n）
    R4D_DEBUG    设 1 打印原始帧/重同步/发指令日志
"""

import os
import sys
import time

from radar_4d import Radar4D, classify_targets, _self_test, Point, SimulatedRadar4D


def dump_frame(f, classify=True):
    print(f"\n=== frame id={f.frame_id} pts={len(f.points)} "
          f"tracks={len(f.tracks)} ===")
    if f.bb_ms is not None:
        print(f"  耗时: bb={f.bb_ms}ms post={f.postbb_ms}ms "
              f"tx={f.tx_ms}ms interval={f.interval_ms}ms")
    if classify and f.points:
        res = classify_targets(f.points)
        print(f"  人体: {res['humans']}")
        print(f"  障碍: {res['obstacles']}")
    # 点云摘要（按类别计数）
    from collections import Counter
    c = Counter(p.cls for p in f.points)
    print(f"  类别: {dict(c)}")
    if f.points:
        print(f"  前 5 点: {[(round(p.x,2), round(p.y,2), round(p.z,2), p.cls) for p in f.points[:5]]}")


def raw_dump(radar):
    """只打印原始字节（hex + 可读 ASCII），不解析。用于排查接线/波特率。"""
    print("原始字节模式：按 Ctrl+C 退出。看到任何字节都说明串口/波特率基本正常。")
    while True:
        try:
            n = radar.ser.in_waiting
        except Exception:
            n = 0
        if n > 0:
            data = radar.ser.read(n)
            # hex 每行 32 字节
            for i in range(0, len(data), 32):
                chunk = data[i:i + 32]
                hexs = ' '.join(f'{b:02X}' for b in chunk)
                asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
                print(f'{i:04X}  {hexs:<95}  {asc}')
        else:
            time.sleep(0.01)


def main():
    if '--selftest' in sys.argv:
        _self_test()
        return
    if '--simulate' in sys.argv:
        radar = SimulatedRadar4D()
        print("模拟点云模式，Ctrl+C 退出")
        try:
            while True:
                f = radar.read_frame()
                dump_frame(f)
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n结束")
        return

    raw_mode = '--raw' in sys.argv
    do_config = '--config' in sys.argv
    no_start = '--no-start' in sys.argv

    port = None
    for a in sys.argv[1:]:
        if not a.startswith('--'):
            port = a
    if port is None:
        port = os.getenv('R4D_PORT', '/dev/ttyACM0')

    radar = Radar4D(port=port)
    if radar.ser is None:
        print(f"无法打开 {port}，退出。可尝试 --simulate 用模拟数据。")
        sys.exit(1)

    print(f"读取 {port} @ {radar.baud} baud，Ctrl+C 退出")

    if raw_mode:
        try:
            raw_dump(radar)
        except KeyboardInterrupt:
            print("\n结束")
        finally:
            radar.close()
        return

    # 默认发 scanstart；--config 发完整配置；--no-start 不发任何指令
    if not no_start:
        if do_config:
            print("发送默认配置 + scanstart ...")
            radar.configure_defaults()
        else:
            print("发送 scanstart ...")
            radar.scan_start()
        time.sleep(0.3)

    try:
        while True:
            f = radar.read_frame()
            if f is not None:
                dump_frame(f)
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n结束")
    finally:
        radar.close()


if __name__ == '__main__':
    main()
