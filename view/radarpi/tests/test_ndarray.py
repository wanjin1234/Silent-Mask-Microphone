"""点云 → ndarray 矩阵接口的自检。

直接运行::

    cd radarpi
    PYTHONPATH=src python tests/test_ndarray.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("本测试需要 numpy：pip3 install numpy（树莓派：sudo apt install python3-numpy）")

from radarpi import ndarray_iface as nd  # noqa: E402
from radarpi import protocol as P  # noqa: E402


def make_frame(frame_id: int = 1, tracks: int = 0) -> P.Frame:
    points = [
        P.Point(0.0, 1.0, 0.5, 100, 0.0, P.GROUP_DYNAMIC_HIGH),
        P.Point(1.0, 2.0, 0.0, 200, 1.5, P.GROUP_DYNAMIC_HIGH),
        P.Point(-1.0, 3.0, 1.0, 300, -2.5, P.GROUP_DYNAMIC_LOW),
        P.Point(2.0, 4.0, 2.0, 400, 0.0, P.GROUP_LONG_MICRO_HIGH),
        P.Point(-2.0, 5.0, 0.2, 500, 0.5, P.GROUP_SHORT_MICRO_LOW),
    ]
    counts = [0] * 6
    for p in points:
        counts[p.group] += 1
    header = P.FrameHeader(frame_id=frame_id, frame_period=100, counts=tuple(counts),
                           track_count=tracks)
    tr = [P.Track(0.5 * i, 6.0, 1.0, 800, 0.3, i + 1) for i in range(tracks)]
    return P.Frame(header=header, points=points, tracks=tr, timestamp=1700000000.5)


class TestMatrixConversion(unittest.TestCase):
    def test_frame_to_ndarray(self):
        arr = nd.frame_to_ndarray(make_frame())
        self.assertEqual(arr.shape, (5, 6))
        self.assertEqual(arr.dtype, np.float32)
        self.assertAlmostEqual(arr[1, 0], 1.0, places=5)   # x
        self.assertAlmostEqual(arr[1, 1], 2.0, places=5)   # y
        self.assertAlmostEqual(arr[1, 2], 0.0, places=5)   # z
        self.assertAlmostEqual(arr[1, 3], 200.0, places=5)  # snr
        self.assertAlmostEqual(arr[1, 4], 1.5, places=5)   # 速度（带符号）
        self.assertEqual(int(arr[1, 5]), P.GROUP_DYNAMIC_HIGH)
        # 列顺序契约
        self.assertEqual(nd.POINT_FIELDS, ("x", "y", "z", "snr", "v", "group"))

    def test_empty_frame(self):
        header = P.FrameHeader(frame_id=9, counts=(0, 0, 0, 0, 0, 0))
        arr = nd.frame_to_ndarray(P.Frame(header=header, points=[]))
        self.assertEqual(arr.shape, (0, 6))
        self.assertEqual(nd.to_bev(arr, resolution=0.5).shape, (20, 20))

    def test_tracks_and_bundle(self):
        frame = make_frame(tracks=2)
        tr = nd.tracks_to_ndarray(frame)
        self.assertEqual(tr.shape, (2, 6))
        self.assertAlmostEqual(tr[1, 0], 0.5, places=5)
        bundle = nd.frame_to_bundle(frame)
        for key in ("points", "tracks", "counts", "frame_id", "timestamp", "frame_period"):
            self.assertIn(key, bundle)
        self.assertEqual(int(bundle["frame_id"]), 1)
        self.assertEqual(bundle["counts"].tolist(), list(frame.header.counts))

    def test_ndarray_roundtrip_to_points(self):
        frame = make_frame()
        arr = nd.frame_to_ndarray(frame)
        points = nd.ndarray_to_points(arr)
        self.assertEqual(len(points), 5)
        for a, b in zip(points, frame.points):
            self.assertAlmostEqual(a.x, b.x, places=4)
            self.assertAlmostEqual(a.speed, b.speed, places=4)
            self.assertEqual(a.group, b.group)
            self.assertEqual(a.snr, b.snr)


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.arr = nd.frame_to_ndarray(make_frame())

    def test_group_filter(self):
        dyn = nd.filter_points(self.arr, groups=nd.GROUP_DYNAMIC)
        self.assertEqual(dyn.shape[0], 3)
        self.assertEqual(nd.filter_points(self.arr, groups=nd.GROUP_MICRO).shape[0], 2)
        self.assertEqual(nd.filter_points(self.arr, groups=(0,)).shape[0], 2)

    def test_range_and_speed_filter(self):
        self.assertEqual(nd.filter_points(self.arr, y_range=(2.0, 4.5)).shape[0], 3)
        self.assertEqual(nd.filter_points(self.arr, x_range=(0.0, 5.0)).shape[0], 3)
        self.assertEqual(nd.filter_points(self.arr, z_range=(0.0, 0.6)).shape[0], 3)
        self.assertEqual(nd.filter_points(self.arr, min_snr=350).shape[0], 2)
        self.assertEqual(nd.filter_points(self.arr, min_speed=1.0).shape[0], 2)
        self.assertEqual(nd.filter_points(self.arr, min_speed=0.5, max_speed=2.0).shape[0], 2)

    def test_filter_keeps_format(self):
        out = nd.filter_points(self.arr, groups=(0,), x_range=(-10, 10))
        self.assertEqual(out.shape[1], 6)


class TestRasterization(unittest.TestCase):
    def test_bev_count_and_coordinates(self):
        # 1 m/格，X 与 Y 都从 0 起，便于手算
        arr = np.array([
            [0.1, 0.1, 0.0, 10, 0.0, 0.0],
            [0.2, 0.2, 0.0, 20, 0.0, 0.0],   # 与上一行同格
            [1.5, 0.5, 0.0, 30, 0.0, 0.0],
            [9.9, 4.5, 0.0, 40, 0.0, 0.0],   # 边界内最后一格
            [12.0, 1.0, 0.0, 50, 0.0, 0.0],  # 越界，应被丢弃
        ], dtype=np.float32)
        bev = nd.to_bev(arr, resolution=1.0, x_range=(0.0, 10.0), y_range=(0.0, 5.0))
        self.assertEqual(bev.shape, (5, 10))
        self.assertEqual(bev[0, 0], 2.0)   # (0.1,0.1) 与 (0.2,0.2) 落入同一格
        self.assertEqual(bev[0, 1], 1.0)
        self.assertEqual(bev[4, 9], 1.0)
        self.assertEqual(bev.sum(), 4.0)   # 越界的那个点没有被统计

    def test_bev_modes(self):
        arr = np.array([
            [0.1, 0.1, 0.0, 10, -3.0, 0.0],
            [0.2, 0.2, 0.0, 50, 1.0, 0.0],
            [5.0, 1.0, 0.0, 20, 0.5, 1.0],
        ], dtype=np.float32)
        common = dict(resolution=1.0, x_range=(0.0, 10.0), y_range=(0.0, 5.0))
        self.assertEqual(nd.to_bev(arr, mode="max_snr", **common)[0, 0], 50.0)
        self.assertEqual(nd.to_bev(arr, mode="max_abs_v", **common)[0, 0], 3.0)
        self.assertAlmostEqual(nd.to_bev(arr, mode="mean_v", **common)[0, 0], -1.0, places=5)
        self.assertAlmostEqual(nd.to_bev(arr, mode="min_dist", **common)[0, 0], np.hypot(0.1, 0.1), places=5)
        # 只统计动态点（group 0/1）时，group=1 的那个点在 (1,5)
        only_dyn = nd.to_bev(arr, mode="count", groups=nd.GROUP_DYNAMIC, **common)
        self.assertEqual(only_dyn[0, 0], 2.0)
        self.assertEqual(only_dyn[1, 5], 1.0)

    def test_bev_channels(self):
        arr = nd.frame_to_ndarray(make_frame())
        groups = nd.to_bev_channels(arr, resolution=0.5, channels="groups",
                                    x_range=(-5, 5), y_range=(0, 10))
        self.assertEqual(groups.shape[0], 6)
        self.assertEqual(groups.shape, (6, 20, 20))
        # 每个通道的和应等于该类点数
        frame = make_frame()
        for g in range(6):
            expected = sum(1 for p in frame.points if p.group == g)
            self.assertEqual(groups[g].sum(), expected)
        fields = nd.to_bev_channels(arr, resolution=0.5, channels="fields")
        self.assertEqual(fields.shape, (3, 20, 20))

    def test_voxel(self):
        arr = np.array([
            [0.05, 0.05, 0.05, 10, 0.0, 0.0],
            [0.09, 0.09, 0.09, 10, 0.0, 0.0],   # 0.1 分辨率下与上一行同一体素
            [0.95, 0.95, 0.95, 10, 0.0, 0.0],
        ], dtype=np.float32)
        vox = nd.to_voxel(arr, resolution=0.1, x_range=(0, 1), y_range=(0, 1), z_range=(0, 1))
        self.assertEqual(vox.shape, (10, 10, 10))
        self.assertEqual(vox[0, 0, 0], 2)
        self.assertEqual(vox[9, 9, 9], 1)
        self.assertEqual(int(vox.sum()), 3)

    def test_extent_and_world_mapping(self):
        info = nd.bev_extent(resolution=0.5, x_range=(-5, 5), y_range=(0, 10))
        self.assertEqual(info["shape"], (20, 20))
        x, y = nd.bev_to_world(0, 0, 0.5, (-5, 5), (0, 10))
        self.assertAlmostEqual(x, -4.75, places=5)
        self.assertAlmostEqual(y, 0.25, places=5)


class TestPipeline(unittest.TestCase):
    def test_pipeline_shape_and_order(self):
        calls = []

        def step1(points, frame):
            calls.append(1)
            return nd.filter_points(points, groups=nd.GROUP_DYNAMIC)

        def step2(points, frame):
            calls.append(2)
            return points * np.array([1, 1, 1, 1, 1, 1], dtype=np.float32)

        def step_noop(points, frame):
            calls.append(3)
            return None

        pipe = nd.ArrayPipeline([step1, step2]).add(step_noop)
        out, frame = pipe.run(nd.frame_to_ndarray(make_frame(tracks=1)), make_frame(tracks=1))
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(out.shape[0], 3)

    def test_load_processor_from_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "algo.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "import numpy as np\n"
                    "def process(points, frame):\n"
                    "    return points[points[:, 1] > 2.0]\n"
                )
            fn = nd.load_processor("%s:process" % path)
            out = fn(nd.frame_to_ndarray(make_frame()), make_frame())
            self.assertTrue(all(row[1] > 2.0 for row in out))

    def test_load_processor_errors(self):
        with self.assertRaises(ValueError):
            nd.load_processor("no_colon_here")
        with self.assertRaises(FileNotFoundError):
            nd.load_processor("./definitely_missing_file.py:process")

    def test_stream_arrays(self):
        from radarpi.link import SimulatedLink

        sim = SimulatedLink(fps=120.0)
        shapes = []
        for points, frame in nd.stream_arrays(sim, max_frames=3):
            shapes.append(points.shape)
            self.assertEqual(points.shape[1], len(nd.POINT_FIELDS))
        self.assertEqual(len(shapes), 3)
        self.assertGreater(shapes[0][0], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
