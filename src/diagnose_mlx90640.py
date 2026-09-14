#!/usr/bin/env python3
"""MLX90640 原始 I2C 诊断（smbus2 直连，绕开 Adafruit/Blinka）。

用途：定位树莓派上读取 MLX90640 RAM 帧全 0 的问题。用 smbus2 的 i2c_rdwr
（原始 I2C_RDWR ioctl）直接读，没有 SMBus block read 的 32 字节上限。

运行：
    python diagnose_mlx90640.py            # 默认 I2C1
    MLX90640_I2C_BUS=3 python diagnose_mlx90640.py   # 其它总线
"""

from __future__ import annotations

import argparse
import os
import time

ADDR = 0x33


def read_words(bus, addr: int, count: int) -> list[int]:
    """从 addr 连续读 count 个字（16bit，repeated-start 读取）。"""
    import smbus2

    cmd = bytes([(addr >> 8) & 0xFF, addr & 0xFF])
    write_msg = smbus2.i2c_msg.write(ADDR, cmd)
    read_msg = smbus2.i2c_msg.read(ADDR, count * 2)
    bus.i2c_rdwr(write_msg, read_msg)
    raw = bytes(read_msg.buf[: count * 2])
    words = [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]
    return words


def write_word(bus, addr: int, value: int) -> None:
    import smbus2

    cmd = bytes([(addr >> 8) & 0xFF, addr & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
    bus.i2c_rdwr(smbus2.i2c_msg.write(ADDR, cmd))


def main() -> int:
    parser = argparse.ArgumentParser(description="MLX90640 原始 I2C 诊断")
    parser.add_argument("--bus", type=int, default=int(os.getenv("MLX90640_I2C_BUS", "1")), help="I2C 总线号（默认：1）")
    args = parser.parse_args()

    import smbus2

    bus = smbus2.SMBus(args.bus)

    # 1) EEPROM 前 8 字（地址 0x2400）
    ee = read_words(bus, 0x2400, 8)
    print(f"[1] EEPROM[0x2400] 前 8 字: {[hex(w) for w in ee]}")

    # 2) 序列号寄存器 0x2407（3 字）
    serial = read_words(bus, 0x2407, 3)
    print(f"[2] 序列号 0x2407: {[hex(w) for w in serial]}")

    # 3) 状态寄存器 0x8000（等 dataReady）
    for attempt in range(50):
        status = read_words(bus, 0x8000, 1)[0]
        if status & 0x0008:
            print(f"[3] 状态寄存器 0x8000 = 0x{status:04X}（dataReady=1，第 {attempt + 1} 次轮询）")
            break
        time.sleep(0.05)
    else:
        status = read_words(bus, 0x8000, 1)[0]
        print(f"[3] 状态寄存器 0x8000 = 0x{status:04X}（50 次轮询仍无 dataReady）")
        return 2

    # 4) 触发读帧：写 0x8000 = 0x0030
    write_word(bus, 0x8000, 0x0030)
    time.sleep(0.01)

    # 5) 读 RAM 0x0400 的 832 字（1664 字节）
    ram = read_words(bus, 0x0400, 832)
    nonzero = sum(1 for w in ram if w != 0)
    print(f"[5] RAM[0x0400] 共 {len(ram)} 字，非零 {nonzero} 字，前 8 字: {[hex(w) for w in ram[:8]]}")
    # 关键位置：ptatArt=ram[768], ptat=ram[800], vdd=ram[810], gain=ram[778]
    for name, idx in (("ptatArt", 768), ("ptat", 800), ("vdd", 810), ("gain", 778)):
        print(f"    {name:8s} ram[{idx}] = 0x{ram[idx]:04X}")

    # 6) 再读状态寄存器，确认读帧后 dataReady 清零
    status2 = read_words(bus, 0x8000, 1)[0]
    print(f"[6] 读帧后状态寄存器 0x8000 = 0x{status2:04X}")

    if nonzero == 0:
        print("\n结论：RAM 全 0 → 硬件 I2C 控制器时钟拉伸超时，需用软件 I2C(i2c-gpio)。")
        return 1
    print(f"\n结论：RAM 有 {nonzero} 个非零字，smbus2 路径可正常读取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
