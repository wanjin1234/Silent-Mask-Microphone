"""协议解析与指令构造的自检。

直接运行即可（无需 pytest）::

    cd radarpi
    PYTHONPATH=src python tests/test_protocol.py
"""

from __future__ import annotations

import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from radarpi import protocol as P  # noqa: E402


def make_frame(frame_id: int = 7, tracks: int = 0, track_fields: int = 3) -> P.Frame:
    """构造一帧覆盖各类分组、含负数与小数点的数据。"""
    points = [
        P.Point(0.123, 4.567, -0.001, 1234, 1.5, P.GROUP_DYNAMIC_HIGH),
        P.Point(-3.000, 0.500, 1.701, 300, -0.4, P.GROUP_DYNAMIC_HIGH),
        P.Point(2.001, 3.999, 0.002, 88, 0.0, P.GROUP_DYNAMIC_LOW),
        P.Point(-1.234, 2.345, 0.600, 512, 0.1, P.GROUP_LONG_MICRO_HIGH),
        P.Point(0.000, 1.000, 0.000, 7, 0.2, P.GROUP_LONG_MICRO_LOW),
        P.Point(1.111, 2.222, 0.333, 64000, -12.3, P.GROUP_SHORT_MICRO_HIGH),
        P.Point(-0.555, 5.555, 2.999, 1, 49.9, P.GROUP_SHORT_MICRO_LOW),
    ]
    counts = [0] * 6
    for p in points:
        counts[p.group] += 1
    header = P.FrameHeader(
        frame_period=100,
        frame_id=frame_id,
        bb_time=7,
        post_bb_time=3,
        transfer_time=2,
        frame_interval=11,
        counts=tuple(counts),
        track_count=tracks,
    )
    tr = [P.Track(1.0 * i, 2.0 * i, 0.5, 900 + i, 1.1 * i, i + 1) for i in range(tracks)]
    return P.Frame(header=header, points=points, tracks=tr, timestamp=1700000000.0)


class TestRoundTrip(unittest.TestCase):
    def test_roundtrip_basic(self):
        frame = make_frame()
        parser = P.RadarStreamParser()
        frames = parser.feed(P.encode_frame(frame))
        self.assertEqual(len(frames), 1)
        got = frames[0]
        self.assertEqual(got.frame_id, 7)
        self.assertTrue(got.complete)
        self.assertEqual(got.header.counts, frame.header.counts)
        self.assertEqual(got.header.frame_period, 100)
        self.assertEqual(got.header.bb_time, 7)
        self.assertEqual(got.header.post_bb_time, 3)
        self.assertEqual(got.header.transfer_time, 2)
        self.assertEqual(got.header.frame_interval, 11)
        self.assertEqual(len(got.points), len(frame.points))
        for a, b in zip(got.points, frame.points):
            self.assertAlmostEqual(a.x, b.x, places=3)
            self.assertAlmostEqual(a.y, b.y, places=3)
            self.assertAlmostEqual(a.z, b.z, places=3)
            self.assertAlmostEqual(a.speed, b.speed, places=2)
            self.assertEqual(a.snr, b.snr)
            self.assertEqual(a.group, b.group)
        self.assertEqual(parser.stats.resyncs, 0)
        self.assertEqual(parser.stats.dropped_bytes, 0)
        self.assertEqual(parser.stats.bad_tails, 0)

    def test_roundtrip_with_tracks_6byte(self):
        frame = make_frame(tracks=3, track_fields=3)
        parser = P.RadarStreamParser()
        frames = parser.feed(P.encode_frame(frame, track_fields=3))
        self.assertEqual(len(frames), 1)
        got = frames[0]
        self.assertEqual(len(got.tracks), 3)
        self.assertAlmostEqual(got.tracks[1].x, 1.0, places=3)
        self.assertAlmostEqual(got.tracks[2].z, 0.5, places=3)
        self.assertEqual(got.header.track_count, 3)

    def test_roundtrip_with_tracks_10byte(self):
        """手册表格在航迹字段处被截断，解析器应能自动识别 10 字节布局。"""
        frame = make_frame(tracks=4, track_fields=5)
        parser = P.RadarStreamParser()
        data = P.encode_frame(frame, track_fields=5) + P.encode_frame(make_frame(frame_id=8))
        frames = parser.feed(data)
        self.assertEqual(len(frames), 2)
        self.assertEqual(len(frames[0].tracks), 4)
        self.assertAlmostEqual(frames[0].tracks[3].speed, 3.3, places=2)
        self.assertEqual(frames[0].tracks[3].snr, 903)
        self.assertEqual(frames[1].frame_id, 8)

    def test_multiple_frames_in_one_chunk(self):
        data = b"".join(P.encode_frame(make_frame(frame_id=i)) for i in range(5))
        parser = P.RadarStreamParser()
        frames = parser.feed(data)
        self.assertEqual([f.frame_id for f in frames], [0, 1, 2, 3, 4])
        self.assertEqual(parser.stats.frames, 5)

    def test_zero_point_frame(self):
        header = P.FrameHeader(frame_id=42, frame_period=50, counts=(0, 0, 0, 0, 0, 0))
        frame = P.Frame(header=header, points=[])
        parser = P.RadarStreamParser()
        frames = parser.feed(P.encode_frame(frame))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].points, [])
        self.assertEqual(frames[0].frame_id, 42)


class TestStreamRobustness(unittest.TestCase):
    def test_byte_by_byte_split(self):
        data = b"".join(P.encode_frame(make_frame(frame_id=i)) for i in range(3))
        parser = P.RadarStreamParser()
        out = []
        for i in range(len(data)):
            out.extend(parser.feed(data[i:i + 1]))
        self.assertEqual([f.frame_id for f in out], [0, 1, 2])

    def test_random_chunking(self):
        frame = make_frame(tracks=2)
        data = P.encode_frame(frame)
        rng = random.Random(1234)
        parser = P.RadarStreamParser()
        out = []
        pos = 0
        while pos < len(data):
            n = rng.randint(1, 17)
            out.extend(parser.feed(data[pos:pos + n]))
            pos += n
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].points), len(frame.points))
        self.assertEqual(len(out[0].tracks), 2)

    def test_garbage_prefix_resync(self):
        junk = bytes(rng for rng in range(0, 200))  # 含大量伪包头候选的噪声
        data = junk + P.encode_frame(make_frame(frame_id=99))
        parser = P.RadarStreamParser()
        frames = parser.feed(data)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].frame_id, 99)
        self.assertGreater(parser.stats.dropped_bytes, 0)

    def test_middle_garbage_recovers(self):
        a, b = P.encode_frame(make_frame(frame_id=1)), P.encode_frame(make_frame(frame_id=2))
        parser = P.RadarStreamParser()
        frames = parser.feed(a[:10])
        frames += parser.feed(b"\xde\xad\xbe\xef" * 5)
        frames += parser.feed(b)
        self.assertEqual([f.frame_id for f in frames][-1], 2)

    def test_big_endian_auto_detect(self):
        frame = make_frame(frame_id=5)
        data = P.encode_frame(frame, endianness="big")
        parser = P.RadarStreamParser()
        frames = parser.feed(data)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].frame_id, 5)
        self.assertEqual(parser.stats.endianness, "big")
        for a, b in zip(frames[0].points, frame.points):
            self.assertAlmostEqual(a.x, b.x, places=3)

    def test_corrupted_tail_recovered(self):
        """包尾被破坏时应重新同步，并仍然解析出后续的帧。"""
        bad = bytearray(P.encode_frame(make_frame(frame_id=1)))
        bad[-4:] = b"\x00\x00\x00\x00"
        good = P.encode_frame(make_frame(frame_id=2))
        parser = P.RadarStreamParser()
        frames = parser.feed(bytes(bad) + good)
        self.assertIn(2, [f.frame_id for f in frames])
        self.assertGreater(parser.stats.bad_tails + parser.stats.resyncs, 0)

    def test_truncated_stream_waits(self):
        frame = make_frame()
        data = P.encode_frame(frame)
        parser = P.RadarStreamParser()
        self.assertEqual(parser.feed(data[:-5]), [])
        self.assertEqual(len(parser.feed(data[-5:])), 1)

    def test_cloud_without_header_recovered(self):
        """帧头丢失时，借助点云包尾反推点数，尽量把数据救回来。"""
        data = P.encode_frame(make_frame(frame_id=3))
        cloud_only = data[P.FRAME_HEADER_SIZE:]
        parser = P.RadarStreamParser()
        frames = parser.feed(cloud_only)
        self.assertEqual(len(frames), 1)
        self.assertEqual(len(frames[0].points), 7)
        self.assertFalse(frames[0].complete)  # 帧头缺失，标记为不完整


class TestCommands(unittest.TestCase):
    def test_all_template_commands(self):
        expected = {
            ("radar_height", ("1.7",)): "set radar_height 1.7\r\n",
            ("radar_inclination", ("30",)): "set radar_inclination 30\r\n",
            ("boundary", ("-3", "3", "0.8", "0.2", "5")): "set boundary -3 3 0.8 0.2 5\r\n",
            ("mmsinterval", ("2", "34")): "set_mmsinterval 2 34\r\n",
            ("cfar_coeff", ("33", "44", "55")): "set_cfar_coeff 33 44 55\r\n",
            ("scan_start", ()): "scan start\r\n",
            ("scan_stop", ()): "scan stop\r\n",
        }
        for (key, values), want in expected.items():
            self.assertEqual(P.command_from_key(key, list(values)).decode("ascii"), want)

    def test_aliases_and_errors(self):
        self.assertEqual(P.command_from_key("start").decode("ascii"), "scan start\r\n")
        self.assertEqual(P.command_from_key("stop").decode("ascii"), "scan stop\r\n")
        with self.assertRaises(ValueError):
            P.command_from_key("boundary", ["1", "2"])
        with self.assertRaises(ValueError):
            P.command_from_key("scan_start", ["x"])
        with self.assertRaises(KeyError):
            P.command_from_key("no_such_command")

    def test_defaults_match_manual(self):
        for cmd in P.COMMANDS:
            if cmd.args:
                rendered = cmd.render(cmd.default.split())
                self.assertTrue(rendered.startswith(cmd.template.split("{")[0].strip()))

    def test_parse_command_line(self):
        self.assertEqual(P.parse_command_line("set radar_height 1.7"), ("radar_height", ["1.7"]))
        self.assertEqual(P.parse_command_line("set_cfar_coeff 33 44 55"), ("cfar_coeff", ["33", "44", "55"]))
        self.assertEqual(P.parse_command_line("scan start"), ("scan", ["start"]))
        self.assertEqual(P.parse_command_line("set_mmsinterval 2 34"), ("mmsinterval", ["2", "34"]))
        self.assertEqual(P.parse_command_line("   "), ("", []))


class TestEncoderGuards(unittest.TestCase):
    def test_count_mismatch_raises(self):
        frame = make_frame()
        frame.header.counts = (99, 0, 0, 0, 0, 0)
        with self.assertRaises(ValueError):
            P.encode_frame(frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)
