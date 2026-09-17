"""雷达数据源：真实串口链路与离线仿真链路。

两种数据源对外接口一致（``frames()`` 迭代器 + ``send()`` + ``status()``），
因此命令行、录制与 Web 上位机都无需关心数据到底来自真机、仿真还是回放文件。
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import Dict, Iterator, List, Optional

from . import protocol as P
from .serialport import BAUD_RATE, SerialError, open_port, resolve_device


class FrameSource:
    """数据源基类。"""

    kind = "base"
    device = ""

    def frames(self) -> Iterator[P.Frame]:  # pragma: no cover - 抽象方法
        raise NotImplementedError

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def status(self) -> dict:
        return {"kind": self.kind, "device": self.device, "connected": False}

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# 真实串口链路
# --------------------------------------------------------------------------


class RadarLink(FrameSource):
    """通过 USB 转串口连接真实雷达。

    * 自动重连：设备被拔掉或串口异常时按退避策略重试，重连成功后重新下发配置
      并恢复 ``scan start``，适合无人值守长期运行。
    * 配置下发：连接建立后按 ``profile`` 依次发送指令并短暂间隔，避免雷达固件
      来不及解析连续指令。
    """

    kind = "serial"

    def __init__(
        self,
        device: str = "auto",
        baudrate: int = BAUD_RATE,
        profile: Optional[List[str]] = None,
        read_size: int = 65536,
        read_timeout: float = 0.2,
        command_gap: float = 0.05,
        auto_start: bool = True,
        reconnect: bool = True,
        keepalive: float = 5.0,
    ) -> None:
        self.device = device
        self.baudrate = baudrate
        self.profile = list(profile or [])
        self.read_size = read_size
        self.read_timeout = read_timeout
        self.command_gap = command_gap
        self.auto_start = auto_start
        self.reconnect = reconnect
        # 保活：雷达固件有扫描超时，需周期性重发 scan start，否则跑一会自己停。
        # 实测 5s 间隔可长期稳定运行；设 0 关闭保活。
        self.keepalive = keepalive

        self.parser = P.RadarStreamParser()
        self._port = None
        self._lock = threading.Lock()
        self._closed = False
        self._connected = False
        self._device_path = ""
        self._last_error = ""
        self._reconnects = 0
        self._sent: List[str] = []
        self._rx_window = _RateMeter()
        self._started_at = time.time()
        self._keepalive_thread = None

    # -- 内部 -------------------------------------------------------------

    def _connect(self) -> None:
        path = resolve_device(self.device)
        self._device_path = path
        self._port = open_port(path, self.baudrate, self.read_timeout)
        self._connected = True
        self._last_error = ""
        self.parser = P.RadarStreamParser()
        self._apply_profile()
        self._start_keepalive()

    def _apply_profile(self) -> None:
        for line in self.profile:
            self.send(P.build_command(line))
        if self.auto_start:
            self.send(P.build_command("scan start"))

    def _start_keepalive(self) -> None:
        """独立后台线程周期重发 scan start，防雷达固件挂死。

        与读循环完全解耦，不受 yield/continue/异常分支影响，行为等价于
        已验证稳定的 keepalive_test.py（每 keepalive 秒无条件发一次）。
        """
        if self.keepalive <= 0:
            return
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            return

        def _loop() -> None:
            while not self._closed:
                time.sleep(self.keepalive)
                if self._closed:
                    break
                try:
                    self.send(P.build_command("scan start"))
                except Exception:
                    pass  # 端口暂不可用时忽略，下个周期再试

        self._keepalive_thread = threading.Thread(target=_loop, daemon=True)
        self._keepalive_thread.start()

    # -- 对外 -------------------------------------------------------------

    def send(self, data: bytes) -> None:
        text = data.decode("ascii", "replace").strip()
        with self._lock:
            port = self._port
        if port is None:
            raise SerialError("串口未连接，无法发送：%s" % text)
        port.write(data)
        self._sent.append(text)
        if len(self._sent) > 200:
            del self._sent[:100]

    def send_text(self, text: str) -> None:
        """发送一条文本指令（自动补 CRLF）。"""
        self.send(P.build_command(text))

    def frames(self) -> Iterator[P.Frame]:
        """不断产出帧；串口断开时自动重连，直到 :meth:`close` 被调用。"""
        backoff = 0.5
        while not self._closed:
            if self._port is None:
                try:
                    self._connect()
                    backoff = 0.5
                except SerialError as exc:
                    self._last_error = str(exc)
                    self._connected = False
                    if not self.reconnect:
                        raise
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue
            try:
                chunk = self._port.read(self.read_size)
            except SerialError as exc:
                self._last_error = str(exc)
                self._drop_port()
                if not self.reconnect:
                    raise
                continue
            except OSError as exc:
                self._last_error = "串口读取出错：%s" % exc
                self._drop_port()
                if not self.reconnect:
                    raise
                continue
            if chunk:
                self._rx_window.add(len(chunk))
                for frame in self.parser.feed(chunk):
                    yield frame
            elif self._port is None:
                # 读超时后检查设备是否还在
                continue

    def _drop_port(self) -> None:
        with self._lock:
            port, self._port = self._port, None
        if port is not None:
            port.close()
        self._connected = False
        self._reconnects += 1
        for frame in self.parser.flush():
            pass  # 断开时残留的半帧直接丢弃

    def status(self) -> dict:
        return {
            "kind": self.kind,
            "device": self.device_path,
            "device_requested": self.device,
            "baudrate": self.baudrate,
            "connected": self._connected and self._port is not None,
            "last_error": self._last_error,
            "reconnects": self._reconnects,
            "bytes_per_sec": round(self._rx_window.rate(), 1),
            "parse": self.parser.stats.as_dict(),
            "last_commands": self._sent[-8:],
            "uptime": round(time.time() - self._started_at, 1),
            "backend": _backend(),
        }

    @property
    def device_path(self) -> str:
        return self._device_path

    def close(self) -> None:
        self._closed = True
        with self._lock:
            port, self._port = self._port, None
        if port is not None:
            try:
                if self.auto_start:
                    port.write(P.build_command("scan stop"))
                    time.sleep(0.05)
            except Exception:
                pass
            port.close()
        self._connected = False


class _RateMeter:
    """滑动窗口速率统计。"""

    def __init__(self, window: float = 2.0) -> None:
        self.window = window
        self._events: List[tuple] = []

    def add(self, count: int) -> None:
        now = time.monotonic()
        self._events.append((now, count))
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.pop(0)

    def rate(self) -> float:
        now = time.monotonic()
        self._trim(now)
        if not self._events:
            return 0.0
        total = sum(c for _, c in self._events)
        span = max(now - self._events[0][0], 0.2)
        return total / span


def _backend() -> str:
    from .serialport import backend_name

    return backend_name()


# --------------------------------------------------------------------------
# 仿真链路（无硬件也能验证整套软件）
# --------------------------------------------------------------------------


class SimulatedLink(FrameSource):
    """生成一段室内场景的仿真点云。

    场景：房间 6m(X) × 5m(Y) × 3m(Z)，雷达装在后墙中央、离地 1.7m；
    另有一个沿椭圆轨迹行走的人，以及少量随机杂点。用于在没有雷达的情况下
    验证解析、显示、录制、回放等全部链路。
    """

    kind = "simulator"

    def __init__(self, fps: float = 10.0, room: tuple = (3.0, 5.0, 3.0), seed: int = 20240913) -> None:
        self.device = "simulator"
        self.fps = fps
        self.room_x, self.room_y, self.room_z = room
        self.rng = random.Random(seed)
        self._frame_id = 0
        self._points_total = 0
        self._running = True
        self._closed = False
        self._started_at = time.time()
        self._sent: List[str] = []
        self._person_phase = 0.0

    # -- 指令：仿真下只记录，不真正下发 --------------------------------

    def send(self, data: bytes) -> None:
        text = data.decode("ascii", "replace").strip()
        self._sent.append(text)
        if self._sent[-1:][0] == "scan start":
            self._running = True
        elif self._sent[-1:][0] == "scan stop":
            self._running = False
        if len(self._sent) > 200:
            del self._sent[:100]

    def send_text(self, text: str) -> None:
        self.send(P.build_command(text))

    # -- 数据生成 ---------------------------------------------------------

    def _wall_points(self) -> List[P.Point]:
        pts: List[P.Point] = []
        rng = self.rng
        # 后墙 y = room_y
        for _ in range(90):
            pts.append(
                P.Point(
                    rng.uniform(-self.room_x, self.room_x),
                    self.room_y + rng.gauss(0, 0.02),
                    rng.uniform(0, self.room_z),
                    rng.randint(300, 1200),
                    0.0,
                    P.GROUP_DYNAMIC_LOW,
                )
            )
        # 左右墙
        for sign in (-1, 1):
            for _ in range(25):
                pts.append(
                    P.Point(
                        sign * self.room_x + rng.gauss(0, 0.02),
                        rng.uniform(0.4, self.room_y),
                        rng.uniform(0, self.room_z),
                        rng.randint(200, 900),
                        0.0,
                        P.GROUP_DYNAMIC_LOW,
                    )
                )
        # 地面
        for _ in range(40):
            pts.append(
                P.Point(
                    rng.uniform(-self.room_x, self.room_x),
                    rng.uniform(0.4, self.room_y),
                    rng.gauss(0, 0.02),
                    rng.randint(150, 600),
                    0.0,
                    P.GROUP_DYNAMIC_LOW,
                )
            )
        return pts

    def _person_points(self, dt: float) -> List[P.Point]:
        # 椭圆轨迹行走
        self._person_phase += dt * 0.55
        cx = 1.1 * math.cos(self._person_phase)
        cy = 2.2 + 0.7 * math.sin(self._person_phase)
        vx = -1.1 * math.sin(self._person_phase) * 0.55
        vy = 0.7 * math.cos(self._person_phase) * 0.55
        speed = math.hypot(vx, vy)

        pts: List[P.Point] = []
        rng = self.rng
        for _ in range(70):
            ang = rng.uniform(0, 2 * math.pi)
            r = 0.28 * math.sqrt(rng.random())
            h = rng.uniform(0.05, 1.75)
            pts.append(
                P.Point(
                    cx + r * math.cos(ang) + rng.gauss(0, 0.01),
                    cy + r * math.sin(ang) + rng.gauss(0, 0.01),
                    h,
                    rng.randint(900, 3000),
                    speed * (1.0 if rng.random() > 0.15 else -1.0),
                    P.GROUP_DYNAMIC_HIGH,
                )
            )
        # 少量静止微动点（呼吸/桌面振动之类）
        for _ in range(12):
            pts.append(
                P.Point(
                    rng.uniform(-2.0, 2.0),
                    rng.uniform(0.5, self.room_y),
                    rng.uniform(0.6, 1.1),
                    rng.randint(200, 700),
                    0.0,
                    rng.choice((P.GROUP_LONG_MICRO_HIGH, P.GROUP_SHORT_MICRO_LOW)),
                )
            )
        return pts

    def _make_frame(self, dt: float) -> P.Frame:
        points = self._wall_points() + self._person_points(dt)
        counts = [0] * 6
        for p in points:
            counts[p.group] += 1
        header = P.FrameHeader(
            frame_period=int(1000.0 / max(self.fps, 1)),
            frame_id=self._frame_id,
            bb_time=self.rng.randint(3, 9),
            post_bb_time=self.rng.randint(1, 5),
            transfer_time=self.rng.randint(1, 4),
            frame_interval=self.rng.randint(8, 14),
            counts=tuple(counts),
            track_count=0,
        )
        return P.Frame(header=header, points=points, tracks=[], timestamp=time.time())

    def frames(self) -> Iterator[P.Frame]:
        period = 1.0 / max(self.fps, 1.0)
        next_at = time.monotonic()
        while not self._closed:
            now = time.monotonic()
            if now < next_at:
                time.sleep(min(period, next_at - now))
                continue
            next_at = max(next_at + period, now)
            if not self._running:
                continue
            self._frame_id = (self._frame_id + 1) & 0xFFFFFFFF
            frame = self._make_frame(period)
            self._points_total += len(frame.points)
            yield frame

    def status(self) -> dict:
        return {
            "kind": self.kind,
            "device": "simulator",
            "connected": True,
            "fps_target": self.fps,
            "scanning": self._running,
            "parse": {
                "frames": self._frame_id,
                "points": self._points_total,
                "resyncs": 0,
                "dropped_bytes": 0,
                "bad_tails": 0,
                "bad_lengths": 0,
                "incomplete_frames": 0,
                "endianness": "n/a",
                "bytes_in": 0,
                "packets": self._frame_id,
            },
            "last_commands": self._sent[-8:],
            "uptime": round(time.time() - self._started_at, 1),
            "bytes_per_sec": 0.0,
            "backend": "simulator",
        }

    def close(self) -> None:
        self._closed = True
