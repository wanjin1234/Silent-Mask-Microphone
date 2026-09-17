"""pygame 界面自检（无显示器也能跑：SDL_VIDEODRIVER=dummy）。

直接运行::

    cd radarpi
    SDL_VIDEODRIVER=dummy PYTHONPATH=src python tests/test_viewer.py

覆盖：数据源接入、渲染循环、矩阵转换、存矩阵(.npz)、截图、处理器插件。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")   # 无显示器环境
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("本测试需要 numpy")

try:
    import pygame
except ImportError:  # pragma: no cover
    sys.exit("本测试需要 pygame")


def make_viewer(**kwargs):
    from radarpi.config import Config
    from radarpi.link import SimulatedLink
    from radarpi.viewer_pygame import RadarViewer

    sim = SimulatedLink(fps=200.0)     # 仿真跑快点，测试更省时间
    viewer = RadarViewer(sim, config=Config(), width=1280, height=720, **kwargs)
    return viewer, sim


class TestViewerRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    def test_draw_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            viewer, _ = make_viewer()
            pygame.display.set_mode((1280, 720))       # dummy 驱动下是离屏画面
            viewer.screen = pygame.display.get_surface()
            viewer.start()
            # 等足够多的帧到来
            import time

            deadline = time.time() + 5.0
            while viewer.frames_seen < 3 and time.time() < deadline:
                time.sleep(0.05)
            for _ in range(5):
                viewer.draw()
            self.assertIsNotNone(viewer.frame, "没有收到帧")
            self.assertIsNotNone(viewer.array, "没有生成 ndarray 矩阵")
            self.assertEqual(viewer.array.shape[1], 6)
            self.assertIsNotNone(viewer.bev, "没有生成 BEV 矩阵")
            self.assertEqual(viewer.bev.ndim, 2)

            shot = os.path.join(tmp, "shot.png")
            viewer.save_snapshot(shot)
            self.assertTrue(os.path.exists(shot))
            self.assertGreater(os.path.getsize(shot), 5000)
            viewer.close()

    def test_save_buffer_npz(self):
        with tempfile.TemporaryDirectory() as tmp:
            viewer, _ = make_viewer(buffer_frames=5)
            viewer.record_dir = tmp
            viewer.start()
            import time

            deadline = time.time() + 5.0
            while len(viewer._frame_buffer) < 5 and time.time() < deadline:
                time.sleep(0.05)
            viewer.save_buffer()
            files = [f for f in os.listdir(tmp) if f.endswith(".npz")]
            self.assertEqual(len(files), 1, "没有生成 .npz：%s" % os.listdir(tmp))
            # np.load 会持有文件句柄，读完要关闭，否则 Windows 上临时目录删不掉
            with np.load(os.path.join(tmp, files[0])) as data:
                keys = set(data.files)
                self.assertIn("points_0000", keys)
                self.assertIn("frame_ids", keys)
                self.assertIn("counts", keys)
                arr = data["points_0000"]
                self.assertEqual(arr.shape[1], 6)
                self.assertEqual(arr.dtype, np.float32)
            viewer.close()

    def test_recorder_from_viewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            viewer, _ = make_viewer()
            viewer.record_dir = tmp
            viewer.record_format = "jsonl"
            viewer.toggle_record()
            self.assertIsNotNone(viewer._recorder)
            viewer.start()
            import time

            time.sleep(0.6)
            viewer.toggle_record()
            self.assertIsNone(viewer._recorder)
            files = [f for f in os.listdir(tmp) if f.endswith(".jsonl")]
            self.assertEqual(len(files), 1)
            self.assertGreater(os.path.getsize(os.path.join(tmp, files[0])), 100)
            viewer.close()

    def test_processor_changes_display(self):
        """处理器返回的矩阵应当成为界面显示的点（块内只保留动态点）。"""
        from radarpi import ndarray_iface as nd

        def only_micro(points, frame):
            return nd.filter_points(points, groups=nd.GROUP_MICRO)

        with tempfile.TemporaryDirectory() as tmp:
            viewer, _ = make_viewer(processor=only_micro, processor_name="test")
            viewer.record_dir = tmp
            pygame.display.set_mode((1280, 720))
            viewer.screen = pygame.display.get_surface()
            viewer.start()
            import time

            deadline = time.time() + 5.0
            while viewer.frames_seen < 3 and time.time() < deadline:
                time.sleep(0.05)
            shown = viewer._point_list()
            self.assertTrue(shown, "处理后没有点可显示")
            self.assertTrue(all(p.group >= 2 for p in shown), "过滤器没有生效")
            # 原始帧里本来是有动态点的，说明确实被处理器改掉了
            self.assertTrue(any(p.group <= 1 for p in viewer.frame.points))
            viewer.close()

    def test_view_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            viewer, _ = make_viewer()
            viewer.record_dir = tmp
            viewer.start()
            import time

            deadline = time.time() + 5.0
            while viewer.frames_seen < 3 and time.time() < deadline:
                time.sleep(0.05)
            viewer.filter = "all"
            all_pts = viewer._point_list()
            viewer.filter = "dynamic"
            dyn_pts = viewer._point_list()
            viewer.filter = "micro"
            micro_pts = viewer._point_list()
            viewer.filter = "tracks"
            self.assertEqual(len(viewer._point_list()), 0)
            self.assertTrue(all(p.group <= 1 for p in dyn_pts))
            self.assertTrue(all(p.group >= 2 for p in micro_pts))
            self.assertEqual(len(all_pts), len(dyn_pts) + len(micro_pts))
            viewer.close()

    def test_draw_does_not_crash_without_data(self):
        """还没收到数据时也要能正常画（避免启动瞬间崩溃）。"""
        viewer, _ = make_viewer()
        pygame.display.set_mode((1280, 720))
        viewer.screen = pygame.display.get_surface()
        viewer.draw()
        viewer.close()


class TestExampleProcessor(unittest.TestCase):
    def test_example_file_loads_and_runs(self):
        from radarpi import ndarray_iface as nd
        from radarpi.link import SimulatedLink

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples", "processor_demo.py")
        fn = nd.load_processor("%s:process" % os.path.abspath(path))
        sim = SimulatedLink(fps=200.0)
        got = 0
        for points, frame in nd.stream_arrays(sim, max_frames=3):
            out = fn(points, frame)
            self.assertEqual(out.shape[1], 6)
            self.assertTrue((out[:, 5] <= 1).all(), "示例应当只保留动态点")
            got += 1
        self.assertEqual(got, 3)

    def test_observe_only_returns_none(self):
        from radarpi import ndarray_iface as nd
        from radarpi.link import SimulatedLink

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples", "processor_demo.py")
        fn = nd.load_processor("%s:observe_only" % os.path.abspath(path))
        sim = SimulatedLink(fps=200.0)
        for points, frame in nd.stream_arrays(sim, max_frames=1):
            self.assertIsNone(fn(points, frame))


if __name__ == "__main__":
    unittest.main(verbosity=2)
