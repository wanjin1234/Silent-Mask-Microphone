# -*- coding: utf-8 -*-
"""C4002 原始帧扩展解析器（供 AI 模型使用）。

为什么不复用 ``src/c4002_parser.py`` 的 ``read_data()``
----------------------------------------------------
``C4002Serial.read_data()`` 是**为阈值规则**写的：它做能量选优、滞回计数、
死区截断、众数滤波、EMA 平滑、突变剔除，最后只吐出「被选中的那一个目标」的
``(distance, speed, energy)``。这正是「只能得到三个点的信息」的由来。

这些处理对规则判定有利，但对神经网络是**信息损失**：

1. ``exist_gate_index``（4 字节存在距离门位掩码，协议里位于 base+3..base+6）
   被解析出来却从未返回。按官方 C4002 协议，80cm 分辨率下 bit0~15、
   20cm 分辨率下 bit0~25 各对应一个距离门，置 1 表示该门内有目标。
   这是雷达给出的**唯一低分辨率距离剖面**（等效 16~26 格一维占据栅格），
   也是把「每方向 1 个点」变成「每方向 16~26 格」的关键信息。
2. ``exist_count_down``（存在倒计时）、``move_direction``（运动方向）、
   ``light``（环境光）同样被解析后丢弃。
3. 死区（<2cm/s 归零）、EMA、滞回会把呼吸/轻微晃动这类低速微动抹平，
   而这些恰是区分「有人静止」与「空场噪声」的主要线索。

因此本模块只做「字节 → 物理量」的忠实还原，**不做任何滤波、死区与判定**，
把判断权完全交给模型。它继承 ``C4002Serial`` 复用串口打开与配置命令，
只覆盖读取与解析部分。

注意：距离门位掩码的语义（位掩码 vs 单门号、16 位 vs 26 位）在不同固件
版本上存在差异，上机请先跑 ``probe_gates.py`` 确认，不要凭假设下结论。
"""

import os
import time

try:  # c4002_parser 顶层 import serial，无 pyserial 时整体不可用
    from c4002_parser import (
        C4002Serial,
        FRAME_HEADER1,
        FRAME_HEADER2,
        FRAME_HEADER3,
        FRAME_HEADER4,
        FRAME_TYPE_NOTIFICATION,
        NOTE_RESULT_CMD,
    )
    PARSER_AVAILABLE = True
except Exception:  # pragma: no cover - 无 pyserial / 非项目目录时
    PARSER_AVAILABLE = False
    C4002Serial = object
    FRAME_HEADER1, FRAME_HEADER2, FRAME_HEADER3, FRAME_HEADER4 = 0xFA, 0xF5, 0xAA, 0xA5
    FRAME_TYPE_NOTIFICATION = 0x04
    NOTE_RESULT_CMD = 0x60


# 距离门位数：默认 16（80cm 分辨率）。20cm 分辨率时为 26。
DEFAULT_GATE_BITS = int(os.getenv('C4002_GATE_BITS', '16'))

# 目标状态枚举（与官方协议一致）
NO_TARGET = 0
PRESENCE = 1
MOTION = 2
MOTION_OR_PRESENCE = 3
MOTION_OR_NO_TARGET = 4
PRESENCE_OR_NO_TARGET = 5

# 本模块输出的原始物理量字段（values 为缺省值），模型侧按此顺序取特征
RAW_FIELD_DEFAULTS = {
    'target_status': 0,
    'light': 0,
    'exist_gate_index': 0,
    'exist_count_down': 0,
    'exist_distance': 0.0,   # m
    'exist_energy': 0,       # 0~99
    'move_distance': 0.0,    # m
    'move_speed': 0,         # cm/s，有符号
    'move_energy': 0,        # 0~99
    'move_direction': 0,     # 0 无 / 1 远离 / 2 靠近（以官方库为准，探测脚本可验证）
}


def _popcount(x):
    return bin(int(x) & 0xFFFFFFFF).count('1')


class RawC4002Serial(C4002Serial):
    """C4002 串口读取器：输出**未经滤波**的完整物理量。

    与父类相比：

    * ``read_data()`` 返回扩展字段（含距离门位掩码），不再做死区/平滑/选优；
    * 保留 ``configure()`` / ``reset_detection()`` 等父类能力，可直接替换使用。

    参数 ``gate_bits`` 控制输出多少位距离门（默认取环境变量
    ``C4002_GATE_BITS``，80cm 分辨率 16、20cm 分辨率 26）。
    """

    def __init__(self, port='/dev/ttyAMA0', baud=115200, angle=0, sensor_id=0,
                 timeout=0.5, gate_bits=None):
        if not PARSER_AVAILABLE:
            raise RuntimeError(
                'c4002_parser 不可用（缺少 pyserial 或不在 src 目录下运行）')
        super().__init__(port=port, baud=baud, angle=angle, sensor_id=sensor_id,
                         timeout=timeout)
        self.gate_bits = int(gate_bits) if gate_bits else DEFAULT_GATE_BITS
        # 统计用：便于诊断解析是否真的在工作
        self.frames_ok = 0
        self.frames_bad_checksum = 0
        self.frames_bad_len = 0

    # ------------------------------------------------------------------
    # 字节层
    # ------------------------------------------------------------------
    def _read_notification_frame(self, timeout=None):
        """从流中同步出一个校验通过的检测结果通知帧。

        逐字节搜索帧头（而非固定 ``read(N)``）：串口流里混有通知帧、配置响应帧
        与半帧残留时，固定长度读取会永久失步。

        返回 ``(pdata, pack_len)``；超时或数据不完整返回 ``None``。
        """
        if self.ser is None:
            return None
        if timeout is None:
            timeout = max(float(self.timeout), 0.2)
        deadline = time.time() + timeout
        while time.time() < deadline:
            b = self.ser.read(1)
            if not b or b[0] != FRAME_HEADER1:
                continue
            rest = self.ser.read(7)
            if not rest or len(rest) < 7:
                return None
            head = [b[0]] + list(rest)
            if not (head[1] == FRAME_HEADER2 and head[2] == FRAME_HEADER3
                    and head[3] == FRAME_HEADER4):
                continue
            pack_len = (head[5] << 8) | head[4]
            # 通知帧长度 = 8(头+长度+类型) + 4(cmd 头) + data_len + 2(校验)
            if pack_len < 20 or pack_len > 128:
                self.frames_bad_len += 1
                continue
            body = self.ser.read(pack_len - 8)
            if not body or len(body) < pack_len - 8:
                return None
            pdata = head + list(body)
            calc = sum(pdata[:pack_len - 2]) & 0xFFFF
            recv = (pdata[pack_len - 1] << 8) | pdata[pack_len - 2]
            if calc != recv:
                self.frames_bad_checksum += 1
                continue
            if pdata[7] != FRAME_TYPE_NOTIFICATION:
                continue  # 配置响应帧，跳过继续找
            if pdata[8] != NOTE_RESULT_CMD:
                continue
            return pdata, pack_len
        return None

    # ------------------------------------------------------------------
    # 物理量层
    # ------------------------------------------------------------------
    def read_data(self):
        """读取一帧并返回**原始**物理量字典。

        返回键：``target_status, light, exist_gate_index, exist_count_down,
        exist_distance, exist_energy, move_distance, move_speed, move_energy,
        move_direction``，外加 ``angle, sensor_id, valid, timestamp`` 与
        派生字段 ``gate_bits``（0/1 列表）、``gate_count``、``raw``。

        与父类不同：``move_speed`` **不套用死区**（呼吸级低速微动予以保留），
        距离不做 EMA/众数/突变剔除，也不做存在性与运动性判定。
        """
        if self.ser is None:
            return {'valid': False}
        got = self._read_notification_frame()
        if got is None:
            return {'valid': False}
        pdata, pack_len = got
        self.frames_ok += 1

        data_len = (pdata[11] << 8) | pdata[10]
        if data_len + 14 != pack_len:
            self.frames_bad_len += 1

        base = 12  # 检测结果结构体起点
        n = len(pdata)

        def u8(off):
            i = base + off
            return pdata[i] if i < n else 0

        def u16(off):
            i = base + off
            return ((pdata[i + 1] << 8) | pdata[i]) if (i + 1) < n else 0

        def u32(off):
            i = base + off
            if (i + 3) >= n:
                return 0
            return ((pdata[i + 3] << 24) | (pdata[i + 2] << 16)
                    | (pdata[i + 1] << 8) | pdata[i])

        target_status = u8(0)
        light = u16(1)
        exist_gate_index = u32(3)
        exist_count_down = u16(7)
        exist_distance_cm = u16(9)
        exist_energy = u8(11)
        move_distance_cm = u16(12)
        move_speed = u16(14)
        if move_speed & 0x8000:  # int16 有符号
            move_speed -= 0x10000
        move_energy = u8(16)
        move_direction = u8(17)

        gate_bits = [(exist_gate_index >> i) & 1 for i in range(self.gate_bits)]

        frame = {
            'target_status': int(target_status),
            'light': int(light),
            'exist_gate_index': int(exist_gate_index),
            'exist_count_down': int(exist_count_down),
            'exist_distance': float(exist_distance_cm) * 0.01,
            'exist_energy': int(exist_energy),
            'move_distance': float(move_distance_cm) * 0.01,
            'move_speed': int(move_speed),
            'move_energy': int(move_energy),
            'move_direction': int(move_direction),
            'gate_bits': gate_bits,
            'gate_count': _popcount(exist_gate_index & ((1 << self.gate_bits) - 1)),
            'raw': pdata,
            'angle': self.angle,
            'sensor_id': self.sensor_id,
            'valid': True,
            'data_len': int(data_len),
            'timestamp': time.time(),
        }
        if self.debug:
            print(f"[C4002 RAW-EXT] sensor:{self.sensor_id} status:{target_status} "
                  f"exist_d:{frame['exist_distance']:.2f} exist_e:{exist_energy} "
                  f"gate:0x{exist_gate_index:08x}({frame['gate_count']}) "
                  f"cd:{exist_count_down} move_d:{frame['move_distance']:.2f} "
                  f"speed:{move_speed} move_e:{move_energy} dir:{move_direction} "
                  f"light:{light}")
        return frame

    def read_raw_frame(self):
        """``read_data()`` 的显式别名，语义更清楚。"""
        return self.read_data()


class RawRadarHub:
    """只读雷达的 hub（不初始化超声波，避免采集时被 150ms 阻塞打断）。

    接口与 ``RealSensorHub`` 保持一致（``radars`` / ``scan_all``），
    便于与现有代码互换。超声波不参与本模型：JSN-SR04T 明确不测人体。
    """

    def __init__(self, ports=None, angles=None, baud=115200, gate_bits=None):
        if ports is None:
            ports = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2']
        if angles is None:
            angles = [-45, 0, 45]
        self.radars = []
        self.errors = []
        for i, p in enumerate(ports):
            ang = angles[i] if i < len(angles) else 0
            try:
                r = RawC4002Serial(port=p, baud=baud, angle=ang, sensor_id=i,
                                   gate_bits=gate_bits)
            except Exception as e:
                self.errors.append(f'{p}: {e}')
                continue
            if os.getenv('C4002_DEBUG') == '1':
                r.debug = True
            # 复用父类配置：低灵敏度 + 检测范围 + 上报周期（10Hz，模型需要足够采样率）
            try:
                r.configure()
            except Exception as e:
                self.errors.append(f'{p} configure: {e}')
            self.radars.append(r)
        if not self.radars:
            raise RuntimeError('未能打开任何雷达串口: ' + '; '.join(self.errors))

    def scan_all(self):
        return [r.read_data() for r in self.radars]

    @property
    def angles(self):
        return [r.angle for r in self.radars]


def demo():
    """无硬件自检：用构造的字节帧验证解析偏移是否正确。"""
    import struct

    body = bytearray()
    body.append(MOTION)          # target_status
    body += struct.pack('<H', 37)   # light
    body += struct.pack('<I', 0b0000_0000_0001_0110)  # gate 1,2,4 置位
    body += struct.pack('<H', 55)   # exist_count_down
    body += struct.pack('<H', 213)  # exist_distance cm = 2.13m
    body.append(41)                 # exist_energy
    body += struct.pack('<H', 210)  # move_distance 2.10m
    body += struct.pack('<h', -37)  # move_speed 有符号
    body.append(66)                 # move_energy
    body.append(2)                  # move_direction
    payload = bytes([NOTE_RESULT_CMD, 0x00, len(body) & 0xFF, (len(body) >> 8) & 0xFF]) + bytes(body)
    total = len(payload) + 10
    frame = bytes([FRAME_HEADER1, FRAME_HEADER2, FRAME_HEADER3, FRAME_HEADER4,
                   total & 0xFF, (total >> 8) & 0xFF, 0x00, FRAME_TYPE_NOTIFICATION]) + payload
    frame += struct.pack('<H', sum(frame) & 0xFFFF)

    obj = RawC4002Serial.__new__(RawC4002Serial)  # 绕过串口打开
    obj.angle = 0
    obj.sensor_id = 0
    obj.debug = False
    obj.timeout = 0.1
    obj.gate_bits = 16
    obj.frames_ok = obj.frames_bad_checksum = obj.frames_bad_len = 0

    class _FakeSerial:
        def __init__(self, data):
            self.data = bytes(data)
            self.i = 0

        def read(self, k=1):
            out = self.data[self.i:self.i + k]
            self.i += len(out)
            return out

    obj.ser = _FakeSerial(frame)
    got = obj.read_data()
    expect = {
        'target_status': MOTION, 'light': 37, 'exist_gate_index': 0b10110,
        'exist_count_down': 55, 'exist_distance': 2.13, 'exist_energy': 41,
        'move_distance': 2.10, 'move_speed': -37, 'move_energy': 66,
        'move_direction': 2,
    }
    ok = True
    for k, v in expect.items():
        if got.get(k) != v:
            ok = False
            print(f'  MISMATCH {k}: got {got.get(k)!r} want {v!r}')
    print('gate_bits:', got.get('gate_bits'), 'gate_count:', got.get('gate_count'))
    print('demo parse:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(demo())
