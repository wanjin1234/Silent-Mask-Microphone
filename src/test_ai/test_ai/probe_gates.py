# -*- coding: utf-8 -*-
"""上机探针：确认 C4002 距离门字段的真实语义与解析是否可信。

为什么必须先跑这个
------------------
``exist_gate_index`` 在官方协议里是「距离门位掩码」：80cm 分辨率下 bit0~15、
20cm 分辨率下 bit0~25 各对应一个距离门，置 1 表示该门内有目标。
但**不同固件/批次存在差异**（有的返回单门序号而非掩码，有的位宽不同），
而且本项目的 ``c4002_parser.py`` 从未使用过这个字段，等于没有经过实测验证。
如果直接按假设训练模型，字段语义错了会静默地毁掉精度。

本脚本做三件事：
1. 打印原始十六进制帧与逐字段解析，人工可核对偏移；
2. 让人在雷达前走动，观察位掩码如何变化，判断是「掩码」还是「单门序号」，
   以及有没有 bit>=16 被置位（有则说明是 20cm 分辨率 / 26 位）；
3. 统计 ``data_len`` 是否与帧长自洽，验证偏移基准是否正确。

用法（树莓派，先 export RADAR_PORTS / RADAR_ANGLES）::

    python probe_gates.py --seconds 30
    python probe_gates.py --seconds 30 --csv gates_probe.csv   # 顺便录一份带新字段的 CSV

判读方法
--------
* 人静止站在约 2m 处：应只有 1~3 个相邻 bit 被置位，且随距离变化整体平移。
* 若某一路长期只有**单个** bit 且数值等于「距离/0.8」的门序号 —— 也可能是掩码
  只置一位（正常现象）；要区分请看「走动时是否出现多个相邻位同时置位」。
* 若出现 bit>=16：说明位宽大于 16，请 ``export C4002_GATE_BITS=26`` 后重采。
* 若 ``data_len`` 与帧长不符打印告警：说明字段偏移假设需要重新核对。
"""

import argparse
import csv
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    p = argparse.ArgumentParser(description='C4002 距离门字段上机探针')
    p.add_argument('--seconds', type=float, default=30.0)
    p.add_argument('--ports', default=None, help='逗号分隔，默认取 RADAR_PORTS')
    p.add_argument('--angles', default=None, help='逗号分隔，默认取 RADAR_ANGLES')
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--gate-bits', type=int, default=16)
    p.add_argument('--csv', default=None, help='把每帧原始字段写入 CSV')
    p.add_argument('--raw', action='store_true', help='同时打印原始十六进制帧')
    args = p.parse_args()

    ports = (args.ports or os.getenv('RADAR_PORTS') or '').strip()
    ports = [x.strip() for x in ports.split(',') if x.strip()] if ports else None
    angles = (args.angles or os.getenv('RADAR_ANGLES') or '-45,0,45').strip()
    angles = [float(x) for x in angles.split(',') if x.strip()]
    if not ports:
        print('需要 --ports 或环境变量 RADAR_PORTS（建议用 /dev/serial/by-path/ 稳定路径）')
        return 2

    from c4002_ext import RawC4002Serial
    radars = []
    for i, port in enumerate(ports):
        ang = angles[i] if i < len(angles) else 0.0
        r = RawC4002Serial(port=port, baud=args.baud, angle=ang, sensor_id=i,
                           gate_bits=args.gate_bits)
        r.debug = bool(args.raw)
        if r.ser is None:
            print(f'[!] 打不开 {port}（角度 {ang:+.0f}°）')
            continue
        radars.append(r)
    if not radars:
        print('没有任何雷达可用')
        return 1
    print(f'已打开 {len(radars)} 路：'
          f'{[(r.angle, r.port) for r in radars]}')
    print(f'距离门位数假设 {args.gate_bits}（若看到 bit>={args.gate_bits} 被置位，'
          f'请改用 C4002_GATE_BITS=26）')
    print(f'\n请让被测者在雷达前 ~1.5m 处左右走动，或静止站立呼吸。采集 {args.seconds:.0f}s …\n')

    stats = {r.angle: {'popcounts': Counter(), 'max_bit': -1, 'lens': Counter(),
                       'dists': [], 'n': 0} for r in radars}
    w = None
    f_csv = None
    if args.csv:
        f_csv = open(args.csv, 'w', newline='', encoding='utf-8')
        w = csv.writer(f_csv)
        w.writerow(['timestamp', 'angle', 'target_status', 'light', 'exist_gate_index',
                    'gate_count', 'exist_count_down', 'exist_distance', 'exist_energy',
                    'move_distance', 'move_speed', 'move_energy', 'move_direction',
                    'data_len'] + [f'gate{i}' for i in range(args.gate_bits)])

    t0 = time.time()
    last_print = 0.0
    try:
        while time.time() - t0 < args.seconds:
            for r in radars:
                d = r.read_data()
                if not d or not d.get('valid'):
                    continue
                s = stats[r.angle]
                s['n'] += 1
                s['popcounts'][int(d['gate_count'])] += 1
                s['lens'][int(d.get('data_len', 0))] += 1
                gi = int(d['exist_gate_index'])
                if gi > 0:
                    s['max_bit'] = max(s['max_bit'], gi.bit_length() - 1)
                if d['exist_distance'] > 0:
                    s['dists'].append(float(d['exist_distance']))
                if w:
                    w.writerow([round(d['timestamp'], 4), r.angle, d['target_status'],
                                d['light'], gi, d['gate_count'], d['exist_count_down'],
                                d['exist_distance'], d['exist_energy'],
                                d['move_distance'], d['move_speed'], d['move_energy'],
                                d['move_direction'], d.get('data_len', 0)]
                               + list(d['gate_bits']))
            now = time.time()
            if now - last_print > 0.5:
                last_print = now
                parts = []
                for r in radars:
                    d_last = stats[r.angle]
                    parts.append(f'{r.angle:+.0f}°:n={d_last["n"]}')
                print(f'\r[{now - t0:5.1f}s] ' + '  '.join(parts), end='', flush=True)
    except KeyboardInterrupt:
        print('\n已中断')
    finally:
        if f_csv:
            f_csv.close()

    print('\n\n=== 判读报告 ===')
    for r in radars:
        s = stats[r.angle]
        print(f'\n雷达 {r.angle:+.0f}°  ({r.port})  帧数 {s["n"]}  解析失败 {r.frames_bad_checksum}'
              f'（校验错）/{r.frames_bad_len}（长度异常）')
        if s['n'] == 0:
            print('  未收到任何有效帧：检查端口映射、波特率、接线（VCC/GND/TX/RX 是否交叉）')
            continue
        print(f'  置位数分布（0 表示该帧无目标）：'
              f'{dict(sorted(s["popcounts"].items()))}')
        print(f'  出现过的最高位序号：{s["max_bit"]}'
              f'{"  ← bit>=16，多半是 20cm 分辨率的 26 位掩码，请设 C4002_GATE_BITS=26" if s["max_bit"] >= 16 else ""}')
        print(f'  data_len 取值：{dict(s["lens"])}')
        if s['dists']:
            arr = np.asarray(s['dists'])
            print(f'  存在距离 {arr.min():.2f}~{arr.max():.2f}m，'
                  f'中位数 {np.median(arr):.2f}m（n={arr.size}）')
        # 关键结论
        nonzero = sum(v for k, v in s['popcounts'].items() if k > 0)
        multi = sum(v for k, v in s['popcounts'].items() if k >= 2)
        if nonzero == 0:
            print('  → 全程没有出现过置位：人可能不在覆盖范围内，或该字段语义与假设不同')
        elif multi / max(nonzero, 1) > 0.1:
            print('  → 有相当比例的帧出现**多个相邻位同时置位**，'
                  '与「位掩码 + 同一人体跨相邻门反射」的解释一致，字段可用。')
        else:
            print('  → 几乎总是单个位置位：仍与位掩码解释兼容（只命中一个门），'
                  '但建议让人贴着雷达缓慢前后走动再测一次，确认距离变化时位是否平移。')
        if s['max_bit'] >= args.gate_bits:
            print(f'  → 警告：出现 bit >= {args.gate_bits}，当前位宽假设偏小，'
                  f'请用 C4002_GATE_BITS=26 重新采集，否则模型会丢掉高距离门的特征。')
    if args.csv:
        print(f'\n原始数据已写入 {args.csv}（可用 detect_live.py --source csv --csv 该文件 回放）')
    print('\n下一步：确认字段语义后，用 collect.py 采集带标签的数据集。')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
