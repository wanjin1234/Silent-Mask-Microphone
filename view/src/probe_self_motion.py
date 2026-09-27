# -*- coding: utf-8 -*-
"""量化"佩戴者自呼吸/晃动"对三个雷达的共模干扰。

实验目的：
  设备戴在人身上（静止佩戴、正常呼吸），前方**没有任何目标**时，
  观察三个雷达各自的 exist_en / move_en / move_speed / target_status。
  —— 若此时雷达大量报"有人"，说明佩戴者自干扰严重，必须用三雷达
  差模对比来区分；若几乎不报，说明现有通道用错才是主因。

用法：
  1. 设置雷达端口/角度（与 run.sh 一致）：
       export RADAR_PORTS=...  export RADAR_ANGLES=-45,0,45
  2. 佩戴好设备，确保前方无人、静止呼吸；
  3. 运行：
       python3 probe_self_motion.py
  4. 观察约 10 秒，每 0.5s 打印三个雷达统计，Ctrl+C 退出。

输出字段（每行一个雷达，按角度）：
  angle         雷达角度
  exist_en      存在通道能量均值(0-100)
  move_en       运动通道能量均值(0-100)
  speed_max     速度绝对值最大(cm/s)
  status        目标状态码分布 Counter
  pres_rate     状态码判为"存在/运动"的帧占比
"""

import os
import time
from collections import Counter, defaultdict

try:
    from c4002_parser import RealSensorHub
except ImportError:
    print("请在本文件所在目录(src)运行，或在 src 下执行。")
    raise SystemExit(1)


def get_ports():
    ports_env = os.getenv('RADAR_PORTS')
    if not ports_env:
        print("未设置 RADAR_PORTS，使用默认 /dev/ttyUSB0,1,2")
        return ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2']
    return [p.strip() for p in ports_env.split(',') if p.strip()]


def get_angles():
    angles_env = os.getenv('RADAR_ANGLES')
    if not angles_env:
        return [-45, 0, 45]
    return [float(a.strip()) for a in angles_env.split(',') if a.strip()]


def main():
    ports = get_ports()
    angles = get_angles()
    hub = RealSensorHub(ports=ports, angles=angles)

    print('=' * 78)
    print('佩戴者自呼吸干扰量化实验')
    print('=' * 78)
    print('请确保：前方无人 / 佩戴者静止呼吸 / 设备静止。\n')
    print('雷达映射：')
    for i, (p, a) in enumerate(zip(ports, angles)):
        print(f'  sensor {i} = {a:>5}°  ->  {p}')
    print('\n开始采样，每 0.5s 输出一次统计（Ctrl+C 退出）...\n')

    # 0.5s 窗口内的累积
    window_len = 0.5
    acc = defaultdict(lambda: {'exist': [], 'move': [], 'speed': [], 'status': []})
    last_dump = time.time()

    try:
        while True:
            for r in hub.radars:
                d = r.read_data()
                if d and d.get('valid'):
                    s = acc[r.sensor_id]
                    s['exist'].append(d.get('exist_en', 0))
                    s['move'].append(d.get('move_en', 0))
                    s['speed'].append(abs(d.get('move_speed', 0)))
                    s['status'].append(d.get('target_status', 0))

            now = time.time()
            if now - last_dump >= window_len:
                last_dump = now
                for sid in sorted(acc):
                    s = acc[sid]
                    n = len(s['status'])
                    if n == 0:
                        continue
                    exist_avg = sum(s['exist']) / n
                    move_avg = sum(s['move']) / n
                    speed_max = max(s['speed'])
                    status_cnt = Counter(s['status'])
                    pres_rate = sum(v for k, v in status_cnt.items() if k != 0) / n
                    ang = angles[sid] if sid < len(angles) else sid
                    print(f"[{ang:>5}°] exist_en={exist_avg:5.1f} move_en={move_avg:5.1f} "
                          f"speed_max={speed_max:4.0f} status={dict(sorted(status_cnt.items()))} "
                          f"pres_rate={pres_rate:.0%}")
                print('-' * 78)
                acc = defaultdict(lambda: {'exist': [], 'move': [], 'speed': [], 'status': []})

            time.sleep(0.02)
    except KeyboardInterrupt:
        print('\n结束。')


if __name__ == '__main__':
    main()

