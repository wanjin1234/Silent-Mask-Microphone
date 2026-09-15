# -*- coding: utf-8 -*-
"""合成热像数据集 —— 无真机数据时打通并验证整条流水线。

用途与边界（诚实界定）
--------------------
本模块**不能替代真机数据**。它只做两件事：

1. 让 ``train.py`` / ``detect_live.py`` / ``smoke_test.py`` 在没有 MLX90640 的
   机器（如 Windows 开发机）上把「生成→训练→导出 TFLite→推理」整条链路跑通；
2. 把热成像人类检测最主要的**误报机理**显式写进数据里：不是所有「热斑」都是人，
   高温物体（热饮、散热器、打火机、火苗）更热、更小。模型必须学会区分
   「30~37°C 的人形斑块」与「>40°C 的小型热点」。

三个场景：
* ``empty`` —— 室温背景 + 噪声，标签 0；
* ``human`` —— 背景 + 30~37°C 的人形斑块（身体+头部，含尺寸/位置随机），标签 1；
* ``hot``   —— 背景 + 更热更小的高温物体，标签 0（负样本中的难例）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preprocess import WIDTH, HEIGHT, PIXEL_COUNT

MODES = ("empty", "human", "hot")


def _background(rng):
    """室温背景：轻微水平梯度 + 空间低频起伏 + 高斯噪声。"""
    bg = np.full((HEIGHT, WIDTH), 21.0, dtype=np.float32)
    bg += 3.0 * np.arange(WIDTH, dtype=np.float32)[None, :] / (WIDTH - 1)
    # 空间低频起伏（房间不同区域的温差）
    ys = np.linspace(0.0, 1.0, HEIGHT, dtype=np.float32)[:, None]
    xs = np.linspace(0.0, 1.0, WIDTH, dtype=np.float32)[None, :]
    bg += 0.8 * np.sin(2 * np.pi * xs * 1.3) * np.cos(2 * np.pi * ys * 0.7)
    bg += rng.normal(0.0, 0.25, (HEIGHT, WIDTH)).astype(np.float32)
    return bg


def _blob(cx, cy, sx, sy, amp):
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    return amp * np.exp(-((xx - cx) ** 2 / (2 * sx * sx) + (yy - cy) ** 2 / (2 * sy * sy)))


def _add_human(frame, rng):
    """在背景上叠一个 30~37°C 的人形斑块（身体 + 头部，含轻微呼吸起伏）。"""
    cx = float(rng.uniform(6.0, WIDTH - 6.0))
    cy = float(rng.uniform(8.0, HEIGHT - 4.0))
    # 尺寸随「距离」变化：近处大、远处小
    scale = float(rng.uniform(0.8, 1.8))
    body_sx, body_sy = 3.0 * scale, 6.0 * scale
    body_temp = float(rng.uniform(33.0, 36.5)) - 21.0  # 相对背景的温升
    head_dy = -7.0 * scale
    head_temp = float(rng.uniform(34.0, 37.0)) - 21.0

    frame += _blob(cx, cy, body_sx, body_sy, body_temp).astype(np.float32)
    frame += _blob(cx, cy + head_dy, body_sx * 0.55, body_sy * 0.45, head_temp).astype(np.float32)


def _add_hot(frame, rng):
    """在背景上叠一个 >40°C 的小型热点（热饮/散热器/火苗类负样本难例）。"""
    cx = float(rng.uniform(3.0, WIDTH - 3.0))
    cy = float(rng.uniform(2.0, HEIGHT - 2.0))
    sx = float(rng.uniform(0.7, 2.0))
    sy = float(rng.uniform(0.7, 2.0))
    temp = float(rng.uniform(45.0, 75.0)) - 21.0
    frame += _blob(cx, cy, sx, sy, temp).astype(np.float32)


def generate_frame(rng, mode: str):
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {MODES} 之一")
    frame = _background(rng)
    label = 0
    if mode == "human":
        _add_human(frame, rng)
        label = 1
    elif mode == "hot":
        _add_hot(frame, rng)
        label = 0
    return frame, label


def synthesize_dataset(n_per_class: int = 200, seed: int = 0, group_size: int = 20):
    """生成合成数据集。

    返回 ``(frames (N,24,32), y (N,), groups (N,))``。组号按 ``group_size``
    帧一段切分，用来验证「按片段切分验证集」的逻辑。
    """
    rng = np.random.default_rng(seed)
    frames, ys, groups = [], [], []
    for mode_idx, mode in enumerate(MODES):
        for i in range(n_per_class):
            frame, label = generate_frame(rng, mode)
            frames.append(frame)
            ys.append(label)
            # 组号 = 类别偏移 + 类别内片段号，保证不同场景属于不同片段
            groups.append(mode_idx * (n_per_class // group_size) + i // group_size)
    return (
        np.asarray(frames, dtype=np.float32),
        np.asarray(ys, dtype=np.int64),
        np.asarray(groups, dtype=np.int64),
    )


def save_npz(path, frames, y, groups, t_min, t_max):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    meta = {"t_min": float(t_min), "t_max": float(t_max),
            "created_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S")}
    np.savez_compressed(
        path,
        frames=np.asarray(frames, dtype=np.float32),
        y=np.asarray(y, dtype=np.int64),
        groups=np.asarray(groups, dtype=np.int64),
        meta=json.dumps(meta),
    )
    print(f"已保存 {len(y)} 帧到 {path}")


def load_npz(path):
    data = np.load(path, allow_pickle=False)
    meta = json.loads(str(data["meta"]))
    return (data["frames"], data["y"], data["groups"], meta)


def main():
    from preprocess import T_MIN, T_MAX
    p = argparse.ArgumentParser(description="生成合成热像数据集")
    p.add_argument("--n-per-class", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default="data/synth.npz")
    args = p.parse_args()
    frames, y, groups = synthesize_dataset(args.n_per_class, seed=args.seed)
    save_npz(args.out, frames, y, groups, T_MIN, T_MAX)


if __name__ == "__main__":
    main()
