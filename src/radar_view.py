#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""透视（X 光）UI 主程序：radarpi 数据源 + 立体透视渲染。

数据链路用 radarpi（正确的 ``scan start`` + keepalive，已验证稳定），
渲染用我们之前写的 ``stereo_ar_display.StereoARDisplay.draw_pointcloud``
（双目分屏 + 地面透视网格 + 深度渐隐点云 + 人体辉光）。

用法（树莓派，需系统 python3 已装 pygame/numpy）::

    # 真机
    PYTHONPATH=radarpi/radarpi/src python3 src/radar_view.py

    # 无硬件时用模拟点云验证 UI
    RADAR_SIMULATE=1 PYTHONPATH=radarpi/radarpi/src python3 src/radar_view.py

按键：ESC 退出。

环境变量：
    RADAR_SIMULATE           设 1 用模拟点云
    R4D_MOVING_V_MIN         判"移动"速度阈值 m/s，默认 0.3
    R4D_CLUSTER_EPS          聚类邻域半径 m，默认 0.6
    R4D_CLUSTER_MIN_POINTS   一簇最少点数，默认 2
    R4D_TRACK_MATCH_DIST     目标匹配距离 m，默认 0.8
    R4D_TRACK_POS_ALPHA      位置平滑系数(0~1)，默认 0.4（越小越平滑）
    R4D_TRACK_CONFIRM        新目标确认所需连续帧数，默认 2
    R4D_TRACK_LOST           目标丢失删除帧数，默认 5
    R4D_ACCUM_FRAMES         点云累积帧数，默认 4
    R4D_FILTER_Z_MIN/MAX     高度过滤范围 m，默认 0.15~2.6
    R4D_FILTER_Y_MIN/MAX     前方距离过滤范围 m，默认 0.3~9.0
    R4D_FILTER_X_RANGE       横向过滤范围 ±m，默认 4.0
    R4D_FILTER_MIN_POINTS    一簇最少点数（孤立点杂波），默认 3
    R4D_FILTER_SNR           1=开启 SNR 底噪过滤，默认 0（保守关闭）
    R4D_FILTER_SNR_FLOOR     SNR 硬下限，默认 0（不限）
    R4D_FILTER_DEBUG         1=每 20 帧打印一次过滤统计，默认 0
"""

import os
import sys
import time
import threading

# 让本脚本能 import radarpi 包（radarpi 的 src 目录）。
# 兼容两种目录结构：~/arDisplay/radarpi/src 与 ~/arDisplay/radarpi/radarpi/src
_HERE = os.path.dirname(os.path.abspath(__file__))
_RADARPI_CANDIDATES = [
    os.path.join(_HERE, '..', 'radarpi', 'src'),
    os.path.join(_HERE, '..', 'radarpi', 'radarpi', 'src'),
    os.path.join(_HERE, '..', 'radarpi'),
]
for _c in _RADARPI_CANDIDATES:
    _c = os.path.abspath(_c)
    if os.path.isdir(os.path.join(_c, 'radarpi')):
        sys.path.insert(0, _c)
        break

from radarpi.link import RadarLink, SimulatedLink  # noqa: E402
from stereo_ar_display import StereoARDisplay      # noqa: E402
from radar_tracker import TargetTracker, PointAccumulator  # noqa: E402
from radar_filter import ClutterFilter             # noqa: E402

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------
MOVING_V_MIN = float(os.getenv('R4D_MOVING_V_MIN', '0.3'))
CLUSTER_EPS = float(os.getenv('R4D_CLUSTER_EPS', '0.6'))
CLUSTER_MIN_POINTS = int(os.getenv('R4D_CLUSTER_MIN_POINTS', '2'))
MICRO_GROUPS = (2, 3, 4, 5)   # 长时微动高/低、短时微动高/低

# radarpi group(0~5) -> 显示层 cls 名
GROUP_TO_CLS = {
    0: 'dyn_hi',
    1: 'dyn_lo',
    2: 'long_hi',
    3: 'long_lo',
    4: 'short_hi',
    5: 'short_lo',
}


# --------------------------------------------------------------------------
# 点云分类（纯 Python，复用 radar_4d 的聚类思路）
# --------------------------------------------------------------------------
def _cluster(pts):
    """贪心聚类，返回簇索引列表 [[i0,i1,...], ...]。"""
    clusters = []
    for i, p in enumerate(pts):
        best = -1
        best_d = CLUSTER_EPS * CLUSTER_EPS
        for ci, c in enumerate(clusters):
            n = len(c)
            cx = sum(pts[j].x for j in c) / n
            cy = sum(pts[j].y for j in c) / n
            cz = sum(pts[j].z for j in c) / n
            d = (p.x - cx) ** 2 + (p.y - cy) ** 2 + (p.z - cz) ** 2
            if d < best_d:
                best_d = d
                best = ci
        if best >= 0:
            clusters[best].append(i)
        else:
            clusters.append([i])
    return clusters


def _pt_disp(p):
    """radar(x左右,y前方,z高度) -> 显示(x左右,y高度,z前方)。"""
    return {'x': p.x, 'y': p.z, 'z': p.y, 'v': p.speed,
            'cls': GROUP_TO_CLS.get(p.group, 'dyn_lo')}


def classify(frame, filt=None):
    """把一帧点云分类为 (点列表, 人体列表, 障碍列表)，坐标均为显示系。

    filt 为 ClutterFilter（可选），先做几何/SNR 过滤再聚类。
    """
    pts = frame.points
    if filt is not None:
        pts = filt.filter(pts)

    points_disp = []
    humans = []
    obstacles = []

    for idxs in _cluster(pts):
        if len(idxs) < CLUSTER_MIN_POINTS:
            # 孤立点：作为杂波丢弃，不再当普通点显示
            if filt is not None:
                filt.mark_cluster_dropped(len(idxs))
            continue

        has_dyn = any(abs(pts[i].speed) >= MOVING_V_MIN for i in idxs)
        has_micro = any(pts[i].group in MICRO_GROUPS for i in idxs)

        if has_dyn or has_micro:
            state = 'moving' if has_dyn else 'breathing'
            n = len(idxs)
            cx = sum(pts[i].x for i in idxs) / n
            cy = sum(pts[i].y for i in idxs) / n   # 前方距离
            cz = sum(pts[i].z for i in idxs) / n   # 高度
            # 人体簇的点也画出来，统一高亮（移动亮黄 / 呼吸青）
            for i in idxs:
                d = _pt_disp(pts[i])
                d['cls'] = 'dyn_hi' if has_dyn else 'long_hi'
                points_disp.append(d)
            # 显示系包围框：x=左右, y=高度, z=前方距离
            xs = [pts[i].x for i in idxs]
            ys = [pts[i].z for i in idxs]   # 高度
            zs = [pts[i].y for i in idxs]   # 前方距离
            humans.append({'x': cx, 'y': cz, 'z': cy,
                           'v': max(abs(pts[i].speed) for i in idxs),
                           'state': state,
                           'x_min': min(xs), 'x_max': max(xs),
                           'y_min': min(ys), 'y_max': max(ys),
                           'z_min': min(zs), 'z_max': max(zs)})
        else:
            # 静态目标 → 障碍
            n = len(idxs)
            cx = sum(pts[i].x for i in idxs) / n
            cy = sum(pts[i].y for i in idxs) / n
            cz = sum(pts[i].z for i in idxs) / n
            obstacles.append({'x': cx, 'y': cz, 'z': cy, 'n_points': n})

    return points_disp, humans, obstacles


def _frame_stats(frame):
    return {
        'frame_id': frame.header.frame_id,
        'n_points': len(frame.points),
        'n_tracks': len(frame.tracks),
        'bb_ms': frame.header.bb_time,
        'postbb_ms': frame.header.post_bb_time,
        'tx_ms': frame.header.transfer_time,
        'interval_ms': frame.header.frame_interval,
    }


# --------------------------------------------------------------------------
# 数据读取线程
# --------------------------------------------------------------------------
def reader_worker(link, state, lock, stop):
    """后台迭代数据源：过滤 → 分类 → 目标平滑 → 点云累积 → 写共享状态。"""
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
    _print_filter_stats = os.getenv('R4D_FILTER_DEBUG', '0') == '1'
    _frame_no = 0
    try:
        for frame in link.frames():
            if stop.is_set():
                break
            _frame_no += 1
            points, humans, obstacles = classify(frame, filt)
            # 目标级平滑：跨帧匹配 + EMA 位置稳定，滤掉一闪而过的假目标
            humans, obstacles = tracker.update(humans, obstacles)
            # 点云级累积：最近几帧叠加，静态障碍物变清晰、瞬时噪声被平均
            points = accumulator.update(points)
            with lock:
                state['points'] = points
                state['humans'] = humans
                state['obstacles'] = obstacles
                state['stats'] = _frame_stats(frame)
            if _print_filter_stats and _frame_no % 20 == 0:
                print("[filter] %s" % filt.summarize())
            for h in humans:
                # 显示系：x=左右, z=前方距离, y=高度
                print("[human] %-10s  左右=%+5.2f  前方=%5.2f  高度=%4.2f"
                      % (h['state'], h['x'], h['z'], h['y']))
    except Exception as exc:
        print("[radar_view] 读取线程异常: %s" % exc)


# --------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------
def main():
    import pygame

    simulate = os.getenv('RADAR_SIMULATE', '0') == '1'
    if simulate:
        link = SimulatedLink(fps=10.0)
    else:
        link = RadarLink(device='auto', auto_start=True, keepalive=5.0)

    display = StereoARDisplay(1920, 1080)

    lock = threading.Lock()
    state = {'points': [], 'humans': [], 'obstacles': [], 'stats': {}}
    stop = threading.Event()
    reader = threading.Thread(target=reader_worker,
                              args=(link, state, lock, stop), daemon=True)
    reader.start()

    clock = pygame.time.Clock()
    running = True
    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    running = False

            with lock:
                points = state['points']
                humans = state['humans']
                obstacles = state['obstacles']
                stats = state['stats']

            display.draw_pointcloud_mono(points, humans, obstacles, stats)
            pygame.display.flip()
            clock.tick(30)
    finally:
        stop.set()
        reader.join(timeout=3.0)
        link.close()
        pygame.quit()


if __name__ == '__main__':
    main()
