"""radarpi 命令行入口。

常用命令::

    radarpi doctor              环境体检（推荐第一步）
    radarpi ports               列出串口设备
    radarpi serve               启动 Web 上位机（浏览器看图）
    radarpi monitor             终端里看实时点云统计
    radarpi record out.csv      录制点云
    radarpi replay out.csv      回放录制文件
    radarpi send "scan start"   直接下发指令
    radarpi config set radar.boundary "-3 3 0.8 0.2 5"
    radarpi sniff               原始抓包，用于核对协议
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Iterator, List, Optional

from . import __version__
from . import doctor as doctor_mod
from . import protocol as P
from .config import Config
from .link import FrameSource, RadarLink, SimulatedLink
from .recorder import Recorder, ReplaySource, default_filename
from .serialport import BAUD_RATE, SerialError, candidate_devices, list_ports, open_port

# --------------------------------------------------------------------------
# 数据源构造
# --------------------------------------------------------------------------


def build_source(args, config: Config, for_web: bool = False) -> FrameSource:
    """按命令行参数构造数据源：真机串口 / 回放 / 仿真。"""
    if getattr(args, "simulate", False):
        return SimulatedLink(fps=getattr(args, "fps", 10.0) or 10.0)
    replay = getattr(args, "replay", None)
    if replay:
        return ReplaySource(
            replay,
            loop=getattr(args, "loop", False),
            speed=getattr(args, "speed", 1.0) or 1.0,
        )
    baud = getattr(args, "baud", None) or config.get_int("serial", "baudrate", BAUD_RATE)
    device = getattr(args, "device", None) or config.get("serial", "device", "auto")
    keepalive = config.get_float("serial", "keepalive", 5.0)
    return RadarLink(
        device=device,
        baudrate=int(baud),
        profile=config.radar_profile(),
        auto_start=config.get_bool("serial", "auto_start", True),
        reconnect=config.get_bool("serial", "reconnect", True),
        keepalive=keepalive,
    )


def _print_ports(ports) -> None:
    if not ports:
        print("没有发现串口设备。")
        print("提示：确认 USB 转串口小板已插入，然后执行 radarpi doctor 查看原因。")
        return
    print("%-28s %-10s %-10s %-14s %s" % ("设备", "VID:PID", "内核驱动", "类型", "说明"))
    print("-" * 100)
    for p in ports:
        vid = "%s:%s" % (p.vid, p.pid) if p.vid else "-"
        kind = "疑似雷达" if p.is_likely_radar else "-"
        print("%-28s %-10s %-10s %-14s %s" % (p.device, vid, p.driver or "-", kind, p.description or "-"))
        if p.by_id:
            print("%-28s %s" % ("", "稳定别名：" + p.by_id))


# --------------------------------------------------------------------------
# 各子命令
# --------------------------------------------------------------------------


def cmd_doctor(args, config: Config) -> int:
    items = doctor_mod.run(device=args.device, probe=args.probe, probe_seconds=args.seconds)
    if args.json:
        print(json.dumps({"items": items, "summary": doctor_mod.summarize(items)}, ensure_ascii=False, indent=2))
    else:
        print(doctor_mod.format_text(items))
    return 0


def cmd_ports(args, config: Config) -> int:
    ports = list_ports()
    if args.json:
        print(json.dumps([p.as_dict() for p in ports], ensure_ascii=False, indent=2))
    else:
        _print_ports(ports)
        cands = candidate_devices()
        if cands:
            print("\n自动识别顺序：%s" % " → ".join(cands[:4]))
    return 0


def cmd_monitor(args, config: Config) -> int:
    source = build_source(args, config)
    parser_stats = getattr(source, "parser", None)
    started = time.time()
    frames = 0
    points = 0
    last_print = 0.0
    try:
        for frame in source.frames():
            frames += 1
            points += len(frame.points)
            if args.json:
                print(json.dumps(frame.as_dict(with_tracks=True), ensure_ascii=False))
            elif args.raw:
                _print_raw_frame(frame)
            else:
                now = time.monotonic()
                if now - last_print > 0.2:
                    last_print = now
                    elapsed = max(time.time() - started, 1e-6)
                    counts = " ".join(
                        "%s=%d" % (name, n) for name, n in zip(P.GROUP_SHORT_NAMES, frame.header.counts) if n
                    )
                    line = "#%-6d 点=%-4d %-46s 周期=%-4dms 平均帧率=%.1ffps" % (
                        frame.frame_id, len(frame.points), counts or "-",
                        frame.header.frame_period, frames / elapsed,
                    )
                    sys.stdout.write("\r" + line[:200])
                    sys.stdout.flush()
            if args.duration and (time.time() - started) >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    except SerialError as exc:
        print("\n串口错误：%s" % exc, file=sys.stderr)
        return 2
    finally:
        source.close()
    if not args.json and not args.raw:
        print()
    elapsed = time.time() - started
    print("共 %d 帧 / %d 点，用时 %.1fs（平均 %.2f fps）" % (frames, points, elapsed, frames / max(elapsed, 1e-6)))
    if parser_stats is not None:
        print("链路统计：%s" % json.dumps(parser_stats.stats.as_dict(), ensure_ascii=False))
    return 0


def _print_raw_frame(frame: P.Frame) -> None:
    """把一帧打印成可读的十六进制块，便于与手册逐字段核对。"""
    for p in frame.points:
        print(
            "#%-6d g=%-10s X=%8.3f Y=%8.3f Z=%8.3f SNR=%-6d V=%6.2f"
            % (frame.frame_id, P.GROUP_SHORT_NAMES[p.group], p.x, p.y, p.z, p.snr, p.speed)
        )


def cmd_record(args, config: Config) -> int:
    source = build_source(args, config)
    fmt = args.format or config.get("record", "format", "csv")
    path = args.output or default_filename(config.get("record", "directory", "."), fmt)
    started = time.time()
    rec = Recorder(path, fmt)
    print("开始录制 → %s" % path)
    try:
        for frame in source.frames():
            rec.write(frame)
            if not args.quiet:
                sys.stdout.write("\r已录制 %d 帧 / %d 点" % (rec.frames, rec.points))
                sys.stdout.flush()
            if args.duration and (time.time() - started) >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    except SerialError as exc:
        print("\n串口错误：%s" % exc, file=sys.stderr)
        return 2
    finally:
        source.close()
        info = rec.close()
    print()
    print("录制结束：%s，%d 帧 / %d 点，时长 %.1fs" % (info["path"], info["frames"], info["points"], info["seconds"]))
    return 0 if info["frames"] else 1


def cmd_replay(args, config: Config) -> int:
    args.replay = args.file
    source = build_source(args, config)
    if args.serve:
        return _serve(args, config, source)
    try:
        frames = 0
        for frame in source.frames():
            frames += 1
            counts = " ".join("%s=%d" % (n, c) for n, c in zip(P.GROUP_SHORT_NAMES, frame.header.counts) if c)
            sys.stdout.write("\r回放 #%-6d 点=%-4d %-40s" % (frame.frame_id, len(frame.points), counts))
            sys.stdout.flush()
            if args.max_frames and frames >= args.max_frames:
                break
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    print("\n共回放 %d 帧。" % frames)
    return 0


def _serve(args, config: Config, source: FrameSource) -> int:
    from .webapp import RadarHub, serve

    hub = RadarHub(source, config)
    if getattr(args, "record_to", None):
        result = hub.record_start(path=args.record_to)
        if result.get("ok"):
            print("同时录制到 %s" % result["path"])
        else:
            print("录制启动失败：%s" % result.get("error"), file=sys.stderr)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    hub.start()
    from .webapp import create_server, detect_lan_ip

    host = args.host or config.get("web", "host", "0.0.0.0")
    port = args.port or config.get_int("web", "port", 8080)
    server = create_server(hub, host, port, verbose=args.verbose)
    server.timeout = 0.5  # 让 handle_request 定期返回，便于响应 Ctrl+C
    shown = detect_lan_ip() if host in ("0.0.0.0", "::") else host
    print("radarpi Web 上位机已启动：http://%s:%d/" % (shown, port))
    print("数据源：%s；浏览器打开上面的地址即可查看点云，Ctrl+C 退出。" % source.status().get("device", "?"))
    try:
        while not stop.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        hub.stop()
    return 0


def cmd_serve(args, config: Config) -> int:
    source = build_source(args, config, for_web=True)
    return _serve(args, config, source)


def cmd_view(args, config: Config) -> int:
    """pygame 桌面版上位机（接显示器 / VNC 时用）。"""
    from .viewer_pygame import run_viewer

    return run_viewer(args, config)


def cmd_send(args, config: Config) -> int:
    """下发指令。

    ``radarpi send scan start``                按原文发送
    ``radarpi send "scan start; scan stop"``   分号分隔可一次发多条
    ``radarpi send --key boundary 3 3 1 1 5``  按指令名+参数发送
    """
    device = args.device or config.get("serial", "device", "auto")
    baud = int(args.baud or config.get_int("serial", "baudrate", BAUD_RATE))

    # 先校验指令本身，再动串口 —— 参数写错时不该先抱怨"打不开串口"
    payloads: List[bytes] = []
    try:
        if args.key:
            payloads = [P.command_from_key(args.key, args.rest)]
        else:
            text = " ".join(args.rest).strip()
            if text:
                payloads = [P.build_command(part) for part in text.split(";") if part.strip()]
        if not payloads:
            print('用法：radarpi send "scan start"  或  radarpi send --key boundary 3 3 1 1 5')
            return 1
    except (KeyError, ValueError) as exc:
        print("指令有误：%s" % exc, file=sys.stderr)
        return 2

    try:
        port = open_port(device, baud, timeout=0.2)
    except SerialError as exc:
        print("无法打开串口：%s" % exc, file=sys.stderr)
        print("提示：运行 `radarpi doctor` 查看原因。", file=sys.stderr)
        return 2

    try:
        for data in payloads:
            text = data.decode("ascii", "replace").strip()
            port.write(data)
            print("已发送：%s（%d 字节，含 CRLF）" % (text, len(data)))
            time.sleep(0.05)
        if args.listen:
            print("监听 %0.1f 秒内的返回数据…" % args.listen)
            deadline = time.monotonic() + args.listen
            parser = P.RadarStreamParser()
            total = 0
            frames = 0
            while time.monotonic() < deadline:
                chunk = port.read(65536)
                if not chunk:
                    continue
                total += len(chunk)
                frames += len(parser.feed(chunk))
            print("收到 %d 字节，解析出 %d 帧。" % (total, frames))
    except SerialError as exc:
        print("发送失败：%s" % exc, file=sys.stderr)
        return 2
    finally:
        port.close()
    return 0


def cmd_apply(args, config: Config) -> int:
    """把配置文件里的雷达参数一次性下发给雷达。"""
    profile = config.radar_profile()
    if not profile:
        print("配置里没有需要下发的雷达参数（radar.apply_on_connect=false 或各项为空）。")
        return 0
    device = args.device or config.get("serial", "device", "auto")
    baud = int(args.baud or config.get_int("serial", "baudrate", BAUD_RATE))
    try:
        port = open_port(device, baud, timeout=0.2)
    except SerialError as exc:
        print("无法打开串口：%s" % exc, file=sys.stderr)
        print("提示：运行 `radarpi doctor` 查看原因。", file=sys.stderr)
        return 2
    try:
        for line in profile:
            port.write(P.build_command(line))
            print("已发送：%s" % line)
            time.sleep(0.05)
        if config.get_bool("serial", "auto_start", True):
            port.write(P.build_command("scan start"))
            print("已发送：scan start")
    finally:
        port.close()
    return 0


def cmd_config(args, config: Config) -> int:
    if args.action == "path":
        print(config.path)
        print("（已加载：%s）" % (config.loaded_from or "内置默认值"))
        return 0
    if args.action == "list":
        section = None
        for item in config.describe():
            if item["section"] != section:
                section = item["section"]
                print("\n[%s] %s" % (section, item["section_title"]))
            print("  %-22s = %-28s %s" % (item["key"], item["value"], item["hint"]))
        return 0
    if args.action == "get":
        for key in args.keys:
            section, _, name = key.partition(".")
            if not name:
                print("格式应为 section.key，例如 radar.boundary", file=sys.stderr)
                return 2
            print("%s = %s" % (key, config.get(section, name)))
        return 0
    if args.action == "set":
        if len(args.keys) < 2:
            print("用法：radarpi config set section.key value", file=sys.stderr)
            return 2
        *pairs, value = args.keys
        for key in pairs:
            section, _, name = key.partition(".")
            if not name:
                print("格式应为 section.key，例如 radar.boundary", file=sys.stderr)
                return 2
            config.set(section, name, value)
            print("%s = %s" % (key, value))
        path = config.save()
        print("已保存到 %s" % path)
        return 0
    print("未知操作：%s" % args.action, file=sys.stderr)
    return 2


def cmd_sniff(args, config: Config) -> int:
    """原始抓包：只按包头切分并打印十六进制，用来核对协议实现。

    这是把新版本雷达接上树莓派后最先该跑的命令 —— 如果这里显示的包头与手册
    不一致，说明固件协议有变化，需要更新 protocol.py 里的常量。
    """
    device = args.device or config.get("serial", "device", "auto")
    baud = int(args.baud or config.get_int("serial", "baudrate", BAUD_RATE))
    try:
        port = open_port(device, baud, timeout=0.2)
    except SerialError as exc:
        print("无法打开串口：%s" % exc, file=sys.stderr)
        return 2
    print("抓包 %s @ %d 8N1，%0.1f 秒…" % (port.device, baud, args.seconds))
    parser = P.RadarStreamParser()
    deadline = time.monotonic() + args.seconds
    total = 0
    frames = 0
    hex_shown = 0
    try:
        while time.monotonic() < deadline:
            chunk = port.read(65536)
            if not chunk:
                continue
            total += len(chunk)
            if args.hex and hex_shown < args.hex_lines:
                for i in range(0, min(len(chunk), 64), 16):
                    print("  %s  |%s|" % (
                        " ".join("%02X" % b for b in chunk[i:i + 16]),
                        "".join(chr(b) if 32 <= b < 127 else "." for b in chunk[i:i + 16]),
                    ))
                    hex_shown += 1
            for frame in parser.feed(chunk):
                frames += 1
                if frames <= args.frames:
                    print("帧 #%d：%s" % (frame.frame_id, json.dumps(frame.header.as_dict(), ensure_ascii=False)))
    finally:
        port.close()
    print("共收到 %d 字节，解析出 %d 帧。" % (total, frames))
    print("解析统计：%s" % json.dumps(parser.stats.as_dict(), ensure_ascii=False))
    if total and frames == 0:
        print("提示：有数据但无帧。请核对原始报文里是否出现 FFEEFFDC（帧头包头）与 FFDDFECB（点云包头）。")
        print("      若字节顺序相反（DC FF EE FF），说明固件使用大端，可先用 --hex 观察后反馈。")
    return 0


def cmd_version(args, config: Config) -> int:
    print("radarpi %s" % __version__)
    print("目标平台：Raspberry Pi 4B / Raspberry Pi OS (aarch64)")
    print("串口参数：%d 8N1" % BAUD_RATE)
    return 0


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


def _add_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-d", "--device", help="串口设备，默认 auto（自动识别）")
    p.add_argument("-b", "--baud", type=int, help="波特率，默认 3000000")
    p.add_argument("--simulate", action="store_true", help="使用内置仿真数据（无雷达也能演示）")
    p.add_argument("--replay", metavar="FILE", help="回放录制文件（csv/jsonl，支持通配符）")
    p.add_argument("--loop", action="store_true", help="回放循环")
    p.add_argument("--speed", type=float, default=1.0, help="回放速度倍率")
    p.add_argument("--fps", type=float, default=10.0, help="仿真帧率")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radarpi",
        description="4D 成像毫米波雷达树莓派上位机（Raspberry Pi OS）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", help="指定配置文件路径")
    parser.add_argument("-V", "--version", action="version", version="radarpi %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="命令")

    p = sub.add_parser("doctor", help="环境体检（先跑这个）")
    p.add_argument("-d", "--device", default="auto", help="要联调的串口设备")
    p.add_argument("--probe", action="store_true", help="实际打开串口读几秒，验证链路")
    p.add_argument("--seconds", type=float, default=3.0, help="联调时长（秒）")
    p.add_argument("--json", action="store_true", help="输出 JSON")

    p = sub.add_parser("ports", help="列出串口设备")
    p.add_argument("--json", action="store_true", help="输出 JSON")

    p = sub.add_parser("monitor", help="终端查看实时点云统计")
    _add_source_args(p)
    p.add_argument("--json", action="store_true", help="每帧输出一行 JSON")
    p.add_argument("--raw", action="store_true", help="逐点打印（核对解析结果）")
    p.add_argument("--duration", type=float, help="运行时长（秒），默认一直运行")

    p = sub.add_parser("record", help="录制点云到文件")
    _add_source_args(p)
    p.add_argument("output", nargs="?", help="输出文件；省略则按时间自动命名")
    p.add_argument("-f", "--format", choices=("csv", "jsonl"), help="录制格式")
    p.add_argument("--duration", type=float, help="录制时长（秒）")
    p.add_argument("-q", "--quiet", action="store_true", help="不显示进度")

    p = sub.add_parser("replay", help="回放录制文件")
    p.add_argument("file", help="录制文件（csv/jsonl，支持通配符）")
    p.add_argument("--loop", action="store_true", help="循环回放")
    p.add_argument("--speed", type=float, default=1.0, help="速度倍率")
    p.add_argument("--serve", action="store_true", help="在 Web 上位机里回放")
    p.add_argument("--max-frames", type=int, help="最多回放多少帧")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")

    p = sub.add_parser("serve", help="启动 Web 上位机")
    _add_source_args(p)
    p.add_argument("--host", help="监听地址，默认 0.0.0.0")
    p.add_argument("--port", type=int, help="监听端口，默认 8080")
    p.add_argument("--verbose", action="store_true", help="打印 HTTP 访问日志")
    p.add_argument("--record-to", metavar="FILE", help="启动时同时开始录制到指定文件")

    p = sub.add_parser("view", help="pygame 桌面版上位机（需显卡/显示器或 VNC）")
    _add_source_args(p)
    p.add_argument("--width", type=int, default=1280, help="窗口宽度")
    p.add_argument("--height", type=int, default=720, help="窗口高度")
    p.add_argument("-f", "--fullscreen", action="store_true", help="全屏")
    p.add_argument("--max-points", type=int, default=4000, help="每帧最多绘制多少点")
    p.add_argument("--bev-resolution", type=float, default=0.1, help="BEV 矩阵分辨率（m/格）")
    p.add_argument("--bev-mode", default="count",
                   choices=("count", "max_snr", "max_abs_v", "mean_v", "min_dist"),
                   help="BEV 矩阵每格填什么值")
    p.add_argument("--processor", metavar="模块:函数",
                   help="每帧调用一次的外部算法，如 ./my_algo.py:process")
    p.add_argument("--buffer", type=int, default=30, help="“存矩阵”保留最近多少帧")
    p.add_argument("--record-format", default="csv", choices=("csv", "jsonl"), help="录制格式")
    p.add_argument("--record-dir", help="录制/存矩阵目录，默认取配置文件")
    p.add_argument("--snapshot", metavar="PNG", help="画够 N 帧后截图并退出（无显示器自检用）")
    p.add_argument("--snapshot-frames", type=int, default=60, help="截图前先画多少帧")
    p.add_argument("--exit-after", type=float, default=0.0, help="运行多少秒后自动退出（0=不退出）")

    p = sub.add_parser("send", help="下发一条指令给雷达")
    p.add_argument("rest", nargs="*", help='指令原文（如 scan start），或配合 --key 时的参数')
    p.add_argument("--key", help="按指令名发送，例如 boundary")
    p.add_argument("-d", "--device")
    p.add_argument("-b", "--baud", type=int)
    p.add_argument("--listen", type=float, default=0.0, help="发送后监听返回数据若干秒")

    p = sub.add_parser("apply", help="把配置文件里的雷达参数下发到雷达")
    p.add_argument("-d", "--device")
    p.add_argument("-b", "--baud", type=int)
    p.add_argument("--listen", type=float, default=0.5)

    p = sub.add_parser("config", help="查看/修改配置")
    p.add_argument("action", choices=("list", "get", "set", "path"), help="操作")
    p.add_argument("keys", nargs="*", help="section.key [value]")

    p = sub.add_parser("sniff", help="原始抓包核对协议")
    p.add_argument("-d", "--device")
    p.add_argument("-b", "--baud", type=int)
    p.add_argument("--seconds", type=float, default=5.0, help="抓包时长")
    p.add_argument("--hex", action="store_true", help="同时打印原始十六进制")
    p.add_argument("--hex-lines", type=int, default=8, help="最多打印多少行十六进制")
    p.add_argument("--frames", type=int, default=3, help="最多打印多少帧的帧头")

    sub.add_parser("version", help="显示版本")
    return parser


HANDLERS = {
    "doctor": cmd_doctor,
    "ports": cmd_ports,
    "monitor": cmd_monitor,
    "record": cmd_record,
    "replay": cmd_replay,
    "serve": cmd_serve,
    "view": cmd_view,
    "send": cmd_send,
    "apply": cmd_apply,
    "config": cmd_config,
    "sniff": cmd_sniff,
    "version": cmd_version,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    config = Config(args.config)
    handler = HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args, config)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except (SerialError, FileNotFoundError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        print("提示：运行 `radarpi doctor` 可以做一次完整体检。", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
