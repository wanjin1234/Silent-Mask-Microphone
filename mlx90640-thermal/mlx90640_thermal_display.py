#!/usr/bin/env python3
"""树莓派 4B 上的 MLX90640 (32 x 24) 彩色热成像显示。

依赖：pygame、adafruit-blinka、adafruit-circuitpython-mlx90640。

真实传感器（I2C1，2 Hz）:
    python3.11 mlx90640_thermal_display.py --rate 2

无传感器预览：
    python3.11 mlx90640_thermal_display.py --simulate

本程序读取每帧 768 个摄氏温度值，并以 32 x 24 个独立色块显示；它不做
插值，因此每个色块都对应一个真实的 MLX90640 像元。
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path
from typing import Sequence

try:
    import pygame
except ImportError:
    pygame = None


WIDTH = 32
HEIGHT = 24
PIXEL_COUNT = WIDTH * HEIGHT
VALID_RATES = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
RATE_NAMES = {
    0.5: "REFRESH_0_5_HZ",
    1.0: "REFRESH_1_HZ",
    2.0: "REFRESH_2_HZ",
    4.0: "REFRESH_4_HZ",
    8.0: "REFRESH_8_HZ",
    16.0: "REFRESH_16_HZ",
    32.0: "REFRESH_32_HZ",
}


def clamp(value: float, low: float, high: float) -> float:
    """Return *value* constrained to the inclusive interval [low, high]."""
    return low if value < low else high if value > high else value


def temperature_to_rgb(value: float, min_t: float, max_t: float) -> tuple[int, int, int]:
    """Map a temperature in Celsius to the reference project's cold-to-hot RGB scale.

    The colour break points are copied from the supplied STM32 demonstration:
    blue -> cyan -> green -> yellow -> red -> magenta.  Values outside the
    chosen scale are clipped instead of wrapping around.
    """
    if not math.isfinite(value) or max_t <= min_t:
        return (128, 128, 128)

    a = min_t + (max_t - min_t) * 0.2121
    b = min_t + (max_t - min_t) * 0.3182
    c = min_t + (max_t - min_t) * 0.4242
    d = min_t + (max_t - min_t) * 0.8182

    red = clamp(255.0 * (value - b) / (c - b), 0.0, 255.0)
    if value < a:
        green = clamp(255.0 * (value - min_t) / (a - min_t), 0.0, 255.0)
    elif value <= c:
        green = 255.0
    else:
        green = clamp(255.0 * (value - d) / (c - d), 0.0, 255.0)

    if value <= b:
        blue = clamp(255.0 * (value - b) / (a - b), 0.0, 255.0)
    elif value <= d:
        blue = 0.0
    else:
        blue = clamp(240.0 * (value - d) / (max_t - d), 0.0, 240.0)

    return (round(red), round(green), round(blue))


def build_lut(min_t: float, max_t: float, levels: int = 256) -> list[tuple[int, int, int]]:
    """Build an RGB look-up table for one colour scale."""
    if levels < 2:
        raise ValueError("levels must be at least 2")
    return [
        temperature_to_rgb(min_t + (max_t - min_t) * index / (levels - 1), min_t, max_t)
        for index in range(levels)
    ]


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linearly interpolated percentile without requiring NumPy."""
    if not values:
        raise ValueError("cannot calculate a percentile of an empty sequence")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    low_index = int(position)
    high_index = min(low_index + 1, len(ordered) - 1)
    part = position - low_index
    return ordered[low_index] * (1.0 - part) + ordered[high_index] * part


class TemperatureScale:
    """A fixed scale or a stable, automatically calculated colour scale."""

    def __init__(
        self,
        fixed_min: float | None,
        fixed_max: float | None,
        minimum_span: float = 5.0,
        smoothing: float = 0.25,
    ) -> None:
        if (fixed_min is None) != (fixed_max is None):
            raise ValueError("--min and --max must be used together")
        if fixed_min is not None and fixed_max is not None and fixed_max <= fixed_min:
            raise ValueError("--max must be greater than --min")
        self.fixed_min = fixed_min
        self.fixed_max = fixed_max
        self.minimum_span = minimum_span
        self.smoothing = smoothing
        self._low: float | None = None
        self._high: float | None = None

    def update(self, temperatures: Sequence[float]) -> tuple[float, float]:
        if self.fixed_min is not None and self.fixed_max is not None:
            return self.fixed_min, self.fixed_max

        # The 5th/95th percentiles stop one bad pixel from making the scene
        # almost monochrome.  Clipping at the ends deliberately shows those
        # extreme pixels as saturated blue/red.
        low = percentile(temperatures, 0.05)
        high = percentile(temperatures, 0.95)
        midpoint = (low + high) / 2.0
        low = min(low, midpoint - self.minimum_span / 2.0)
        high = max(high, midpoint + self.minimum_span / 2.0)

        if self._low is None or self._high is None:
            self._low, self._high = low, high
        else:
            self._low += self.smoothing * (low - self._low)
            self._high += self.smoothing * (high - self._high)
        return self._low, self._high


class _SmbusI2C:
    """把 smbus2 封装成 adafruit_bus_device 所需的 busio 兼容传输层。

    这是树莓派 4B 上读 MLX90640 的**首选**路径。Adafruit 库默认走 Blinka 的
    ``busio.I2C``，其在 Linux 上把块读转发给 SMBus，单次 block read 有 32 字节
    上限；而 MLX90640 的 RAM 帧需一次性读 832 字 (1664 字节)，被截断后帧数据
    几乎全 0，``getFrame`` 内部会因 ptat/ptatArt 同时为 0 而除零。smbus2 的
    ``i2c_rdwr`` 走原始 I2C_RDWR ioctl，无此长度限制，并正确处理 repeated-start
    读取，可完整读回 1664 字节。

    若硬件 I2C 控制器仍因时钟拉伸超时读不到 RAM（症状：EEPROM/序列号正常但
    RAM 全 0），请改用软件 I2C（见 README 的 i2c-gpio 配置），并把
    ``MLX90640_I2C_BUS`` 设为对应总线号。
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

    def deinit(self) -> None:
        try:
            self._bus.close()
        except Exception:
            pass

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

    def read_words(self, address: int, register: int, count: int) -> list[int]:
        """从寄存器连续读 count 个字（16bit，repeated-start 读取）。"""
        cmd = bytes([(register >> 8) & 0xFF, register & 0xFF])
        write_msg = self._smbus2.i2c_msg.write(address, cmd)
        read_msg = self._smbus2.i2c_msg.read(address, count * 2)
        self._bus.i2c_rdwr(write_msg, read_msg)
        raw = bytes(read_msg.buf[: count * 2])
        return [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]

    def write_word(self, address: int, register: int, value: int) -> None:
        cmd = bytes([(register >> 8) & 0xFF, register & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
        self._bus.i2c_rdwr(self._smbus2.i2c_msg.write(address, cmd))


class Mlx90640Sensor:
    """Read a calibrated 768-value Celsius frame through Raspberry Pi I2C."""

    def __init__(self, rate_hz: float, i2c_frequency: int, address: int) -> None:
        try:
            import adafruit_mlx90640
        except ImportError as exc:
            raise RuntimeError(
                "缺少硬件驱动。请先执行：python3.11 -m pip install -r requirements.txt"
            ) from exc

        self._address = address
        self._i2c = self._build_i2c(i2c_frequency)
        try:
            self._mlx = adafruit_mlx90640.MLX90640(self._i2c, address=address)
            refresh_name = RATE_NAMES[rate_hz]
            self._mlx.refresh_rate = getattr(adafruit_mlx90640.RefreshRate, refresh_name)
            self.serial_number = tuple(self._mlx.serial_number)
        except Exception as exc:
            deinit = getattr(self._i2c, "deinit", None)
            if callable(deinit):
                deinit()
            raise RuntimeError(
                f"无法打开 MLX90640 (I2C 7 位地址 0x{address:02X})：{exc}"
            ) from exc

    @staticmethod
    def _build_i2c(i2c_frequency: int):
        """选择 I2C 传输层：默认 smbus2（Pi 4B 稳健），可用环境变量切换。

        - ``MLX90640_I2C_BUS``：总线号，默认 ``1``（硬件 I2C1）；软件 I2C 时为
          对应 i2c-gpio 总线号（如 ``3``）。
        - ``MLX90640_USE_BLINKA=1``：强制走 Blinka ``busio.I2C``（不推荐）。
        """
        use_blinka = os.getenv("MLX90640_USE_BLINKA", "0") == "1"
        if use_blinka:
            import board
            import busio

            return busio.I2C(board.SCL, board.SDA, frequency=i2c_frequency)

        i2c_bus = int(os.getenv("MLX90640_I2C_BUS", "1"))
        return _SmbusI2C(i2c_bus, frequency=i2c_frequency)

    def read_frame(self) -> list[float]:
        # 树莓派 4B 上 Adafruit 的 getFrame 内部 `while dataReady == 0` 无超时无
        # sleep，可能死循环卡死。这里改用已验证可用的 smbus2 直连读两个子页，
        # 再复用 Adafruit 的 _GetTa/_CalculateTo 做温度换算（纯数学，不碰 I2C）。
        if isinstance(self._i2c, _SmbusI2C):
            return self._read_frame_smbus()

        frame: list[float] = [0.0] * PIXEL_COUNT
        self._mlx.getFrame(frame)
        if not all(math.isfinite(value) for value in frame):
            raise ValueError("驱动返回了非有限温度值")
        return frame

    def _read_frame_smbus(self) -> list[float]:
        result: list[float] = [0.0] * PIXEL_COUNT
        frame_data: list[int] = [0] * 834
        emissivity = 0.95
        tr = 23.15
        for _ in range(2):
            self._read_subpage(frame_data)
            tr = self._mlx._GetTa(frame_data) - 8.0  # OPENAIR_TA_SHIFT
            self._mlx._CalculateTo(frame_data, emissivity, tr, result)
        if not all(math.isfinite(value) for value in result):
            raise ValueError("驱动返回了非有限温度值")
        return result

    def _read_subpage(self, frame_data: list[int]) -> int:
        bus = self._i2c
        addr = self._address

        # 1) 等待 dataReady（bit3），带超时，避免死循环
        deadline = time.monotonic() + 2.0
        status = 0
        while True:
            status = bus.read_words(addr, 0x8000, 1)[0]
            if status & 0x0008:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"等待 MLX90640 dataReady 超时（status=0x{status:04X}）")
            time.sleep(0.01)

        # 2) 触发读帧并清除 dataReady
        bus.write_word(addr, 0x8000, 0x0030)

        # 3) 读 RAM 0x0400（832 字 = 1664 字节）
        ram = bus.read_words(addr, 0x0400, 832)
        frame_data[0:832] = ram

        # 4) 再读状态寄存器，取子页位
        status2 = bus.read_words(addr, 0x8000, 1)[0]

        # 5) 读控制寄存器 0x800D（分辨率等字段，供 _GetVdd 使用）
        control = bus.read_words(addr, 0x800D, 1)[0]
        frame_data[832] = control
        frame_data[833] = status2 & 0x0001
        return frame_data[833]

    def close(self) -> None:
        deinit = getattr(self._i2c, "deinit", None)
        if callable(deinit):
            deinit()


def synthetic_frame(elapsed: float) -> list[float]:
    """Generate a moving 20-45 C test scene for desktop verification."""
    center_x = 16.0 + 11.0 * math.sin(elapsed * 0.7)
    center_y = 12.0 + 6.0 * math.cos(elapsed * 0.4)
    frame: list[float] = []
    for y in range(HEIGHT):
        for x in range(WIDTH):
            base = 20.0 + 7.0 * x / (WIDTH - 1)
            hotspot = 20.0 * math.exp(-((x - center_x) ** 2 / 18.0 + (y - center_y) ** 2 / 12.0))
            ripple = 0.35 * math.sin(x * 0.6 + elapsed) * math.cos(y * 0.4 - elapsed)
            frame.append(base + hotspot + ripple)
    return frame


def center_temperature(temperatures: Sequence[float]) -> float:
    """Average the central 2 x 2 sensor pixels, matching the reference firmware."""
    x0, y0 = WIDTH // 2 - 1, HEIGHT // 2 - 1
    return sum(temperatures[y * WIDTH + x] for y in (y0, y0 + 1) for x in (x0, x0 + 1)) / 4.0


class ThermalDisplay:
    """Pygame renderer that preserves the MLX90640's 32 x 24 pixel grid."""

    def __init__(self, cell: int, fullscreen: bool, flip_h: bool, flip_v: bool, show_legend: bool) -> None:
        if pygame is None:
            raise RuntimeError("缺少 pygame。请先执行：python3.11 -m pip install -r requirements.txt")
        self.cell = max(4, cell)
        self.flip_h = flip_h
        self.flip_v = flip_v
        self.show_legend = show_legend
        self.grid_width = WIDTH * self.cell
        self.grid_height = HEIGHT * self.cell
        self.info_height = max(62, self.cell * 4) if show_legend else max(24, self.cell * 2)

        pygame.init()
        flags = pygame.SCALED
        if fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((self.grid_width, self.grid_height + self.info_height), flags)
        pygame.display.set_caption("MLX90640 32x24 Thermal Display")
        self.font = pygame.font.Font(None, max(17, self.cell))
        self.small_font = pygame.font.Font(None, max(14, int(self.cell * 0.78)))

    def poll_events(self) -> bool:
        """Return False when the user asks to close the display."""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                return False
        return True

    def render(self, temperatures: Sequence[float], low: float, high: float) -> None:
        if len(temperatures) != PIXEL_COUNT:
            raise ValueError(f"frame must contain {PIXEL_COUNT} values")
        if not all(math.isfinite(value) for value in temperatures):
            raise ValueError("frame contains a non-finite temperature")

        lut = build_lut(low, high)
        span = high - low
        self.screen.fill((0, 0, 0))
        gap = 1 if self.cell >= 7 else 0
        square = self.cell - gap
        for source_y in range(HEIGHT):
            target_y = HEIGHT - 1 - source_y if self.flip_v else source_y
            for source_x in range(WIDTH):
                target_x = WIDTH - 1 - source_x if self.flip_h else source_x
                value = temperatures[source_y * WIDTH + source_x]
                color_index = int(clamp((value - low) * 255.0 / span, 0.0, 255.0))
                pygame.draw.rect(
                    self.screen,
                    lut[color_index],
                    (target_x * self.cell, target_y * self.cell, square, square),
                )

        # Mark the four pixels used for the reported centre measurement.
        center_x = WIDTH // 2 - 1
        center_y = HEIGHT // 2 - 1
        if self.flip_h:
            center_x = WIDTH - 2 - center_x
        if self.flip_v:
            center_y = HEIGHT - 2 - center_y
        pygame.draw.rect(
            self.screen,
            (255, 255, 255),
            (center_x * self.cell, center_y * self.cell, self.cell * 2 - gap, self.cell * 2 - gap),
            1,
        )

        if self.show_legend:
            self._draw_legend(low, high, lut)
        self._draw_readings(temperatures, low, high)
        pygame.display.flip()

    def _draw_legend(self, low: float, high: float, lut: Sequence[tuple[int, int, int]]) -> None:
        bar_top = self.grid_height + 5
        bar_height = max(10, self.cell - 4)
        for index, color in enumerate(lut):
            x0 = index * self.grid_width // len(lut)
            x1 = (index + 1) * self.grid_width // len(lut)
            pygame.draw.rect(self.screen, color, (x0, bar_top, max(1, x1 - x0), bar_height))
        low_surface = self.small_font.render(f"{low:.1f} C", True, (235, 235, 235))
        high_surface = self.small_font.render(f"{high:.1f} C", True, (235, 235, 235))
        label_top = bar_top + bar_height + 2
        self.screen.blit(low_surface, (2, label_top))
        self.screen.blit(high_surface, (self.grid_width - high_surface.get_width() - 2, label_top))

    def _draw_readings(self, temperatures: Sequence[float], low: float, high: float) -> None:
        center = center_temperature(temperatures)
        maximum = max(temperatures)
        text = f"Center {center:.1f} C   Peak {maximum:.1f} C"
        if not self.show_legend:
            text += f"   Scale {low:.1f}..{high:.1f} C"
        surface = self.font.render(text, True, (255, 255, 255))
        self.screen.blit(surface, (4, self.grid_height + self.info_height - surface.get_height() - 3))

    def save_screenshot(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(self.screen, str(filename))

    def close(self) -> None:
        if pygame is not None:
            pygame.quit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MLX90640 32x24 彩色温度点阵显示（树莓派 4B）")
    parser.add_argument("--rate", type=float, choices=VALID_RATES, default=2.0, help="传感器帧率 Hz，默认 2")
    parser.add_argument("--i2c-frequency", type=int, default=800_000, help="传给 I2C 驱动的频率 Hz，默认 800000")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x33, help="I2C 7 位地址，默认 0x33")
    parser.add_argument("--cell", type=int, default=16, help="每个传感器像元的显示边长，默认 16")
    parser.add_argument("--min", dest="fixed_min", type=float, help="固定色阶下限 Celsius；须与 --max 一起使用")
    parser.add_argument("--max", dest="fixed_max", type=float, help="固定色阶上限 Celsius；须与 --min 一起使用")
    parser.add_argument("--flip-h", action="store_true", help="水平镜像显示（传感器倒装时使用）")
    parser.add_argument("--flip-v", action="store_true", help="垂直镜像显示（传感器倒装时使用）")
    parser.add_argument("--fullscreen", action="store_true", help="全屏显示")
    parser.add_argument("--no-legend", action="store_true", help="隐藏色阶条")
    parser.add_argument("--simulate", action="store_true", help="使用模拟温度场，无需 MLX90640")
    parser.add_argument("--headless", action="store_true", help="不创建真实窗口；与 --screenshot 一起用于无图形环境")
    parser.add_argument("--screenshot", type=Path, help="保存一帧 PNG 后退出")
    parser.add_argument("--verbose", action="store_true", help="显示帧读取重试信息")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.headless:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    if args.i2c_frequency <= 0:
        print("--i2c-frequency must be positive", file=sys.stderr)
        return 2
    if not 0 <= args.address <= 0x7F:
        print("--address must be a 7-bit I2C address (0x00..0x7F)", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        scale = TemperatureScale(args.fixed_min, args.fixed_max)
        display = ThermalDisplay(args.cell, args.fullscreen, args.flip_h, args.flip_v, not args.no_legend)
    except (RuntimeError, ValueError) as exc:
        print(f"初始化显示失败：{exc}", file=sys.stderr)
        return 2

    sensor: Mlx90640Sensor | None = None
    try:
        if not args.simulate:
            sensor = Mlx90640Sensor(args.rate, args.i2c_frequency, args.address)
            print("MLX90640 connected; serial:", " ".join(f"{part:04X}" for part in sensor.serial_number))

        running = True

        def stop_handler(_signal: int, _frame: object) -> None:
            nonlocal running
            running = False

        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)
        started = time.monotonic()
        next_frame_at = started
        while running:
            if not display.poll_events():
                break
            now = time.monotonic()
            if now < next_frame_at:
                time.sleep(min(0.01, next_frame_at - now))
                continue
            next_frame_at = now + 1.0 / args.rate

            try:
                temperatures = synthetic_frame(now - started) if sensor is None else sensor.read_frame()
                low, high = scale.update(temperatures)
                display.render(temperatures, low, high)
            except (OSError, RuntimeError, ValueError) as exc:
                logging.warning("frame skipped: %s", exc)
                time.sleep(0.05)
                continue

            if args.screenshot is not None:
                display.save_screenshot(args.screenshot)
                print(f"已保存截图：{args.screenshot}")
                break
    except RuntimeError as exc:
        print(f"传感器初始化失败：{exc}", file=sys.stderr)
        return 2
    finally:
        if sensor is not None:
            sensor.close()
        display.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
