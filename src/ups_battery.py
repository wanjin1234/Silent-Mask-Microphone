# -*- coding: utf-8 -*-
"""
微雪 UPS HAT (E) 电池监测模块。

硬件：UPS HAT (E) 通过弹簧顶针给树莓派（Pi 4/5）供电，板载 IP2368（充电管理）
+ BQ4050（电量计）经 I2C 上报电池信息，从站地址默认 0x2D。

本模块的数据读取方法完全照搬微雪官方寄存器文档
（https://www.waveshare.com/wiki/UPS_HAT_(E)_Register）：
    - 从站地址：0x2D（寄存器 0x41 可改，0x08~0x77）
    - ID    0x00：固定 0x0A
    - 充电  0x02：bit7=1 充电中 / 0 未充电
    - 电池总电压 0x20(低) 0x21(高)，单位 mV
    - 电池电流   0x22(低) 0x23(高)，有符号 16 位 mA，正=充电、负=对外输出
    - 电量百分比 0x24(低) 0x25(高)，单位 %（BQ4050 电量计直接给出）
    - 剩余容量   0x26/0x27，mAh；剩余放电时间 0x28/0x29，分钟
    - 断电  0x01：写入 0x55 切断 UPS 输出，并启用来电自动重启

无 smbus / 非树莓派环境（如 Windows 调试）时自动禁用，仅当设 UPS_SIMULATE=1
时提供模拟数据，便于在开发机查看图标与闪烁效果。
"""

import os
import time
import subprocess

# ---- UPS HAT (E) 寄存器（官方 UPS HAT (E) Register）----
_REG_ID = 0x00          # ID，固定 0x0A
_REG_POWER_CTRL = 0x01  # 写 0x55 断电 + 启用来电重启
_REG_CHARGING = 0x02    # bit7=1 充电中
_REG_VBAT_LO = 0x20     # 电池总电压 低字节 (mV)
_REG_VBAT_HI = 0x21     # 电池总电压 高字节 (mV)
_REG_IBAT_LO = 0x22     # 电池电流 低字节 (mA，有符号)
_REG_IBAT_HI = 0x23     # 电池电流 高字节
_REG_PERCENT_LO = 0x24  # 电量百分比 低字节 (%)
_REG_PERCENT_HI = 0x25  # 电量百分比 高字节
_REG_CAP_LO = 0x26      # 剩余容量 低字节 (mAh)
_REG_CAP_HI = 0x27      # 剩余容量 高字节
_REG_DISC_LO = 0x28     # 剩余放电时间 低字节 (min)
_REG_DISC_HI = 0x29     # 剩余放电时间 高字节

_ID_VALUE = 0x0A
_POWER_OFF_VALUE = 0x55


def _load_smbus():
    """优先系统 smbus（apt install python3-smbus），退而求其次 smbus2（pip）。"""
    try:
        import smbus
        return smbus
    except Exception:
        pass
    try:
        import smbus2 as smbus
        return smbus
    except Exception:
        return None


class _UpsHatE:
    """微雪 UPS HAT (E) 寄存器读取实现（官方 UPS HAT (E) Register）。"""

    def __init__(self, bus, addr):
        self.bus = bus
        self.addr = addr
        # 上电自检：读 ID 寄存器，必须等于 0x0A 才确认是本设备
        if self._read_u8(_REG_ID) != _ID_VALUE:
            raise IOError('非 UPS HAT (E) 设备（ID != 0x0A）')

    def _read_u8(self, reg):
        return self.bus.read_byte_data(self.addr, reg)

    def _read_u16(self, reg):
        lo = self.bus.read_byte_data(self.addr, reg)
        hi = self.bus.read_byte_data(self.addr, reg + 1)
        return lo | (hi << 8)

    def battery_voltage_mv(self):
        return self._read_u16(_REG_VBAT_LO)

    def battery_current_ma(self):
        value = self._read_u16(_REG_IBAT_LO)
        if value > 32767:
            value -= 65536
        return value

    def battery_percent(self):
        return self._read_u16(_REG_PERCENT_LO)

    def remaining_capacity_mah(self):
        return self._read_u16(_REG_CAP_LO)

    def remaining_discharge_min(self):
        return self._read_u16(_REG_DISC_LO)

    def is_charging(self):
        return (self._read_u8(_REG_CHARGING) & 0x80) != 0

    def power_off(self):
        """写入 0x55 切断 UPS 输出（并启用来电自动重启）。"""
        self.bus.write_byte_data(self.addr, _REG_POWER_CTRL, _POWER_OFF_VALUE)


class UpsBattery:
    """UPS HAT (E) 电池读取封装，带优雅降级与模拟模式。"""

    def __init__(self):
        self.poll_seconds = self._env_float('UPS_POLL_SECONDS', 1.0)
        self.real = False
        self.simulated = os.getenv('UPS_SIMULATE', '0') == '1'
        self.available = self.simulated
        self._dev = None
        self._sim_start = time.time()

        smbus = _load_smbus()
        if smbus is None:
            if not self.simulated:
                print('[UPS] 未安装 smbus/smbus2，电池监测已禁用')
            return

        bus = int(os.getenv('UPS_I2C_BUS', '1'))
        addr = int(os.getenv('UPS_I2C_ADDR', '0x2D'), 16)
        try:
            self._dev = _UpsHatE(smbus.SMBus(bus), addr)
            self.real = True
            self.available = True
            print(f'[UPS] 已连接 UPS HAT (E)（I2C bus={bus}, addr=0x{addr:02X}）')
        except Exception as e:
            if self.simulated:
                print(f'[UPS] 未检测到硬件（{e}），使用模拟数据')
            else:
                print(f'[UPS] 初始化失败（{e}），电池监测已禁用')

    @staticmethod
    def _env_float(name, default):
        try:
            return float(os.getenv(name, str(default)))
        except Exception:
            return default

    def read(self):
        """返回电池状态 dict；无硬件/读取失败返回 None。

        dict: {'voltage': V, 'current_ma': mA, 'power_w': W,
               'percent': 0~100, 'charging': bool,
               'remaining_mah': mAh, 'remaining_min': 分钟}
        """
        if self.simulated and not self.real:
            return self._read_simulated()
        if self._dev is None:
            return None
        try:
            voltage_mv = self._dev.battery_voltage_mv()
            current_ma = self._dev.battery_current_ma()
            percent = self._dev.battery_percent()
            charging = self._dev.is_charging()
            remaining_mah = self._dev.remaining_capacity_mah()
            remaining_min = self._dev.remaining_discharge_min()
        except Exception:
            return None

        voltage = voltage_mv / 1000.0
        power_w = voltage * current_ma / 1000.0
        return {
            'voltage': round(voltage, 3),
            'current_ma': round(current_ma, 3),
            'power_w': round(power_w, 4),
            'percent': round(float(percent), 1),
            'charging': bool(charging),
            'remaining_mah': int(remaining_mah),
            'remaining_min': int(remaining_min),
        }

    def power_off(self):
        """切断 UPS 输出（低电压关机时调用，数据已 sync 落盘）。"""
        if self._dev is not None:
            self._dev.power_off()

    def _read_simulated(self):
        # 约 30 分钟从 100% 线性掉到 15%，便于观察图标与低电量闪烁
        elapsed = time.time() - self._sim_start
        percent = max(15.0, min(100.0, 100.0 - (elapsed / 1800.0) * 85.0))
        voltage = 3.0 + (percent / 100.0) * 1.2
        return {
            'voltage': round(voltage, 3),
            'current_ma': -180.0,
            'power_w': round(voltage * 0.18, 4),
            'percent': round(percent, 1),
            'charging': False,
            'remaining_mah': int(percent * 10),
            'remaining_min': int(percent * 3),
        }


def shutdown_system():
    """低电压时执行系统关机（备用）。依序尝试常见关机命令，任意一个调度成功即返回。"""
    cmds = [
        ['shutdown', '-h', 'now'],
        ['systemctl', 'poweroff'],
        ['sudo', 'shutdown', '-h', 'now'],
        ['sudo', 'systemctl', 'poweroff'],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, timeout=10, check=False)
            return
        except Exception:
            continue
