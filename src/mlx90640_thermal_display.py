#!/usr/bin/env python3
"""MLX90640 32x24 红外温度点阵显示（树莓派 4B，pygame）。

将 MLX90640 的一帧 768 个温度点（32 列 x 24 行）按“红外热成像惯例”的
冷→热渐变配色映射成颜色，并渲染为点阵画面。配色算法忠实移植自参考实现
`MLX90640-D110 Thermal/.../mlx90640_lcd_display.c` 中的 `TempToColor`：
低温为蓝、随温度升高依次过渡为青→绿→黄→红→品红（jet/铁红色系）。

运行示例（真实传感器，I2C1，8 Hz）：
    python mlx90640_thermal_display.py --rate 8

无硬件/Windows 下用合成数据调试：
    python mlx90640_thermal_display.py --simulate

生成一张静态画面用于验证配色（无窗口/无头环境下也可用）：
    python mlx90640_thermal_display.py --simulate --screenshot thermal.png
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import time
from typing import Sequence

try:
    import pygame
except ImportError:  # 允许在没有 pygame 的环境中仅使用配色函数
    pygame = None

WIDTH = 32
HEIGHT = 24
PIXEL_COUNT = WIDTH * HEIGHT


# ---------------------------------------------------------------------------
# 颜色映射（移植自 mlx90640_lcd_display.c 的 TempToColor）
# ---------------------------------------------------------------------------
def _constrain(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def temperature_to_rgb(value: float, min_t: float, max_t: float) -> tuple[int, int, int]:
    """把单个温度值映射为 RGB 颜色（冷→热）。

    采用参考 STM32 固件相同的分段线性渐变与截止点：
        a=21.21%  b=31.82%  c=42.42%  d=81.82%（按 min_t~max_t 归一化）。
    """
    if max_t <= min_t:
        # 全画面温度一致的退化情况：中性灰，避免除零
        return (128, 128, 128)

    a = min_t + (max_t - min_t) * 0.2121
    b = min_t + (max_t - min_t) * 0.3182
    c = min_t + (max_t - min_t) * 0.4242
    d = min_t + (max_t - min_t) * 0.8182

    # 红：b→c 之间由 0 升到 255
    red = _constrain(255.0 / (c - b) * value - (b * 255.0) / (c - b), 0.0, 255.0)

    # 绿：min_t→a 上升；a→c 保持 255；c→d 下降到 0
    if min_t < value < a:
        green = _constrain(255.0 / (a - min_t) * value - (255.0 * min_t) / (a - min_t), 0.0, 255.0)
    elif a <= value <= c:
        green = 255.0
    elif value > c:
        green = _constrain(255.0 / (c - d) * value - (d * 255.0) / (c - d), 0.0, 255.0)
    else:  # value <= min_t（val > d 已由上面 value > c 分支覆盖）
        green = 0.0

    # 蓝：≤a 为 255，a→b 降到 0；b→d 保持 0；d→max_t 升到 240
    if value <= b:
        blue = _constrain(255.0 / (a - b) * value - (255.0 * b) / (a - b), 0.0, 255.0)
    elif b < value <= d:
        blue = 0.0
    else:  # value > d
        blue = _constrain(240.0 / (max_t - d) * value - (d * 240.0) / (max_t - d), 0.0, 240.0)

    return (int(round(red)), int(round(green)), int(round(blue)))


def build_lut(min_t: float, max_t: float, levels: int = 256) -> list[tuple[int, int, int]]:
    """预生成 256 级查找表，渲染 768 点时避免逐点重复浮点运算。"""
    if max_t <= min_t:
        return [(128, 128, 128)] * levels
    lut: list[tuple[int, int, int]] = []
    for i in range(levels):
        value = min_t + (max_t - min_t) * i / (levels - 1)
        lut.append(temperature_to_rgb(value, min_t, max_t))
    return lut


def synthetic_frame(t: float) -> list[float]:
    """生成一帧平滑的温度场，用于无硬件调试与配色验证。

    包含一个缓慢移动的高温“热斑”和一个从左到右的基础温度梯度，
    温度范围约 20~45 °C，能完整覆盖蓝→红的热成像渐变。
    """
    cx = 16 + 12 * math.sin(t * 0.7)
    cy = 12 + 6 * math.cos(t * 0.4)
    frame: list[float] = []
    for y in range(HEIGHT):
        for x in range(WIDTH):
            base = 22.0 + 8.0 * (x / (WIDTH - 1))
            hotspot = 24.0 * math.exp(-(((x - cx) ** 2) / (2 * 3.0 ** 2) + ((y - cy) ** 2) / (2 * 2.5 ** 2)))
            ripple = 0.6 * math.sin(x * 0.6 + t * 1.5) * math.cos(y * 0.5 - t)
            frame.append(base + hotspot + ripple)
    return frame


# ---------------------------------------------------------------------------
# pygame 点阵显示
# ---------------------------------------------------------------------------
class ThermalDisplay:
    def __init__(
        self,
        cell: int = 20,
        fullscreen: bool = False,
        flip_h: bool = False,
        flip_v: bool = False,
        fixed_min: float | None = None,
        fixed_max: float | None = None,
        show_legend: bool = True,
        window_title: str = "MLX90640 32x24 红外热成像",
    ) -> None:
        if pygame is None:
            raise RuntimeError("未安装 pygame，请执行：python -m pip install pygame")

        self.cell = max(2, int(cell))
        self.flip_h = flip_h
        self.flip_v = flip_v
        self.fixed_min = fixed_min
        self.fixed_max = fixed_max
        self.show_legend = show_legend

        self.grid_w = WIDTH * self.cell
        self.grid_h = HEIGHT * self.cell
        self.legend_h = max(24, int(self.cell * 1.2)) if show_legend else 0
        self.width = self.grid_w
        self.height = self.grid_h + self.legend_h

        if not pygame.get_init():
            pygame.init()
        flags = pygame.SCALED
        if fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption(window_title)

        self._font = pygame.font.Font(None, max(16, int(self.cell * 0.9)))
        self._small_font = pygame.font.Font(None, max(12, int(self.cell * 0.7)))

    def render_frame(self, frame: Sequence[float]) -> tuple[float, float, float]:
        """渲染一帧，返回 (min_c, max_c, center_c)。"""
        if len(frame) != PIXEL_COUNT:
            raise ValueError(f"帧长度应为 {PIXEL_COUNT}，实际为 {len(frame)}")
        temps = [float(v) for v in frame]
        if not all(math.isfinite(v) for v in temps):
            raise ValueError("帧中含有非有限温度值")

        min_t = self.fixed_min if self.fixed_min is not None else min(temps)
        max_t = self.fixed_max if self.fixed_max is not None else max(temps)
        if max_t <= min_t:
            max_t = min_t + 1.0  # 单值画面：给一个最小动态范围，避免 LUT 全灰

        lut = build_lut(min_t, max_t, 256)
        inv = 255.0 / (max_t - min_t)

        self.screen.fill((0, 0, 0))
        gap = 1 if self.cell >= 6 else 0
        block = self.cell - gap
        for y in range(HEIGHT):
            row = y * WIDTH
            draw_y = (HEIGHT - 1 - y) if self.flip_v else y
            for x in range(WIDTH):
                value = temps[row + x]
                idx = int((value - min_t) * inv)
                idx = 0 if idx < 0 else 255 if idx > 255 else idx
                draw_x = (WIDTH - 1 - x) if self.flip_h else x
                rect = (draw_x * self.cell, draw_y * self.cell, block, block)
                pygame.draw.rect(self.screen, lut[idx], rect)

        if self.show_legend:
            self._draw_legend(min_t, max_t, lut)

        center = _center_temperature(temps)
        self._draw_center_text(center)

        pygame.display.flip()
        return (min_t, max_t, center)

    def _draw_legend(self, min_t: float, max_t: float, lut: list[tuple[int, int, int]]) -> None:
        top = self.grid_h + 2
        bar_h = max(8, self.legend_h - 18)
        slices = len(lut)
        for i, color in enumerate(lut):
            x0 = int(i * self.grid_w / slices)
            x1 = int((i + 1) * self.grid_w / slices)
            pygame.draw.rect(self.screen, color, (x0, top, max(1, x1 - x0), bar_h))

        min_surf = self._small_font.render(f"{min_t:.1f}", True, (220, 220, 220))
        max_surf = self._small_font.render(f"{max_t:.1f}", True, (220, 220, 220))
        label_y = top + bar_h + 2
        self.screen.blit(min_surf, (2, label_y))
        self.screen.blit(max_surf, (self.grid_w - max_surf.get_width() - 2, label_y))

    def _draw_center_text(self, center: float) -> None:
        text = f"center {center:.2f} C"
        surf = self._font.render(text, True, (255, 255, 255))
        self.screen.blit(surf, (4, 4))


def _center_temperature(temps: Sequence[float]) -> float:
    """取画面中心 2x2 像素的平均温度，与参考固件 drawMeasurement 语义一致。"""
    x0, y0 = WIDTH // 2 - 1, HEIGHT // 2 - 1
    return sum(temps[y * WIDTH + x] for y in (y0, y0 + 1) for x in (x0, x0 + 1)) / 4.0


def _open_sensor(rate_hz: float):
    from mlx90640_temperature_detection import open_sensor

    return open_sensor(rate_hz)


def _read_frame(sensor) -> list[float]:
    from mlx90640_temperature_detection import read_frame

    return read_frame(sensor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MLX90640 32x24 红外温度点阵显示")
    parser.add_argument("--rate", type=float, choices=(0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0), default=4.0, help="传感器帧率 Hz（默认：4）")
    parser.add_argument("--period", type=float, default=0.0, help="渲染循环周期秒数（默认：自动，等于 1/帧率）")
    parser.add_argument("--cell", type=int, default=20, help="每个温度点的像素边长（默认：20）")
    parser.add_argument("--fullscreen", action="store_true", help="全屏显示")
    parser.add_argument("--flip-h", action="store_true", help="水平镜像（适配传感器倒装）")
    parser.add_argument("--flip-v", action="store_true", help="垂直镜像")
    parser.add_argument("--min", dest="fixed_min", type=float, default=None, help="固定色阶下限 °C（默认：自动取帧内最小）")
    parser.add_argument("--max", dest="fixed_max", type=float, default=None, help="固定色阶上限 °C（默认：自动取帧内最大）")
    parser.add_argument("--no-legend", action="store_true", help="不显示底部色阶条")
    parser.add_argument("--simulate", action="store_true", help="使用合成数据（无需硬件）")
    parser.add_argument("--screenshot", type=str, default=None, help="渲染一帧并保存为图片后退出")
    parser.add_argument("--verbose", action="store_true", help="显示读帧重试日志")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    period = args.period if args.period > 0 else 1.0 / args.rate

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.screenshot:
        # 无头环境也能保存截图：使用 dummy 视频驱动，不弹出窗口
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    try:
        display = ThermalDisplay(
            cell=args.cell,
            fullscreen=args.fullscreen,
            flip_h=args.flip_h,
            flip_v=args.flip_v,
            fixed_min=args.fixed_min,
            fixed_max=args.fixed_max,
            show_legend=not args.no_legend,
        )
    except RuntimeError as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 2

    sensor = None
    if not args.simulate:
        try:
            sensor = _open_sensor(args.rate)
        except RuntimeError as exc:
            print(f"初始化失败：{exc}", file=sys.stderr)
            return 2

    running = True

    def stop_handler(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    start_time = time.monotonic()
    while running:
        cycle_started = time.monotonic()
        try:
            if sensor is not None:
                frame = _read_frame(sensor)
            else:
                frame = synthetic_frame(time.monotonic() - start_time)
            display.render_frame(frame)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"渲染失败：{exc}", file=sys.stderr, flush=True)
            if sensor is not None:
                time.sleep(0.05)
                continue
        if args.screenshot:
            pygame.image.save(display.screen, args.screenshot)
            print(f"已保存截图到 {args.screenshot}")
            break
        time.sleep(max(0.0, period - (time.monotonic() - cycle_started)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
