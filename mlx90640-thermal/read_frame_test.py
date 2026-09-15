#!/usr/bin/env python3
"""MLX90640 读帧逐步测试（纯 smbus2，无 pygame，无 Adafruit getFrame）。

逐步打印时间戳，精确定位读帧卡在哪一步。连续读两个子页，模拟真实显示程序。

运行：
    export MLX90640_I2C_BUS=3
    python read_frame_test.py
"""

from __future__ import annotations

import os
import sys
import time

ADDR = 0x33


def t():  # 便于看每一步耗时
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def read_words(bus, reg: int, count: int) -> list[int]:
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


def read_one_subpage(bus, tag: str) -> list[int]:
    print(f"[{t()}] {tag}: 读状态寄存器 0x8000 …", flush=True)
    status = read_words(bus, 0x8000, 1)[0]
    print(f"[{t()}] {tag}: status = 0x{status:04X} (dataReady={'YES' if status & 0x0008 else 'no'})", flush=True)

    print(f"[{t()}] {tag}: 写 0x8000 = 0x0030 …", flush=True)
    write_word(bus, 0x8000, 0x0030)
    print(f"[{t()}] {tag}: 写完成", flush=True)

    print(f"[{t()}] {tag}: 读 RAM 0x0400（832 字）…", flush=True)
    ram = read_words(bus, 0x0400, 832)
    nonzero = sum(1 for w in ram if w != 0)
    print(f"[{t()}] {tag}: RAM 完成，非零 {nonzero}/832，前 4 字 {[hex(w) for w in ram[:4]]}", flush=True)
    return ram


def main() -> int:
    bus_no = int(os.getenv("MLX90640_I2C_BUS", "1"))
    import smbus2

    print(f"[{t()}] 打开 I2C 总线 {bus_no}，设备 0x{ADDR:02X}", flush=True)
    bus = smbus2.SMBus(bus_no)

    print(f"[{t()}] 读序列号 0x2407 …", flush=True)
    serial = read_words(bus, 0x2407, 3)
    print(f"[{t()}] 序列号 = {[hex(w) for w in serial]}", flush=True)

    print(f"[{t()}] ===== 第 1 个子页 =====", flush=True)
    ram1 = read_one_subpage(bus, "sub1")
    print(f"[{t()}] 第 1 个子页 ptatArt[768]=0x{ram1[768]:04X} ptat[800]=0x{ram1[800]:04X}", flush=True)

    print(f"[{t()}] ===== 第 2 个子页（等 dataReady）=====", flush=True)
    # 第 2 个子页需要等待新的 dataReady（约 250ms @2Hz），最多等 3 秒
    deadline = time.monotonic() + 3.0
    while True:
        status = read_words(bus, 0x8000, 1)[0]
        if status & 0x0008:
            print(f"[{t()}] 第 2 个子页 dataReady=1，status=0x{status:04X}", flush=True)
            break
        if time.monotonic() > deadline:
            print(f"[{t()}] 3 秒超时仍未 dataReady，最后 status=0x{status:04X}", flush=True)
            print("结论：第 2 个子页的 dataReady 没等到（传感器不产出下一帧）。", flush=True)
            return 2
        time.sleep(0.01)

    ram2 = read_one_subpage(bus, "sub2")
    print(f"[{t()}] 第 2 个子页 ptatArt[768]=0x{ram2[768]:04X} ptat[800]=0x{ram2[800]:04X}", flush=True)

    print(f"[{t()}] 两个子页都读成功。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
