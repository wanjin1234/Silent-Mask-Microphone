"""点云录制与回放。

* CSV：一行一个点，便于用 Excel / pandas 做二次分析；
* JSONL：一行一帧（含帧头信息），无损、回放最快，也是 Web 回放的首选格式。
"""

from __future__ import annotations

import csv
import glob
import json
import os
import time
from datetime import datetime
from typing import Dict, Iterator, List, Optional

from . import protocol as P

CSV_HEADER = [
    "timestamp",
    "unix_time",
    "frame_id",
    "group",
    "group_name",
    "x_m",
    "y_m",
    "z_m",
    "snr",
    "speed_mps",
]

DEFAULT_MAX_BYTES = 256 * 1024 * 1024


def default_filename(directory: str = ".", fmt: str = "csv", prefix: str = "radar", when: Optional[datetime] = None) -> str:
    """生成带时间戳的默认文件名。"""
    when = when or datetime.now()
    name = "%s_%s.%s" % (prefix, when.strftime("%Y%m%d_%H%M%S"), fmt)
    return os.path.join(directory, name)


class Recorder:
    """把帧写入 CSV 或 JSONL 文件。"""

    def __init__(
        self,
        path: str,
        fmt: str = "csv",
        max_bytes: int = DEFAULT_MAX_BYTES,
        rotate: bool = True,
    ) -> None:
        self.path = path
        self.fmt = fmt.lower().lstrip(".")
        if self.fmt not in ("csv", "jsonl", "ndjson"):
            raise ValueError("不支持的录制格式：%s（可选 csv / jsonl）" % fmt)
        self.max_bytes = max_bytes
        self.rotate = rotate
        self.frames = 0
        self.points = 0
        self.bytes_written = 0
        self.started_at = time.time()
        self._fh = None
        self._writer = None
        self._open(self.path)

    # -- 文件管理 ---------------------------------------------------------

    def _open(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fh = open(path, "w", encoding="utf-8", newline="")
        if self.fmt == "csv":
            self._writer = csv.writer(self._fh)
            self._writer.writerow(CSV_HEADER)

    def _rotate(self) -> None:
        base, ext = os.path.splitext(self.path)
        index = 1
        while os.path.exists("%s_%03d%s" % (base, index, ext)):
            index += 1
        self._close_handle()
        self.path = "%s_%03d%s" % (base, index, ext)
        self._open(self.path)
        self.bytes_written = 0

    def _close_handle(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            self._writer = None

    # -- 写入 -------------------------------------------------------------

    def write(self, frame: P.Frame) -> None:
        if self._fh is None:
            return
        if self.fmt == "csv":
            self._write_csv(frame)
        else:
            self._write_jsonl(frame)
        self.frames += 1
        self.points += len(frame.points)
        if self.rotate and self._fh is not None and self._fh.tell() > self.max_bytes:
            self._rotate()

    def _write_csv(self, frame: P.Frame) -> None:
        ts = datetime.fromtimestamp(frame.timestamp).isoformat(timespec="milliseconds")
        fid = frame.frame_id
        rows = [
            (ts, "%.3f" % frame.timestamp, fid, p.group, P.GROUP_NAMES[p.group],
             "%.3f" % p.x, "%.3f" % p.y, "%.3f" % p.z, p.snr, "%.2f" % p.speed)
            for p in frame.points
        ]
        for t in frame.tracks:
            rows.append((ts, "%.3f" % frame.timestamp, fid, -1, "航迹", "%.3f" % t.x,
                         "%.3f" % t.y, "%.3f" % t.z, t.snr, "%.2f" % t.speed))
        self._writer.writerows(rows)

    def _write_jsonl(self, frame: P.Frame) -> None:
        self._fh.write(json.dumps(frame.as_dict(with_tracks=True), ensure_ascii=False))
        self._fh.write("\n")

    def close(self) -> Dict[str, object]:
        self._close_handle()
        return {
            "path": self.path,
            "format": self.fmt,
            "frames": self.frames,
            "points": self.points,
            "seconds": round(time.time() - self.started_at, 1),
        }

    def __enter__(self) -> "Recorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# 回放
# --------------------------------------------------------------------------


class ReplaySource:
    """把录制文件当作数据源回放，接口与 :class:`radarpi.link.FrameSource` 一致。"""

    kind = "replay"

    def __init__(self, path: str, loop: bool = False, speed: float = 1.0, fps_limit: float = 0.0) -> None:
        matches = sorted(glob.glob(path))
        if not matches:
            raise FileNotFoundError("找不到回放文件：%s" % path)
        self.path = matches[0]
        self.paths = matches
        self.loop = loop
        self.speed = max(speed, 0.01)
        self.fps_limit = fps_limit
        self.device = os.path.basename(self.path)
        self.format = "jsonl" if self.path.lower().endswith((".jsonl", ".ndjson")) else "csv"
        self._closed = False
        self._started_at = time.time()
        self._played = 0

    # -- 命令行兼容 -------------------------------------------------------

    def send(self, data: bytes) -> None:
        # 回放时指令仅记录（可通过 status().last_commands 查看）
        self._last_command = data.decode("ascii", "replace").strip()

    def send_text(self, text: str) -> None:
        self.send(P.build_command(text))

    def status(self) -> dict:
        return {
            "kind": "replay",
            "device": self.device,
            "connected": not self._closed,
            "file": self.path,
            "format": self.format,
            "frames_played": self._played,
            "loop": self.loop,
            "speed": self.speed,
            "last_commands": [getattr(self, "_last_command", "")][-1:],
            "uptime": round(time.time() - self._started_at, 1),
            "bytes_per_sec": 0.0,
            "backend": "replay",
        }

    def close(self) -> None:
        self._closed = True

    # -- 数据 -------------------------------------------------------------

    def frames(self) -> Iterator[P.Frame]:
        while not self._closed:
            for frame in self._iter_once():
                self._played += 1
                yield frame
            if not self.loop:
                break
            time.sleep(0.2)

    def _iter_once(self) -> Iterator[P.Frame]:
        if self.format == "jsonl":
            yield from self._iter_jsonl()
        else:
            yield from self._iter_csv()

    def _iter_jsonl(self) -> Iterator[P.Frame]:
        last_wall: Optional[float] = None
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if self._closed:
                    return
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                frame = frame_from_dict(obj)
                wall = frame.timestamp
                if last_wall is not None and self.speed > 0:
                    delay = (wall - last_wall) / self.speed
                    if self.fps_limit > 0:
                        delay = max(delay, 1.0 / self.fps_limit)
                    if 0 < delay < 2.0:
                        time.sleep(delay)
                last_wall = wall
                yield frame

    def _iter_csv(self) -> Iterator[P.Frame]:
        current: Optional[P.Frame] = None
        current_key = None
        with open(self.path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                if self._closed:
                    return
                try:
                    key = (row["frame_id"], row["unix_time"])
                except KeyError:
                    continue
                if current is None or key != current_key:
                    if current is not None:
                        yield current
                    current = P.Frame(header=P.FrameHeader(frame_id=_safe_int(row.get("frame_id"))), timestamp=float(row.get("unix_time") or 0.0))
                    current_key = key
                group = _safe_int(row.get("group"), P.GROUP_DYNAMIC_HIGH)
                point = P.Point(
                    float(row.get("x_m") or 0.0),
                    float(row.get("y_m") or 0.0),
                    float(row.get("z_m") or 0.0),
                    _safe_int(row.get("snr")),
                    float(row.get("speed_mps") or 0.0),
                    group if group >= 0 else P.GROUP_DYNAMIC_HIGH,
                )
                if group < 0:
                    current.tracks.append(P.Track(point.x, point.y, point.z, point.snr, point.speed, len(current.tracks) + 1))
                else:
                    current.points.append(point)
        if current is not None:
            yield current


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def frame_from_dict(obj: dict) -> P.Frame:
    """把 :meth:`protocol.Frame.as_dict` 的输出还原成 Frame。"""
    hdr = obj.get("header", {})
    counts = hdr.get("counts", {}) or {}
    header = P.FrameHeader(
        frame_period=_safe_int(hdr.get("frame_period")),
        frame_id=_safe_int(hdr.get("frame_id")),
        bb_time=_safe_int(hdr.get("bb_time")),
        post_bb_time=_safe_int(hdr.get("post_bb_time")),
        transfer_time=_safe_int(hdr.get("transfer_time")),
        frame_interval=_safe_int(hdr.get("frame_interval")),
        counts=tuple(_safe_int(counts.get(k)) for k in P.GROUP_KEYS),
        track_count=_safe_int(counts.get("tracks")),
    )
    points = [
        P.Point(
            float(p.get("x", 0.0)),
            float(p.get("y", 0.0)),
            float(p.get("z", 0.0)),
            _safe_int(p.get("snr")),
            float(p.get("v", 0.0)),
            _safe_int(p.get("g")),
            i + 1,
        )
        for i, p in enumerate(obj.get("points", []))
    ]
    tracks = [
        P.Track(
            float(t.get("x", 0.0)),
            float(t.get("y", 0.0)),
            float(t.get("z", 0.0)),
            _safe_int(t.get("snr")),
            float(t.get("v", 0.0)),
            _safe_int(t.get("id"), i + 1),
        )
        for i, t in enumerate(obj.get("tracks", []))
    ]
    return P.Frame(header=header, points=points, tracks=tracks, timestamp=float(obj.get("ts") or time.time()))
