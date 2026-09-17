"""radarpi —— 4D 成像毫米波雷达的树莓派上位机。

本包把 Windows 端「Calterah Radar Data Application Management Tool」的核心功能
（串口配置、点云实时显示、参数下发、数据录制）移植到 Raspberry Pi OS，
纯 Python 实现，只依赖标准库（可选 pyserial）。
"""

__version__ = "1.0.0"
__all__ = ["__version__", "protocol", "link", "config", "recorder", "webapp", "cli"]
