"""4D 成像毫米波雷达串口协议解析与配置指令构造。

协议依据：《4D 成像毫米波雷达使用说明》第四节「数据输出与解析」
（表格 1 Demo 输出协议）以及第三节第 3 条「高级功能配置」指令表。

串口参数：3000000 bit/s，8 数据位，1 停止位，无校验。

数据流由三种包组成，每种包均以 32bit 固定包头开始：

    帧头包   0xFFEEFFDC ... 0xFFEEFFD3   固定 28 字节，携带本帧各类点数与耗时
    点云包   0xFFDDFECB ... 0xFFDDFEC4   点数由帧头决定，每点 10 字节
    航迹包   0xFFCCFDBA ...              航迹数由帧头决定

一帧的典型顺序为「帧头包 → 点云包 →（可选）航迹包」。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "FRAME_HEADER_MAGIC",
    "CLOUD_MAGIC",
    "TRACK_MAGIC",
    "GROUP_KEYS",
    "GROUP_NAMES",
    "GROUP_COLORS",
    "Point",
    "Track",
    "FrameHeader",
    "Frame",
    "ParseStats",
    "RadarStreamParser",
    "ConfigCommand",
    "COMMANDS",
    "COMMAND_BY_KEY",
    "build_command",
    "command_from_key",
    "parse_command_line",
    "encode_frame",
]

# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

FRAME_HEADER_MAGIC = 0xFFEEFFDC
FRAME_HEADER_TAIL = 0xFFEEFFD3
CLOUD_MAGIC = 0xFFDDFECB
CLOUD_TAIL = 0xFFDDFEC4
TRACK_MAGIC = 0xFFCCFDBA

FRAME_HEADER_SIZE = 28
"""帧头包长度：包头(4) + 5 个 32bit 字段(20) + 包尾(4)。"""

POINT_SIZE = 10
"""单个点：X/Y/Z/SNR/速度 各 16bit。"""

TRACK_FIELD_COUNTS = (3, 5)
"""航迹可能的字段数。手册表格在「航迹 End X」处被截断，未列出航迹是否含 SNR/速度。
3 = X/Y/Z（6 字节），5 = X/Y/Z/SNR/速度（10 字节）。解析器会自动判别。"""

ALL_MAGICS = (FRAME_HEADER_MAGIC, CLOUD_MAGIC, TRACK_MAGIC)

# 点云分组顺序，与手册表格中「点云包」字段的出现顺序一致。
GROUP_DYNAMIC_HIGH = 0
GROUP_DYNAMIC_LOW = 1
GROUP_LONG_MICRO_HIGH = 2
GROUP_LONG_MICRO_LOW = 3
GROUP_SHORT_MICRO_HIGH = 4
GROUP_SHORT_MICRO_LOW = 5

GROUP_KEYS: Tuple[str, ...] = (
    "dynamic_high",
    "dynamic_low",
    "long_micro_high",
    "long_micro_low",
    "short_micro_high",
    "short_micro_low",
)

GROUP_NAMES: Tuple[str, ...] = (
    "动态高置信度点",
    "动态低置信度点",
    "长时微动高置信度点",
    "长时微动低置信度点",
    "短时微动高置信度点",
    "短时微动低置信度点",
)

GROUP_SHORT_NAMES: Tuple[str, ...] = ("动高", "动低", "长微高", "长微低", "短微高", "短微低")

GROUP_COLORS: Tuple[str, ...] = (
    "#ffd23f",  # 动态高置信度 —— 亮黄
    "#8a8a5c",  # 动态低置信度 —— 暗黄
    "#37c8ff",  # 长时微动高置信度 —— 青
    "#2b6f8a",  # 长时微动低置信度 —— 暗青
    "#7cff6b",  # 短时微动高置信度 —— 绿
    "#3f7a3a",  # 短时微动低置信度 —— 暗绿
)

_MAGIC_PATTERNS = {
    magic: {"little": struct.pack("<I", magic), "big": struct.pack(">I", magic)}
    for magic in ALL_MAGICS
}

#: 包尾的字节模式（仅用于丢帧头时按包尾反推点数）
_TAIL_PATTERNS = {
    magic: {"little": struct.pack("<I", magic), "big": struct.pack(">I", magic)}
    for magic in (FRAME_HEADER_TAIL, CLOUD_TAIL)
}


def _bits(word: int, lo: int, hi: int) -> int:
    """取出 32bit 字中 [lo, hi] 区间（含两端）的位。"""
    return (word >> lo) & ((1 << (hi - lo + 1)) - 1)


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass
class Point:
    """一个点云点。坐标单位为米，速度为米/秒。"""

    x: float
    y: float
    z: float
    snr: int
    speed: float
    group: int = GROUP_DYNAMIC_HIGH
    index: int = 0

    @property
    def group_name(self) -> str:
        return GROUP_NAMES[self.group]

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "z": round(self.z, 3),
            "snr": self.snr,
            "v": round(self.speed, 2),
            "g": self.group,
        }


@dataclass
class Track:
    """一条跟踪航迹。"""

    x: float
    y: float
    z: float
    snr: int = 0
    speed: float = 0.0
    index: int = 0

    def as_dict(self) -> dict:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "z": round(self.z, 3),
            "snr": self.snr,
            "v": round(self.speed, 2),
            "id": self.index,
        }


@dataclass
class FrameHeader:
    """帧头包内容。"""

    frame_period: int = 0
    frame_id: int = 0
    bb_time: int = 0
    post_bb_time: int = 0
    transfer_time: int = 0
    frame_interval: int = 0
    counts: Tuple[int, ...] = (0, 0, 0, 0, 0, 0)
    track_count: int = 0

    @property
    def point_count(self) -> int:
        return sum(self.counts)

    def count_map(self) -> Dict[str, int]:
        d = {k: c for k, c in zip(GROUP_KEYS, self.counts)}
        d["tracks"] = self.track_count
        d["points"] = self.point_count
        return d

    def as_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "frame_period": self.frame_period,
            "bb_time": self.bb_time,
            "post_bb_time": self.post_bb_time,
            "transfer_time": self.transfer_time,
            "frame_interval": self.frame_interval,
            "counts": self.count_map(),
        }


@dataclass
class Frame:
    """一帧数据。"""

    header: FrameHeader
    points: List[Point] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    timestamp: float = 0.0
    """本帧解析完成时刻（time.time()）。"""
    complete: bool = True
    """当帧头声明的点数与实际收到的点数一致时为 True。"""

    @property
    def frame_id(self) -> int:
        return self.header.frame_id

    def as_dict(self, max_points: Optional[int] = None, with_tracks: bool = True) -> dict:
        pts = self.points
        truncated = False
        if max_points is not None and len(pts) > max_points:
            step = len(pts) / float(max_points)  # 均匀抽样，保持各类点比例
            pts = [pts[int(i * step)] for i in range(max_points)]
            truncated = True
        d = {
            "ts": round(self.timestamp, 3),
            "header": self.header.as_dict(),
            "points": [p.as_dict() for p in pts],
            "sent_points": len(pts),
            "truncated": truncated,
            "complete": self.complete,
        }
        if with_tracks:
            d["tracks"] = [t.as_dict() for t in self.tracks]
        return d


@dataclass
class ParseStats:
    """解析统计，用于诊断链路质量（网页面板与 doctor 都会显示）。"""

    bytes_in: int = 0
    frames: int = 0
    packets: int = 0
    points: int = 0
    resyncs: int = 0
    dropped_bytes: int = 0
    bad_tails: int = 0
    bad_lengths: int = 0
    incomplete_frames: int = 0
    endianness: str = "little"

    def as_dict(self) -> dict:
        return {
            "bytes_in": self.bytes_in,
            "frames": self.frames,
            "packets": self.packets,
            "points": self.points,
            "resyncs": self.resyncs,
            "dropped_bytes": self.dropped_bytes,
            "bad_tails": self.bad_tails,
            "bad_lengths": self.bad_lengths,
            "incomplete_frames": self.incomplete_frames,
            "endianness": self.endianness,
        }


# --------------------------------------------------------------------------
# 流式解析
# --------------------------------------------------------------------------


class RadarStreamParser:
    """增量式字节流解析器。

    用法::

        parser = RadarStreamParser()
        for chunk in serial_chunks():
            for frame in parser.feed(chunk):
                ...

    一帧的完整判定：帧头声明点数与点云包实际点数一致，且（若帧头声明有航迹）
    航迹包已收齐；或在收到下一帧帧头时强制结束上一帧。

    解析器具备自同步能力：半帧接入、丢包或噪声之后会自动丢弃无效字节并从
    下一个包头恢复。字节序在首次命中包头时自动判定。
    """

    #: 单帧点数上限，超过则判定为误同步
    MAX_POINTS = 20000
    #: 缓冲区上限，超过则丢弃最旧数据，避免内存无界增长
    MAX_BUFFER = 4 * 1024 * 1024
    #: 无帧头时等待点云包尾的上限（字节）
    RECOVER_LIMIT = 256 * 1024

    def __init__(self, track_fields: Optional[int] = None, endianness: Optional[str] = None) -> None:
        self.stats = ParseStats(endianness=endianness or "little")
        self._buf = bytearray()
        self._current: Optional[Frame] = None
        self._ready: List[Frame] = []
        self._track_fields = track_fields
        self._endian = endianness

    # -- 内部工具 ---------------------------------------------------------

    @property
    def endian(self) -> str:
        return self._endian or "little"

    def _fmt(self, spec: str) -> str:
        return ("<" if self.endian == "little" else ">") + spec

    def _unpack_i(self, offset: int = 0) -> int:
        return struct.unpack_from(self._fmt("I"), self._buf, offset)[0]

    def _find_magic(self) -> Optional[Tuple[int, int, str]]:
        """寻找最早出现的包头，返回 (偏移, 包头值, 字节序)。"""
        best: Optional[Tuple[int, int, str]] = None
        for magic, patterns in _MAGIC_PATTERNS.items():
            for endian, pat in patterns.items():
                if self._endian is not None and endian != self._endian:
                    continue
                idx = self._buf.find(pat)
                if idx == -1:
                    continue
                if best is None or idx < best[0]:
                    best = (idx, magic, endian)
        return best

    def _discard(self, count: int) -> None:
        if count <= 0:
            return
        del self._buf[:count]
        self.stats.dropped_bytes += count
        self.stats.resyncs += 1

    def _emit_current(self, complete: Optional[bool] = None) -> None:
        frame = self._current
        if frame is None:
            return
        if complete is not None:
            frame.complete = frame.complete and complete
        if not frame.complete:
            self.stats.incomplete_frames += 1
        self._ready.append(frame)
        self._current = None
        self.stats.frames += 1

    # -- 对外接口 ---------------------------------------------------------

    def feed(self, data: bytes) -> List[Frame]:
        """喂入字节，返回本次解析出的完整帧（可能为空或多帧）。"""
        if data:
            self.stats.bytes_in += len(data)
            self._buf.extend(data)
        if len(self._buf) > self.MAX_BUFFER:
            del self._buf[: len(self._buf) - self.MAX_BUFFER]

        while True:
            found = self._find_magic()
            if found is None:
                # 未找到包头：仅保留末尾 3 字节，避免切断跨块的包头
                if len(self._buf) > 3:
                    self.stats.dropped_bytes += len(self._buf) - 3
                    del self._buf[: len(self._buf) - 3]
                break
            offset, magic, endian = found
            if offset > 0:
                self._discard(offset)
            if self._endian is None:
                self._endian = endian
                self.stats.endianness = endian

            if magic == FRAME_HEADER_MAGIC:
                if not self._parse_header():
                    break
            elif magic == CLOUD_MAGIC:
                if not self._parse_cloud():
                    break
            else:
                if not self._parse_track():
                    break

        out, self._ready = self._ready, []
        return out

    def flush(self) -> List[Frame]:
        """结束解析：把尚未收尾的当前帧也交出来（用于回放结束或串口断开）。"""
        self._emit_current()
        out, self._ready = self._ready, []
        return out

    # -- 各包解析 ---------------------------------------------------------

    def _parse_header(self) -> bool:
        if len(self._buf) < FRAME_HEADER_SIZE:
            return False
        magic, w1, frame_id, w3, w4, w5, tail = struct.unpack_from(self._fmt("7I"), self._buf, 0)
        if magic != FRAME_HEADER_MAGIC or tail != FRAME_HEADER_TAIL:
            self.stats.bad_tails += 1
            self._discard(1)
            return True

        counts = (
            _bits(w1, 10, 19),  # 动态高置信度点数
            _bits(w3, 0, 9),  # 动态低置信度点数
            _bits(w3, 20, 29),  # 长时微动高置信度点数
            _bits(w4, 0, 9),  # 长时微动低置信度点数
            _bits(w4, 10, 19),  # 短时微动高置信度点数
            _bits(w4, 20, 29),  # 短时微动低置信度点数
        )
        track_count = _bits(w3, 10, 19)
        if sum(counts) > self.MAX_POINTS or track_count > self.MAX_POINTS:
            # 明显是误同步（例如噪声中恰好出现包头），丢弃后重新同步
            self.stats.bad_lengths += 1
            self._discard(4)
            return True

        del self._buf[:FRAME_HEADER_SIZE]
        # 新帧头出现，意味着上一帧到此为止
        self._emit_current()

        self.stats.packets += 1
        self._current = Frame(
            header=FrameHeader(
                frame_period=_bits(w1, 0, 9),
                frame_id=frame_id,
                bb_time=_bits(w5, 0, 7),
                post_bb_time=_bits(w5, 8, 15),
                transfer_time=_bits(w5, 16, 23),
                frame_interval=_bits(w5, 24, 31),
                counts=counts,
                track_count=track_count,
            ),
            timestamp=time.time(),
        )
        return True

    def _parse_cloud(self) -> bool:
        """解析点云包。

        正常情况下点数由前面收到的帧头决定；若帧头丢失，则借包尾反推点数，
        尽量把数据救回来（会记一次 resync）。
        """
        header = self._current.header if self._current else None
        if header is None:
            tail_pat = _TAIL_PATTERNS[CLOUD_TAIL][self.endian]
            idx = self._buf.find(tail_pat)
            if idx < 0:
                # 等待包尾到齐。只有缓冲异常膨胀（例如噪声里混进了假包头）
                # 才放弃并逐步重新同步。
                if len(self._buf) > self.RECOVER_LIMIT:
                    self._discard(1)
                    return True
                return False
            count = (idx - 4 - 4) // POINT_SIZE
            if count < 0 or count > self.MAX_POINTS:
                self.stats.bad_lengths += 1
                self._discard(1)
                return True
            self.stats.resyncs += 1
            header = FrameHeader(counts=(count, 0, 0, 0, 0, 0))
            self._current = Frame(header=header, timestamp=time.time(), complete=False)

        need = 4 + POINT_SIZE * header.point_count + 8
        if len(self._buf) < need:
            return False
        if self._unpack_i(0) != CLOUD_MAGIC or self._unpack_i(need - 4) != CLOUD_TAIL:
            self.stats.bad_tails += 1
            self._discard(1)
            return True

        raw = bytes(self._buf[:need])
        del self._buf[:need]

        points: List[Point] = []
        offset = 4
        index = 0
        for group, count in enumerate(header.counts):
            for _ in range(count):
                x, y, z, snr, v = struct.unpack_from(self._fmt("3hHh"), raw, offset)
                offset += POINT_SIZE
                index += 1
                points.append(Point(x * 0.001, y * 0.001, z * 0.001, snr, v * 0.1, group, index))
        self._current.points = points
        self.stats.packets += 1
        self.stats.points += len(points)

        if header.track_count == 0:
            # 本帧没有航迹包，点云收齐即可交帧
            self._emit_current(complete=True)
        return True

    def _parse_track(self) -> bool:
        """解析航迹包。

        手册表格在「航迹 End X」处被截断，未明确航迹是否含 SNR/速度字段。
        这里按 6 字节（X/Y/Z）与 10 字节（X/Y/Z/SNR/速度）两种布局试探，
        用「包后是否紧接合法包头」判别真实布局，并记住结果。
        """
        if self._current is None:
            # 没有帧头信息时无法确定航迹数
            self.stats.bad_lengths += 1
            self._discard(4)
            return True
        count = self._current.header.track_count
        layouts = (self._track_fields,) if self._track_fields else TRACK_FIELD_COUNTS

        chosen: Optional[int] = None
        payload_size = 0
        for fields in layouts:
            size = 4 + 2 * fields * count
            if len(self._buf) < size + 4:
                continue
            nxt = self._unpack_i(size)
            if nxt in ALL_MAGICS:
                chosen, payload_size = fields, size
                self._track_fields = fields
                break
        if chosen is None:
            size = 4 + 2 * layouts[0] * count
            if len(self._buf) < size:
                return False  # 数据未到齐，继续等待
            chosen, payload_size = layouts[0], size
            self.stats.bad_lengths += 1

        raw = bytes(self._buf[:payload_size])
        del self._buf[:payload_size]

        tracks: List[Track] = []
        for i in range(count):
            offset = 4 + 2 * chosen * i
            if chosen == 3:
                x, y, z = struct.unpack_from(self._fmt("hhh"), raw, offset)
                snr, v = 0, 0
            else:
                x, y, z, snr, v = struct.unpack_from(self._fmt("3hHh"), raw, offset)
            tracks.append(Track(x * 0.001, y * 0.001, z * 0.001, snr, v * 0.1, i + 1))
        self._current.tracks = tracks
        self.stats.packets += 1
        self._emit_current(complete=True)
        return True


# --------------------------------------------------------------------------
# 配置指令
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigCommand:
    """一条上位机指令模板。"""

    key: str
    template: str
    """含 {0} 等占位符的指令文本（不含结尾的 CRLF）。"""
    args: int
    """期望的参数个数，0 表示无参数。"""
    default: str
    """手册中给出的示例参数。"""
    title: str
    desc: str

    def render(self, values: Sequence[object] = ()) -> str:
        if self.args == 0:
            if values:
                raise ValueError("%s 不接受参数" % self.key)
            return self.template
        values = list(values)
        if len(values) != self.args:
            raise ValueError("%s 需要 %d 个参数，收到 %d 个" % (self.key, self.args, len(values)))
        # 占位符必须齐全，否则 str.format 会静默丢弃多余参数（曾因此漏发坐标值）
        missing = [i for i in range(self.args) if ("{%d}" % i) not in self.template]
        if missing:
            raise ValueError("%s 的指令模板缺少占位符：%s" % (self.key, missing))
        return self.template.format(*values)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "template": self.template,
            "args": self.args,
            "default": self.default,
            "title": self.title,
            "desc": self.desc,
        }


#: 指令表，逐条对应手册第三节第 3 条「成像雷达启动配置指令说明」。
#: 注意：手册中指令名的写法并不统一 —— radar_height / radar_inclination /
#: boundary 用空格分隔，mmsinterval / cfar_coeff 用下划线。此处完全按手册原文，
#: 避免臆改导致固件不识别。
COMMANDS: Tuple[ConfigCommand, ...] = (
    ConfigCommand(
        key="radar_height",
        template="set radar_height {0}",
        args=1,
        default="1.7",
        title="雷达安装高度",
        desc="雷达安装位置距离地面的高度，单位 m。",
    ),
    ConfigCommand(
        key="radar_inclination",
        template="set radar_inclination {0}",
        args=1,
        default="30",
        title="安装倾斜角度",
        desc="雷达安装的下倾角，单位度。",
    ),
    ConfigCommand(
        key="boundary",
        template="set boundary {0} {1} {2} {3} {4}",
        args=5,
        default="-3 3 0.8 0.2 5",
        title="点云输出范围",
        desc="X、Y、Z 坐标值均在地面坐标系中指定；设成与房间边界重合可滤除房间外的杂点。",
    ),
    ConfigCommand(
        key="mmsinterval",
        template="set_mmsinterval {0} {1}",
        args=2,
        default="2 34",
        title="微动点处理间隔",
        desc="长时微动点处理间隔、短时微动点处理间隔，单位为帧。",
    ),
    ConfigCommand(
        key="cfar_coeff",
        template="set_cfar_coeff {0} {1} {2}",
        args=3,
        default="33 44 55",
        title="检测门限系数",
        desc="动态点、短时微动点、长时微动点的检测门限系数。",
    ),
    ConfigCommand(
        key="scan_start",
        template="scan start",
        args=0,
        default="",
        title="开始发送点云",
        desc="雷达开始输出点云数据。",
    ),
    ConfigCommand(
        key="scan_stop",
        template="scan stop",
        args=0,
        default="",
        title="停止发送点云",
        desc="雷达停止输出点云数据。",
    ),
)

COMMAND_BY_KEY: Dict[str, ConfigCommand] = {c.key: c for c in COMMANDS}
#: 便于命令行使用的别名
COMMAND_BY_KEY["start"] = COMMAND_BY_KEY["scan_start"]
COMMAND_BY_KEY["stop"] = COMMAND_BY_KEY["scan_stop"]

LINE_ENDING = "\r\n"
"""手册「格式」列明确要求每条指令以 \\r\\n 结尾。"""


def build_command(text: str) -> bytes:
    """把指令文本补齐 CRLF 并编码为字节。"""
    text = text.strip()
    if not text:
        raise ValueError("指令不能为空")
    return (text + LINE_ENDING).encode("ascii")


def command_from_key(key: str, values: Sequence[object] = ()) -> bytes:
    """按指令名与参数生成待发送字节。"""
    cmd = COMMAND_BY_KEY.get(key)
    if cmd is None:
        raise KeyError("未知指令 %r，可用：%s" % (key, ", ".join(sorted(COMMAND_BY_KEY))))
    return build_command(cmd.render(values))


def parse_command_line(text: str) -> Tuple[str, List[str]]:
    """把 ``"set radar_height 1.7"`` 拆成 ``("radar_height", ["1.7"])``。

    手册里指令名有两种写法（``set boundary`` 与 ``set_cfar_coeff``），
    这里统一归一化成不带 ``set`` 前缀的名字。
    """
    parts = text.strip().split()
    if not parts:
        return "", []
    head = parts[0]
    if head == "set" and len(parts) >= 2:
        return parts[1], parts[2:]
    if head.startswith("set_"):
        return head[4:], parts[1:]
    return head, parts[1:]


# --------------------------------------------------------------------------
# 组包（回环测试、协议自检、未来做 TCP/文件注入都要用到）
# --------------------------------------------------------------------------


def encode_frame(frame: Frame, endianness: str = "little", track_fields: int = 3) -> bytes:
    """把一帧编码成串口线上的字节流。

    与 :class:`RadarStreamParser` 互为逆运算，用于离线回环测试；也可用来把
    录制数据还原成原始报文，喂给其它工具。
    """
    fmt = ("<" if endianness == "little" else ">") + "I"
    hdr = frame.header
    counts = list(hdr.counts) + [0] * (6 - len(hdr.counts))

    w1 = (hdr.frame_period & 0x3FF) | ((counts[0] & 0x3FF) << 10)
    w3 = (counts[1] & 0x3FF) | ((hdr.track_count & 0x3FF) << 10) | ((counts[2] & 0x3FF) << 20)
    w4 = (counts[3] & 0x3FF) | ((counts[4] & 0x3FF) << 10) | ((counts[5] & 0x3FF) << 20)
    w5 = (
        (hdr.bb_time & 0xFF)
        | ((hdr.post_bb_time & 0xFF) << 8)
        | ((hdr.transfer_time & 0xFF) << 16)
        | ((hdr.frame_interval & 0xFF) << 24)
    )
    out = bytearray()
    out += struct.pack(fmt, FRAME_HEADER_MAGIC)
    out += struct.pack(fmt, w1)
    out += struct.pack(fmt, hdr.frame_id & 0xFFFFFFFF)
    out += struct.pack(fmt, w3)
    out += struct.pack(fmt, w4)
    out += struct.pack(fmt, w5)
    out += struct.pack(fmt, FRAME_HEADER_TAIL)

    # 点云包：按分组顺序排列，组内保持原始顺序
    out += struct.pack(fmt, CLOUD_MAGIC)
    for group, count in enumerate(counts):
        pts = [p for p in frame.points if p.group == group]
        if len(pts) != count:
            raise ValueError(
                "第 %d 组点数不一致：帧头声明 %d，实际 %d" % (group, count, len(pts))
            )
        for p in pts:
            out += struct.pack(
                ("<" if endianness == "little" else ">") + "3hHh",
                int(round(p.x * 1000)),
                int(round(p.y * 1000)),
                int(round(p.z * 1000)),
                int(p.snr) & 0xFFFF,
                int(round(p.speed * 10)),
            )
    out += struct.pack(fmt, 0)  # Reserved
    out += struct.pack(fmt, CLOUD_TAIL)

    if hdr.track_count:
        if len(frame.tracks) != hdr.track_count:
            raise ValueError("航迹数不一致：帧头声明 %d，实际 %d" % (hdr.track_count, len(frame.tracks)))
        out += struct.pack(fmt, TRACK_MAGIC)
        for t in frame.tracks:
            if track_fields == 3:
                out += struct.pack(
                    ("<" if endianness == "little" else ">") + "hhh",
                    int(round(t.x * 1000)),
                    int(round(t.y * 1000)),
                    int(round(t.z * 1000)),
                )
            else:
                out += struct.pack(
                    ("<" if endianness == "little" else ">") + "3hHh",
                    int(round(t.x * 1000)),
                    int(round(t.y * 1000)),
                    int(round(t.z * 1000)),
                    int(t.snr) & 0xFFFF,
                    int(round(t.speed * 10)),
                )
    return bytes(out)
