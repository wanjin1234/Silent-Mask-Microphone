"""串口链路的集成自检：用假串口把「真实链路」跑通，无需接硬件。

直接运行::

    cd radarpi
    PYTHONPATH=src python tests/test_link.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from radarpi import link as link_mod  # noqa: E402
from radarpi import protocol as P  # noqa: E402
from radarpi.recorder import Recorder, ReplaySource  # noqa: E402
from radarpi.serialport import SerialError  # noqa: E402


def make_frame(frame_id: int = 1, points: int = 20) -> P.Frame:
    pts = [
        P.Point(0.01 * i, 1.5 + 0.01 * i, 0.5, 100 + i, 0.1 * i, i % 6)
        for i in range(points)
    ]
    counts = [0] * 6
    for p in pts:
        counts[p.group] += 1
    header = P.FrameHeader(frame_id=frame_id, frame_period=100, counts=tuple(counts))
    return P.Frame(header=header, points=pts, timestamp=1700000000.0 + frame_id)


class FakePort:
    """把预生成的字节流按块吐出，并记录所有写入。"""

    def __init__(self, chunks, fail_after=None, label="fake"):
        self.chunks = list(chunks)
        self.written = []
        self.closed = False
        self.reads = 0
        self.fail_after = fail_after
        self.device = label

    def read(self, size=4096):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise SerialError("模拟读取失败")
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def write(self, data):
        self.written.append(data)
        return len(data)

    def close(self):
        self.closed = True


class PatchPorts:
    """临时替换 link 模块里的 open_port / resolve_device。"""

    def __init__(self, ports):
        self.ports = list(ports)
        self.opened = []

    def __enter__(self):
        self._orig_open = link_mod.open_port
        self._orig_resolve = link_mod.resolve_device

        def fake_open(device, baudrate, timeout, prefer_pyserial=True):
            port = self.ports.pop(0)
            self.opened.append(port)
            return port

        link_mod.open_port = fake_open
        link_mod.resolve_device = lambda device: device or "/dev/fake"
        return self

    def __exit__(self, *exc):
        link_mod.open_port = self._orig_open
        link_mod.resolve_device = self._orig_resolve


class TestRadarLink(unittest.TestCase):
    def test_profile_and_frames(self):
        data = b"".join(P.encode_frame(make_frame(i)) for i in range(1, 5))
        port = FakePort([data[:100], data[100:]])
        profile = ["set radar_height 1.7", "set boundary -3 3 0.8 0.2 5"]
        with PatchPorts([port]):
            link = link_mod.RadarLink(device="/dev/fake", profile=profile, read_timeout=0.01)
            frames = []
            for frame in link.frames():
                frames.append(frame)
                if len(frames) == 4:
                    link.close()
            link.close()

        # 连接后应先下发配置，最后自动 scan start
        sent = [w.decode("ascii") for w in port.written]
        self.assertEqual(
            sent,
            [
                "set radar_height 1.7\r\n",
                "set boundary -3 3 0.8 0.2 5\r\n",
                "scan start\r\n",
                "scan stop\r\n",  # close() 时自动停
            ],
        )
        self.assertEqual([f.frame_id for f in frames], [1, 2, 3, 4])
        self.assertEqual(len(frames[0].points), 20)
        status = link.status()
        self.assertEqual(status["parse"]["frames"], 4)
        self.assertGreater(status["parse"]["bytes_in"], 0)
        self.assertEqual(status["parse"]["resyncs"], 0)
        self.assertTrue(status["connected"] is False or port.closed)

    def test_reconnect_after_failure(self):
        first = b"".join(P.encode_frame(make_frame(i)) for i in range(1, 3))
        second = P.encode_frame(make_frame(9))
        port1 = FakePort([first[:50]], fail_after=1)
        port2 = FakePort([first[50:], second])
        with PatchPorts([port1, port2]):
            link = link_mod.RadarLink(device="/dev/fake", read_timeout=0.01, command_gap=0)
            frames = []
            for frame in link.frames():
                frames.append(frame)
                if frame.frame_id == 9:
                    link.close()
            link.close()
        self.assertIn(9, [f.frame_id for f in frames])
        self.assertGreaterEqual(link.status()["reconnects"], 1)

    def test_no_reconnect_raises(self):
        port = FakePort([b"x"], fail_after=1)
        with PatchPorts([port]):
            link = link_mod.RadarLink(device="/dev/fake", read_timeout=0.01, reconnect=False)
            with self.assertRaises(SerialError):
                for _ in link.frames():
                    pass

    def test_send_command_roundtrip(self):
        port = FakePort([P.encode_frame(make_frame(1))])
        with PatchPorts([port]):
            link = link_mod.RadarLink(device="/dev/fake", read_timeout=0.01, auto_start=False)
            for _ in link.frames():  # 触发连接并读走一帧
                break
            link.send_text("set_cfar_coeff 33 44 55")
            link.send(P.build_command("scan stop"))
            link.close()
        texts = [w.decode("ascii") for w in port.written]
        self.assertIn("set_cfar_coeff 33 44 55\r\n", texts)
        self.assertIn("scan stop\r\n", texts)
        self.assertIn("set_cfar_coeff 33 44 55", link.status()["last_commands"])

    def test_stats_reflect_garbage(self):
        junk = bytes(range(256)) * 2
        data = junk + P.encode_frame(make_frame(3))
        port = FakePort([data])
        with PatchPorts([port]):
            link = link_mod.RadarLink(device="/dev/fake", read_timeout=0.01)
            for frame in link.frames():
                self.assertEqual(frame.frame_id, 3)
                break
            link.close()
        st = link.status()["parse"]
        self.assertGreater(st["dropped_bytes"], 0)
        self.assertEqual(st["frames"], 1)


class TestSimulator(unittest.TestCase):
    def test_simulator_produces_frames(self):
        sim = link_mod.SimulatedLink(fps=200.0)
        frames = []
        for frame in sim.frames():
            frames.append(frame)
            if len(frames) >= 5:
                break
        sim.close()
        self.assertEqual(len(frames), 5)
        self.assertGreater(len(frames[0].points), 50)
        self.assertEqual(sum(frames[0].header.counts), len(frames[0].points))
        self.assertEqual(frames[0].header.counts, tuple(
            sum(1 for p in frames[0].points if p.group == g) for g in range(6)
        ))

    def test_simulator_scan_stop(self):
        sim = link_mod.SimulatedLink(fps=200.0)
        sim.send_text("scan stop")
        self.assertFalse(sim.status()["scanning"])
        sim.send_text("scan start")
        self.assertTrue(sim.status()["scanning"])
        sim.close()


class TestRecorderReplay(unittest.TestCase):
    def test_jsonl_roundtrip(self):
        import tempfile

        frame = make_frame(11, points=9)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            with Recorder(path, "jsonl") as rec:
                rec.write(frame)
            src = ReplaySource(path)
            got = list(src.frames())
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].frame_id, 11)
            self.assertEqual(len(got[0].points), 9)
            self.assertAlmostEqual(got[0].points[3].x, frame.points[3].x, places=3)
            self.assertEqual(got[0].points[3].group, frame.points[3].group)

    def test_csv_roundtrip(self):
        import tempfile

        frame = make_frame(12, points=6)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.csv")
            with Recorder(path, "csv") as rec:
                rec.write(frame)
            got = list(ReplaySource(path).frames())
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].frame_id, 12)
            self.assertEqual(len(got[0].points), 6)
            self.assertAlmostEqual(got[0].points[2].y, frame.points[2].y, places=3)

    def test_rotation_creates_new_file(self):
        import glob
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rot.csv")
            rec = Recorder(path, "csv", max_bytes=1500)
            for i in range(40):
                rec.write(make_frame(i, points=8))
            rec.close()
            files = sorted(glob.glob(os.path.join(tmp, "rot*.csv")))
            self.assertGreater(len(files), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
