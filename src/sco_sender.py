#!/usr/bin/env python3
import sys

import bluetooth
from bluetooth import SCO, BluetoothSocket

BT_ADDR = "C4:FF:99:AC:A6:6A"
SCO_PACKET_SIZE = 64


def main():
    print("🔗 正在建立 SCO 连接...", flush=True)
    sock = BluetoothSocket(SCO)
    try:
        sock.connect(BT_ADDR)  # 只传 MAC 地址，不传端口
    except Exception as e:
        print(f"❌ SCO 连接失败: {e}", flush=True)
        sys.exit(1)
    print("✅ SCO 已连接，等待音频数据...", flush=True)

    while True:
        chunk = sys.stdin.buffer.read(SCO_PACKET_SIZE)
        if not chunk:
            break
        try:
            sock.send(chunk)
        except Exception:
            break
    sock.close()
    print("🛑 SCO 发送停止", flush=True)


if __name__ == "__main__":
    main()
