# -*- coding: utf-8 -*-
"""人体检测处理器：在 radarpi 界面上高亮"人"（移动 / 呼吸微动）。

把 4D 点云聚类后识别出人体簇：
  * 移动的人 —— 簇内含动态点且 |速度| >= 阈值  → 重标为 group 0（亮黄）
  * 呼吸（静止）的人 —— 簇内含长时/短时微动点   → 重标为 group 2（青）
  * 其余孤立点保持原样（背景/杂波）

用法（树莓派上）::

    PYTHONPATH=src python3 -m radarpi view \
        --processor examples/human_detector.py:process

返回值是"重标记后的点云矩阵"，因此界面里人体簇会被统一高亮，
同时终端会打印每帧检测到的人体（位置/状态/点数）。

处理器约定见 ``examples/processor_demo.py`` 顶部的说明：
``fn(points, frame) -> ndarray | None``
"""

from __future__ import annotations

import numpy as np

from radarpi import ndarray_iface as nd

# ---- 可调阈值（按实际场景在树莓派上微调）----
MOVING_V_MIN = 0.3       # 判定"移动"的最小径向速度 m/s
CLUSTER_EPS = 0.6        # 聚类邻域半径 m
CLUSTER_MIN_POINTS = 2   # 一簇至少几个点才算有效目标（滤孤立杂点）

# 重标记后的人体点类别（沿用 radarpi 的配色）
GROUP_MOVING = 0         # 动态高置信度 = 亮黄
GROUP_BREATHING = 2      # 长时微动高置信度 = 青

# 微动类别（长时微动高/低、短时微动高/低）
MICRO_GROUPS = (2, 3, 4, 5)

# 打印节流：人体集合变化或每 N 帧打印一次
_PRINT_EVERY = 10
_last_signature = None
_frame_count = 0


def _cluster_centers(xyz: np.ndarray, eps: float):
    """贪心聚类，返回簇索引列表 [[i0,i1...], ...]，按 xyz 欧氏距离归并。"""
    n = xyz.shape[0]
    clusters = []
    for i in range(n):
        p = xyz[i]
        best = -1
        best_d = eps * eps
        for ci, c in enumerate(clusters):
            # 用簇内当前质心判断
            center = xyz[c].mean(axis=0)
            d = float(np.sum((p - center) ** 2))
            if d < best_d:
                best_d = d
                best = ci
        if best >= 0:
            clusters[best].append(i)
        else:
            clusters.append([i])
    return clusters


def detect_humans(points: np.ndarray) -> tuple:
    """聚类 + 分类，返回 (重标记矩阵, 人体列表)。

    人体列表元素 = (state, x, y, z, n_points)。
    """
    out = points.copy()
    humans = []
    if points.shape[0] == 0:
        return out, humans

    xyz = points[:, :3]
    v = points[:, 4]
    g = points[:, 5].astype(int)

    for idxs in _cluster_centers(xyz, CLUSTER_EPS):
        idxs = np.asarray(idxs, dtype=int)
        if len(idxs) < CLUSTER_MIN_POINTS:
            continue
        cl_v = v[idxs]
        cl_g = g[idxs]
        has_dyn = bool(np.any(np.abs(cl_v) >= MOVING_V_MIN))
        has_micro = bool(np.any(np.isin(cl_g, MICRO_GROUPS)))

        if not (has_dyn or has_micro):
            continue  # 不是人体（静态杂波/墙面）
        state = "moving" if has_dyn else "breathing"
        cx, cy, cz = xyz[idxs].mean(axis=0)
        humans.append((state, float(cx), float(cy), float(cz), int(len(idxs))))
        # 人体簇统一高亮：移动→亮黄，呼吸→青
        out[idxs, 5] = GROUP_MOVING if has_dyn else GROUP_BREATHING

    return out, humans


def process(points: np.ndarray, frame) -> np.ndarray:
    """每帧调用：聚类识别人体，重标记后返回给界面显示。"""
    global _last_signature, _frame_count
    _frame_count += 1

    out, humans = detect_humans(points)

    # 打印人体检测结果（签名变化时必打，否则每 N 帧打一次）
    sig = tuple((h[0], round(h[1], 1), round(h[2], 1)) for h in humans)
    if sig != _last_signature or _frame_count % _PRINT_EVERY == 0:
        if humans:
            for h in humans:
                print("[human] %-10s x=%+5.2f  y=%5.2f  z=%4.2f  点=%d"
                      % (h[0], h[1], h[2], h[3], h[4]))
        else:
            print("[human] 未检测到人体（帧 %d）" % (frame.frame_id if frame else 0))
    _last_signature = sig

    return out


def observe_only(points: np.ndarray, frame) -> None:
    """只打印检测结果、不修改界面显示（保留原始配色）。"""
    global _last_signature, _frame_count
    _frame_count += 1
    _, humans = detect_humans(points)
    sig = tuple((h[0], round(h[1], 1), round(h[2], 1)) for h in humans)
    if sig != _last_signature or _frame_count % _PRINT_EVERY == 0:
        if humans:
            for h in humans:
                print("[human] %-10s x=%+5.2f  y=%5.2f  z=%4.2f  点=%d"
                      % (h[0], h[1], h[2], h[3], h[4]))
        else:
            print("[human] 未检测到人体")
    _last_signature = sig
    return None


if __name__ == "__main__":
    # 自检：构造几个"移动人 + 呼吸人 + 杂波"点，验证聚类分类
    rng = np.random.default_rng(0)
    rows = []
    # 移动的人（动态点，v 较大）位于 (1.0, 3.0, 1.2) 附近
    for _ in range(5):
        rows.append((1.0 + rng.uniform(-0.1, 0.1), 3.0 + rng.uniform(-0.1, 0.1),
                     1.2 + rng.uniform(-0.1, 0.1), 120.0, 0.8, 0.0))
    # 呼吸的人（微动点，v=0）位于 (-1.0, 2.5, 1.0) 附近
    for _ in range(4):
        rows.append((-1.0 + rng.uniform(-0.1, 0.1), 2.5 + rng.uniform(-0.1, 0.1),
                     1.0 + rng.uniform(-0.05, 0.05), 60.0, 0.0, 2.0))
    # 杂波（低速动态孤立点）
    for _ in range(6):
        rows.append((rng.uniform(-3, 3), rng.uniform(1, 6), rng.uniform(0, 1.5),
                     30.0, 0.05, 1.0))
    pts = np.asarray(rows, dtype=np.float32)

    class _F:
        frame_id = 1
    out, humans = detect_humans(pts)
    print("自测点云：%d 点" % pts.shape[0])
    print("检测到人体：")
    for h in humans:
        print("  %-10s x=%+5.2f  y=%5.2f  z=%4.2f  点=%d" % (h[0], h[1], h[2], h[3], h[4]))
    print("重标记后的 group 列：", out[:, 5].astype(int).tolist())
