# -*- coding: utf-8 -*-
"""示例算法处理器：演示如何接入 radarpi 的 ndarray 接口。

用法（界面会实时显示处理后的点云）::

    radarpi view --simulate --processor examples/processor_demo.py:process
    radarpi view --processor ./examples/processor_demo.py:process      # 接真机

也可以完全不启动界面，直接当算法脚本用::

    from radarpi.link import RadarLink
    from radarpi import ndarray_iface as nd
    for points, frame in nd.stream_arrays(RadarLink()):
        print(points.shape)          # (N, 6) float32
        bev = nd.to_bev(points, resolution=0.05)

处理器约定
----------
``fn(points, frame) -> ndarray | None``

* ``points``：``(N, 6)`` float32 矩阵，列顺序 = x, y, z, snr, v, group
  （见 :data:`radarpi.ndarray_iface.POINT_FIELDS`）
* ``frame``：原始 :class:`radarpi.protocol.Frame`（需要帧号、各类点数时用）
* 返回 **新的矩阵** 就替换显示内容；返回 ``None`` 表示不修改（只做观测/统计）

因此这个文件既是"过滤器"插件，也是"算法接入点"的样板。
"""

from __future__ import annotations

import numpy as np

from radarpi import ndarray_iface as nd

# 关心的区域：人活动的地面以上区域（单位 m）
Z_MIN, Z_MAX = 0.25, 2.2
Y_MIN, Y_MAX = 1.0, 8.0
X_MIN, X_MAX = -3.0, 3.0

#: 统计信息（界面里看不到，但脚本/日志里可以取）
_stats = {"frames": 0, "kept": 0, "dropped": 0}


def process(points: np.ndarray, frame) -> np.ndarray:
    """只保留"房间中间、地面以上、动态"的点，其余滤掉。

    返回新矩阵 → 界面显示的就是处理后的结果，适合用来验证算法效果。
    """
    _stats["frames"] += 1
    kept = nd.filter_points(
        points,
        groups=nd.GROUP_DYNAMIC,          # 只留动态高/低置信度点
        x_range=(X_MIN, X_MAX),
        y_range=(Y_MIN, Y_MAX),
        z_range=(Z_MIN, Z_MAX),
    )
    _stats["kept"] += int(kept.shape[0])
    _stats["dropped"] += int(points.shape[0] - kept.shape[0])
    if _stats["frames"] % 100 == 0:
        print("[processor_demo] 帧 %d：保留 %d / 累计 %d，平均每帧 %.1f 点，矩阵 %s"
              % (frame.frame_id, kept.shape[0], _stats["frames"],
                 _stats["kept"] / _stats["frames"], kept.shape))
    return kept


def observe_only(points: np.ndarray, frame) -> None:
    """只观测不修改：每 50 帧打印一次 BEV 矩阵的统计量。

    返回 ``None``，界面显示不受影响 —— 适合把数据喂给自己的模型/记录器。
    """
    if frame.frame_id % 50 != 0:
        return None
    bev = nd.to_bev(points, resolution=0.1, mode="count")
    voxel = nd.to_voxel(points, resolution=0.2)
    print("[processor_demo] 帧 %d points=%s bev=%s(峰值 %d) voxel=%s(非空 %d)"
          % (frame.frame_id, points.shape, bev.shape, int(bev.max()),
             voxel.shape, int((voxel > 0).sum())))
    return None


def cluster_by_grid(points: np.ndarray, frame) -> np.ndarray:
    """一个稍有用的例子：按 0.5 m 网格粗聚类，只保留"人多"的格子。

    真正的聚类（DBSCAN 等）逻辑一样，只是把这里的网格判定换掉即可。
    注意返回值只保留每个簇的中心点，用来在界面上看得更清楚。
    """
    if points.shape[0] == 0:
        return points
    grid = 0.5
    keys = np.floor(points[:, :2] / grid).astype(np.int64)
    order = np.lexsort((keys[:, 1], keys[:, 0]))
    sorted_keys = keys[order]
    # 找出同一格子的连续段
    boundaries = np.flatnonzero((sorted_keys[1:] != sorted_keys[:-1]).any(axis=1)) + 1
    groups = np.split(order, boundaries)
    centers = []
    for idx in groups:
        if len(idx) < 5:            # 少于 5 个点的格子当噪声丢掉
            continue
        centers.append(points[idx].mean(axis=0))
    if not centers:
        return points[:0]
    return np.asarray(centers, dtype=np.float32)
