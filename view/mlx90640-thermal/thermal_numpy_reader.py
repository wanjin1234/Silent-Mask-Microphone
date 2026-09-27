#!/usr/bin/env python3
"""MLX90640 温度数据 -> NumPy 三维数组（[x][y][i] 布局）。

本文件不改动任何现有代码，只在其之上提供一个读取函数：把传感器的 768 个
摄氏温度值（32 x 24）按帧收集，堆叠成一个 NumPy 三维数组，方便后续做
均值/滤波/时序分析或直接喂给绘图、模型代码。

数组形状与轴的含义（C 顺序，即最后一个轴变化最快）：

    shape = (列 cols=WIDTH=32, 行 rows=HEIGHT=24, 帧数 frames)
    array[x][y][i]   -> 第 i 帧中第 y 行、第 x 列像元的摄氏度
    array[x][y]      -> 该像元随时间的温度序列，可直接画时序曲线
    array[:, :, i]   -> 第 i 帧的完整温度矩阵（32 x 24），即 [x][y]
    dtype            -> float32

这个布局与驱动输出的扁平索引关系是：``array[x][y][i] == frames[i][y * WIDTH + x]``，
其中 ``frames[i]`` 是第 i 帧那 768 个值的扁平序列。注意 ``[x][y]`` 与 NumPy /
matplotlib 默认的 ``[行][列]`` = ``[y][x]`` 是转置关系：交给 ``plt.imshow``
之类的接口时传 ``array[:, :, i].T``（或任何一帧写成 ``[y][x]`` 时用 ``.T``）。

若需要其它布局，用一次 transpose 即可，不需要重新读传感器：
``array.transpose(2, 1, 0)`` 得到 ``[i][y][x]``（帧, 行, 列）；``array[:, :, i].T``
得到 ``[y][x]``（行, 列）。行 0 是数组的“视觉顶部”（imshow 默认把第 0 行画在
最上面），传感器倒装时用 ``flip_h`` / ``flip_v`` 校正，语义与现有显示程序一致。

依赖：numpy（若未安装：``python3.11 -m pip install numpy``）。硬件读取复用
``mlx90640_thermal_display.Mlx90640Sensor``，因此设备端还需要 requirements.txt
里的 smbus2 / adafruit-circuitpython-mlx90640。

真实传感器用法（树莓派）::

    export MLX90640_I2C_BUS=3          # 软件 I2C 时按 README 设置总线号
    python thermal_numpy_reader.py --frames 10 --rate 2 --save thermal.npy

不接硬件的自检（开发机可用）::

    python thermal_numpy_reader.py --frames 3 --simulate

在其它脚本里作为函数调用::

    from thermal_numpy_reader import read_thermal_3d

    data = read_thermal_3d(10, rate_hz=2.0)   # -> (32, 24, 10) float32
    print(data[:, :, 0])                      # 第 1 帧的 32x24 温度矩阵
    print(data[16, 12])                       # 中心像元的 10 帧时序
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

# 只是取温度数据时不需要 pygame 的启动横幅（现有模块顶层会 import pygame，
# 无论是否用到显示都会打印那段提示），在导入之前设好环境变量屏蔽掉。
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# 复用现有实现而不是重写 I2C 读帧逻辑：现有模块顶层只做定义（pygame 导入是
# 可选保护），因此这里导入它不会产生副作用。同目录不在 sys.path 时（从别处
# 调用本文件）先把它加进去。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlx90640_thermal_display import (  # noqa: E402  (必须在 sys.path 调整之后)
    HEIGHT,
    PIXEL_COUNT,
    WIDTH,
    Mlx90640Sensor,
    synthetic_frame,
)

# 单帧矩阵按 [x][y] 排布时的形状：(列, 行)。
FRAME_SHAPE = (WIDTH, HEIGHT)


def frame_to_2d(
    frame: Sequence[float],
    flip_h: bool = False,
    flip_v: bool = False,
) -> np.ndarray:
    """把一帧 768 个温度值转成 (32, 24) 的二维温度矩阵，索引为 [x][y]。

    参数：
        frame: 长度为 768 的温度序列，索引方式为 ``y * WIDTH + x``（与传感器
               驱动输出的顺序一致）。
        flip_h: 水平镜像（沿 x 方向左右翻转）。
        flip_v: 垂直镜像（沿 y 方向上下翻转）。

    返回：
        np.ndarray，形状 (WIDTH, HEIGHT) = (32, 24)，dtype float32，
        ``result[x][y]`` 为第 y 行第 x 列像元的摄氏度。
    """
    # 先按驱动顺序还原成 grid[y][x]，翻转在“行/列”视角下更容易对应物理方向：
    # 水平镜像翻最后一维（x），垂直镜像翻第 0 维（y）。
    grid = np.asarray(frame, dtype=np.float32).reshape(HEIGHT, WIDTH)
    if flip_v:
        grid = grid[::-1, :]
    if flip_h:
        grid = grid[:, ::-1]
    # 转置成 [x][y]；ascontiguousarray 把转置产生的非连续视图复制成连续内存，
    # 避免下游写 npy 或需要 buffer 协议的接口报错。
    return np.ascontiguousarray(grid.T)


def read_thermal_3d(
    num_frames: int = 1,
    *,
    rate_hz: float = 2.0,
    i2c_frequency: int = 800_000,
    address: int = 0x33,
    flip_h: bool = False,
    flip_v: bool = False,
    simulate: bool = False,
    sensor: Mlx90640Sensor | None = None,
    retries: int = 3,
    on_progress: Callable[[int, int], None] | None = None,
) -> np.ndarray:
    """连续读取 MLX90640 的多帧温度，并返回 [x][y][i] 布局的 NumPy 三维数组。

    参数：
        num_frames: 读取帧数，即返回数组最后一位的长度，必须 >= 1。
        rate_hz: 采集帧率 Hz；函数会按 ``1 / rate_hz`` 的间隔取帧，建议与传感器的
            刷新率（`Mlx90640Sensor` 构造时传入的值）保持一致，默认 2 Hz。
        i2c_frequency: 传给 I2C 驱动的请求频率 Hz，仅在需要自行打开传感器时使用。
        address: 传感器 7 位 I2C 地址，默认 0x33。
        flip_h / flip_v: 是否水平（x 方向）/ 垂直（y 方向）镜像，与显示程序的同名
            参数含义相同，用于校正传感器倒装。
        simulate: True 时用合成温度场代替真实硬件（开发机上无传感器时自检用），
            此时不会打开 I2C。
        sensor: 已经创建好的 ``Mlx90640Sensor``。连续采集时复用它可以省去每次
            重新初始化 I2C 的开销；为 None 时本函数自行创建并在结束时关闭。
            注意：外部传入的 sensor 由调用方负责关闭，本函数不会关闭它。
        retries: 单帧读取失败（I2C 抖动、dataReady 超时等）时的重试次数，
            全部失败则抛出 RuntimeError。
        on_progress: 可选回调 ``(已完成帧数, 总帧数)``，便于打印进度。

    返回：
        np.ndarray，形状 ``(WIDTH, HEIGHT, num_frames)`` = ``(32, 24, 帧数)``，
        dtype float32，单位摄氏度。``result[x][y][i]`` 为第 i 帧中第 y 行第 x 列
        的像元温度；即使只读一帧也会保留最后那一维，下游代码不必区分单帧/多帧。

    异常：
        ValueError: num_frames < 1、rate_hz <= 0、retries < 1。
        RuntimeError: 传感器初始化失败，或某帧在所有重试后仍读取失败。
    """
    if num_frames < 1:
        raise ValueError("num_frames 必须 >= 1")
    if rate_hz <= 0:
        raise ValueError("rate_hz 必须为正数")
    if retries < 1:
        raise ValueError("retries 必须 >= 1")

    # 采集期间先用 [帧][y][x] 的临时缓冲区逐帧写入（这样每帧只需一次 reshape，
    # 不必为每帧单独做转置），最后一次性转置成 [x][y][帧]，只付一次拷贝成本。
    buffer = np.empty((num_frames, HEIGHT, WIDTH), dtype=np.float32)

    owned_sensor = sensor is None and not simulate
    active_sensor = sensor
    if owned_sensor:
        # 构造失败时抛出 RuntimeError，交由调用方决定如何处理。
        active_sensor = Mlx90640Sensor(rate_hz, i2c_frequency, address)

    started = time.monotonic()
    next_frame_at = started
    try:
        for index in range(num_frames):
            # 按 rate_hz 节拍采集：先等到本帧的时间点，再读帧，避免 CPU 空转。
            now = time.monotonic()
            if now < next_frame_at:
                time.sleep(next_frame_at - now)
            next_frame_at = max(next_frame_at + 1.0 / rate_hz, time.monotonic())

            flat: Sequence[float] = _read_one_frame(
                active_sensor,
                simulate=simulate,
                elapsed=time.monotonic() - started,
                retries=retries,
            )
            buffer[index] = np.asarray(flat, dtype=np.float32).reshape(HEIGHT, WIDTH)

            if on_progress is not None:
                on_progress(index + 1, num_frames)
    finally:
        # 只关闭自己创建的传感器；外部传入的由调用方管理。
        if owned_sensor and active_sensor is not None:
            active_sensor.close()

    # 翻转（x 是最后一维，y 是倒数第二维），再转置到 [x][y][帧]。
    if flip_v:
        buffer = buffer[:, ::-1, :]
    if flip_h:
        buffer = buffer[:, :, ::-1]
    return np.ascontiguousarray(buffer.transpose(2, 1, 0))


def _read_one_frame(
    sensor: Mlx90640Sensor | None,
    *,
    simulate: bool,
    elapsed: float,
    retries: int,
) -> Sequence[float]:
    """读取一帧 768 个温度值，失败时重试；全部失败抛 RuntimeError。

    真实硬件上 ``read_frame`` 可能因为 I2C 抖动或等待 dataReady 超时而抛错
    （见 mlx90640_thermal_display.Mlx90640Sensor），这类偶发失败直接重试即可，
    不必让整段采集作废。
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if simulate or sensor is None:
                # 合成场景随时间缓变，多帧之间有内容差异，便于验证三维数组。
                return synthetic_frame(elapsed)
            frame = sensor.read_frame()
            if len(frame) != PIXEL_COUNT:
                raise ValueError(f"驱动返回 {len(frame)} 个值，期望 {PIXEL_COUNT}")
            return frame
        except (OSError, RuntimeError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.05 * attempt)  # 退避一下再重试
    raise RuntimeError(f"连续 {retries} 次读取温度帧失败：{last_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="读取 MLX90640 温度并转为 NumPy 三维数组 [x][y][帧] (32, 24, 帧数)"
    )
    parser.add_argument("--frames", type=int, default=1, help="采集帧数，默认 1")
    parser.add_argument(
        "--rate",
        type=float,
        choices=(0.5, 1.0, 2.0, 4.0, 8.0),
        default=2.0,
        help="采集帧率 Hz，默认 2",
    )
    parser.add_argument(
        "--i2c-frequency",
        type=int,
        default=800_000,
        help="I2C 请求频率 Hz，默认 800000",
    )
    parser.add_argument(
        "--address",
        type=lambda value: int(value, 0),
        default=0x33,
        help="I2C 7 位地址，默认 0x33",
    )
    parser.add_argument(
        "--flip-h", action="store_true", help="水平镜像（传感器倒装时使用）"
    )
    parser.add_argument(
        "--flip-v", action="store_true", help="垂直镜像（传感器倒装时使用）"
    )
    parser.add_argument(
        "--simulate", action="store_true", help="用合成温度场，无需传感器"
    )
    parser.add_argument("--save", type=Path, help="把结果保存为 .npy 文件")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        data = read_thermal_3d(
            args.frames,
            rate_hz=args.rate,
            i2c_frequency=args.i2c_frequency,
            address=args.address,
            flip_h=args.flip_h,
            flip_v=args.flip_v,
            simulate=args.simulate,
            on_progress=lambda done, total: print(
                f"\r采集 {done}/{total} 帧", end="", flush=True
            ),
        )
    except (RuntimeError, ValueError) as exc:
        print(f"\n读取失败：{exc}", file=sys.stderr)
        return 2
    print()

    # 打印形状/统计，方便肉眼确认数组结构与数值范围是否合理。
    print(f"shape={data.shape}  dtype={data.dtype}  单位=摄氏度  (x, y, 帧)")
    for index in range(data.shape[2]):
        frame = data[:, :, index]  # [x][y]
        print(
            f"  第 {index + 1} 帧: min={frame.min():.2f}  max={frame.max():.2f}  mean={frame.mean():.2f}"
        )

    if args.save is not None:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        np.save(args.save, data)
        print(f"已保存：{args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
