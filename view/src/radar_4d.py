#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4D 成像毫米波雷达（60GHz，Calterah 方案）串口帧解析器。

协议来源：产品资料《4D成像雷达使用说明》"Demo 输出协议"（表格1）。
串口：波特率 3000000（3M），8N1，无校验，TX 对 RX（需支持高波特率的串口小板）。

帧结构（所有多字节字段均小端）::

    [帧头 0xFFEEFFDC] + 5×u32 统计字 + [帧头包尾 0xFFEEFFD3]
    [点云包头 0xFFDDFECB] + N点×10B + Reserved(u32) + [点云包尾 0xFFDDFEC4]
    [航迹包头 0xFFCCFDBA] + N航迹×6B + Reserved(u32) + [航迹包尾 0xFFCCFDB5]

帧头统计字（共 7 个 u32）::

    w0  帧头魔数 0xFFEEFFDC
    w1  帧周期(bit0-9) | 动态高置信度点数(bit10-19) | Reserved(bit20-31)
    w2  帧号（u32）
    w3  动态低置信度点数(bit0-9) | 跟踪航迹数(bit10-19) | 长时微动高置信度点数(bit20-29) | Reserved(bit30-31)
    w4  长时微动低置信度点数(bit0-9) | 短时微动高置信度点数(bit10-19) | 短时微动低置信度点数(bit20-29) | Reserved(bit30-31)
    w5  上帧BB处理时间(bit0-7,ms) | Post-BB处理时间(bit8-15,ms) | 结果传输时间(bit16-23,ms) | 上帧间隔(bit24-31,ms)
    w6  帧头包尾 0xFFEEFFD3

每个点 10 字节::

    X (int16, 0.001m)  Y (int16, 0.001m)  Z (int16, 0.001m)
    SNR (uint16, 相对线性值)  V (int16, 0.1m/s)

每个航迹 6 字节::

    X (int16, 0.001m)  Y (int16, 0.001m)  Z (int16, 0.001m)

坐标系（地面系）：X=左右，Y=前方距离，Z=高度。
注意：这与旧 data_fusion 的"y=高度/z=前方"轴约定不同，显示层用
:func:`to_display_coord` 做轴映射（radar X→x, radar Y→z纵深, radar Z→y高度）。

点云类别与语义（供人体/障碍分类使用）::

    dyn_hi / dyn_lo      动态（移动）目标，高/低置信度，V 有效
    long_hi / long_lo    长时微动（如呼吸的缓慢周期起伏），V 默认 0
    short_hi / short_lo  短时微动（更快的小幅微动），V 默认 0
"""

import os
import time
import struct
import math
import random
from collections import namedtuple

# ---------------------------------------------------------------------------
# 帧常量
# ---------------------------------------------------------------------------
RADAR4D_DEFAULT_BAUD = int(os.getenv('R4D_BAUD', '3000000'))
RADAR4D_DEFAULT_PORT = os.getenv('R4D_PORT', '/dev/ttyACM0')

# 32bit 魔数（小端传输，字节序为 [LSB ... MSB]）
FRAME_HDR_MAGIC = 0xFFEEFFDC
FRAME_HDR_TAIL = 0xFFEEFFD3
CLOUD_MAGIC = 0xFFDDFECB
CLOUD_TAIL = 0xFFDDFEC4
TRACK_MAGIC = 0xFFCCFDBA
TRACK_TAIL = 0xFFCCFDB5

# 魔数对应的小端字节序列，用于流式查找
_HDR_BYTES = struct.pack('<I', FRAME_HDR_MAGIC)

# 点云类别顺序（与帧头统计字里的计数顺序一致）
POINT_CLASSES = ['dyn_hi', 'dyn_lo', 'long_hi', 'long_lo', 'short_hi', 'short_lo']

# 每点字段数：X/Y/Z/SNR/V 各 16bit = 10 字节
POINT_BYTES = 10
# 每航迹字段数：X/Y/Z 各 16bit = 6 字节
TRACK_BYTES = 6

# 帧头字节数：7 × u32 = 28
HDR_BYTES = 28
# 各段固定开销（不含数据）：点云段 = 包头4 + Reserved4 + 包尾4 = 12；航迹段同理 12
CLOUD_OVERHEAD = 12
TRACK_OVERHEAD = 12

# ---------------------------------------------------------------------------
# 可调阈值（环境变量覆盖）
# ---------------------------------------------------------------------------
# 判定"移动"的最小速度（m/s）。Point.v 已从 0.1m/s 原始值换算为 m/s。
MOVING_V_MIN = float(os.getenv('R4D_MOVING_V_MIN', '0.3'))
# 聚类邻域半径（米）。点云稀疏、人占据约 0.5m，默认 0.6m。
CLUSTER_EPS = float(os.getenv('R4D_CLUSTER_EPS', '0.6'))
# 一簇至少需要多少个点才认为是有效目标（过滤孤立杂点）。
CLUSTER_MIN_POINTS = int(os.getenv('R4D_CLUSTER_MIN_POINTS', '2'))
# 高置信度类（dyn_hi/long_hi/short_hi）权重，低置信度类权重。
CLS_WEIGHT = {'dyn_hi': 2, 'long_hi': 2, 'short_hi': 2,
              'dyn_lo': 1, 'long_lo': 1, 'short_lo': 1}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
Point = namedtuple('Point', 'x y z snr v cls')
Track = namedtuple('Track', 'x y z')


class Radar4DFrame:
    """一帧完整解析结果。"""

    __slots__ = ('frame_id', 'frame_period', 'counts', 'bb_ms', 'postbb_ms',
                 'tx_ms', 'interval_ms', 'points', 'tracks', 'timestamp',
                 'raw_size')

    def __init__(self):
        self.frame_id = 0
        self.frame_period = 0
        self.counts = {c: 0 for c in POINT_CLASSES}
        self.counts['track'] = 0
        self.bb_ms = 0
        self.postbb_ms = 0
        self.tx_ms = 0
        self.interval_ms = 0
        self.points = []      # list[Point]
        self.tracks = []      # list[Track]
        self.timestamp = 0.0
        self.raw_size = 0

    @property
    def total_points(self):
        return sum(self.counts[c] for c in POINT_CLASSES)

    def __repr__(self):
        return (f"<Radar4DFrame id={self.frame_id} pts={self.total_points} "
                f"tracks={self.counts['track']} {self.counts}>")


# ---------------------------------------------------------------------------
# 字节工具
# ---------------------------------------------------------------------------
def _u16(b, i):
    return b[i] | (b[i + 1] << 8)


def _s16(b, i):
    v = _u16(b, i)
    return v - 0x10000 if v & 0x8000 else v


def _u32(b, i):
    return b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24)


def _frame_size(np_, nt):
    """给定点数 np 与航迹数 nt，计算完整帧字节数。"""
    return HDR_BYTES + (CLOUD_OVERHEAD + np_ * POINT_BYTES) + \
           (TRACK_OVERHEAD + nt * TRACK_BYTES)


def to_display_coord(px, py, pz):
    """radar(X左右,Y前方,Z高度) -> display(x左右, y高度, z前方)。"""
    return px, pz, py


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
class Radar4DParser:
    """无状态字节流解析器：喂入字节，吐出完整帧。"""

    def __init__(self, debug=False):
        self.debug = debug
        self.buf = bytearray()
        self.resync_count = 0
        self.frames = 0

    def feed(self, data):
        """喂入一段字节，返回解析出的完整帧列表（可能为空）。"""
        self.buf.extend(data)
        out = []
        while True:
            frame = self._try_parse_one()
            if frame is None:
                break
            out.append(frame)
        # 防止恶意/异常流导致缓冲区无限增长
        if len(self.buf) > 65536:
            self.buf = self.buf[-32768:]
        return out

    def _try_parse_one(self):
        buf = self.buf
        # 1. 定位帧头魔数
        idx = buf.find(_HDR_BYTES)
        if idx < 0:
            # 保留末尾 3 字节（可能是半截魔数），其余丢弃
            if len(buf) > 3:
                self.resync_count += 1
                del buf[:-3]
            return None
        if idx > 0:
            # 丢弃魔数之前的杂散字节
            if self.debug:
                print(f"[R4D] resync: dropped {idx} bytes")
            self.resync_count += 1
            del buf[:idx]

        # 2. 需要至少读满帧头
        if len(buf) < HDR_BYTES:
            return None

        # 3. 解析帧头统计字
        w0 = _u32(buf, 0)
        w1 = _u32(buf, 4)
        w2 = _u32(buf, 8)   # 帧号
        w3 = _u32(buf, 12)
        w4 = _u32(buf, 16)
        w5 = _u32(buf, 20)
        w6 = _u32(buf, 24)

        # 校验帧头魔数与包尾，防误同步
        if w0 != FRAME_HDR_MAGIC or w6 != FRAME_HDR_TAIL:
            del buf[0]  # 假魔数，前进 1 字节重找
            return None

        counts = {
            'dyn_hi': (w1 >> 10) & 0x3FF,
            'dyn_lo': (w3 >> 0) & 0x3FF,
            'track': (w3 >> 10) & 0x3FF,
            'long_hi': (w3 >> 20) & 0x3FF,
            'long_lo': (w4 >> 0) & 0x3FF,
            'short_hi': (w4 >> 10) & 0x3FF,
            'short_lo': (w4 >> 20) & 0x3FF,
        }
        np_ = sum(counts[c] for c in POINT_CLASSES)
        nt = counts['track']
        size = _frame_size(np_, nt)

        # 4. 数据未到齐，等待更多字节
        if len(buf) < size:
            return None

        # 5. 提取整帧并校验各段包尾
        raw = bytes(buf[:size])
        del buf[:size]

        # 点云段
        cloud_off = HDR_BYTES
        if _u32(raw, cloud_off) != CLOUD_MAGIC:
            return self._bad_frame(raw)
        # 航迹段起始 = 帧头 + 点云段(4 + np*10 + 4 + 4)
        track_off = HDR_BYTES + (CLOUD_OVERHEAD + np_ * POINT_BYTES)
        if _u32(raw, track_off) != TRACK_MAGIC:
            return self._bad_frame(raw)
        if _u32(raw, track_off + 4 + nt * TRACK_BYTES + 4) != TRACK_TAIL:
            return self._bad_frame(raw)
        if _u32(raw, cloud_off + 4 + np_ * POINT_BYTES + 4) != CLOUD_TAIL:
            return self._bad_frame(raw)

        # 6. 组装帧对象
        f = Radar4DFrame()
        f.frame_id = w2
        f.frame_period = w1 & 0x3FF
        f.counts = counts
        f.bb_ms = w5 & 0xFF
        f.postbb_ms = (w5 >> 8) & 0xFF
        f.tx_ms = (w5 >> 16) & 0xFF
        f.interval_ms = (w5 >> 24) & 0xFF
        f.timestamp = time.time()
        f.raw_size = size

        # 解析点云：按类别顺序依次读取 np 个点
        p = cloud_off + 4
        for cls in POINT_CLASSES:
            for _ in range(counts[cls]):
                x = _s16(raw, p) * 0.001
                y = _s16(raw, p + 2) * 0.001
                z = _s16(raw, p + 4) * 0.001
                snr = _u16(raw, p + 6)
                v = _s16(raw, p + 8) * 0.1
                f.points.append(Point(x, y, z, snr, v, cls))
                p += POINT_BYTES

        # 解析航迹
        t = track_off + 4
        for _ in range(nt):
            x = _s16(raw, t) * 0.001
            y = _s16(raw, t + 2) * 0.001
            z = _s16(raw, t + 4) * 0.001
            f.tracks.append(Track(x, y, z))
            t += TRACK_BYTES

        self.frames += 1
        return f

    def _bad_frame(self, raw):
        """某段包尾校验失败：放弃本帧（已从 buf 移除），前进 1 字节重同步。"""
        self.resync_count += 1
        if self.debug:
            print(f"[R4D] bad frame tail, resync (total {self.resync_count})")
        # 把该帧除首字节外的部分塞回缓冲（首字节是魔数 0xDC，丢弃后重新找）
        self.buf[:0] = raw[1:]
        return None


# ---------------------------------------------------------------------------
# 点云分类：聚类 + 人体/障碍判定
# ---------------------------------------------------------------------------
def cluster_points(points, eps=CLUSTER_EPS, min_points=CLUSTER_MIN_POINTS):
    """对点云做贪心空间聚类（欧氏距离 < eps 归为一簇）。

    返回簇列表，每簇::

        {'centroid': (x,y,z), 'points': [Point...], 'max_v': 最大|速度|,
         'weight': 置信度加权点数, 'classes': {cls: count}}
    """
    clusters = []
    for pt in points:
        best = None
        best_d = eps * eps
        for c in clusters:
            cx, cy, cz = c['centroid']
            d = (pt.x - cx) ** 2 + (pt.y - cy) ** 2 + (pt.z - cz) ** 2
            if d < best_d:
                best_d = d
                best = c
        if best is not None:
            best['points'].append(pt)
            n = len(best['points'])
            cx, cy, cz = best['centroid']
            best['centroid'] = ((cx * (n - 1) + pt.x) / n,
                                (cy * (n - 1) + pt.y) / n,
                                (cz * (n - 1) + pt.z) / n)
        else:
            clusters.append({'centroid': (pt.x, pt.y, pt.z), 'points': [pt]})

    # 汇总每簇统计量
    result = []
    for c in clusters:
        pts = c['points']
        max_v = max((abs(p.v) for p in pts), default=0.0)
        weight = sum(CLS_WEIGHT.get(p.cls, 1) for p in pts)
        classes = {}
        for p in pts:
            classes[p.cls] = classes.get(p.cls, 0) + 1
        result.append({
            'centroid': c['centroid'],
            'points': pts,
            'max_v': max_v,
            'weight': weight,
            'classes': classes,
        })
    # 按置信度权重降序，最显著的目标排前
    result.sort(key=lambda c: c['weight'], reverse=True)
    return result


def classify_targets(points, moving_v_min=MOVING_V_MIN):
    """把点云分类成：移动人体、微动(呼吸)人体、静态目标。

    返回 dict::

        {'humans': [ {x,y,z,v,state,moving,n_points} ... ],
         'obstacles': [ {x,y,z,n_points} ... ]}

    判定规则：
      - 簇内含动态点且 |v| >= moving_v_min → 移动人体（state='moving'）
      - 簇内含长时/短时微动点 → 微动人体（state='breathing'）
      - 其余（低速动态孤立点等）→ 静态目标/障碍
    """
    humans = []
    obstacles = []
    for c in cluster_points(points):
        cx, cy, cz = c['centroid']
        classes = c['classes']
        has_micro = any(k in classes for k in ('long_hi', 'long_lo', 'short_hi', 'short_lo'))
        dyn_points = [p for p in c['points'] if p.cls in ('dyn_hi', 'dyn_lo')]
        is_moving = any(abs(p.v) >= moving_v_min for p in dyn_points)

        if is_moving or has_micro:
            state = 'moving' if is_moving else 'breathing'
            humans.append({
                'x': cx, 'y': cy, 'z': cz,
                'v': c['max_v'],
                'state': state,
                'moving': is_moving,
                'n_points': len(c['points']),
            })
        else:
            obstacles.append({
                'x': cx, 'y': cy, 'z': cz,
                'n_points': len(c['points']),
            })
    return {'humans': humans, 'obstacles': obstacles}


# ---------------------------------------------------------------------------
# 串口读取器
# ---------------------------------------------------------------------------
class Radar4D:
    """4D 雷达串口读取器：非阻塞读取 + 解析，返回最新完整帧。"""

    def __init__(self, port=RADAR4D_DEFAULT_PORT, baud=RADAR4D_DEFAULT_BAUD,
                 timeout=0.2, debug=None):
        import serial
        self.port = port
        self.baud = baud
        self.debug = (os.getenv('R4D_DEBUG') == '1') if debug is None else debug
        self.parser = Radar4DParser(debug=self.debug)
        self.ser = None
        try:
            self.ser = serial.Serial(port, baudrate=baud, bytesize=8,
                                     parity='N', stopbits=1, timeout=timeout)
        except Exception as e:
            print(f"[R4D] 打开串口 {port} 失败: {e}")

    def read_frame(self):
        """读取并返回最新完整帧；暂无完整帧时返回 None（非阻塞）。"""
        if self.ser is None:
            return None
        try:
            waiting = self.ser.in_waiting
            if waiting <= 0:
                # 高波特率下 in_waiting 可能短暂为 0，仍尝试短读一次
                waiting = 1
            chunk = self.ser.read(waiting)
        except Exception as e:
            if self.debug:
                print(f"[R4D] 读取错误: {e}")
            return None
        if not chunk:
            return None
        frames = self.parser.feed(chunk)
        return frames[-1] if frames else None

    # ------------------------------------------------------------------
    # 配置指令（雷达默认不输出数据，必须先下发 scanstart 才上报点云）
    # 指令为 ASCII 文本 + 换行符（默认 \\r\\n，可用 R4D_CMD_EOL 覆盖）
    # ------------------------------------------------------------------
    def send_command(self, text, eol=None):
        """发送一条 ASCII 文本配置指令，返回是否发送成功。"""
        if self.ser is None:
            if self.debug:
                print("[R4D] 串口未打开，无法发送指令")
            return False
        if eol is None:
            eol = os.getenv('R4D_CMD_EOL', '\r\n')
        try:
            self.ser.reset_input_buffer()
            self.ser.write((text + eol).encode('utf-8'))
            self.ser.flush()
            if self.debug:
                print(f"[R4D] 已发送指令: {text!r}")
            return True
        except Exception as e:
            print(f"[R4D] 发送指令失败: {e}")
            return False

    def scan_start(self):
        """开始发送点云数据（必须先收到此指令才会上报）。"""
        return self.send_command('scanstart')

    def scan_stop(self):
        """停止发送点云数据。"""
        return self.send_command('scanstop')

    def configure_defaults(self, height=1.7, inclination=30,
                           boundary=(-3, 3, 0, 8, 0, 2.5),
                           mms_interval=(2, 3, 4),
                           cfar_coeff=(3, 3, 4, 4, 5, 5)):
        """下发默认配置（高度/倾角/边界/微动间隔/门限系数）并开始扫描。

        各参数可用环境变量 R4D_HEIGHT / R4D_INCLINATION 覆盖。
        返回所有指令是否都发送成功。
        """
        try:
            height = float(os.getenv('R4D_HEIGHT', str(height)))
        except Exception:
            pass
        try:
            inclination = int(os.getenv('R4D_INCLINATION', str(inclination)))
        except Exception:
            pass
        cmds = [
            f"set radar height {height}",
            f"set radar inclination {inclination}",
            "set boundary %d %d %d %d %d %d" % tuple(boundary),
            "set_mmsinterval %d %d %d" % tuple(mms_interval),
            "set_cfar_coeff %d %d %d %d %d %d" % tuple(cfar_coeff),
        ]
        ok = True
        for c in cmds:
            ok = self.send_command(c) and ok
            time.sleep(0.05)
        ok = self.scan_start() and ok
        return ok

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None


class SimulatedRadar4D:
    """模拟 4D 雷达：生成移动人体 + 呼吸人体 + 低速杂波点，用于无硬件调试。

    不依赖 pygame / 串口，可在任意环境运行，验证解析、分类与显示链路。
    """

    def __init__(self):
        self.t = 0.0
        self.frame_id = 0

    def read_frame(self):
        self.t += 0.05
        self.frame_id += 1
        f = Radar4DFrame()
        f.frame_id = self.frame_id
        f.timestamp = time.time()

        # 1) 一个沿 x 往返移动的人（动态高置信度点）
        px = 1.5 * math.sin(self.t * 0.6)
        py = 3.0
        pz = 1.2
        for _ in range(4):
            f.points.append(Point(px + random.uniform(-0.15, 0.15),
                                  py + random.uniform(-0.15, 0.15),
                                  pz + random.uniform(-0.15, 0.15),
                                  120, 0.9, 'dyn_hi'))

        # 2) 一个静止呼吸的人（长时微动点）
        for _ in range(3):
            f.points.append(Point(-0.8 + random.uniform(-0.1, 0.1),
                                  2.5 + random.uniform(-0.1, 0.1),
                                  1.0 + random.uniform(-0.05, 0.05),
                                  60, 0.0, 'long_hi'))

        # 3) 少量低速动态杂波（被当作障碍/静态目标）
        for _ in range(5):
            f.points.append(Point(random.uniform(-2, 2),
                                  random.uniform(1, 6),
                                  random.uniform(0, 1.5),
                                  30, 0.05, 'dyn_lo'))
        return f


# ---------------------------------------------------------------------------
# 合成帧构建（自测用）
# ---------------------------------------------------------------------------
def build_synthetic_frame(frame_id=1, points=None, tracks=None):
    """构造一帧合法数据，用于解析器自测。points/tracks 为 Point/Track 列表。"""
    points = points or []
    tracks = tracks or []

    counts = {c: 0 for c in POINT_CLASSES}
    counts['track'] = 0
    for p in points:
        counts[p.cls] = counts.get(p.cls, 0) + 1
    counts['track'] = len(tracks)

    # 帧头 7 字
    w1 = (1 & 0x3FF) | ((counts['dyn_hi'] & 0x3FF) << 10)   # 帧周期占位 1
    w3 = ((counts['dyn_lo'] & 0x3FF) << 0) | ((counts['track'] & 0x3FF) << 10) | \
         ((counts['long_hi'] & 0x3FF) << 20)
    w4 = ((counts['long_lo'] & 0x3FF) << 0) | ((counts['short_hi'] & 0x3FF) << 10) | \
         ((counts['short_lo'] & 0x3FF) << 20)
    w5 = 1 | (2 << 8) | (3 << 16) | (4 << 24)   # bb/postbb/tx/interval 占位

    header = struct.pack('<IIIIIII', FRAME_HDR_MAGIC, w1, frame_id, w3, w4, w5,
                         FRAME_HDR_TAIL)

    cloud = bytearray(struct.pack('<I', CLOUD_MAGIC))
    # 协议要求点云按类别分组输出，顺序与帧头统计字计数一致
    for cls in POINT_CLASSES:
        for p in points:
            if p.cls != cls:
                continue
            cloud += struct.pack('<hhhHh',
                                 int(round(p.x * 1000)), int(round(p.y * 1000)),
                                 int(round(p.z * 1000)), int(p.snr),
                                 int(round(p.v * 10)))
    cloud += struct.pack('<I', 0)          # Reserved
    cloud += struct.pack('<I', CLOUD_TAIL)

    trk = bytearray(struct.pack('<I', TRACK_MAGIC))
    for t in tracks:
        trk += struct.pack('<hhh',
                           int(round(t.x * 1000)), int(round(t.y * 1000)),
                           int(round(t.z * 1000)))
    trk += struct.pack('<I', 0)            # Reserved
    trk += struct.pack('<I', TRACK_TAIL)

    return bytes(header + cloud + trk)


def _self_test():
    """构造已知合成帧，回环解析并校验，验证解析器正确性。"""
    pts = [
        Point(0.5, 2.0, 1.2, 120, 0.8, 'dyn_hi'),
        Point(0.6, 2.1, 1.2, 90, 0.7, 'dyn_hi'),
        Point(-0.4, 3.5, 1.0, 40, 0.0, 'long_hi'),
        Point(-0.5, 3.6, 1.0, 30, 0.0, 'long_lo'),
        Point(1.0, 1.5, 0.3, 15, 0.1, 'dyn_lo'),
    ]
    trks = [Track(0.55, 2.05, 1.2), Track(-0.45, 3.55, 1.0)]
    raw = build_synthetic_frame(frame_id=1234, points=pts, tracks=trks)

    parser = Radar4DParser()
    # 前面加一段噪声字节，验证重同步
    noisy = b'\x00\x01\x02\x03' + raw
    frames = parser.feed(noisy)
    assert len(frames) == 1, f"期望解析出 1 帧，实际 {len(frames)}"
    f = frames[0]
    assert f.frame_id == 1234, f"帧号错误: {f.frame_id}"
    assert f.total_points == 5, f"点数错误: {f.total_points}"
    assert f.counts['dyn_hi'] == 2 and f.counts['long_hi'] == 1 \
        and f.counts['long_lo'] == 1 and f.counts['dyn_lo'] == 1, f.counts
    assert f.counts['track'] == 2
    assert len(f.tracks) == 2
    # 校验一个点的值（0.001m / 0.1m/s 量化回程）
    p0 = f.points[0]
    assert abs(p0.x - 0.5) < 0.001 and abs(p0.v - 0.8) < 0.01, p0

    # 分类自测
    res = classify_targets(f.points)
    assert len(res['humans']) >= 2, res
    print("radar_4d 自测通过 ✔")
    print(f"  frame: {f}")
    print(f"  点云: {[(p.x, p.y, p.z, p.v, p.cls) for p in f.points]}")
    print(f"  航迹: {[(t.x, t.y, t.z) for t in f.tracks]}")
    print(f"  人体: {res['humans']}")
    print(f"  障碍: {res['obstacles']}")


if __name__ == '__main__':
    _self_test()
