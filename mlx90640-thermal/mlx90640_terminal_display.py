#!/usr/bin/env python3
"""MLX90640 终端热成像显示（无需图形桌面，SSH / Raspberry Pi OS Lite 可用）。

用 ANSI 24-bit 真彩色在终端实时渲染 32x24 色块，每个像元两列空格。
适合没有 X11/Wayland 的 Lite 系统，也适合远程 SSH 查看。

运行：
    export MLX90640_I2C_BUS=3
    python mlx90640_terminal_display.py --rate 2

无传感器测试：
    python mlx90640_terminal_display.py --simulate
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from mlx90640_thermal_display import (
    HEIGHT,
    WIDTH,
    Mlx90640Sensor,
    TemperatureScale,
    center_temperature,
    synthetic_frame,
    temperature_to_rgb,
)


def _home() -> str:
    return "\x1b[H"


def _clear() -> str:
    return "\x1b[2J\x1b[H"


def render_frame(frame, low: float, high: float) -> str:
    """把一帧 768 温度值渲染成 ANSI 彩色文本。"""
    lines: list[str] = []
    for y in range(HEIGHT):
        row: list[str] = []
        for x in range(WIDTH):
            value = frame[y * WIDTH + x]
            r, g, b = temperature_to_rgb(value, low, high)
            row.append(f"\x1b[48;2;{r};{g};{b}m  ")
        row.append("\x1b[0m")
        lines.append("".join(row))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MLX90640 终端热成像显示（无图形桌面）")
    parser.add_argument("--rate", type=float, choices=(0.5, 1.0, 2.0, 4.0, 8.0), default=2.0, help="帧率 Hz，默认 2")
    parser.add_argument("--i2c-frequency", type=int, default=800_000, help="I2C 频率，默认 800000")
    parser.add_argument("--address", type=lambda v: int(v, 0), default=0x33, help="I2C 7 位地址，默认 0x33")
    parser.add_argument("--min", dest="fixed_min", type=float, help="固定色阶下限")
    parser.add_argument("--max", dest="fixed_max", type=float, help="固定色阶上限")
    parser.add_argument("--flip-h", action="store_true", help="水平镜像")
    parser.add_argument("--flip-v", action="store_true", help="垂直镜像")
    parser.add_argument("--simulate", action="store_true", help="使用模拟温度场，无需传感器")
    parser.add_argument("--once", action="store_true", help="只显示一帧后退出")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        scale = TemperatureScale(args.fixed_min, args.fixed_max)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    sensor: Mlx90640Sensor | None = None
    if not args.simulate:
        try:
            sensor = Mlx90640Sensor(args.rate, args.i2c_frequency, args.address)
        except RuntimeError as exc:
            print(f"传感器初始化失败：{exc}", file=sys.stderr)
            return 2
        print("MLX90640 connected; serial:", " ".join(f"{part:04X}" for part in sensor.serial_number))

    running = True

    def stop(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    sys.stdout.write(_clear())
    sys.stdout.flush()

    started = time.monotonic()
    next_frame_at = started
    try:
        while running:
            now = time.monotonic()
            if now < next_frame_at:
                time.sleep(min(0.02, next_frame_at - now))
                continue
            next_frame_at = now + 1.0 / args.rate

            try:
                frame = synthetic_frame(now - started) if sensor is None else sensor.read_frame()
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"\r读帧失败：{exc}", file=sys.stderr, flush=True)
                time.sleep(0.1)
                continue

            if args.flip_h:
                frame = [frame[y * WIDTH + (WIDTH - 1 - x)] for y in range(HEIGHT) for x in range(WIDTH)]
            if args.flip_v:
                frame = [frame[(HEIGHT - 1 - y) * WIDTH + x] for y in range(HEIGHT) for x in range(WIDTH)]

            low, high = scale.update(frame)
            center = center_temperature(frame)
            peak = max(frame)

            out = [_home(), render_frame(frame, low, high)]
            out.append(f"\x1b[0m\nCenter {center:.1f} C   Peak {peak:.1f} C   Scale {low:.1f}..{high:.1f} C   (Ctrl+C 退出)")
            sys.stdout.write("".join(out))
            sys.stdout.flush()

            if args.once:
                break
    finally:
        sys.stdout.write("\x1b[0m\n")
        if sensor is not None:
            sensor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
