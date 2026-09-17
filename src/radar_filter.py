#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""杂波过滤：识别并过滤毫米波雷达点云中的噪声/杂波点。

针对 4D 成像雷达点云的常见杂波，提供三类无副作用过滤（几何范围、孤立点、
可选 SNR 底噪），全部可调、可单独开关。

纯 Python 实现，不依赖 numpy，树莓派系统 python3 直接可用。

环境变量（在 radar_view.py 里读取并传入）：
    R4D_FILTER_Z_MIN / Z_MAX      高度范围 m（默认 0.15 ~ 2.6）
    R4D_FILTER_Y_MIN / Y_MAX      前方距离范围 m（默认 0.3 ~ 9.0）
    R4D_FILTER_X_RANGE            横向范围 ±m（默认 4.0）
    R4D_FILTER_MIN_POINTS         一簇最少点数（默认 3）
    R4D_FILTER_SNR                1=开启 SNR 底噪过滤（默认 0，保守关闭）
    R4D_FILTER_SNR_FLOOR          SNR 硬下限，低于此值的点直接丢（默认 0=不限）
"""


class ClutterFilter:
    """点云杂波过滤器。"""

    def __init__(self, z_range=(0.15, 2.6), y_range=(0.3, 9.0),
                 x_range=(-4.0, 4.0), min_points=3,
                 snr_enabled=False, snr_floor=0):
        self.z_min, self.z_max = z_range
        self.y_min, self.y_max = y_range
        self.x_min, self.x_max = x_range
        self.min_points = min_points
        self.snr_enabled = snr_enabled
        self.snr_floor = snr_floor
        # 统计
        self.stats = {'total': 0, 'z': 0, 'range': 0, 'snr': 0,
                      'cluster': 0, 'kept': 0}
        self._last_filtered = 0

    def filter_geometry(self, points):
        """几何范围过滤：只保留人体活动高度带 + 距离/横向范围内的点。

        注意：radarpi 坐标系为 x=左右, y=前方距离, z=高度。
        """
        kept = []
        for p in points:
            # 高度
            if not (self.z_min <= p.z <= self.z_max):
                self.stats['z'] += 1
                continue
            # 距离
            if not (self.y_min <= p.y <= self.y_max):
                self.stats['range'] += 1
                continue
            # 横向
            if not (self.x_min <= p.x <= self.x_max):
                self.stats['range'] += 1
                continue
            # 可选 SNR 硬下限
            if self.snr_enabled and p.snr < self.snr_floor:
                self.stats['snr'] += 1
                continue
            kept.append(p)
        return kept

    def filter(self, points):
        """完整过滤：几何 + SNR，返回保留的点列表（孤立点过滤在聚类层做）。"""
        self.stats = {'total': len(points), 'z': 0, 'range': 0, 'snr': 0,
                      'cluster': 0, 'kept': 0}
        kept = self.filter_geometry(points)
        self.stats['kept'] = len(kept)
        return kept

    def mark_cluster_dropped(self, n):
        """聚类层丢弃孤立点时，累计统计。"""
        self.stats['cluster'] += n

    def summarize(self):
        """返回一行可读的过滤统计。"""
        s = self.stats
        return (f"点 {s['total']} -> 高度滤 {s['z']} 范围滤 {s['range']} "
                f"SNR滤 {s['snr']} 孤立滤 {s['cluster']} 保留 {s['kept']}")
