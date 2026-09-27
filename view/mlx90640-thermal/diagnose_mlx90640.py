#!/usr/bin/env python3
"""MLX90640 分步诊断（软件 I2C 总线，smbus2 直连）。

逐项打印 EEPROM / 序列号 / 状态寄存器 dataReady / RAM 帧，用于精确定位
getFrame 卡住的位置。每步之间用明显标记分隔，哪一步卡住一看便知。

运行（软件 I2C 总线 3）：
    export MLX90640_I2C_BUS=3
    python diagnose_mlx90640.py
"""

from __future__ import annotations

import os
import sys
import time

ADDR = 0x33


def read_words(bus, reg: int, count: int) -> list[int]:
    """从寄存器 reg 连续读 count 个字（16bit，repeated-start）。"""
    import smbus2

    cmd = bytes([(reg >> 8) & 0xFF, reg & 0xFF])
    write_msg = smbus2.i2c_msg.write(ADDR, cmd)
    read_msg = smbus2.i2c_msg.read(ADDR, count * 2)
    bus.i2c_rdwr(write_msg, read_msg)
    raw = bytes(read_msg.buf[: count * 2])
    return [(raw[i] << 8) | raw[i + 1] for i in range(0, len(raw), 2)]


def write_word(bus, reg: int, value: int) -> None:
    import smbus2

    cmd = bytes([(reg >> 8) & 0xFF, reg & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
    bus.i2c_rdwr(smbus2.i2c_msg.write(ADDR, cmd))


def main() -> int:
    bus_no = int(os.getenv("MLX90640_I2C_BUS", "1"))
    import smbus2

    print(f"=== 使用 I2C 总线 {bus_no}，设备地址 0x{ADDR:02X} ===", flush=True)
    bus = smbus2.SMBus(bus_no)

    # 1) EEPROM 前 8 字（0x2400）
    ee = read_words(bus, 0x2400, 8)
    print(f"[1] EEPROM[0x2400] 前 8 字: {[hex(w) for w in ee]}", flush=True)

    # 2) 序列号 0x2407
    serial = read_words(bus, 0x2407, 3)
    print(f"[2] 序列号 0x2407: {[hex(w) for w in serial]}", flush=True)

    # 3) 轮询状态寄存器 0x8000 的 dataReady（bit3 = 0x0008）
    print("[3] 开始轮询状态寄存器 0x8000（dataReady=0x0008）…", flush=True)
    seen = []
    data_ready = False
    for attempt in range(30):
        status = read_words(bus, 0x8000, 1)[0]
        seen.append(status)
        # 前 3 次 + 每 5 次打印一次原始值，便于观察变化
        if attempt < 3 or attempt % 5 == 0:
            print(f"    attempt {attempt:2d}: status = 0x{status:04X} (dataReady={'YES' if status & 0x0008 else 'no'})", flush=True)
        if status & 0x0008:
            data_ready = True
            print(f"[3] dataReady=1 出现（第 {attempt + 1} 次轮询）", flush=True)
            break
        time.sleep(0.1)

    if not data_ready:
        print(f"[3] 30 次轮询（约 3 秒）后 dataReady 仍未置位。最近 5 次 status: {[hex(w) for w in seen[-5:]]}", flush=True)
        print("    结论：传感器未产生数据，dataReady 恒为 0。", flush=True)
        return 2

    # 4) 触发读帧：写 0x8000 = 0x0030
    print("[4] 写 0x8000 = 0x0030（触发读帧并清除 dataReady）…", flush=True)
    write_word(bus, 0x8000, 0x0030)
    time.sleep(0.01)
    print("[4] 写入完成", flush=True)

    # 5) 读 RAM 0x0400（832 字 = 1664 字节）
    print("[5] 开始读 RAM[0x0400] 共 832 字…（若卡住即时钟拉伸问题）", flush=True)
    ram = read_words(bus, 0x0400, 832)
    nonzero = sum(1 for w in ram if w != 0)
    print(f"[5] RAM 读回完成：共 {len(ram)} 字，非零 {nonzero} 字，前 8 字: {[hex(w) for w in ram[:8]]}", flush=True)
    for name, idx in (("ptatArt", 768), ("ptat", 800), ("vdd", 810), ("gain", 778)):
        print(f"    {name:8s} ram[{idx}] = 0x{ram[idx]:04X}", flush=True)

    # 6) 读帧后再读状态寄存器
    status2 = read_words(bus, 0x8000, 1)[0]
    print(f"[6] 读帧后状态寄存器 0x8000 = 0x{status2:04X}", flush=True)

    if nonzero == 0:
        print("    结论：RAM 全 0（读到了但数据为空，异常）。", flush=True)
        return 1
    print("    结论：RAM 有非零数据，软件 I2C 路径可正常读取。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
