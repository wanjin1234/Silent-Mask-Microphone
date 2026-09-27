#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断雷达"扫一会自己停"：保持串口常开，周期性重发 scan start。

验证两个假设：
  A. 雷达需要周期性重发 scan start 保活（keepalive）才能持续输出；
  B. 雷达会持续输出，问题其实是供电/掉压（此时加了 keepalive 也照样会停）。

用法（在 radarpi 目录运行）：
    PYTHONPATH=src python3 keepalive_test.py

每 5 秒重发一次 scan start，观察：雷达是否不再自己停？
若加上 keepalive 后持续输出 → 证明是 A，需要在主程序里加保活；
若加了 keepalive 依然会停（灯灭）→ 证明是 B，供电问题，与指令无关。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from radarpi.serialport import open_port, BAUD_RATE  # noqa: E402
from radarpi import protocol as P  # noqa: E402

KEEPALIVE = 5.0   # 每 5 秒重发一次 scan start


def main():
    port = open_port("auto", BAUD_RATE, timeout=0.2)
    print(f"打开 {port.device} @ {BAUD_RATE} 8N1，保持常开…")
    port.write(P.build_command("scan start"))
    print("已发送 scan start")

    parser = P.RadarStreamParser()
    total = 0
    frames = 0
    last_keepalive = time.monotonic()
    last_data_at = time.monotonic()

    try:
        while True:
            chunk = port.read(65536)
            if chunk:
                total += len(chunk)
                last_data_at = time.monotonic()
                for f in parser.feed(chunk):
                    frames += 1
                    print(f"帧 #{f.frame_id}  点={f.header.point_count}  "
                          f"航迹={f.header.track_count}")

            now = time.monotonic()
            if now - last_keepalive >= KEEPALIVE:
                port.write(P.build_command("scan start"))
                print(f"[keepalive] 重发 scan start  "
                      f"(累计 {frames} 帧 / {total} 字节)")
                last_keepalive = now
    except KeyboardInterrupt:
        pass
    finally:
        port.close()

    print(f"\n共 {frames} 帧 / {total} 字节，运行 "
          f"{time.monotonic() - last_data_at + (last_data_at - last_data_at):.1f}s")


if __name__ == "__main__":
    main()
