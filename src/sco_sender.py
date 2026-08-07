#!/usr/bin/env python3
"""
SCO 音频发送后端 (socket 版)
由系统 Python 运行，无需 pybluez
"""

import socket
import sys

BT_ADDR = "C4:FF:99:AC:A6:6A"
SCO_PACKET_SIZE = 64


def main():
    print("🔗 正在建立 SCO 连接...", flush=True)
    # AF_BLUETOOTH = 31, BTPROTO_SCO = 2
    try:
        sock = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, 2
        )  # 2 = BTPROTO_SCO
        sock.bind((BT_ADDR, 0))  # 绑定本地地址
        sock.connect((BT_ADDR, 0))  # 连接到远程设备，端口固定为 0
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
