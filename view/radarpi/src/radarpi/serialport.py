"""串口访问层。

设计目标：在 Raspberry Pi OS 上无需任何第三方依赖即可打开 3000000 8N1 的串口。
因此这里有两套后端：

* 优先使用 ``pyserial``（若已安装，功能最完整，也便于在 Windows 上开发调试）；
* 否则退回到纯标准库实现（``os`` + ``termios`` + ``select``），Linux 上完全够用。

Linux 内核 termios 从 asm-generic/termbits.h 起就定义了 B3000000，CPython 的
``termios`` 模块会导出该常量，所以 3 Mbit/s 不需要 pyserial 也能设置。
"""

from __future__ import annotations

import os
import select
import struct
import sys
import time
from dataclasses import dataclass
from glob import glob
from typing import Dict, List, Optional

BAUD_RATE = 3000000
"""雷达固定波特率（手册第四节）。"""

#: 已知可用的 USB 转串口芯片 VID，用于自动识别雷达所在端口
KNOWN_VIDS = {
    "1a86": "WCH(CH343/CH344/CH9102)",
    "0403": "FTDI(FT232/FT2232/FT4232)",
    "10c4": "Silicon Labs(CP210x)",
    "067b": "Prolific(PL2303)",
}

#: 串口设备节点的候选前缀（HAL 与厂商驱动命名不同）
DEVICE_GLOBS = (
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
    "/dev/ttyCH343USB*",
    "/dev/ttyCH9344USB*",
    "/dev/ttyAMA*",
    "/dev/ttyS*",
)

#: 厂商内核驱动的可能名称，用于 doctor 诊断
KERNEL_DRIVERS = ("ch343", "ch341", "ftdi_sio", "cdc_acm", "cp210x", "pl2303")


class SerialError(RuntimeError):
    """串口打开或读写失败。"""


@dataclass
class PortInfo:
    """一个可用串口。"""

    device: str
    description: str = ""
    vid: str = ""
    pid: str = ""
    driver: str = ""
    by_id: str = ""

    @property
    def is_likely_radar(self) -> bool:
        return self.vid.lower() in KNOWN_VIDS

    def as_dict(self) -> dict:
        return {
            "device": self.device,
            "description": self.description,
            "vid": self.vid,
            "pid": self.pid,
            "driver": self.driver,
            "by_id": self.by_id,
            "likely_radar": self.is_likely_radar,
        }

    def label(self) -> str:
        bits = [self.device]
        if self.vid:
            bits.append("%s:%s" % (self.vid, self.pid))
        if self.driver:
            bits.append("驱动=%s" % self.driver)
        if self.description:
            bits.append(self.description)
        return "  ".join(bits)


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _sysfs_port_info(device: str) -> Dict[str, str]:
    """从 sysfs 读取设备的 USB 描述、VID/PID 与已绑定驱动。"""
    info = {"vid": "", "pid": "", "driver": "", "description": ""}
    name = os.path.basename(device)
    base = "/sys/class/tty/%s/device" % name
    if not os.path.exists(base):
        return info

    # 已绑定驱动：/sys/class/tty/ttyUSB0/device/driver -> .../drivers/ch341
    link = os.path.join(base, "driver")
    if os.path.islink(link):
        info["driver"] = os.path.basename(os.path.realpath(link))

    # 沿 USB 设备树向上找 idVendor/idProduct 与产品名
    node = os.path.realpath(base)
    for _ in range(6):
        node = os.path.dirname(node)
        if not node or node == "/":
            break
        vid = _read_text(os.path.join(node, "idVendor"))
        if vid:
            info["vid"] = vid
            info["pid"] = _read_text(os.path.join(node, "idProduct"))
            product = _read_text(os.path.join(node, "product"))
            manufacturer = _read_text(os.path.join(node, "manufacturer"))
            info["description"] = " ".join(x for x in (manufacturer, product) if x)
            break
    if not info["description"]:
        # 非 USB 串口（例如 GPIO 上的 /dev/ttyAMA0）
        info["description"] = _read_text(os.path.join(base, "uevent")).replace("\n", " ")[:80]
    return info


def list_ports(include_virtual: bool = False) -> List[PortInfo]:
    """枚举本机串口。

    默认只返回真实串口，额外包含 ``/dev/serial/by-id`` 里的稳定别名。
    """
    ports: Dict[str, PortInfo] = {}

    # 先收录 by-id 稳定别名（USB 插拔后设备名可能变化，by-id 不变）
    by_id_map: Dict[str, str] = {}
    for link in glob("/dev/serial/by-id/*"):
        try:
            real = os.path.realpath(link)
        except OSError:
            continue
        by_id_map[os.path.realpath(real)] = link

    for pattern in DEVICE_GLOBS:
        for device in sorted(glob(pattern)):
            if not os.path.exists(device):
                continue
            name = os.path.basename(device)
            if not include_virtual:
                if name.startswith("ttyS") and not _read_text("/sys/class/tty/%s/device/uevent" % name).startswith("DRIVER="):
                    continue
                if name.startswith("ttyAMA") and not os.path.exists("/sys/class/tty/%s/device" % name):
                    continue
                if name in ("ttyS0",) and os.path.exists("/dev/ttyAMA0"):
                    pass
            info = _sysfs_port_info(device)
            ports[device] = PortInfo(
                device=device,
                description=info["description"],
                vid=info["vid"],
                pid=info["pid"],
                driver=info["driver"],
                by_id=by_id_map.get(os.path.realpath(device), ""),
            )

    # 顺带用 pyserial 的描述信息补全（若可用）
    try:
        from serial.tools import list_ports as ps_list_ports

        for p in ps_list_ports.comports():
            if p.device in ports:
                if not ports[p.device].description:
                    ports[p.device].description = p.description or ""
                if not ports[p.device].vid and p.vid is not None:
                    ports[p.device].vid = "%04x" % p.vid
                    ports[p.device].pid = "%04x" % (p.pid or 0)
            elif p.device and os.path.exists(p.device):
                ports[p.device] = PortInfo(
                    device=p.device,
                    description=p.description or "",
                    vid="%04x" % p.vid if p.vid is not None else "",
                    pid="%04x" % (p.pid or 0) if p.pid is not None else "",
                )
    except Exception:  # pragma: no cover - pyserial 不存在时正常走这里
        pass

    return sorted(ports.values(), key=lambda x: x.device)


def candidate_devices() -> List[str]:
    """按「最可能是雷达」的顺序返回候选设备节点。"""
    ports = list_ports()
    known = [p for p in ports if p.is_likely_radar]
    others = [p for p in ports if not p.is_likely_radar and p.driver in ("ch343", "ch341", "ftdi_sio", "cdc_acm")]
    rest = [p for p in ports if p not in known and p not in others and not p.device.startswith("/dev/ttyS")]
    ordered = known + others + rest
    out: List[str] = []
    for p in ordered:
        if p.by_id:
            out.append(p.by_id)
        out.append(p.device)
    return out


def resolve_device(device: str) -> str:
    """解析 ``auto`` 与 ``/dev/serial/by-id/...`` 之类的写法。"""
    if device and device.lower() not in ("auto", ""):
        return device
    candidates = candidate_devices()
    if not candidates:
        raise SerialError(
            "没有发现串口设备。请确认 USB 转串口小板已插好，然后运行 `radarpi doctor` 查看诊断。"
        )
    return candidates[0]


# --------------------------------------------------------------------------
# 后端一：纯标准库（Linux）
# --------------------------------------------------------------------------


class PosixSerial:
    """基于 os/termios 的串口实现，无第三方依赖。"""

    def __init__(self, device: str, baudrate: int = BAUD_RATE, timeout: float = 0.2) -> None:
        import termios  # 只在 Linux 上可用

        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        try:
            self._fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialError("打开 %s 失败：%s" % (device, exc)) from exc

        speed = getattr(termios, "B%d" % baudrate, None)
        if speed is None:
            self.close()
            raise SerialError(
                "本机 termios 不支持 %d 波特率常量 B%d（内核过旧？）" % (baudrate, baudrate)
            )
        try:
            iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(self._fd)
            iflag = 0
            oflag = 0
            lflag = 0
            cflag = termios.CS8 | termios.CREAD | termios.CLOCAL  # 8N1，忽略调制解调器线
            cc = list(cc)
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])
            termios.tcflush(self._fd, termios.TCIOFLUSH)
        except termios.error as exc:
            self.close()
            raise SerialError("配置 %s 失败：%s" % (device, exc)) from exc

        self._tune_latency()

    def _tune_latency(self) -> None:
        """把 USB 串口延迟定时器调到 1ms，降低点云到达的抖动。"""
        name = os.path.basename(self.device)
        path = "/sys/bus/usb-serial/devices/%s/latency_timer" % name
        try:
            with open(path, "w", encoding="ascii") as fh:
                fh.write("1\n")
        except OSError:
            pass  # 非 USB 串口或权限不足时忽略

    # -- 读写 -------------------------------------------------------------

    @property
    def in_waiting(self) -> int:
        import fcntl

        if self._fd is None:
            return 0
        try:
            buf = fcntl.ioctl(self._fd, 0x541B, b"\x00\x00\x00\x00")  # FIONREAD
            return struct.unpack("I", buf)[0]
        except (OSError, ValueError):
            return 0

    def read(self, size: int = 4096) -> bytes:
        # 退出竞态防护：主线程 close() 后 _fd 会被置 None，此时直接返回空，
        # 避免 select/os.read 收到 None 描述符抛出 TypeError。
        fd = self._fd
        if fd is None:
            return b""
        try:
            ready, _, _ = select.select([fd], [], [], self.timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return os.read(fd, size)
        except (BlockingIOError, OSError):
            return b""

    def read_exact_wait(self, nbytes: int, timeout: float) -> bytes:
        """尽量读满 nbytes 字节（读不满也返回已读到的内容）。"""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < nbytes and time.monotonic() < deadline:
            chunk = self.read(min(65536, nbytes - len(buf)))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def write(self, data: bytes) -> int:
        total = 0
        while total < len(data):
            _, writable, _ = select.select([], [self._fd], [], 1.0)
            if not writable:
                raise SerialError("写入 %s 超时" % self.device)
            total += os.write(self._fd, data[total:])
        return total

    def flush_input(self) -> None:
        import termios

        try:
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def close(self) -> None:
        fd = getattr(self, "_fd", None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "PosixSerial":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# 后端二：pyserial
# --------------------------------------------------------------------------


class PySerialPort:
    """pyserial 包装，接口与 :class:`PosixSerial` 保持一致。"""

    def __init__(self, device: str, baudrate: int = BAUD_RATE, timeout: float = 0.2) -> None:
        import serial

        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        try:
            self._port = serial.Serial(
                port=device,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
                write_timeout=1.0,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
        except Exception as exc:  # serial.SerialException 等
            raise SerialError("打开 %s 失败：%s" % (device, exc)) from exc
        self._port.reset_input_buffer()

    @property
    def in_waiting(self) -> int:
        return self._port.in_waiting

    def read(self, size: int = 4096) -> bytes:
        return self._port.read(size)

    def read_exact_wait(self, nbytes: int, timeout: float) -> bytes:
        return self._port.read(nbytes) or b""

    def write(self, data: bytes) -> int:
        return self._port.write(data)

    def flush_input(self) -> None:
        self._port.reset_input_buffer()

    def close(self) -> None:
        try:
            self._port.close()
        except Exception:
            pass

    def __enter__(self) -> "PySerialPort":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------


def open_port(device: str, baudrate: int = BAUD_RATE, timeout: float = 0.2, prefer_pyserial: bool = True):
    """打开串口，自动挑选可用后端。"""
    device = resolve_device(device)
    errors: List[str] = []
    if prefer_pyserial:
        try:
            return PySerialPort(device, baudrate, timeout)
        except SerialError as exc:
            errors.append(str(exc))
        except ImportError:
            pass
    if sys.platform.startswith("linux"):
        try:
            return PosixSerial(device, baudrate, timeout)
        except SerialError as exc:
            errors.append(str(exc))
    raise SerialError("；".join(errors) or ("无法打开 %s" % device))


def backend_name() -> str:
    """返回当前会使用的后端名称，供 doctor 显示。"""
    try:
        import serial  # noqa: F401

        return "pyserial"
    except ImportError:
        return "stdlib(termios)" if sys.platform.startswith("linux") else "none"
