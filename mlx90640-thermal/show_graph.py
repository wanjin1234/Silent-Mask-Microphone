#!/usr/bin/env python3
"""连续温度图显示：MLX90640 -> PNG -> pygame 连续全屏显示。

数据流（采集与显示各自独立，互不阻塞）：

    采集线程 ——读传感器——> draw_graph() ——原子覆写 PNG——> 显示循环

显示端以 ``--fps`` 恒定刷新，采集端以 ``--rate`` 取帧。这样采集变慢或偶发
失败时，画面会停留在上一帧并继续刷新窗口，而不是卡死或整个程序退出。

树莓派（真实传感器）::

    export MLX90640_I2C_BUS=3        # 软件 I2C 时按 README 设置总线号
    python show_graph.py             # 全屏连续显示；关窗 / Esc / Q / Ctrl+C 退出
    python show_graph.py --vmin 20 --vmax 40 --rate 8 --fps 10

开发机上不接传感器验证显示链路::

    python show_graph.py --simulate --windowed --duration 10

只当看图程序（不读传感器，只显示别人写好的 graph/current_img.png）::

    python show_graph.py --no-capture --windowed
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

# matplotlib 只负责把温度矩阵渲染成 PNG（用离屏 Agg 后端），窗口显示交给 pygame。
# 两者在同一进程里各自带一套 GUI 库，用一个需要图形环境的后端会互相抢 SDL，
# 所以在导入 matplotlib 之前就定死 Agg。
os.environ.setdefault("MPLBACKEND", "Agg")
# 不显示 pygame 的启动横幅（本程序自己会打印状态）。
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame as pg

# 本文件与 continuos_graphic.py / thermal_numpy_reader.py /
# mlx90640_thermal_display.py 都在 mlx90640-thermal/ 下。把本文件所在目录加入
# sys.path，这样从任何工作目录运行都能导入，不依赖 CWD。
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from continuos_graphic import FRAME_PATH, draw_graph  # noqa: E402
from mlx90640_thermal_display import Mlx90640Sensor, synthetic_frame  # noqa: E402
from thermal_numpy_reader import frame_to_2d, read_thermal_3d  # noqa: E402

# pygame-ce 里 smoothscale 仍在，但不同发行版可能没有，缺了就退回不平滑的 scale。
_SMOOTHSCALE = getattr(pg.transform, "smoothscale", pg.transform.scale)

# pygame 自带的默认字体（freesansbold）没有中文字形，直接往上写中文会显示成一排
# 方框。所以先找系统中文字体（Windows 的开发机通常有微软雅黑，树莓派 Lite 一般
# 一个都没有），找不到就用纯英文标签。
CJK_FONT_NAMES = "notosanscjksc,notosanscjk,sourcehansanssc,wenquanyimicrohei,wqymicrohei,microsoftyahei,msyh,simhei"
_FONT_CACHE: dict[int, tuple[pg.font.Font, bool]] = {}


def load_font(size: int) -> tuple[pg.font.Font, bool]:
    """返回 (字体, 是否支持中文)。按字号缓存，避免每帧重复加载字体文件。"""
    if size not in _FONT_CACHE:
        font: pg.font.Font | None = None
        cjk = False
        try:
            path = pg.font.match_font(CJK_FONT_NAMES)
            if path:
                font, cjk = pg.font.Font(path, size), True
        except Exception:  # noqa: BLE001
            # 枚举系统字体本身可能抛错：pygame 2.6.1 在部分 Windows 机器上会在
            # pygame.sysfont.initsysfonts_win32() 里把注册表字体列表中的某个 int
            # 当路径传给 splitext，直接 TypeError。字体探测失败只是没中文标签，
            # 不该让整个显示程序崩掉，所以这里一律降级到默认字体。
            font = None
        if font is None:
            font, cjk = pg.font.Font(None, size), False
        _FONT_CACHE[size] = (font, cjk)
    return _FONT_CACHE[size]


class CaptureStats:
    """采集线程的计数与最近一次错误，显示端只读它来画状态栏。"""

    def __init__(self) -> None:
        self.frames = 0
        self.errors = 0
        self.last_error = ""


def capture_loop(
    args: argparse.Namespace,
    sensor: Mlx90640Sensor | None,
    stop_event: threading.Event,
    stats: CaptureStats,
) -> None:
    """按 ``--rate`` 取帧并渲染成 PNG，直到 stop_event 被置位。

    sensor 为 None 时用合成温度场（``--simulate``）。传感器由外部创建、外部
    关闭，本函数只负责读，避免每帧重建 I2C 连接。
    """
    started = time.monotonic()
    next_frame_at = started
    while not stop_event.is_set():
        now = time.monotonic()
        if now < next_frame_at:
            # 用 Event.wait 代替 sleep：退出时能立刻醒来，不用等这一帧睡完。
            stop_event.wait(min(0.05, next_frame_at - now))
            continue
        next_frame_at += 1.0 / args.rate
        if next_frame_at < time.monotonic():
            # 采集落后太多（例如某一帧重试了很久）就重新对齐节拍，而不是连读几帧追回来。
            next_frame_at = time.monotonic() + 1.0 / args.rate

        try:
            if sensor is None:
                # 合成场景随时间缓变，这里必须用外层单调时钟计时：如果交给
                # read_thermal_3d 内部计时，每次调用都是"第 0 秒"，画面会纹丝不动。
                frame = frame_to_2d(
                    synthetic_frame(time.monotonic() - started),
                    flip_h=args.flip_h,
                    flip_v=args.flip_v,
                )
            else:
                # read_thermal_3d 负责重试（I2C 抖动、dataReady 超时等）；
                # 传入已有 sensor，它不会去关闭这个连接。第 3 维是帧轴，取第 0 帧。
                frame = read_thermal_3d(
                    1,
                    sensor=sensor,
                    retries=args.retries,
                    flip_h=args.flip_h,
                    flip_v=args.flip_v,
                )[:, :, 0]
            draw_graph(frame, vmin=args.vmin, vmax=args.vmax)
            stats.frames += 1
        except (OSError, RuntimeError, ValueError) as exc:
            # 单帧失败不清屏、不退出：显示端继续放着上一帧，状态栏显示错误计数。
            stats.errors += 1
            stats.last_error = str(exc)
            stop_event.wait(0.05)


def load_frame_surface(size: tuple[int, int]) -> pg.Surface | None:
    """读取帧 PNG 并缩放到窗口尺寸；读不到就返回 None（由调用方沿用上一帧）。

    写入方是原子替换，正常情况下不会读到半张图；但文件仍可能被外部进程改动、
    或被删除，所以这里容忍读取失败，而不是让整个显示崩掉。
    """
    try:
        image = pg.image.load(str(FRAME_PATH))
    except (pg.error, FileNotFoundError, OSError):
        return None
    image = image.convert()  # 统一成显示格式（32 位），smoothscale 需要
    if image.get_size() != size:
        image = _SMOOTHSCALE(image, size)
    return image


def draw_hud(
    screen: pg.Surface,
    args: argparse.Namespace,
    stats: CaptureStats,
    shown: int,
    fps: float,
) -> None:
    """在左上角画状态栏：采集/显示帧数、帧率、色阶、最近错误。"""
    font, cjk = load_font(22)
    if args.vmin is not None and args.vmax is not None:
        scale = f"{args.vmin:.1f}..{args.vmax:.1f} C"
    else:
        scale = "自动缩放" if cjk else "auto"
    if cjk:
        lines = [
            f"采集 {stats.frames} 帧   显示 {shown} 帧   读帧错误 {stats.errors} 次",
            f"传感器 {args.rate:g} Hz   显示 {fps:.1f} FPS   色阶 {scale}",
        ]
        if stats.last_error:
            lines.append(f"最近错误：{stats.last_error[:70]}")
    else:
        lines = [
            f"Frames {stats.frames}   Shown {shown}   Errors {stats.errors}",
            f"Sensor {args.rate:g} Hz   Display {fps:.1f} FPS   Scale {scale}",
        ]
        if stats.last_error:
            lines.append(f"Last error: {stats.last_error[:70]}")

    rendered = [font.render(line, True, (255, 255, 255)) for line in lines]
    panel_width = max(line.get_width() for line in rendered) + 16
    panel_height = sum(line.get_height() for line in rendered) + 12

    panel = pg.Surface((panel_width, panel_height), pg.SRCALPHA)
    panel.fill((0, 0, 0, 160))  # 半透明黑底，避免文字被热成像亮色淹没
    screen.blit(panel, (8, 8))
    y = 14
    for line in rendered:
        screen.blit(line, (16, y))
        y += line.get_height()


def display_loop(
    screen: pg.Surface,
    args: argparse.Namespace,
    stats: CaptureStats,
    stop_event: threading.Event,
) -> int:
    """显示循环：只在帧文件更新时重新读图，每轮都刷新窗口并处理事件。

    返回实际显示过的不同帧数。
    """
    clock = pg.time.Clock()
    shown = 0
    last_mtime: int | None = None
    surface: pg.Surface | None = None
    started = time.monotonic()
    size = screen.get_size()
    # 注意属性名：argparse 把 --no-hud 存成 args.no_hud。
    hud = not args.no_hud

    while not stop_event.is_set():
        # 事件泵：不处理事件的话关窗按钮和按键都没反应，只能杀进程。
        for event in pg.event.get():
            if event.type == pg.QUIT:
                return shown
            if event.type == pg.KEYDOWN and event.key in (pg.K_ESCAPE, pg.K_q):
                return shown

        try:
            mtime = FRAME_PATH.stat().st_mtime_ns
        except OSError:
            mtime = None  # 第一帧还没写出来

        if mtime is not None and mtime != last_mtime:
            fresh = load_frame_surface(size)
            if fresh is not None:
                surface, last_mtime, shown = fresh, mtime, shown + 1

        screen.fill((0, 0, 0))
        if surface is not None:
            screen.blit(surface, (0, 0))
        elif hud:
            font, cjk = load_font(30)
            text = (
                f"等待温度数据：{FRAME_PATH.name} …"
                if cjk
                else f"Waiting for frames: {FRAME_PATH.name} ..."
            )
            hint = font.render(text, True, (200, 200, 200))
            screen.blit(hint, hint.get_rect(center=(size[0] // 2, size[1] // 2)))
        if hud:
            draw_hud(screen, args, stats, shown, clock.get_fps())
        pg.display.flip()

        clock.tick(args.fps)
        if args.duration > 0 and time.monotonic() - started >= args.duration:
            break
    return shown


def create_screen(args: argparse.Namespace) -> pg.Surface:
    """初始化 pygame 并创建窗口。set_mode 只在这里调用一次。"""
    pg.init()
    pg.display.set_caption("MLX90640 连续热成像")
    if args.windowed:
        return pg.display.set_mode((960, 720))
    info = pg.display.Info()
    if info.current_w <= 0 or info.current_h <= 0:
        # 拿不到显示器尺寸（例如无头环境）时退回窗口模式，别拿 (0, 0) 去建窗口。
        return pg.display.set_mode((960, 720))
    return pg.display.set_mode((info.current_w, info.current_h), pg.FULLSCREEN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MLX90640 连续温度图显示（传感器 -> PNG -> pygame 全屏）"
    )
    parser.add_argument("--rate", type=float, choices=(0.5, 1.0, 2.0, 4.0, 8.0), default=8.0, help="传感器取帧率 Hz，默认 8")
    parser.add_argument("--fps", type=float, default=10.0, help="显示刷新率 Hz，默认 10")
    parser.add_argument("--i2c-frequency", type=int, default=800_000, help="I2C 请求频率 Hz，默认 800000")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x33, help="I2C 7 位地址，默认 0x33")
    parser.add_argument("--vmin", type=float, default=20.0, help="色阶下限摄氏度，默认 20（须与 --vmax 配对）")
    parser.add_argument("--vmax", type=float, default=40.0, help="色阶上限摄氏度，默认 40（须与 --vmin 配对）")
    parser.add_argument("--flip-h", action="store_true", help="水平镜像（传感器倒装时使用）")
    parser.add_argument("--flip-v", action="store_true", help="垂直镜像（传感器倒装时使用）")
    parser.add_argument("--retries", type=int, default=3, help="单帧读取失败的重试次数，默认 3")
    parser.add_argument("--simulate", action="store_true", help="用合成温度场，无需传感器")
    parser.add_argument("--no-capture", action="store_true", help="不读传感器，只显示已有的帧 PNG")
    parser.add_argument("--windowed", action="store_true", help="窗口模式（默认全屏）")
    parser.add_argument("--no-hud", action="store_true", help="不显示左上角状态栏")
    parser.add_argument("--duration", type=float, default=0.0, help="运行多少秒后自动退出，0 表示一直运行")
    parser.add_argument("--screenshot", type=Path, help="退出前把当前画面保存为 PNG（无头调试用）")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if (args.vmin is None) != (args.vmax is None):
        print("--vmin 和 --vmax 必须成对给出", file=sys.stderr)
        return 2
    if args.vmin is not None and args.vmax is not None and args.vmax <= args.vmin:
        print("--vmax 必须大于 --vmin", file=sys.stderr)
        return 2
    if args.no_capture and args.simulate:
        print("--no-capture 和 --simulate 不能同时使用", file=sys.stderr)
        return 2

    # 传感器只创建一次（原来是每帧调用 read_thermal_3d() 自行初始化：每帧重开
    # I2C、重读 EEPROM、重设刷新率，在树莓派上又慢又容易踩到设备的不稳定点）。
    sensor: Mlx90640Sensor | None = None
    if not args.no_capture and not args.simulate:
        try:
            sensor = Mlx90640Sensor(args.rate, args.i2c_frequency, args.address)
        except RuntimeError as exc:
            print(f"传感器初始化失败：{exc}", file=sys.stderr)
            return 2
        print("MLX90640 connected; serial:", " ".join(f"{part:04X}" for part in sensor.serial_number))

    try:
        screen = create_screen(args)
    except pg.error as exc:
        print(f"创建显示窗口失败：{exc}", file=sys.stderr)
        if sensor is not None:
            sensor.close()
        return 2
    print(f"帧文件：{FRAME_PATH}")

    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    stats = CaptureStats()
    capture_thread: threading.Thread | None = None
    if not args.no_capture:
        capture_thread = threading.Thread(
            target=capture_loop,
            args=(args, sensor, stop_event, stats),
            name="capture",
            daemon=True,
        )
        capture_thread.start()

    shown = 0
    try:
        shown = display_loop(screen, args, stats, stop_event)
        if args.screenshot is not None and shown > 0:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            pg.image.save(screen, str(args.screenshot))
            print(f"已保存截图：{args.screenshot}")
    finally:
        stop_event.set()
        if capture_thread is not None:
            capture_thread.join(timeout=3.0)
        if sensor is not None:
            sensor.close()
        pg.quit()

    print(f"显示结束：采集 {stats.frames} 帧，显示 {shown} 帧，读帧错误 {stats.errors} 次")
    if stats.last_error:
        print(f"最近一次读帧错误：{stats.last_error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
