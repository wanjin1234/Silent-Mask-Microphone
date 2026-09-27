#!/usr/bin/env python3
"""把一帧温度矩阵渲染成 PNG，供 show_graph.py 连续显示。

这个模块只做一件事：接收温度矩阵，用 jet 色阶渲染成 PNG。帧文件的目录和
文件名在这里统一给出（``FRAME_DIR`` / ``FRAME_PATH``），写的一方
（``draw_graph``）和读的一方（``show_graph.py``）都引用它，避免两边文件名
写岔（之前写的是 current_img.png、读的是 current_image.png）。

写入采用"先写同目录临时文件，再 os.replace 原子替换"：读的一方要么看到旧的
完整 PNG，要么看到新的完整 PNG，不会读到写了一半的文件。极端情况下 replace
失败（例如 Windows 上读方正好持有文件句柄）会抛 OSError，由调用方决定重试
还是跳过本帧。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# MLX90640 的像元阵列：32 列 x 24 行（宽 x 高）。
SENSOR_WIDTH = 32
SENSOR_HEIGHT = 24

# 帧文件的固定位置：本文件同级目录下的 graph/ 子目录。用绝对路径而不是相对
# 路径（原来是 "graph/current_img.png"），这样从任何工作目录运行，写和读都指向
# 同一个文件，不会写到哪里去、又读不到。
FRAME_DIR = Path(__file__).resolve().parent / "graph"
FRAME_PATH = FRAME_DIR / "current_img.png"


def draw_graph(
    graph_arr: np.ndarray,
    path: Path | str = FRAME_PATH,
    *,
    cmap: str = "jet",
    vmin: float | None = None,
    vmax: float | None = None,
    transpose: bool | None = None,
    show: bool = False,
) -> Path:
    """把温度矩阵渲染并保存为 PNG，返回实际写入的路径。

    参数：
        graph_arr: 温度矩阵，单位摄氏度。可以是
            - (32, 24) 的 ``[x][y]`` 矩阵（thermal_numpy_reader 的输出布局）；
            - (24, 32) 的 ``[y][x]`` 矩阵（matplotlib 的"行/列"布局）；
            - (32, 24, 1) / (24, 32, 1)，即带一维帧轴的读取结果
              （``read_thermal_3d(1)`` 的返回值，会被自动压掉这一维）。
        path: 输出 PNG 路径，默认 ``FRAME_PATH``。
        cmap: 色阶，默认 ``"jet"``。注意这个参数以前只作用于不显示的 imshow，
            真正存盘的那次调用没传色阶，导致文件是 viridis；现已修正为存盘
            和显示用同一个色阶。
        vmin / vmax: 固定色阶上下限（摄氏度）。两个都不给则按本帧数据自动缩放，
            此时每帧的颜色不能跨帧比较 —— 连续显示温度建议固定，例如 20 / 40。
        transpose: 是否把 ``[x][y]`` 转置成 ``[y][x]`` 再渲染。默认 ``None``
            表示按形状自动判断：形状 (32, 24) 只可能是本传感器 32 列 x 24 行的
            ``[x][y]``（传感器产不出 24 列宽的帧），因此自动转置；形状 (24, 32)
            视为已经是 ``[y][x]``，不再转置。
        show: ``True`` 时额外用 matplotlib 交互窗口显示一次（调试用）。默认
            ``False``：连续调用时反复 imshow 会在同一个坐标轴上不断叠加图像
            对象（实测 25 次调用累积 25 个 image 对象），白占内存。

    返回：
        实际写入的 PNG 路径（Path）。

    异常：
        ValueError: 输入不是二维矩阵（压掉单帧轴后）、矩阵为空，或含 NaN/Inf。
    """
    arr = np.asarray(graph_arr, dtype=np.float32)
    # 带单帧轴的输入 ((32, 24, 1)) 等价于二维矩阵：压掉最后一维。
    # 不压的话 matplotlib 会把三维数组当成 RGB 图，报
    # "ValueError: Third dimension must be 3 or 4"。
    if arr.ndim == 3 and arr.shape[-1] == 1:
        arr = arr[:, :, 0]
    if arr.ndim != 2:
        raise ValueError(f"需要二维温度矩阵（或带 1 帧的三维数组），收到 shape={arr.shape}")
    if arr.size == 0:
        raise ValueError("温度矩阵为空")
    if not np.isfinite(arr).all():
        raise ValueError("温度矩阵里含 NaN/Inf，无法渲染")

    if transpose is None:
        transpose = arr.shape == (SENSOR_WIDTH, SENSOR_HEIGHT)
    if transpose:
        arr = arr.T

    if show:
        plt.imshow(arr, cmap=cmap, vmin=vmin, vmax=vmax, interpolation="bicubic")
        plt.yticks([])
        plt.xticks([])
        plt.show(block=False)

    target = Path(path)
    # 原来是 os.mkdir("graph")：目录已存在就抛 FileExistsError，导致从第二帧起
    # 必然中断。改用 makedirs(exist_ok=True)，并且连父目录一起建。
    target.parent.mkdir(parents=True, exist_ok=True)

    # 原子写：临时文件必须和目标同目录，os.replace 才能保证原子替换。
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".frame-", suffix=".png")
    os.close(handle)
    try:
        plt.imsave(temp_name, arr, cmap=cmap, vmin=vmin, vmax=vmax)
        os.replace(temp_name, target)
    finally:
        # 写失败时别把临时文件留在 graph/ 里。
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return target
