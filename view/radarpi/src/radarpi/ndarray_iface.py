"""点云 → numpy 矩阵 的对外接口层（下游算法的接入点）。

这一层是给"拿到数据之后要做算法"的人准备的：GUI（Web 或 pygame）只是调用方之一，
你也可以完全不用界面，直接把它接进自己的脚本/模型里::

    from radarpi.link import RadarLink
    from radarpi import ndarray_iface as nd

    link = RadarLink(device="auto")          # 真机；也可用 SimulatedLink
    for points, frame in nd.stream_arrays(link):     # points: (N, 6) float32
        bev = nd.to_bev(points, resolution=0.05)     # (H, W) 俯视占据矩阵
        ...

约定（下游算法请按此取值，不要按位置猜列号）
------------------------------------------------
``points`` 矩阵形状为 ``(N, 6)``，float32，列顺序固定为 :data:`POINT_FIELDS`::

    0: x      横向坐标，单位 m，右为正
    1: y      前向距离，单位 m，雷达正前方为正
    2: z      高度，单位 m，地面为 0
    3: snr    线性 SNR，uint16 原本的值（只反映相对强弱，非绝对值）
    4: v      径向速度，单位 m/s，靠近为负
    5: group  点类别 0~5，见 :data:`radarpi.protocol.GROUP_NAMES`

矩阵类接口（:func:`to_bev` / :func:`to_voxel` / :func:`to_bev_channels`）的坐标系：

* BEV 矩阵 ``(H, W)``：行对应 Y（前向），**行 0 是最近处**（图像习惯），
  列对应 X（横向），``W//2`` 对应 X=0；
* 体素矩阵 ``(NX, NY, NZ)``：与 X/Y/Z 同序，便于直接喂 3D 卷积；
* 世界坐标与像素的换算用 :func:`bev_extent` 与 :func:`bev_to_world`。
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import protocol as P

try:
    import numpy as np
except ImportError:  # pragma: no cover - 只有用到矩阵接口时才需要 numpy
    np = None

__all__ = [
    "POINT_FIELDS",
    "TRACK_FIELDS",
    "numpy_required",
    "frame_to_ndarray",
    "tracks_to_ndarray",
    "frame_to_bundle",
    "ndarray_to_points",
    "filter_points",
    "to_bev",
    "to_bev_channels",
    "to_voxel",
    "bev_extent",
    "bev_to_world",
    "stream_arrays",
    "ArrayPipeline",
    "load_processor",
    "DEFAULT_X_RANGE",
    "DEFAULT_Y_RANGE",
]

#: ``points`` 矩阵的列顺序（与文档一致，勿改）
POINT_FIELDS: Tuple[str, ...] = ("x", "y", "z", "snr", "v", "group")
TRACK_FIELDS: Tuple[str, ...] = ("x", "y", "z", "snr", "v", "index")

#: BEV / 体素矩阵的默认视野
DEFAULT_X_RANGE: Tuple[float, float] = (-5.0, 5.0)
DEFAULT_Y_RANGE: Tuple[float, float] = (0.0, 10.0)

#: 点类别掩码，配合 filter_points(groups=...) 使用
GROUP_ALL = (0, 1, 2, 3, 4, 5)
GROUP_DYNAMIC = (0, 1)
GROUP_MICRO = (2, 3, 4, 5)
GROUP_DYNAMIC_HIGH = (0,)


def numpy_required() -> "np.ndarray":
    """检查 numpy 是否可用，缺失时给出可执行的安装提示。"""
    if np is None:
        raise RuntimeError(
            "使用矩阵接口需要 numpy：\n"
            "  Raspberry Pi OS:  sudo apt install python3-numpy\n"
            "  其它系统:         pip3 install numpy"
        )
    return np


# --------------------------------------------------------------------------
# 帧 → 矩阵
# --------------------------------------------------------------------------


def frame_to_ndarray(frame: P.Frame, include_tracks: bool = False) -> "np.ndarray":
    """把一帧点云转成 ``(N, 6)`` float32 矩阵。

    这是最核心的接口：拿到的矩阵可以直接送进聚类、跟踪、神经网络等下游处理。
    """
    np = numpy_required()
    rows: List[Tuple[float, float, float, float, float, float]] = [
        (p.x, p.y, p.z, float(p.snr), p.speed, float(p.group)) for p in frame.points
    ]
    if include_tracks:
        rows += [(t.x, t.y, t.z, float(t.snr), t.speed, -1.0) for t in frame.tracks]
    if not rows:
        return np.zeros((0, len(POINT_FIELDS)), dtype=np.float32)
    return np.asarray(rows, dtype=np.float32)


def tracks_to_ndarray(frame: P.Frame) -> "np.ndarray":
    """航迹转 ``(M, 6)`` float32 矩阵（最后一列是航迹号）。"""
    np = numpy_required()
    rows = [(t.x, t.y, t.z, float(t.snr), t.speed, float(t.index)) for t in frame.tracks]
    if not rows:
        return np.zeros((0, len(TRACK_FIELDS)), dtype=np.float32)
    return np.asarray(rows, dtype=np.float32)


def frame_to_bundle(frame: P.Frame) -> Dict[str, object]:
    """把一帧打包成字典，便于跨进程/网络传递或存成 ``.npz``。

    ::

        np.savez_compressed("frame.npz", **nd.frame_to_bundle(frame))
    """
    np = numpy_required()
    return {
        "points": frame_to_ndarray(frame),
        "tracks": tracks_to_ndarray(frame),
        "counts": np.asarray(frame.header.counts, dtype=np.int32),
        "frame_id": np.int64(frame.frame_id),
        "timestamp": np.float64(frame.timestamp),
        "frame_period": np.int32(frame.header.frame_period),
    }


def ndarray_to_points(matrix: "np.ndarray", frame: Optional[P.Frame] = None) -> List[P.Point]:
    """矩阵转回 :class:`radarpi.protocol.Point` 列表（渲染/回写录制时用）。

    处理器（processor）返回矩阵后，GUI 用这个函数把结果变回可渲染的点，
    因此算法对点云的筛选、变换能立刻反映到画面上。
    """
    np = numpy_required()
    arr = np.asarray(matrix, dtype=np.float32).reshape(-1, len(POINT_FIELDS))
    out: List[P.Point] = []
    for i, row in enumerate(arr):
        group = int(row[5])
        if group < 0:  # -1 表示这是航迹，不当作点渲染
            continue
        out.append(
            P.Point(float(row[0]), float(row[1]), float(row[2]), int(row[3]), float(row[4]),
                    group if 0 <= group <= 5 else 0, i + 1)
        )
    return out


# --------------------------------------------------------------------------
# 过滤
# --------------------------------------------------------------------------


def filter_points(
    points: "np.ndarray",
    groups: Optional[Sequence[int]] = None,
    x_range: Optional[Tuple[float, float]] = None,
    y_range: Optional[Tuple[float, float]] = None,
    z_range: Optional[Tuple[float, float]] = None,
    min_snr: Optional[float] = None,
    min_speed: Optional[float] = None,
    max_speed: Optional[float] = None,
) -> "np.ndarray":
    """按类别 / 空间范围 / SNR / 速度筛选点，返回同格式矩阵。

    典型用法：只留动态点做跟踪::

        dynamic = nd.filter_points(points, groups=nd.GROUP_DYNAMIC)
    """
    np = numpy_required()
    arr = np.asarray(points, dtype=np.float32).reshape(-1, len(POINT_FIELDS))
    if arr.size == 0:
        return arr
    mask = np.ones(arr.shape[0], dtype=bool)
    if groups is not None:
        mask &= np.isin(arr[:, 5].astype(np.int32), list(groups))
    if x_range is not None:
        mask &= (arr[:, 0] >= x_range[0]) & (arr[:, 0] <= x_range[1])
    if y_range is not None:
        mask &= (arr[:, 1] >= y_range[0]) & (arr[:, 1] <= y_range[1])
    if z_range is not None:
        mask &= (arr[:, 2] >= z_range[0]) & (arr[:, 2] <= z_range[1])
    if min_snr is not None:
        mask &= arr[:, 3] >= min_snr
    if min_speed is not None:
        mask &= np.abs(arr[:, 4]) >= min_speed
    if max_speed is not None:
        mask &= np.abs(arr[:, 4]) <= max_speed
    return arr[mask]


# --------------------------------------------------------------------------
# 栅格化：BEV 矩阵 / 体素矩阵
# --------------------------------------------------------------------------


def bev_extent(resolution: float = 0.1,
               x_range: Tuple[float, float] = DEFAULT_X_RANGE,
               y_range: Tuple[float, float] = DEFAULT_Y_RANGE) -> Dict[str, object]:
    """返回 BEV 矩阵的尺寸与世界坐标范围，便于把矩阵画到图上或反查坐标。"""
    nx = max(int(round((x_range[1] - x_range[0]) / resolution)), 1)
    ny = max(int(round((y_range[1] - y_range[0]) / resolution)), 1)
    return {
        "shape": (ny, nx),          # (行=Y, 列=X)
        "resolution": resolution,
        "x_range": x_range,
        "y_range": y_range,
        "x_origin": x_range[0],
        "y_origin": y_range[0],
    }


def _cell_indices(points: "np.ndarray", resolution: float,
                  x_range: Tuple[float, float], y_range: Tuple[float, float]):
    np = numpy_required()
    arr = np.asarray(points, dtype=np.float32).reshape(-1, len(POINT_FIELDS))
    cols = np.floor((arr[:, 0] - x_range[0]) / resolution).astype(np.int64)
    rows = np.floor((arr[:, 1] - y_range[0]) / resolution).astype(np.int64)
    nx = max(int(round((x_range[1] - x_range[0]) / resolution)), 1)
    ny = max(int(round((y_range[1] - y_range[0]) / resolution)), 1)
    keep = (cols >= 0) & (cols < nx) & (rows >= 0) & (rows < ny)
    return rows[keep], cols[keep], arr[keep], (ny, nx)


def to_bev(
    points: "np.ndarray",
    resolution: float = 0.1,
    x_range: Tuple[float, float] = DEFAULT_X_RANGE,
    y_range: Tuple[float, float] = DEFAULT_Y_RANGE,
    mode: str = "count",
    groups: Optional[Sequence[int]] = None,
) -> "np.ndarray":
    """把点云投影成俯视（BEV）矩阵 ``(H, W)`` float32。

    :param mode: 每个栅格填什么值

        * ``count``    点数（默认，做占据栅格/建图用）
        * ``max_snr``  该栅格内最大 SNR
        * ``max_abs_v``该栅格内最大速度绝对值
        * ``mean_v``   该栅格内平均速度
        * ``min_dist`` 该栅格内最近距离

    :param groups: 只统计这些类别（如 ``GROUP_DYNAMIC``），默认全部。

    示例::

        bev = nd.to_bev(points, resolution=0.05, mode="max_snr")
        # bev.shape == (200, 200)，bev[行, 列] 对应 Y、X
    """
    np = numpy_required()
    if mode not in ("count", "max_snr", "max_abs_v", "mean_v", "min_dist"):
        raise ValueError("未知的 mode：%s" % mode)
    pts = points if groups is None else filter_points(points, groups=groups)
    if pts is None or len(pts) == 0:
        info = bev_extent(resolution, x_range, y_range)
        return np.zeros(info["shape"], dtype=np.float32)

    rows, cols, data, shape = _cell_indices(pts, resolution, x_range, y_range)
    out = np.zeros(shape, dtype=np.float32)
    if rows.size == 0:
        return out
    flat = rows * shape[1] + cols

    if mode == "count":
        counts = np.bincount(flat, minlength=shape[0] * shape[1]).astype(np.float32)
        return counts.reshape(shape)
    if mode == "max_snr":
        np.maximum.at(out.reshape(-1), flat, data[:, 3])
        return out
    if mode == "max_abs_v":
        np.maximum.at(out.reshape(-1), flat, np.abs(data[:, 4]))
        return out
    if mode == "min_dist":
        dist = np.sqrt(data[:, 0] ** 2 + data[:, 1] ** 2)
        out.reshape(-1)[:] = np.inf
        np.minimum.at(out.reshape(-1), flat, dist)
        out[~np.isfinite(out)] = 0.0
        return out
    # mean_v
    sums = np.bincount(flat, weights=data[:, 4], minlength=shape[0] * shape[1]).astype(np.float32)
    nums = np.bincount(flat, minlength=shape[0] * shape[1]).astype(np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(nums > 0, sums / np.maximum(nums, 1), 0.0)
    return mean.reshape(shape).astype(np.float32)


def to_bev_channels(
    points: "np.ndarray",
    resolution: float = 0.1,
    x_range: Tuple[float, float] = DEFAULT_X_RANGE,
    y_range: Tuple[float, float] = DEFAULT_Y_RANGE,
    channels: str = "groups",
) -> "np.ndarray":
    """多通道 BEV 矩阵，方便直接喂卷积网络。

    :param channels:

        * ``groups`` —— ``(6, H, W)``：6 类点各占一个通道（点数）
        * ``fields`` —— ``(3, H, W)``：点数、最大 SNR、最大速度绝对值

    返回的矩阵是 C 在前（channel-first），需要 HWC 时自己 ``transpose(1, 2, 0)``。
    """
    np = numpy_required()
    if channels == "groups":
        stack = [to_bev(points, resolution, x_range, y_range, "count", groups=(g,)) for g in range(6)]
        return np.stack(stack, axis=0).astype(np.float32)
    if channels == "fields":
        stack = [
            to_bev(points, resolution, x_range, y_range, "count"),
            to_bev(points, resolution, x_range, y_range, "max_snr"),
            to_bev(points, resolution, x_range, y_range, "max_abs_v"),
        ]
        return np.stack(stack, axis=0).astype(np.float32)
    raise ValueError("未知的 channels：%s" % channels)


def to_voxel(
    points: "np.ndarray",
    resolution: float = 0.1,
    x_range: Tuple[float, float] = DEFAULT_X_RANGE,
    y_range: Tuple[float, float] = DEFAULT_Y_RANGE,
    z_range: Tuple[float, float] = (0.0, 3.0),
    groups: Optional[Sequence[int]] = None,
) -> "np.ndarray":
    """体素化 ``(NX, NY, NZ)`` uint16，值为该体素内的点数。"""
    np = numpy_required()
    pts = points if groups is None else filter_points(points, groups=groups)
    nx = max(int(round((x_range[1] - x_range[0]) / resolution)), 1)
    ny = max(int(round((y_range[1] - y_range[0]) / resolution)), 1)
    nz = max(int(round((z_range[1] - z_range[0]) / resolution)), 1)
    out = np.zeros((nx, ny, nz), dtype=np.uint16)
    arr = np.asarray(pts, dtype=np.float32).reshape(-1, len(POINT_FIELDS))
    if arr.shape[0] == 0:
        return out
    ix = np.floor((arr[:, 0] - x_range[0]) / resolution).astype(np.int64)
    iy = np.floor((arr[:, 1] - y_range[0]) / resolution).astype(np.int64)
    iz = np.floor((arr[:, 2] - z_range[0]) / resolution).astype(np.int64)
    keep = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny) & (iz >= 0) & (iz < nz)
    if not np.any(keep):
        return out
    np.add.at(out, (ix[keep], iy[keep], iz[keep]), 1)
    return out


def bev_to_world(row: float, col: float, resolution: float = 0.1,
                 x_range: Tuple[float, float] = DEFAULT_X_RANGE,
                 y_range: Tuple[float, float] = DEFAULT_Y_RANGE) -> Tuple[float, float]:
    """矩阵行列（可以是小数，表示栅格中心）→ 世界坐标 (x, y)。"""
    return (x_range[0] + (col + 0.5) * resolution,
            y_range[0] + (row + 0.5) * resolution)


# --------------------------------------------------------------------------
# 数据流接口与处理器链
# --------------------------------------------------------------------------


def stream_arrays(source, max_frames: Optional[int] = None,
                  processor: Optional[Callable] = None) -> Iterator[Tuple["np.ndarray", P.Frame]]:
    """把任意数据源变成 ``(矩阵, 帧)`` 的迭代器，供算法脚本直接使用::

        for points, frame in nd.stream_arrays(RadarLink()):
            print(points.shape)          # (N, 6)

    数据源可以是 :class:`radarpi.link.RadarLink`（真机）、
    :class:`radarpi.link.SimulatedLink`（仿真）或
    :class:`radarpi.recorder.ReplaySource`（回放）。
    """
    numpy_required()
    count = 0
    try:
        for frame in source.frames():
            points = frame_to_ndarray(frame)
            if processor is not None:
                result = processor(points, frame)
                if result is not None:
                    points = np.asarray(result, dtype=np.float32).reshape(-1, len(POINT_FIELDS))
            yield points, frame
            count += 1
            if max_frames is not None and count >= max_frames:
                break
    finally:
        try:
            source.close()
        except Exception:
            pass


class ArrayPipeline:
    """处理器链：把若干 ``fn(points, frame) -> points`` 串起来。

    每个处理器都可以返回 ``None`` 表示"不修改"，或返回一个新的 ``(N, 6)`` 矩阵。

    示例（只保留 2 m 以外的动态点）::

        pipe = nd.ArrayPipeline()
        pipe.add(lambda pts, fr: nd.filter_points(pts, groups=nd.GROUP_DYNAMIC,
                                                  y_range=(2.0, 10.0)))
        pipe.add(my_clustering)      # 自己写的函数
        points, frame = pipe.run(points, frame)
    """

    def __init__(self, steps: Optional[Iterable[Callable]] = None) -> None:
        self.steps: List[Callable] = list(steps or [])

    def add(self, fn: Callable) -> "ArrayPipeline":
        self.steps.append(fn)
        return self

    def run(self, points: "np.ndarray", frame: P.Frame) -> Tuple["np.ndarray", P.Frame]:
        np = numpy_required()
        for fn in self.steps:
            result = fn(points, frame)
            if result is not None:
                points = np.asarray(result, dtype=np.float32).reshape(-1, len(POINT_FIELDS))
        return points, frame

    def __call__(self, points: "np.ndarray", frame: P.Frame) -> "np.ndarray":
        return self.run(points, frame)[0]


def load_processor(spec: str) -> Callable:
    """加载外部处理器，规格为 ``"模块名:函数名"`` 或 ``"文件路径.py:函数名"``。

    GUI 用 ``--processor`` 传入，这样算法代码可以独立于界面开发、放到任意目录::

        radarpi view --processor ./my_algo.py:process
        radarpi view --processor mypackage.detect:process

    被调用的函数签名为 ``fn(points, frame) -> ndarray | None``。
    """
    import importlib
    import importlib.util
    import os

    if ":" not in spec:
        raise ValueError("处理器写法应为 模块名:函数名，例如 ./my_algo.py:process")
    # 用最后一个冒号切分：Windows 盘符（C:\...）里也有冒号
    target, _, func_name = spec.rpartition(":")
    target = target.strip()
    func_name = func_name.strip()
    if not target or not func_name:
        raise ValueError("处理器写法应为 模块名:函数名，例如 ./my_algo.py:process")

    if target.endswith(".py") or os.path.sep in target or "/" in target:
        path = os.path.abspath(os.path.expanduser(target))
        if not os.path.exists(path):
            raise FileNotFoundError("找不到处理器文件：%s" % path)
        module_name = "radarpi_processor_" + os.path.splitext(os.path.basename(path))[0]
        spec_obj = importlib.util.spec_from_file_location(module_name, path)
        if spec_obj is None or spec_obj.loader is None:
            raise ImportError("无法加载 %s" % path)
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)
    else:
        module = importlib.import_module(target)

    fn = getattr(module, func_name, None)
    if fn is None:
        raise AttributeError("%s 里没有函数 %s" % (spec, func_name))
    if not callable(fn):
        raise TypeError("%s 不是可调用的函数" % spec)
    return fn
