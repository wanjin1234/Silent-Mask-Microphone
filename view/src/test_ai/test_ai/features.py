# -*- coding: utf-8 -*-
"""多雷达原始帧 → 模型张量。

设计要点
--------
1. **不做滤波**。父项目 ``c4002_parser.read_data()`` 里的死区/EMA/众数/滞回
   会把呼吸级微动抹平；模型需要看见原始抖动，由它自己学噪声与信号的区别。
2. **多传感器时间对齐**。三个串口各自异步上报（配置为 10Hz），丢帧是常态。
   本模块把三路样本用零阶保持重采样到统一时间栅格，并用 ``fresh`` 标志位
   明确告诉模型「这一格是实测还是上一帧顶替」，避免丢帧被误读成静止。
3. **两路输入**：
   - ``seq`` 形状 ``(T, F)``：逐帧原始物理量序列（含距离门位掩码），交给卷积/循环层；
   - ``agg`` 形状 ``(A,)``：窗口级统计量，把父项目现有规则用的物理证据
     （速度 RMS、过零率、距离方差、门掩码稳定性）显式喂给模型。
     这两类特征在 ``breath_detector.py`` 里是人手写死的判定依据，
     在这里变成模型可见的输入，小样本下也能训得动。
4. **距离门位掩码**：``exist_gate_index`` 的每一位对应一个距离门
   （80cm 分辨率 16 门 / 20cm 分辨率 26 门），是雷达给的低分辨率一维距离剖面。
   这一步把「每方向 1 个点」变成「每方向 16~26 格」，是本方案的主要信息增量。
"""

import math
import os
import time
from collections import deque

import numpy as np

# 雷达量程 11m / 99 能量满量程 / 100cm·s⁻¹ 缩放，均为固定物理标度，
# 目的只是把数值压到 O(1)，真正的标准化由 Normalizer 承担。
DIST_SCALE = 11.0
ENERGY_SCALE = 99.0
SPEED_SCALE = 100.0
SPEED_CLIP = 2.0            # ±200 cm/s 之外按饱和处理
COUNTDOWN_CLIP = 100.0

SCALAR_NAMES = [
    'move_speed',        # 多普勒速度（有符号，保留低速微动）
    'move_distance',     # 运动目标距离
    'move_energy',       # 运动目标能量 0~99
    'exist_distance',    # 存在目标距离
    'exist_energy',      # 存在目标能量 0~99
    'exist_count_down',  # 存在倒计时
    'move_direction',    # 运动方向 0/1/2
    'target_status',     # 固件状态 0~5
    'light_log',         # log1p(环境光)
    'gate_count',        # 距离门置位数（占据格数）
    'fresh',             # 该格是否有实测帧（0=零阶保持）
]

AGG_NAMES = [
    'speed_rms', 'speed_mean_abs', 'speed_max_abs', 'speed_zero_cross',
    'speed_nonzero_frac',
    'move_energy_mean', 'move_energy_std', 'move_energy_max', 'move_energy_sat_frac',
    'exist_energy_mean', 'exist_energy_std', 'exist_energy_max',
    'move_dist_std', 'exist_dist_std', 'move_dist_range',
    'gate_count_mean', 'gate_change_frac', 'gate_union_frac', 'gate_max_run_frac',
    'countdown_mean', 'valid_frac',
]


def default_gate_bits():
    return int(os.getenv('C4002_GATE_BITS', '16'))


class WindowSpec:
    """窗口规格：时间窗长度、采样率、距离门位数与传感器角度顺序。"""

    def __init__(self, window_frames=20, fps=10.0, gate_bits=None, angles=(-45.0, 0.0, 45.0),
                 fresh_limit_s=None):
        self.window_frames = int(window_frames)
        self.fps = float(fps)
        self.gate_bits = int(gate_bits) if gate_bits else default_gate_bits()
        self.angles = [float(a) for a in angles]
        # 超过该时长未更新的样本视为丢失（默认 3 个采样周期）
        self.fresh_limit_s = (float(fresh_limit_s) if fresh_limit_s is not None
                              else 3.0 / max(self.fps, 1e-6))

    # --- 维度 ---
    @property
    def sensor_scalars(self):
        return len(SCALAR_NAMES)

    @property
    def seq_dim(self):
        return len(self.angles) * (self.sensor_scalars + self.gate_bits)

    @property
    def agg_dim(self):
        return len(self.angles) * len(AGG_NAMES)

    @property
    def window_seconds(self):
        return self.window_frames / max(self.fps, 1e-6)

    def to_dict(self):
        return {
            'window_frames': self.window_frames,
            'fps': self.fps,
            'gate_bits': self.gate_bits,
            'angles': self.angles,
            'fresh_limit_s': self.fresh_limit_s,
            'seq_dim': self.seq_dim,
            'agg_dim': self.agg_dim,
            'scalar_names': SCALAR_NAMES,
            'agg_names': AGG_NAMES,
        }

    @staticmethod
    def from_dict(d):
        spec = WindowSpec(
            window_frames=d['window_frames'], fps=d['fps'],
            gate_bits=d['gate_bits'], angles=d['angles'],
            fresh_limit_s=d.get('fresh_limit_s'))
        return spec


# ----------------------------------------------------------------------
# 单帧 → 特征
# ----------------------------------------------------------------------
def frame_to_vector(frame, spec):
    """把一帧原始物理量压成 ``sensor_scalars + gate_bits`` 维向量。"""
    if not frame or not frame.get('valid'):
        return np.zeros(spec.sensor_scalars + spec.gate_bits, dtype=np.float32)
    speed = float(frame.get('move_speed') or 0.0) / SPEED_SCALE
    speed = max(-SPEED_CLIP, min(SPEED_CLIP, speed))
    vec = [
        speed,
        min(float(frame.get('move_distance') or 0.0), DIST_SCALE) / DIST_SCALE,
        min(float(frame.get('move_energy') or 0.0), ENERGY_SCALE) / ENERGY_SCALE,
        min(float(frame.get('exist_distance') or 0.0), DIST_SCALE) / DIST_SCALE,
        min(float(frame.get('exist_energy') or 0.0), ENERGY_SCALE) / ENERGY_SCALE,
        min(float(frame.get('exist_count_down') or 0.0), COUNTDOWN_CLIP) / COUNTDOWN_CLIP,
        float(frame.get('move_direction') or 0.0) / 3.0,
        float(frame.get('target_status') or 0.0) / 5.0,
        math.log1p(max(float(frame.get('light') or 0.0), 0.0)) / 10.0,
        float(frame.get('gate_count') or 0.0) / max(spec.gate_bits, 1),
        1.0,  # fresh：实测帧
    ]
    gates = frame.get('gate_bits')
    if not gates:
        idx = int(frame.get('exist_gate_index') or 0)
        gates = [(idx >> i) & 1 for i in range(spec.gate_bits)]
    gates = list(gates)[:spec.gate_bits]
    if len(gates) < spec.gate_bits:
        gates = gates + [0] * (spec.gate_bits - len(gates))
    vec.extend(float(g) for g in gates)
    return np.asarray(vec, dtype=np.float32)


def _zero_sensor_vector(spec):
    v = np.zeros(spec.sensor_scalars + spec.gate_bits, dtype=np.float32)
    return v


# ----------------------------------------------------------------------
# 窗口缓冲
# ----------------------------------------------------------------------
class WindowBuilder:
    """按角度缓存最近若干帧，并构建对齐后的 ``(seq, agg)`` 输入。

    用法::

        wb = WindowBuilder(WindowSpec())
        for frame in hub.scan_all():
            wb.add(frame)
            if wb.ready():
                seq, agg = wb.build()
    """

    def __init__(self, spec=None):
        self.spec = spec or WindowSpec()
        self.buffers = {a: deque(maxlen=max(self.spec.window_frames * 6, 60))
                        for a in self.spec.angles}
        # 每个角度最后收到的时间戳：用于拒收「同一采样时刻被反复读入」的帧
        self.last_ts = {a: None for a in self.spec.angles}
        self.unknown_angle_frames = 0
        self.frames_total = 0
        self.duplicate_frames = 0

    def reset(self):
        for buf in self.buffers.values():
            buf.clear()
        for a in self.last_ts:
            self.last_ts[a] = None
        self.unknown_angle_frames = 0
        self.frames_total = 0
        self.duplicate_frames = 0

    def add(self, frame):
        """加入一帧。**时间戳不前进的帧会被丢弃**。

        这一步是必需的：采集/推理主循环的迭代频率远高于雷达 10Hz 的上报率
        （模拟数据源下可达每秒上万次），同一个采样时刻的帧会被反复读入。
        若不去重，环形缓冲会被同一帧的副本填满，构建出的窗口变成
        「前面全是空档 + 最后一格有数据」，与训练时的窗口分布完全不同，
        模型输出会退化成常数（实测所有概率恒为 1.0）。
        """
        if not frame:
            return
        if not frame.get('valid'):
            return
        ang = frame.get('angle')
        if ang is None:
            return
        ang = float(ang)
        self.frames_total += 1
        if ang not in self.buffers:
            self.unknown_angle_frames += 1
            return
        # 注意不能用 `ts or time.time()`：合成/回放数据源的起始时间戳就是 0.0，
        # 会被 `or` 判成「缺失」而替换成墙钟时间，导致后续帧全被判为乱序丢弃。
        ts_raw = frame.get('timestamp')
        ts = time.time() if ts_raw is None else float(ts_raw)
        prev = self.last_ts[ang]
        if prev is not None and ts <= prev:
            self.duplicate_frames += 1
            return
        self.last_ts[ang] = ts
        self.buffers[ang].append((ts, frame))

    def ready(self):
        """每路雷达都至少有一帧（能建出张量，但**未必覆盖完整时间窗**）。"""
        return all(len(b) > 0 for b in self.buffers.values())

    def covered(self):
        """缓冲是否覆盖了**完整一个时间窗**——出窗必须用它，而不是 ``ready()``。

        为什么关键：训练时的窗口永远是完整的 2s（``build_dataset`` 会先跳过
        ``window_frames`` 帧）。若推理/采集在「每路只有 1 帧」时就建窗，
        得到的窗口是「只有最后一格有数据、其余为空档」的形态，
        与训练分布完全不同，模型输出会退化成常数或恒为 0。
        实测：空场被稳定判成「有人」（P≈0.92），而训练/评估指标却是满分。

        判据用时间跨度而非帧数：丢帧时帧数可能够但时间跨度不够，
        零阶保持会把过期的帧填进去，同样属于分布外输入。
        """
        need = (self.spec.window_frames - 1) / max(self.spec.fps, 1e-6) * 0.95
        for buf in self.buffers.values():
            if not buf or (buf[-1][0] - buf[0][0]) < need:
                return False
        return True

    def _grid(self, now=None):
        """构造以最新样本时间为末端的等间隔时间栅格。"""
        latest = None
        for buf in self.buffers.values():
            if buf:
                ts = buf[-1][0]
                latest = ts if latest is None else max(latest, ts)
        if latest is None:
            latest = now if now is not None else time.time()
        dt = 1.0 / max(self.spec.fps, 1e-6)
        return latest - dt * (self.spec.window_frames - 1 - np.arange(self.spec.window_frames))

    @staticmethod
    def _sample(buf, t, fresh_limit):
        """零阶保持：取 ``ts <= t`` 的最近一帧；过旧或没有则返回 None。"""
        for i in range(len(buf) - 1, -1, -1):
            ts, frame = buf[i]
            if ts <= t + 1e-3:
                if (t - ts) > fresh_limit:
                    return None
                return frame
        return None

    def build(self, now=None):
        """返回 ``(seq, agg)``；缓冲为空时返回 ``(None, None)``。"""
        if not self.ready():
            return None, None
        grid = self._grid(now)
        seq = np.zeros((self.spec.window_frames, self.spec.seq_dim), dtype=np.float32)
        agg = np.zeros(self.spec.agg_dim, dtype=np.float32)
        zero_vec = _zero_sensor_vector(self.spec)

        for si, ang in enumerate(self.spec.angles):
            buf = self.buffers[ang]
            vlen = self.spec.sensor_scalars + self.spec.gate_bits
            block = np.zeros((self.spec.window_frames, vlen), dtype=np.float32)
            for ti, t in enumerate(grid):
                f = self._sample(buf, t, self.spec.fresh_limit_s)
                block[ti] = frame_to_vector(f, self.spec) if f is not None else zero_vec
            seq[:, si * vlen:(si + 1) * vlen] = block
            agg[si * len(AGG_NAMES):(si + 1) * len(AGG_NAMES)] = _aggregate(
                block, self.spec, valid=(block[:, -1] > 0.5))
        return seq, agg


def _aggregate(block, spec, valid=None):
    """从单传感器窗口矩阵算窗口级物理统计量（``AGG_NAMES`` 顺序）。"""
    T = block.shape[0]
    speed = block[:, 0] * SPEED_SCALE           # cm/s
    move_energy = block[:, 2] * ENERGY_SCALE
    exist_energy = block[:, 4] * ENERGY_SCALE
    move_dist = block[:, 1] * DIST_SCALE
    exist_dist = block[:, 3] * DIST_SCALE
    countdown = block[:, 5] * COUNTDOWN_CLIP
    gates = block[:, spec.sensor_scalars:]      # (T, G)
    fresh = block[:, SCALAR_NAMES.index('fresh')]

    abs_speed = np.abs(speed)
    mean_speed = float(speed.mean())
    zc = 0
    for i in range(1, T):
        a, b = speed[i - 1] - mean_speed, speed[i] - mean_speed
        if (a < 0 <= b) or (a > 0 >= b):
            zc += 1
    nz_move = move_dist[move_dist > 0.02]
    nz_exist = exist_dist[exist_dist > 0.02]

    # 门掩码稳定性：变化次数少、最长同态游程长 → 目标静止；频繁跳变 → 噪声/运动
    gate_change = 0
    max_run = 1
    run = 1
    for i in range(1, T):
        if not np.array_equal(gates[i], gates[i - 1]):
            gate_change += 1
            run = 1
        else:
            run += 1
            max_run = max(max_run, run)
    union = float((gates.max(axis=0) > 0.5).sum())

    return np.asarray([
        float(np.sqrt((speed ** 2).mean())) / SPEED_SCALE,
        float(abs_speed.mean()) / SPEED_SCALE,
        float(abs_speed.max()) / SPEED_SCALE,
        zc / max(T - 1, 1),
        float((abs_speed > 0).mean()),
        float(move_energy.mean()) / ENERGY_SCALE,
        float(move_energy.std()) / ENERGY_SCALE,
        float(move_energy.max()) / ENERGY_SCALE,
        float((move_energy >= 95).mean()),
        float(exist_energy.mean()) / ENERGY_SCALE,
        float(exist_energy.std()) / ENERGY_SCALE,
        float(exist_energy.max()) / ENERGY_SCALE,
        float(nz_move.std()) / DIST_SCALE if nz_move.size else 0.0,
        float(nz_exist.std()) / DIST_SCALE if nz_exist.size else 0.0,
        (float(nz_move.max() - nz_move.min()) / DIST_SCALE) if nz_move.size else 0.0,
        float(gates.sum(axis=1).mean()) / max(spec.gate_bits, 1),
        gate_change / max(T - 1, 1),
        union / max(spec.gate_bits, 1),
        max_run / max(T, 1),
        float(countdown.mean()) / COUNTDOWN_CLIP,
        float(fresh.mean()),
    ], dtype=np.float32)


# ----------------------------------------------------------------------
# 标准化
# ----------------------------------------------------------------------
class Normalizer:
    """逐特征 z-score 标准化。均值/方差来自训练集，随模型一起保存。"""

    def __init__(self, seq_mean=None, seq_std=None, agg_mean=None, agg_std=None):
        self.seq_mean = seq_mean
        self.seq_std = seq_std
        self.agg_mean = agg_mean
        self.agg_std = agg_std

    @staticmethod
    def _fit_one(x):
        mean = x.mean(axis=0)
        std = x.std(axis=0)
        std = np.where(std < 1e-6, 1.0, std)
        return mean.astype(np.float32), std.astype(np.float32)

    @classmethod
    def fit(cls, seq, agg):
        """``seq``: (N, T, F)；``agg``: (N, A)。"""
        seq2 = seq.reshape(-1, seq.shape[-1])
        seq_mean, seq_std = cls._fit_one(seq2)
        agg_mean, agg_std = cls._fit_one(agg)
        return cls(seq_mean, seq_std, agg_mean, agg_std)

    def transform_seq(self, seq):
        return (seq - self.seq_mean) / self.seq_std

    def transform_agg(self, agg):
        return (agg - self.agg_mean) / self.agg_std

    def transform(self, seq, agg):
        return self.transform_seq(seq), self.transform_agg(agg)

    def to_dict(self):
        return {
            'seq_mean': self.seq_mean.tolist() if self.seq_mean is not None else None,
            'seq_std': self.seq_std.tolist() if self.seq_std is not None else None,
            'agg_mean': self.agg_mean.tolist() if self.agg_mean is not None else None,
            'agg_std': self.agg_std.tolist() if self.agg_std is not None else None,
        }

    @classmethod
    def from_dict(cls, d):
        def conv(v):
            return None if v is None else np.asarray(v, dtype=np.float32)
        return cls(conv(d.get('seq_mean')), conv(d.get('seq_std')),
                   conv(d.get('agg_mean')), conv(d.get('agg_std')))


def feature_names(spec):
    """返回 ``(seq_feature_names, agg_feature_names)``，用于特征重要性排查。"""
    seq_names = []
    for ang in spec.angles:
        tag = f'{ang:+.0f}'
        seq_names += [f'{tag}:{n}' for n in SCALAR_NAMES]
        seq_names += [f'{tag}:gate{i}' for i in range(spec.gate_bits)]
    agg_names = [f'{a:+.0f}:{n}' for a in spec.angles for n in AGG_NAMES]
    return seq_names, agg_names


# ----------------------------------------------------------------------
# 特征消融（用于量化「新挖掘字段」的贡献）
# ----------------------------------------------------------------------
# 旧解析器 read_data() 实际暴露给上层、并被规则判定使用的字段对应的特征。
# 消融成 'legacy' 即模拟「不改进解析器、只用原有的 5 个标量」的情形。
LEGACY_SCALARS = ('move_speed', 'move_distance', 'move_energy',
                  'exist_distance', 'exist_energy')
# 依赖新增字段（距离门掩码 / 存在倒计时）才能算出的窗口统计量
LEGACY_EXTRA_AGG = ('gate_count_mean', 'gate_change_frac', 'gate_union_frac',
                    'gate_max_run_frac', 'countdown_mean')

ABLATE_MODES = ('none', 'gates', 'agg', 'legacy')


def _zero_cols(mat, col_indices):
    """把最后一维上的指定列置零。兼容 agg ``(N, A)`` 与 seq ``(N, T, F)``。"""
    if mat.ndim == 2:
        for c in col_indices:
            mat[:, c] = 0.0
    else:
        for c in col_indices:
            mat[:, :, c] = 0.0
    return mat


def ablate(seq, agg, spec, mode='none'):
    """把指定特征组置零，用于消融实验。

    * ``none``   不做处理
    * ``gates``  去掉距离门位掩码（保留其余全部字段）
    * ``agg``    去掉窗口统计量（只留时序支路）
    * ``legacy`` **只用旧解析器暴露的 5 个标量**，即不利用本次新挖掘的字段
                 （距离门掩码、存在倒计时、运动方向、固件状态、环境光）

    注意 ``fresh``（对齐新鲜度）在 legacy 模式下**保留**：它来自多传感器时间对齐，
    不属于雷达新字段，旧系统同样有帧有效性信息。
    """
    if mode not in ABLATE_MODES:
        raise ValueError(f'未知消融模式 {mode}，可选 {ABLATE_MODES}')
    seq = seq.copy()
    agg = agg.copy()
    if mode == 'none':
        return seq, agg

    if mode == 'agg':
        agg[:] = 0.0
        return seq, agg

    scalar_count = len(SCALAR_NAMES)
    drop_scalars = [SCALAR_NAMES.index(n) for n in SCALAR_NAMES
                    if n not in LEGACY_SCALARS and n != 'fresh'] if mode == 'legacy' else []
    for si in range(len(spec.angles)):
        base = si * (scalar_count + spec.gate_bits)
        if mode in ('gates', 'legacy'):
            _zero_cols(seq, [base + scalar_count + g for g in range(spec.gate_bits)])
        for c in drop_scalars:
            seq[:, :, base + c] = 0.0
    if mode == 'legacy':
        for si in range(len(spec.angles)):
            base = si * len(AGG_NAMES)
            for name in LEGACY_EXTRA_AGG:
                agg[:, base + AGG_NAMES.index(name)] = 0.0
    return seq, agg
