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

# 雷达串口调试打印：1 开启 / 0 关闭（开机自启动设为 0，避免刷屏）
export C4002_DEBUG=0

# 上报周期（0.1s 单位）：1 = 100ms = 10Hz。改周期必须重启雷达才生效。
export C4002_CONFIG_REPORT_PERIOD=1
export C4002_CONFIG_RESTART=1

# 使用项目 venv 里的 python（pygame 等依赖装在这里，系统 python3 没有）
PY="venv/bin/python3"
if [ ! -x "$PY" ]; then
  # 兜底：无 venv 时退回系统 python3
  PY="python3"
fi

exec "$PY" src/main_stereo.py
