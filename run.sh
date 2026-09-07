#!/usr/bin/env bash
# Silent-Mask-Microphone 启动脚本
# 固定雷达端口与角度映射，避免 /dev/ttyUSB* 编号漂移。

set -e
cd "$(dirname "$0")"

# 雷达端口（用 by-path 稳定路径，按 左/中/右 顺序）
export RADAR_PORTS="/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1:1.0-port0,/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-port0,/dev/serial/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.4:1.0-port0"

# 每个端口对应的角度（与 RADAR_PORTS 顺序一一对应：左/中/右）
export RADAR_ANGLES="-45,0,45"

# 按钮 GPIO（BCM 编号，默认 4）
export BUTTON_GPIO=4

# 雷达串口调试打印：1 开启 / 0 关闭（默认 1）
export C4002_DEBUG=0

exec python3 src/main_stereo.py
