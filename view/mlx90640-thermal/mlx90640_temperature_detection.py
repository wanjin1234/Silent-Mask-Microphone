#!/usr/bin/env python3
"""MLX90640 32x24 红外阵列温度检测（Python 3.11.4）。

运行示例：
    python mlx90640_temperature_detection.py --threshold 50 --min-pixels 4

程序每帧输出一行 JSON，其中包含全视场温度统计值、最高温坐标和告警状态。
当相邻热像素组成的区域在连续若干帧中都满足条件时，detected 才为 true，
以降低单像素噪声、偶发读帧错误造成的误告警。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

WIDTH = 32
HEIGHT = 24
PIXEL_COUNT = WIDTH * HEIGHT
SENSOR_ADDRESS = 0x33


@dataclass(frozen=True)
class Roi:
    """感兴趣区域，坐标原点为帧数组左上角，单位为像素。"""

    x: int = 0
    y: int = 0
    width: int = WIDTH
    height: int = HEIGHT

    def validate(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("ROI 的宽和高必须大于 0")
        if self.x < 0 or self.y < 0 or self.x + self.width > WIDTH or self.y + self.height > HEIGHT:
            raise ValueError("ROI 必须完全位于 32x24 画面内")

    def pixels(self, frame: Sequence[float]) -> Iterable[tuple[int, int, float]]:
        for y in range(self.y, self.y + self.height):
            row_start = y * WIDTH
            for x in range(self.x, self.x + self.width):
                yield x, y, frame[row_start + x]


@dataclass(frozen=True)
class DetectorConfig:
    high_threshold_c: float
    min_hot_pixels: int
    consecutive_frames: int
    roi: Roi
    association_distance_px: float = 4.0

    def validate(self) -> None:
        self.roi.validate()
        if not -40.0 <= self.high_threshold_c <= 300.0:
            raise ValueError("阈值必须在 MLX90640 的 -40 至 300 °C 目标测温范围内")
        if self.min_hot_pixels < 1:
            raise ValueError("min-hot-pixels 必须至少为 1")
        if self.consecutive_frames < 1:
            raise ValueError("consecutive-frames 必须至少为 1")
        if self.association_distance_px < 0:
            raise ValueError("association-distance 必须不小于 0")


@dataclass(frozen=True)
class FrameResult:
    timestamp: float
    minimum_c: float
    mean_c: float
    maximum_c: float
    peak_xy: tuple[int, int]
    hot_component_pixels: int
    hot_center_xy: tuple[float, float] | None
    hot_peak_c: float | None
    consecutive_hot_frames: int
    detected: bool

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "timestamp": round(self.timestamp, 3),
            "roi_min_c": round(self.minimum_c, 2),
            "roi_mean_c": round(self.mean_c, 2),
            "roi_max_c": round(self.maximum_c, 2),
            "peak_xy": {"x": self.peak_xy[0], "y": self.peak_xy[1]},
            "hot_component_pixels": self.hot_component_pixels,
            "consecutive_hot_frames": self.consecutive_hot_frames,
            "detected": self.detected,
        }
        if self.hot_center_xy is not None and self.hot_peak_c is not None:
            data["hotspot"] = {
                "center_x": round(self.hot_center_xy[0], 1),
                "center_y": round(self.hot_center_xy[1], 1),
                "peak_c": round(self.hot_peak_c, 2),
            }
        return data


class TemperatureDetector:
    """对已补偿的 768 个温度值执行 ROI 统计、连通域和去抖判定。"""

    def __init__(self, config: DetectorConfig) -> None:
        config.validate()
        self.config = config
        self._consecutive_hot_frames = 0
        self._previous_center: tuple[float, float] | None = None

    @staticmethod
    def _largest_hot_component(
        pixels: Sequence[tuple[int, int, float]], threshold_c: float
    ) -> list[tuple[int, int, float]]:
        """返回大于等于阈值的四连通热区中面积最大的一个。"""
        hot = {(x, y): value for x, y, value in pixels if value >= threshold_c}
        seen: set[tuple[int, int]] = set()
        largest: list[tuple[int, int, float]] = []

        for seed in hot:
            if seed in seen:
                continue
            stack = [seed]
            seen.add(seed)
            component: list[tuple[int, int, float]] = []
            while stack:
                x, y = stack.pop()
                component.append((x, y, hot[(x, y)]))
                for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if neighbor in hot and neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)
            if len(component) > len(largest):
                largest = component
        return largest

    def analyse(self, frame: Sequence[float]) -> FrameResult:
        if len(frame) != PIXEL_COUNT:
            raise ValueError(f"帧长度应为 {PIXEL_COUNT}，实际为 {len(frame)}")

        roi_pixels = list(self.config.roi.pixels(frame))
        temperatures = [temperature for _, _, temperature in roi_pixels]
        if not all(math.isfinite(temperature) for temperature in temperatures):
            raise ValueError("帧中含有非有限温度值")

        peak_x, peak_y, peak_c = max(roi_pixels, key=lambda item: item[2])
        component = self._largest_hot_component(roi_pixels, self.config.high_threshold_c)
        component_is_large_enough = len(component) >= self.config.min_hot_pixels

        center: tuple[float, float] | None = None
        component_peak_c: float | None = None
        if component:
            center = (
                statistics.fmean(x for x, _, _ in component),
                statistics.fmean(y for _, y, _ in component),
            )
            component_peak_c = max(value for _, _, value in component)

        # 只有同一附近热区持续出现才累计帧数，避免画面中不同物体交替触发告警。
        same_target = (
            center is not None
            and self._previous_center is not None
            and math.dist(center, self._previous_center) <= self.config.association_distance_px
        )
        if component_is_large_enough:
            self._consecutive_hot_frames = self._consecutive_hot_frames + 1 if same_target else 1
            self._previous_center = center
        else:
            self._consecutive_hot_frames = 0
            self._previous_center = None

        return FrameResult(
            timestamp=time.time(),
            minimum_c=min(temperatures),
            mean_c=statistics.fmean(temperatures),
            maximum_c=peak_c,
            peak_xy=(peak_x, peak_y),
            hot_component_pixels=len(component),
            hot_center_xy=center,
            hot_peak_c=component_peak_c,
            consecutive_hot_frames=self._consecutive_hot_frames,
            detected=self._consecutive_hot_frames >= self.config.consecutive_frames,
        )


def parse_roi(value: str) -> Roi:
    try:
        x, y, width, height = (int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ROI 格式应为 x,y,width,height，例如 8,4,16,12") from exc
    roi = Roi(x, y, width, height)
    try:
        roi.validate()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return roi


def refresh_rate_enum(rate_hz: float):
    """延迟导入硬件库，使 --help 和纯逻辑单元测试无需硬件环境。"""
    import adafruit_mlx90640

    rates = {
        0.5: adafruit_mlx90640.RefreshRate.REFRESH_0_5_HZ,
        1.0: adafruit_mlx90640.RefreshRate.REFRESH_1_HZ,
        2.0: adafruit_mlx90640.RefreshRate.REFRESH_2_HZ,
        4.0: adafruit_mlx90640.RefreshRate.REFRESH_4_HZ,
        8.0: adafruit_mlx90640.RefreshRate.REFRESH_8_HZ,
        16.0: adafruit_mlx90640.RefreshRate.REFRESH_16_HZ,
        32.0: adafruit_mlx90640.RefreshRate.REFRESH_32_HZ,
    }
    return rates[rate_hz]


class _SmbusI2C:
    """把 smbus2 封装成 adafruit_bus_device 所需的 busio 兼容接口。

    这是树莓派上读 MLX90640 的**首选**路径：Adafruit 库默认走 Blinka 的
    ``busio.I2C``，其在 Linux 上把块读转发给 SMBus 的 ``read_i2c_block_data``，
    单次 block read 有 32 字节上限；而 MLX90640 的 RAM 帧需一次性读 832 字
    (1664 字节)，被截断后帧数据几乎全 0，导致 ``_GetTa`` 中 ptat/ptatArt 同时
    为 0 而除零。smbus2 的 ``i2c_rdwr`` 走原始 I2C_RDWR ioctl，无此长度限制，
    并正确处理 repeated-start 读取，可完整读回 1664 字节。
    """

    def __init__(self, bus: int, frequency: int = 400_000) -> None:
        import smbus2

        self._smbus2 = smbus2
        self._bus = smbus2.SMBus(bus)
        self._frequency = frequency

    def try_lock(self) -> bool:
        return True

    def unlock(self) -> None:
        return None

    def writeto(self, address: int, buffer, *, start: int = 0, end=None) -> None:
        data = bytes(buffer[start:end])
        if not data:
            # I2CDevice 构造时的空写探测，直接跳过
            return
        self._bus.i2c_rdwr(self._smbus2.i2c_msg.write(address, data))

    def readfrom_into(self, address: int, buffer, *, start: int = 0, end=None) -> None:
        length = len(buffer) if end is None else end - start
        read_msg = self._smbus2.i2c_msg.read(address, length)
        self._bus.i2c_rdwr(read_msg)
        buffer[start : start + length] = read_msg.buf[:length]

    def writeto_then_readfrom(
        self,
        address: int,
        out_buffer,
        in_buffer,
        *,
        out_start: int = 0,
        out_end=None,
        in_start: int = 0,
        in_end=None,
    ) -> None:
        out_data = bytes(out_buffer[out_start:out_end])
        in_length = len(in_buffer) if in_end is None else in_end - in_start
        write_msg = self._smbus2.i2c_msg.write(address, out_data)
        read_msg = self._smbus2.i2c_msg.read(address, in_length)
        self._bus.i2c_rdwr(write_msg, read_msg)
        in_buffer[in_start : in_start + in_length] = read_msg.buf[:in_length]


def open_sensor(rate_hz: float):
    try:
        import adafruit_mlx90640
    except ImportError as exc:
        raise RuntimeError(
            "未安装硬件库。请在虚拟环境中执行：\n"
            "python -m pip install adafruit-blinka adafruit-circuitpython-mlx90640"
        ) from exc

    # 总线号：默认 I2C1；传感器接在其它总线（如 I2C4、i2c-gpio）时设：
    #   export MLX90640_I2C_BUS=4
    i2c_bus = int(os.getenv("MLX90640_I2C_BUS", "1"))
    # 默认用 smbus2 直连（绕开 Blinka 的 SMBus 32 字节限制）；
    # 设 MLX90640_USE_BLINKA=1 才回退到 Blinka 的 busio.I2C（不推荐）。
    use_blinka = os.getenv("MLX90640_USE_BLINKA", "0") == "1"
    if i2c_bus == 1 and use_blinka:
        import board
        import busio

        i2c = busio.I2C(board.SCL, board.SDA, frequency=400_000)
    else:
        i2c = _SmbusI2C(i2c_bus, frequency=400_000)

    sensor = adafruit_mlx90640.MLX90640(i2c, address=SENSOR_ADDRESS)
    sensor.refresh_rate = refresh_rate_enum(rate_hz)
    # 打印序列号：能读到合法序列号，说明 I2C 地址正确、EEPROM 读取正常，
    # 可据此区分「地址/接线错误」与「RAM 帧读取的时钟拉伸问题」。
    try:
        serial = sensor.serial_number
        logging.info("MLX90640 序列号：%s", [hex(value) for value in serial])
    except Exception as exc:  # 序列号读取失败不阻断，但记录便于诊断
        logging.warning("读取 MLX90640 序列号失败：%s", exc)
    return sensor


def read_frame(sensor, retries: int = 3) -> list[float]:
    frame = [0.0] * PIXEL_COUNT
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            sensor.getFrame(frame)
            # Adafruit 库在帧数据全 0 时会于 _GetTa 中除零；此处提前识别，
            # 转化为可重试的错误，而不是让 ZeroDivisionError 直接崩溃。
            if not any(value != 0.0 for value in frame):
                raise ValueError("帧数据全为 0（传感器未就绪或 I2C 时钟拉伸超时）")
            if not all(math.isfinite(value) for value in frame):
                raise ValueError("帧数据含非有限值")
            return frame
        except (OSError, RuntimeError, ValueError, ArithmeticError) as exc:
            last_error = exc
            logging.warning("读取第 %d/%d 帧失败：%s", attempt, retries, exc)
            time.sleep(0.05 * attempt)
    raise RuntimeError(f"连续 {retries} 次读取 MLX90640 失败：{last_error}") from last_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MLX90640 温度检测与高温告警")
    parser.add_argument("--threshold", type=float, default=50.0, help="高温阈值，单位 °C（默认：50）")
    parser.add_argument("--min-pixels", type=int, default=4, help="热区最小四连通像素数（默认：4）")
    parser.add_argument("--consecutive-frames", type=int, default=3, help="连续命中帧数（默认：3）")
    parser.add_argument("--roi", type=parse_roi, default=Roi(), help="检测区域 x,y,width,height（默认：全画面）")
    parser.add_argument(
        "--rate",
        type=float,
        choices=(0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0),
        default=4.0,
        help="帧率 Hz（默认：4）",
    )
    parser.add_argument("--period", type=float, default=0.25, help="输出周期秒数，至少应不小于 1/帧率（默认：0.25）")
    parser.add_argument("--once", action="store_true", help="仅读取、分析并输出一帧")
    parser.add_argument("--verbose", action="store_true", help="显示读帧重试日志")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.period <= 0:
        raise SystemExit("--period 必须大于 0")
    if args.period < 1 / args.rate:
        logging.warning("period 小于 1/帧率，实际会受传感器帧率限制")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s")

    detector = TemperatureDetector(
        DetectorConfig(
            high_threshold_c=args.threshold,
            min_hot_pixels=args.min_pixels,
            consecutive_frames=args.consecutive_frames,
            roi=args.roi,
        )
    )
    try:
        sensor = open_sensor(args.rate)
    except RuntimeError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2

    running = True

    def stop_handler(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    print(json.dumps({"status": "started", "address": "0x33", "rate_hz": args.rate}, ensure_ascii=False))
    while running:
        cycle_started = time.monotonic()
        try:
            result = detector.analyse(read_frame(sensor))
            print(json.dumps(result.as_dict(), ensure_ascii=False), flush=True)
        except RuntimeError as exc:
            print(json.dumps({"status": "read_error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        if args.once:
            break
        time.sleep(max(0.0, args.period - (time.monotonic() - cycle_started)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
