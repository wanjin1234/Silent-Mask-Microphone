#!/usr/bin/env python3
# test_radar.py
import serial
import time
import sys

# 默认串口，可通过命令行参数覆盖，例如: python test_radar.py /dev/ttyUSB0
if len(sys.argv) > 1:
    SERIAL_PORT = sys.argv[1]
else:
    SERIAL_PORT = '/dev/ttyUSB0'

BAUD = 115200

def parse_c4002(data):
    """
    尝试解析 C4002 数据帧，假设帧格式：
    帧头: AA 55
    距离: 2字节 (高字节在前，单位cm)
    信号强度: 1字节
    状态: 1字节 (0x01 表示有人)
    校验: 1字节 (可选)
    总长度: 7字节
    返回 (distance_m, presence) 或 None
    """
    if len(data) < 7:
        return None
    # 查找帧头
    for i in range(len(data) - 6):
        if data[i] == 0xAA and data[i+1] == 0x55:
            # 检查剩余长度
            if len(data) - i < 7:
                continue
            dist_cm = (data[i+2] << 8) | data[i+3]
            dist_m = dist_cm / 100.0
            # 状态字节假设为第6个字节 (索引 i+5)
            status = data[i+5]
            presence = 1 if (status & 0x01) else 0
            return dist_m, presence
    return None

def main():
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD, timeout=0.5)
    except Exception as e:
        print(f"无法打开串口 {SERIAL_PORT}: {e}")
        sys.exit(1)

    print(f"正在从 {SERIAL_PORT} 读取数据...")
    print("按 Ctrl+C 停止\n")

    buffer = bytearray()
    try:
        while True:
            if ser.in_waiting:
                chunk = ser.read(ser.in_waiting)
                buffer.extend(chunk)
                # 打印原始数据（十六进制）
                hex_str = ' '.join(f'{b:02X}' for b in chunk)
                print(f"原始: {hex_str}")

                # 尝试解析
                result = parse_c4002(buffer)
                if result:
                    dist_m, presence = result
                    print(f"解析: 距离={dist_m:.2f} m, 有人={'是' if presence else '否'}")
                    # 移除已解析的数据（简化：清空缓冲区）
                    buffer.clear()
                else:
                    # 防止缓冲区过大
                    if len(buffer) > 100:
                        buffer = buffer[-50:]

            time.sleep(0.05)
    except KeyboardInterrupt:
        ser.close()
        print("\n测试结束")

if __name__ == "__main__":
    main()
