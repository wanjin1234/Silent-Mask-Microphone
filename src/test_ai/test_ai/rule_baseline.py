# -*- coding: utf-8 -*-
"""现行规则判定基线——把父项目当前部署的逻辑复刻成「窗口 → 判定」。

存在的意义：``evaluate.py`` 要在**同一批真机标注数据**上比较
「现有规则」与「神经网络」。没有这个基线，任何「AI 有效」的说法都无法证伪。

复刻的是 ``src/main_stereo.py`` 的扫描聚合 + ``src/c4002_parser.py`` 的物理判据：

1. 多普勒速度死区：``C4002_SPEED_DEADZONE``（默认 2cm/s）以下按 0 处理；
2. 运动判定：``|move_speed| > C4002_MOTION_SPEED_MIN``（默认 5cm/s）记一次命中；
3. 任一雷达命中帧数 >= ``C4002_SCAN_MOTION_MIN``（默认 1）即判「有人」。

第 3 条里默认 ``>= 1`` 意味着**单帧速度尖峰就报警**，这正是它对吊扇 /
桌面振动 / 幻影目标敏感的原因，也正是模型要改进的地方。

``breath`` 变体近似 ``breath_detector.py`` 的周期判据：速度幅值达标且确有振荡
（过零次数 >= 2）。``motion_or_breath`` 为两者取或，代表「现有能力的上界」。

注意：``move_speed`` 在 ``RawC4002Serial`` 里**未经死区处理**（为了给模型保留
低速微动），所以本模块显式施加死区，以忠实还原现有系统的行为。
"""

import os

import numpy as np

from features import SCALAR_NAMES, AGG_NAMES

RULE_MODES = ('motion', 'breath', 'motion_or_breath')


def _speed_cols(seq, spec):
    """抽出每个传感器的时间轴速度列，返回 ``(N, T, S)``（单位 cm/s）。"""
    cols = []
    for si in range(len(spec.angles)):
        base = si * (len(SCALAR_NAMES) + spec.gate_bits)
        cols.append(seq[:, :, base + SCALAR_NAMES.index('move_speed')] * 100.0)
    return np.stack(cols, axis=2)


def _agg_col(agg, spec, name):
    """抽出某统计量在三个传感器上的值，返回 ``(N, S)``。"""
    idx = AGG_NAMES.index(name)
    return np.stack([agg[:, si * len(AGG_NAMES) + idx]
                     for si in range(len(spec.angles))], axis=1)


def agg_from_seq(seq, spec):
    """从 seq 张量重建 ``agg``（消融数据或推理时若没有 agg 可用）。

    只依赖 seq 本身，逐窗口调用 ``features._aggregate``，保证列序与
    ``AGG_NAMES`` 一致。
    """
    from features import _aggregate  # 局部导入，避免模块级循环依赖
    n = seq.shape[0]
    width = len(SCALAR_NAMES) + spec.gate_bits
    out = np.zeros((n, spec.agg_dim), dtype=np.float32)
    for i in range(n):
        for si in range(len(spec.angles)):
            block = seq[i, :, si * width:(si + 1) * width]
            out[i, si * len(AGG_NAMES):(si + 1) * len(AGG_NAMES)] = _aggregate(block, spec)
    return out


def rule_scores(seq, spec, mode='motion', agg=None,
                speed_deadzone=None, motion_speed_min=None, scan_motion_min=None,
                breath_min_rms=None, breath_min_zero_cross=2):
    """返回 ``(decision, score)``：硬判定 0/1 与连续分数（供 ROC-AUC 使用）。

    参数默认值取自环境变量，与父项目保持一致，便于直接对齐上线参数。
    ``agg`` 为 ``None`` 时按需从 ``seq`` 重建（略慢，但调用方不必关心）。
    """
    if mode not in RULE_MODES:
        raise ValueError(f'未知基线模式 {mode}，可选 {RULE_MODES}')
    speed_deadzone = (float(os.getenv('C4002_SPEED_DEADZONE', '2'))
                      if speed_deadzone is None else float(speed_deadzone))
    motion_speed_min = (float(os.getenv('C4002_MOTION_SPEED_MIN', '5'))
                        if motion_speed_min is None else float(motion_speed_min))
    scan_motion_min = (int(os.getenv('C4002_SCAN_MOTION_MIN', '1'))
                       if scan_motion_min is None else int(scan_motion_min))
    breath_min_rms = (float(os.getenv('C4002_BREATH_MIN_AMPLITUDE', '1.5'))
                      if breath_min_rms is None else float(breath_min_rms))

    speed = _speed_cols(seq, spec)
    speed = np.where(np.abs(speed) < speed_deadzone, 0.0, speed)  # 死区，与父项目一致
    abs_speed = np.abs(speed)
    hit_counts = (abs_speed > motion_speed_min).sum(axis=1)       # (N, S)
    motion_ok = (hit_counts >= scan_motion_min).any(axis=1)       # (N,)
    score = abs_speed.max(axis=(1, 2))                            # 越大越像有人

    if mode != 'motion':
        if agg is None:
            agg = agg_from_seq(seq, spec)
        rms = _agg_col(agg, spec, 'speed_rms') * 100.0            # 回到 cm/s
        zc = _agg_col(agg, spec, 'speed_zero_cross')
        breath_ok = ((rms >= breath_min_rms) & (zc >= breath_min_zero_cross)).any(axis=1)
        breath_score = (rms * np.minimum(zc, 4.0)).max(axis=1)
        if mode == 'breath':
            return breath_ok.astype(np.int64), breath_score
        return (motion_ok | breath_ok).astype(np.int64), np.maximum(score, breath_score)

    return motion_ok.astype(np.int64), score
