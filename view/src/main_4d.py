#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4D 成像毫米波雷达主程序：点云透视视图。

替代 main_stereo.py 的 C4002 单点雷达路径——用单颗 4D 成像雷达（60GHz）
输出完整点云，直接渲染"透视/X 光"立体视图，并持续检测移动/呼吸人体。

用法（在 src 目录运行，树莓派）::

    export R4D_PORT=/dev/ttyACM0             # 雷达串口（默认 /dev/ttyACM0）
    python3 main_4d.py

按键::

    V    切换 点云透视 / 俯视散点
    ESC  退出

无硬件时（Windows / 串口打不开）自动回退到模拟点云，便于调试显示层。

环境变量::

    R4D_PORT            雷达串口，默认 /dev/ttyACM0
    R4D_BAUD            波特率，默认 3000000
    R4D_SIMULATE        设 1 强制模拟点云
    R4D_DEBUG           设 1 打印解析/重同步日志
    R4D_SENSOR_HZ       采集循环频率，默认 20
    R4D_MOVING_V_MIN    判"移动"速度阈值 m/s，默认 0.3
    R4D_CLUSTER_EPS     点云聚类邻域半径 m，默认 0.6
    R4D_CLUSTER_MIN_POINTS  一簇最少点数，默认 2
"""

import os
import time
import threading

import pygame

from radar_4d import (Radar4D, SimulatedRadar4D, classify_targets,
                      to_display_coord)
from stereo_ar_display import StereoARDisplay


def _make_radar():
    if os.getenv('R4D_SIMULATE', '0') == '1':
        return SimulatedRadar4D()
    port = os.getenv('R4D_PORT', '/dev/ttyACM0')
    baud = int(os.getenv('R4D_BAUD', '3000000'))
    r = Radar4D(port=port, baud=baud)
    if r.ser is not None:
        return r
    print("[R4D] 串口不可用，回退到模拟点云")
    return SimulatedRadar4D()


def radar_worker(lock, state, radar, stop_event):
    """后台采集线程：读帧 → 聚类分类 → 轴映射 → 发布共享状态。"""
    # 雷达默认不输出数据，启动时先下发配置 + scanstart（模拟源跳过）。
    if hasattr(radar, 'configure_defaults') and os.getenv('R4D_SEND_CONFIG', '1') != '0':
        print("[R4D] 下发默认配置 + scanstart ...")
        radar.configure_defaults()
    hz = float(os.getenv('R4D_SENSOR_HZ', '20'))
    interval = 1.0 / hz
    last = 0.0
    while not stop_event.is_set():
        now = time.time()
        if now - last < interval:
            time.sleep(0.001)
            continue
        last = now

        frame = radar.read_frame()
        if frame is None:
            continue

        # 雷达坐标系(X左右,Y前方,Z高度) -> 显示坐标系(x左右,y高度,z前方)
        pts_disp = []
        for p in frame.points:
            dx, dy, dz = to_display_coord(p.x, p.y, p.z)
            pts_disp.append({'x': dx, 'y': dy, 'z': dz, 'v': p.v, 'cls': p.cls})

        res = classify_targets(frame.points)
        humans = []
        for h in res['humans']:
            dx, dy, dz = to_display_coord(h['x'], h['y'], h['z'])
            humans.append({'x': dx, 'y': dy, 'z': dz, 'v': h['v'],
                           'state': h['state']})
        obstacles = []
        for o in res['obstacles']:
            dx, dy, dz = to_display_coord(o['x'], o['y'], o['z'])
            obstacles.append({'x': dx, 'y': dy, 'z': dz,
                              'n_points': o['n_points']})

        stats = {
            'frame_id': frame.frame_id,
            'n_points': len(frame.points),
            'n_tracks': len(frame.tracks),
            'bb_ms': getattr(frame, 'bb_ms', None),
            'postbb_ms': getattr(frame, 'postbb_ms', None),
            'tx_ms': getattr(frame, 'tx_ms', None),
            'interval_ms': getattr(frame, 'interval_ms', None),
        }

        with lock:
            state['points'] = pts_disp
            state['humans'] = humans
            state['obstacles'] = obstacles
            state['stats'] = stats


def draw_top_scatter(display, points, humans):
    """俯视散点视图：单屏、上=前方，快速观察点云分布（调试用）。"""
    screen = display.screen
    screen.fill((10, 10, 18))
    cx = display.width // 2
    cy = display.height // 2
    scale = 90 * display.ui_scale  # 每米像素数
    # 距离环
    for r in range(1, 7):
        pygame.draw.circle(screen, (50, 55, 70), (cx, cy), int(r * scale), 1)
    # 点云：x=左右 -> 屏幕 x；z=前方 -> 屏幕 y（上=前方）
    for p in points:
        sx = cx + int(p['x'] * scale)
        sy = cy - int(p['z'] * scale)
        if 0 <= sx < display.width and 0 <= sy < display.height:
            pygame.draw.circle(screen, (120, 120, 140), (sx, sy), 2)
    # 人体
    for h in humans:
        sx = cx + int(h['x'] * scale)
        sy = cy - int(h['z'] * scale)
        col = (255, 80, 60) if h.get('state') == 'moving' else (40, 220, 255)
        pygame.draw.circle(screen, col, (sx, sy), 6)
        pygame.draw.circle(screen, (255, 255, 255), (sx, sy), 6, 1)


def main():
    radar = _make_radar()
    display = StereoARDisplay(1920, 1080)

    lock = threading.Lock()
    state = {'points': [], 'humans': [], 'obstacles': [], 'stats': {}}
    stop_event = threading.Event()
    worker = threading.Thread(target=radar_worker,
                              args=(lock, state, radar, stop_event), daemon=True)
    worker.start()

    view_mode = 'pointcloud'   # 默认点云透视
    clock = pygame.time.Clock()
    running = True
    fps_frames = 0
    fps_t0 = time.time()
    fps_current = 0.0

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_v:
                    view_mode = 'top' if view_mode == 'pointcloud' else 'pointcloud'

        with lock:
            points = state['points']
            humans = state['humans']
            obstacles = state['obstacles']
            stats = state['stats']

        if view_mode == 'pointcloud':
            display.draw_pointcloud(points, humans, obstacles, stats)
        else:
            draw_top_scatter(display, points, humans)

        # FPS 统计
        fps_frames += 1
        now_f = time.time()
        if now_f - fps_t0 >= 0.5:
            fps_current = fps_frames / (now_f - fps_t0)
            fps_frames = 0
            fps_t0 = now_f

        pygame.display.flip()
        clock.tick(30)

    stop_event.set()
    worker.join(timeout=3.0)
    pygame.quit()


if __name__ == '__main__':
    main()
