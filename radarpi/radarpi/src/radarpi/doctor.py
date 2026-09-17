"""环境体检：把「为什么连不上雷达」这类问题尽量一次性说清楚。

``radarpi doctor`` 会逐项检查串口驱动、权限、波特率支持、被系统服务抢占等情况，
并针对 Raspberry Pi OS 给出具体的修复命令。
"""

from __future__ import annotations

import glob
import os
import platform
import subprocess
import sys
import time
from typing import Dict, List, Optional

from . import protocol as P
from .serialport import BAUD_RATE, KNOWN_VIDS, SerialError, backend_name, list_ports, open_port

OK = "ok"
WARN = "warn"
FAIL = "fail"
INFO = "info"

_MARK = {OK: "[ OK ]", WARN: "[警告]", FAIL: "[失败]", INFO: "[信息]"}


def _run(cmd: List[str], timeout: float = 5.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _check(name: str, status: str, detail: str, fix: str = "") -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------


def check_system() -> List[Dict[str, str]]:
    model = _read("/proc/device-tree/model").rstrip("\x00")
    if not model:
        model = platform.machine() or "未知设备"
    os_release = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            os_release[k] = v.strip().strip('"')
    kernel = platform.release()
    core = os.cpu_count() or 0

    items = [
        _check("硬件平台", INFO, model),
        _check("操作系统", INFO, os_release.get("PRETTY_NAME", "未知（可能不是 Raspberry Pi OS）")),
        _check("内核版本", INFO, kernel),
        _check("Python", INFO, "%s（%d 核）" % (platform.python_version(), core)),
    ]
    if "raspberry" not in model.lower() and "bcm" not in model.lower() and platform.machine() not in ("aarch64", "armv7l"):
        items.append(
            _check(
                "平台提示",
                INFO,
                "当前不是树莓派，但 radarpi 是纯 Python 实现，在 x86 上同样可以运行（用于调试）。",
            )
        )
    maj_min = tuple(int(x) for x in kernel.split("-")[0].split(".")[:2] if x.isdigit()) if kernel else (0, 0)
    if maj_min and maj_min < (5, 10):
        items.append(
            _check(
                "内核版本偏低",
                WARN,
                "内核 %s 较旧，CH34x 高波特率串口驱动可能缺失。" % kernel,
                "建议升级 Raspberry Pi OS 到 Bookworm（内核 6.x）或更新固件。",
            )
        )
    return items


def check_python_env() -> List[Dict[str, str]]:
    items = [_check("串口后端", INFO, backend_name())]
    if backend_name() == "pyserial":
        items.append(_check("pyserial", OK, "已安装，使用 pyserial 访问串口。"))
    else:
        items.append(
            _check(
                "pyserial",
                INFO,
                "未安装，已自动使用内置的标准库串口实现（功能完整，无需安装）。",
                "如需 pyserial：sudo apt install python3-serial",
            )
        )
    host = sys.platform.startswith("linux")
    items.append(_check("运行平台", OK if host else WARN, "Linux" if host else "非 Linux（仅标准库后端不可用）"))
    if host:
        try:
            import termios

            has = hasattr(termios, "B3000000")
            items.append(
                _check(
                    "3000000 波特率支持",
                    OK if has else FAIL,
                    "termios 提供 B3000000（0x100d）。" if has else "termios 中没有 B3000000 常量。",
                    "" if has else "内核过旧，请升级系统。",
                )
            )
        except ImportError:
            items.append(_check("termios", FAIL, "无法导入 termios。"))
    return items


def _is_root() -> bool:
    """Windows 上没有 geteuid，这里统一做个安全判断。"""
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def check_permissions() -> List[Dict[str, str]]:
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or os.environ.get("LOGNAME") or "?"
    groups = _run(["id", "-nG", user]).split()
    in_dialout = "dialout" in groups or _is_root()
    items = [
        _check(
            "用户串口权限",
            OK if in_dialout else WARN,
            "用户 %s 属于：%s" % (user, " ".join(groups) or "未知"),
            "" if in_dialout else "执行：sudo usermod -aG dialout %s && 重新登录（或重启）" % user,
        )
    ]
    return items


def check_serial_devices() -> List[Dict[str, str]]:
    ports = list_ports()
    items: List[Dict[str, str]] = []
    if not ports:
        items.append(
            _check(
                "串口设备",
                FAIL,
                "没有找到任何串口设备节点。",
                "1) 检查 USB 转串口小板是否插好、TX/RX 是否交叉；"
                "2) 执行 lsusb 看是否识别到芯片；3) 执行 dmesg | tail 看内核报错。",
            )
        )
        usb = _run(["lsusb"])
        if usb.strip():
            interesting = [l for l in usb.splitlines() if any(v in l.lower() for v in KNOWN_VIDS) or "serial" in l.lower()]
            items.append(_check("lsusb", INFO, "; ".join(interesting) if interesting else usb.strip().replace("\n", " | ")[:300]))
        return items

    for p in ports:
        if p.is_likely_radar:
            chip = KNOWN_VIDS.get(p.vid.lower(), "USB 串口")
            status = OK
            detail = "%s（%s，%s）" % (p.device, chip, p.vid + ":" + p.pid)
            fix = ""
            if p.vid.lower() == "1a86" and p.driver in ("", "unknown"):
                status = WARN
                detail += " —— 未绑定内核驱动"
                fix = (
                    "CH343/CH344 不在 Linux 主线上。若 /dev/ttyACM* 也未出现，"
                    "请按《使用说明》附录安装 WCH 厂商驱动，或改用 FT232 串口小板。"
                )
            elif p.vid.lower() == "1a86" and p.driver == "cdc_acm":
                detail += "（由 cdc_acm 接管，可用）"
        else:
            status = INFO
            detail = "%s（%s%s）" % (p.device, p.description or "未知设备", ("，驱动=" + p.driver) if p.driver else "")
            fix = ""
        items.append(_check("串口设备", status, detail, fix))
    return items


def check_kernel_drivers() -> List[Dict[str, str]]:
    mod_dir = "/lib/modules/%s/kernel/drivers/usb/serial" % platform.release()
    items: List[Dict[str, str]] = []
    if not os.path.isdir(mod_dir):
        items.append(_check("内核驱动模块", INFO, "未找到 %s（容器或非标准内核？）" % mod_dir))
        return items
    found = {os.path.basename(f).split(".")[0] for f in glob.glob(os.path.join(mod_dir, "*.ko*"))}
    for mod in ("ftdi_sio", "ch341", "ch343", "cdc_acm", "cp210x"):
        if mod in found:
            items.append(_check("驱动 %s" % mod, OK, "内核自带"))
        elif mod == "ch343":
            items.append(
                _check(
                    "驱动 ch343",
                    INFO,
                    "内核未自带（主线内核没有 CH343 驱动，属正常现象）",
                    "使用 FT232 串口小板可免装驱动；若必须用 CH343，见《使用说明》附录。",
                )
            )
    return items


def check_service_conflicts() -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    dpkg = _run(["dpkg", "-l", "brltty"])
    installed = "\nii " in ("\n" + dpkg)
    rules = "/usr/lib/udev/rules.d/85-brltty.rules"
    disabled = glob.glob("/usr/lib/udev/rules.d/*brltty*.rules.disabled*") or glob.glob("/etc/udev/rules.d/*brltty*")
    if installed:
        if os.path.exists(rules):
            items.append(
                _check(
                    "brltty 冲突",
                    WARN,
                    "系统装有 brltty，其 udev 规则会抢占部分 USB 串口（CH340/FT232 常见）。",
                    "执行：sudo apt remove brltty  （或用安装包自带的 radarpi udev 规则屏蔽）",
                )
            )
        else:
            items.append(_check("brltty 冲突", OK, "brltty 已安装但规则已屏蔽。"))
    else:
        items.append(_check("brltty 冲突", OK, "未安装 brltty。"))

    mm = _run(["systemctl", "is-active", "ModemManager"])
    if mm.strip() == "active":
        items.append(
            _check(
                "ModemManager",
                WARN,
                "ModemManager 正在运行，可能主动探测并打开 USB 串口，导致数据被抢读。",
                "安装包已写入 udev 规则忽略此类设备；如仍有问题：sudo systemctl disable --now ModemManager",
            )
        )
    else:
        items.append(_check("ModemManager", OK, "未运行。"))

    rules_path = "/etc/udev/rules.d/99-radarpi.rules"
    if os.path.exists(rules_path):
        items.append(_check("radarpi udev 规则", OK, rules_path))
    else:
        items.append(
            _check(
                "radarpi udev 规则",
                WARN,
                "未安装 %s，串口权限与 ModemManager 忽略规则不会自动生效。" % rules_path,
                "重新执行安装脚本，或手动复制仓库中的 udev/99-radarpi.rules。",
            )
        )
    return items


def check_port_open(device: str = "auto", seconds: float = 3.0, baudrate: int = BAUD_RATE) -> Dict[str, str]:
    """真正打开串口读一会儿，确认链路通。"""
    try:
        port = open_port(device, baudrate, timeout=0.2)
    except SerialError as exc:
        return _check(
            "串口联调",
            FAIL,
            str(exc),
            "确认设备节点存在且当前用户有权限；未接雷达时本项失败属正常。",
        )
    parser = P.RadarStreamParser()
    frames = 0
    points = 0
    received = 0
    deadline = time.monotonic() + seconds
    detail = ""
    try:
        while time.monotonic() < deadline:
            chunk = port.read(65536)
            if not chunk:
                continue
            received += len(chunk)
            for frame in parser.feed(chunk):
                frames += 1
                points += len(frame.points)
        if frames == 0 and received > 0:
            detail = "收到 %d 字节但没有解析出完整帧" % received
        elif received == 0:
            detail = "%.1f 秒内没有收到任何数据" % seconds
        else:
            detail = "收到 %d 字节，解析出 %d 帧 / %d 点，统计 %s" % (
                received, frames, points, parser.stats.as_dict()
            )
    finally:
        try:
            port.close()
        except Exception:
            pass

    if frames > 0:
        return _check("串口联调", OK, detail)
    status = WARN if received > 0 else FAIL
    fix = "确认雷达已上电（电源灯常亮绿灯）且已下发 scan start。"
    return _check("串口联调", status, detail, fix)


# --------------------------------------------------------------------------


def run(device: str = "auto", probe: bool = False, probe_seconds: float = 3.0) -> List[Dict[str, str]]:
    """执行全部检查。"""
    items: List[Dict[str, str]] = []
    items += check_system()
    items += check_python_env()
    if not sys.platform.startswith("linux"):
        items.append(
            _check(
                "Linux 专属检查",
                INFO,
                "当前不是 Linux，已跳过内核驱动、dialout 权限、udev 等检查项。",
            )
        )
        items += check_serial_devices()
        return items
    items += check_permissions()
    items += check_kernel_drivers()
    items += check_serial_devices()
    items += check_service_conflicts()
    if probe:
        items.append(check_port_open(device, probe_seconds))
    return items


def summarize(items: List[Dict[str, str]]) -> Dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0, INFO: 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return counts


def format_text(items: List[Dict[str, str]]) -> str:
    lines: List[str] = []
    for it in items:
        lines.append("%s %s" % (_MARK.get(it["status"], "[????]"), it["name"]))
        if it["detail"]:
            for line in str(it["detail"]).splitlines():
                lines.append("        %s" % line)
        if it["fix"]:
            lines.append("        → %s" % it["fix"])
    counts = summarize(items)
    lines.append("")
    lines.append("小结：%d 项正常，%d 项警告，%d 项失败。" % (counts[OK], counts[WARN], counts[FAIL]))
    if counts[FAIL] or counts[WARN]:
        lines.append("按上面的「→」提示处理后重新运行 `radarpi doctor`。")
    return "\n".join(lines)
