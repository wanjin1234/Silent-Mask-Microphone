# -*- coding: utf-8 -*-
"""32x24 热像帧 → 模型输入张量，以及热区统计、按片段切分。

设计要点
--------
1. **绝对温度归一化**（固定温度窗），不是逐帧百分位归一化。人体体表温度是
   ~30~37°C 的**绝对**量，房间背景约 18~28°C、高温物体（散热器/火/热饮）可达
   40~80°C。若按每帧百分位伸缩，会把「人」和「热物体」都拉成一样的亮斑，
   丢掉最关键的绝对温度信息。这里用固定窗 [T_MIN, T_MAX] 线性映射到 [0, 1]，
   高于 T_MAX 的（过热物体）裁到 1.0，低于 T_MIN 的裁到 0.0。
2. **只依赖 numpy**：预处理与推理不需要 TensorFlow，模型本身在
   ``model_io.py`` 里用轻量解释器加载。
"""

from __future__ import annotations

import numpy as np

WIDTH = 32
HEIGHT = 24
PIXEL_COUNT = WIDTH * HEIGHT

# 绝对温度归一化窗口。人体 30~37°C 落在窗内中上部；室温背景落在中下部；
# 过热物体（>40°C）裁到 1.0，靠「比人更热」这个形态与人体区分。
T_MIN = 15.0
T_MAX = 40.0

# 展示用的「疑似人体热区」下限（°C）。仅供 HUD 显示粗略位置，不参与判定。
HUMAN_TEMP_LOW = 28.0


def frame_to_tensor(frames, t_min: float = T_MIN, t_max: float = T_MAX) -> np.ndarray:
    """把原始温度帧转成 ``(N, 24, 32, 1)`` float32 张量。

    支持：``(768,)`` / ``(24, 32)`` / ``(N, 768)`` / ``(N, 24, 32)``。
    """
    a = np.asarray(frames, dtype=np.float32)
    if a.ndim == 1:
        a = a.reshape(1, HEIGHT, WIDTH)
    elif a.ndim == 2:
        if a.shape == (HEIGHT, WIDTH):
            a = a.reshape(1, HEIGHT, WIDTH)
        else:  # (N, 768)
            a = a.reshape(-1, HEIGHT, WIDTH)
    elif a.ndim == 3:
        if a.shape[1:] != (HEIGHT, WIDTH):  # (N, 768, ?) 之类异常
            a = a.reshape(a.shape[0], HEIGHT, WIDTH)
    if a.shape[1:] != (HEIGHT, WIDTH):
        raise ValueError(f"无法把 shape={frames_shape(frames)} 解析成 24x32 帧")
    a = np.clip(a, t_min, t_max)
    a = (a - t_min) / (t_max - t_min)
    return a[..., None].astype(np.float32)


def frames_shape(frames) -> tuple:
    return np.asarray(frames).shape


def frame_stats(frame) -> dict:
    """单帧温度统计（供日志/HUD 显示）。"""
    a = np.asarray(frame, dtype=np.float32).reshape(-1)
    return {
        "min_c": round(float(a.min()), 2),
        "mean_c": round(float(a.mean()), 2),
        "max_c": round(float(a.max()), 2),
    }


def human_region(frame, low_c: float = HUMAN_TEMP_LOW) -> dict | None:
    """估算「疑似人体热区」的质心、像素数与峰值（仅展示用）。

    把 >= low_c 的像素做加权质心，若没有满足条件的像素返回 ``None``。
    人体热区通常是一块连续的 30~37°C 斑块，而高温物体更热、背景更凉。
    """
    a = np.asarray(frame, dtype=np.float32).reshape(HEIGHT, WIDTH)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    mask = a >= low_c
    if not mask.any():
        return None
    weights = np.clip(a - low_c, 0.0, None)
    total = float(weights.sum())
    if total <= 0:
        return None
    cx = float((xx * weights).sum() / total)
    cy = float((yy * weights).sum() / total)
    return {
        "center_x": round(cx, 1),
        "center_y": round(cy, 1),
        "pixels": int(mask.sum()),
        "peak_c": round(float(a[mask].max()), 2),
    }


def group_split(groups, y, val_frac: float = 0.3, seed: int = 0):
    """按「采集片段」切分训练/验证，避免相邻帧泄漏到验证集。

    ``groups`` 是每帧所属片段编号（同一次录制的相邻帧同组）。切分对象是
    **片段**而不是帧：若按帧随机切分，同一时刻前后几帧会同时出现在训练与
    验证里，指标会虚高。
    """
    groups = np.asarray(groups)
    y = np.asarray(y)
    rng = np.random.default_rng(seed)
    unique = np.unique(groups)
    rng.shuffle(unique)
    n_val = max(1, int(round(len(unique) * val_frac)))
    val_groups = set(unique[:n_val].tolist())
    val_idx = np.where(np.isin(groups, list(val_groups)))[0]
    tr_idx = np.where(~np.isin(groups, list(val_groups)))[0]
    return tr_idx, val_idx
