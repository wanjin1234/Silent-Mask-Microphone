#!/usr/bin/env python3
"""MLX90640 (32 x 24) 读取器 —— 给 thermal_ai 复用的最小 I2C 传输层。

与 ``mlx90640-thermal/mlx90640_thermal_display.py`` 里的 ``Mlx90640Sensor``
使用同一套、且已被验证可用的读帧方式：

1. 默认走 ``smbus2.i2c_rdwr`` 原始 I2C 传输（绕开 Blinka ``busio.I2C`` 转发到
   SMBus 时的 32 字节块读上限；MLX90640 RAM 帧需一次读 1664 字节）。
2. **绕过 Adafruit ``getFrame`` 的 dataReady 死循环**：自己用带 2 秒超时的
   ``read_words`` 等 dataReady，再读两个子页，复用 Adafruit 的 ``_GetTa`` +
   ``_CalculateTo`` 做纯数学温度换算（不再碰 I2C）。

树莓派 4B 的 BCM2711 硬件 I2C 对时钟拉伸容忍时间过短，读 RAM 帧可能返回全 0；
此时请改用软件 I2C（见 README 的 i2c-gpio 配置），并把 ``MLX90640_I2C_BUS``
设为对应总线号。
"""

from __future__ import annotations

import math
import os
import time

WIDTH = 32
HEIGHT = 24
PIXEL_COUNT = WIDTH * HEIGHT
SENSOR_ADDRESS = 0x33

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


class _SmbusI2C:
    """把 smbus2 封装成 adafruit_bus_device 需要的 busio 兼容接口。

    只实现 ``adafruit_mlx90640`` 构造与 ``_GetTa/_CalculateTo`` 实际用到的少量
    方法；``i2c_rdwr`` 走原始 I2C_RDWR ioctl，无 SMBus block read 长度限制。
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
            return
        self._bus.i2c_rdwr(self._smbus2.i2c_msg.write(address, data))

    def readfrom_into(self, address: int, buffer, *, start: int = 0, end=None) -> None:
        length = len(buffer) if end is None else end - start
        read_msg = self._smbus2.i2c_msg.read(address, length)
        self._bus.i2c_rdwr(read_msg)
        buffer[start : start + length] = read_msg.buf[:length]

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


class ThermalCamera:
    """读出一帧 768 个摄氏温度值的 MLX90640 传感器。"""

    def __init__(self, rate_hz: float = 2.0, i2c_bus: int | None = None,
                 address: int = SENSOR_ADDRESS, i2c_frequency: int = 400_000) -> None:
        if rate_hz not in VALID_RATES:
            raise ValueError(f"rate_hz 必须是 {VALID_RATES} 之一")
        try:
            import adafruit_mlx90640
        except ImportError as exc:
            raise RuntimeError(
                "缺少硬件驱动，请先执行：python -m pip install -r requirements.txt"
            ) from exc

        self._address = address
        self._i2c = self._build_i2c(i2c_bus, i2c_frequency)
        try:
            self._mlx = adafruit_mlx90640.MLX90640(self._i2c, address=address)
            refresh = getattr(adafruit_mlx90640.RefreshRate, RATE_NAMES[rate_hz])
            self._mlx.refresh_rate = refresh
            self.serial_number = tuple(self._mlx.serial_number)
        except Exception as exc:
            deinit = getattr(self._i2c, "deinit", None)
            if callable(deinit):
                deinit()
            raise RuntimeError(
                f"无法打开 MLX90640 (I2C 7 位地址 0x{address:02X})：{exc}"
            ) from exc

    @staticmethod
    def _build_i2c(i2c_bus: int | None, i2c_frequency: int):
        use_blinka = os.getenv("MLX90640_USE_BLINKA", "0") == "1"
        if use_blinka:
            import board
            import busio

            return busio.I2C(board.SCL, board.SDA, frequency=i2c_frequency)

        bus = i2c_bus if i2c_bus is not None else int(os.getenv("MLX90640_I2C_BUS", "1"))
        return _SmbusI2C(bus, frequency=i2c_frequency)

    def read_frame(self, retries: int = 3) -> list[float]:
        """读一帧 768 个摄氏温度；失败自动重试，最终失败抛 ``RuntimeError``。"""
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                frame = self._read_once()
                if not any(v != 0.0 for v in frame):
                    raise ValueError("帧数据全为 0（传感器未就绪或 I2C 时钟拉伸超时）")
                if not all(math.isfinite(v) for v in frame):
                    raise ValueError("帧数据含非有限值")
                return frame
            except (OSError, RuntimeError, ValueError, ArithmeticError) as exc:
                last_error = exc
                time.sleep(0.05 * attempt)
        raise RuntimeError(f"连续 {retries} 次读取 MLX90640 失败：{last_error}") from last_error

    def _read_once(self) -> list[float]:
        if isinstance(self._i2c, _SmbusI2C):
            return self._read_frame_smbus()
        frame = [0.0] * PIXEL_COUNT
        self._mlx.getFrame(frame)
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
        if not all(math.isfinite(v) for v in result):
            raise ValueError("驱动返回了非有限温度值")
        return result

    def _read_subpage(self, frame_data: list[int]) -> int:
        bus = self._i2c
        addr = self._address

        # 1) 等 dataReady（bit3），带 2s 超时，避免死循环
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


def synthetic_frame(step: int, mode: str = "human", seed: int = 0) -> list[float]:
    """生成一帧模拟热像（无硬件时调试/演示用），返回 768 个摄氏温度。

    与 ``synth.py`` 的生成逻辑保持一致，但这里只按 ``step`` 简单推进位置。
    """
    import numpy as np
    rng = np.random.default_rng(seed + step)
    bg = np.full((HEIGHT, WIDTH), 20.0, np.float32)
    bg += 4.0 * np.arange(WIDTH, dtype=np.float32)[None, :] / (WIDTH - 1)
    bg += rng.normal(0.0, 0.3, (HEIGHT, WIDTH)).astype(np.float32)

    if mode == "human":
        cx = 16.0 + 10.0 * np.sin(step * 0.08)
        cy = 12.0 + 3.0 * np.cos(step * 0.05)
        yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
        body = 14.0 * np.exp(-((xx - cx) ** 2 / (2 * 4.0 ** 2) + (yy - cy) ** 2 / (2 * 7.0 ** 2)))
        head = 2.0 * np.exp(-((xx - cx) ** 2 / (2 * 2.0 ** 2) + (yy - (cy - 8)) ** 2 / (2 * 2.0 ** 2)))
        bg = bg + body.astype(np.float32) + head.astype(np.float32)
    elif mode == "hot":
        cx, cy = rng.uniform(4, 28), rng.uniform(3, 21)
        yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
        obj = 30.0 * np.exp(-((xx - cx) ** 2 / (2 * 1.5 ** 2) + (yy - cy) ** 2 / (2 * 1.5 ** 2)))
        bg = bg + obj.astype(np.float32)

    return bg.reshape(-1).tolist()
