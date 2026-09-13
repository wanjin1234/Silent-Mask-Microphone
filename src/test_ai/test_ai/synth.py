# -*- coding: utf-8 -*-
"""合成雷达场景 → 训练集。无需硬件即可跑通并验证整条流水线。

用途与边界（请诚实看待）
------------------------
本模块**不替代真机数据**，作用有两个：

1. 让 ``train.py`` / ``detect_live.py`` 在没有雷达的机器（比如 Windows 开发机）
   上能完整跑通，验证维度、对齐、归一化、训练、推理、导出各环节没写错；
2. 把项目已知的**真实误报机理**显式写进数据里，作为模型必须学会区分的难点。

它模拟的物理量（全部对应 ``c4002_ext.py`` 的字段）：

* 空场：多普勒速度不是干净的 0，而是带 1/f 低频起伏的噪声（电源纹波、
  风扇空调、桌面微振动），能量有静态杂波底，固件偶发把静态强反射
  误报成存在；``exist_gate_index`` 偶尔有孤立门被点亮。
* 空场 + **杂波突发**：模拟项目 README 里记录的「天花板强反射 / 吊扇」场景——
  持续不到 1 秒的 5~15cm/s 速度与高存在能量。旧规则（速度>5cm/s 即判有人）
  会在这里误报，而门掩码与历史一致性并不支持「有人」。
* 有人（静止呼吸）：速度呈 0.15~0.4Hz 周期性微动，幅值 2~8cm/s；
  存在距离稳定、存在能量高、``exist_count_down`` 持续累积、距离门掩码
  稳定地聚集在人所在的那几个门。
* 有人（走动/挥手）：速度 20~120cm/s、正负交替，距离随时间变化，
  运动能量高，门掩码随人移动而移动。
* 多传感器一致性：人体只在邻近 1~2 个雷达上有强响应，其余雷达接近空场。

因此真机训练时请用 ``collect.py`` 采数据，本模块只用于打通流程与冒烟测试。
"""

import argparse
import json
import math
import os
import sys

import numpy as np

# 允许 `python src/test_ai/synth.py` 从任意目录运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import WindowSpec, WindowBuilder

RESOLUTION_M = 0.8          # 80cm 分辨率 → 16 个距离门（11m 量程内）


def _dist_to_gate(dist_m, gate_bits):
    if dist_m <= 0:
        return -1
    g = int(dist_m / RESOLUTION_M)
    return g if 0 <= g < gate_bits else -1


class SessionSim:
    """一段场景（空场 / 有人静止 / 有人运动）的逐帧生成器。"""

    def __init__(self, spec, rng, mode, angles=None):
        self.spec = spec
        self.rng = rng
        self.mode = mode                       # 'empty' | 'still' | 'moving'
        self.angles = [float(a) for a in (angles or spec.angles)]
        # 人体的角度与距离：随机落在某个雷达附近，或落在两个雷达之间
        self.person_angle = float(rng.choice(self.angles))
        self.person_dist = float(rng.uniform(1.2, 5.0))
        self.breath_hz = float(rng.uniform(0.15, 0.4))       # 呼吸频率
        self.breath_amp = float(rng.uniform(2.0, 8.0))       # cm/s
        self.sway_phase = float(rng.uniform(0, 2 * math.pi))
        # 空场的慢漂移状态（AR(1)），每个传感器独立
        self.noise_state = {a: 0.0 for a in self.angles}
        self.clutter_until = 0.0
        self.clutter_amp = 0.0
        self.t_walk = 0.0
        # 慢速周期干扰源（吊扇 / 空调出风 / 摆动窗帘）：落在呼吸频段内，
        # 单看速度时序几乎与「呼吸的人」不可区分——这正是 README 里记录的
        # 已知失效场景，也是本模型必须靠门掩码/能量/距离一致性才能拆开的地方。
        self.interf_sensor = None
        self.interf_hz = 0.0
        self.interf_amp = 0.0
        self.interf_phase = 0.0
        if mode == 'empty' and rng.random() < 0.4:
            self.interf_sensor = float(rng.choice(self.angles))
            self.interf_hz = float(rng.uniform(0.18, 0.5))
            self.interf_amp = float(rng.uniform(3.0, 9.0))
            self.interf_phase = float(rng.uniform(0, 2 * math.pi))
        # 空场的「幻影目标」：固件在陌生环境里把静态强反射（天花板/金属门/墙角）
        # 当成目标跟踪，持续数秒并给出看似合理的距离与能量。这是父项目 README
        # 记录的主要误报来源，也是本数据集必须包含的难点——没有它，
        # 「exist_distance>0 即有人」这种平凡规则就能满分，模型收益无从体现。
        # 每个传感器**独立**触发：真实静态反射只会被正对它的那一路雷达看到，
        # 三路同时出现同一距离的「幻影」在物理上不合理。
        self.phantom_until = {a: 0.0 for a in self.angles}
        self.phantom_dist = {a: 0.0 for a in self.angles}

    def _sensor_weight(self, ang):
        """人体对该雷达的响应强度：越靠近人体方位越强。"""
        d = abs(ang - self.person_angle)
        return math.exp(-((d / 26.0) ** 2))

    def _speeds_empty(self, t, dt):
        out = {}
        for a in self.angles:
            # AR(1) 低频噪声：真实空场的 move_speed 非零，这也是父项目要设 2cm/s 死区的原因
            s = 0.86 * self.noise_state[a] + self.rng.normal(0.0, 0.55)
            # 偶发杂波突发（吊扇/空调/桌面振动）
            if t >= self.clutter_until and self.rng.random() < 0.006:
                self.clutter_until = t + float(self.rng.uniform(0.3, 1.0))
                self.clutter_amp = float(self.rng.uniform(5.0, 15.0))
            if t < self.clutter_until:
                s += self.clutter_amp * math.sin(2 * math.pi * 1.7 * t) * 0.6
            if self.interf_sensor is not None and a == self.interf_sensor:
                s += self.interf_amp * math.sin(2 * math.pi * self.interf_hz * t + self.interf_phase)
            self.noise_state[a] = s
            out[a] = s
        return out

    def step(self, t, dt):
        """返回该时刻三路雷达的帧列表（字段与 ``c4002_ext.read_data()`` 一致）。"""
        frames = []
        empty_speeds = self._speeds_empty(t, dt)

        for ang in self.angles:
            w = self._sensor_weight(ang) if self.mode != 'empty' else 0.0
            speed = empty_speeds[ang]
            frame = {
                'angle': ang, 'valid': True, 'timestamp': t,
                'target_status': 0, 'light': 300, 'exist_gate_index': 0,
                'exist_count_down': 0, 'exist_distance': 0.0, 'exist_energy': 0,
                'move_distance': 0.0, 'move_energy': 0, 'move_direction': 0,
                'gate_count': 0,
            }

            # --- 静态杂波底：空场也有低能量与零星门点亮（固件误报来源）---
            base_exist_energy = int(np.clip(self.rng.normal(12, 5), 0, 30))
            frame['exist_energy'] = base_exist_energy
            if self.rng.random() < 0.12:
                gi = int(self.rng.integers(0, self.spec.gate_bits))
                frame['exist_gate_index'] = 1 << gi
                frame['gate_count'] = 1
                frame['exist_distance'] = round(gi * RESOLUTION_M + RESOLUTION_M / 2, 2)
                frame['target_status'] = 1  # 固件偶发把静态强反射报成存在
            # 幻影目标：持续数秒、距离稳定、能量中等——单看能量/距离无法与真人区分
            if self.mode == 'empty':
                if t >= self.phantom_until[ang] and self.rng.random() < 0.004:
                    self.phantom_until[ang] = t + float(self.rng.uniform(1.0, 4.0))
                    self.phantom_dist[ang] = float(self.rng.uniform(0.8, 6.5))
                if t < self.phantom_until[ang]:
                    pd = max(0.4, self.phantom_dist[ang] + self.rng.normal(0, 0.05))
                    gi = _dist_to_gate(pd, self.spec.gate_bits)
                    gate_idx = 0
                    if gi >= 0:
                        for d in (-1, 0, 1):
                            g = gi + d
                            if 0 <= g < self.spec.gate_bits and self.rng.random() < 0.7:
                                gate_idx |= (1 << g)
                    frame['exist_gate_index'] = int(gate_idx)
                    frame['gate_count'] = bin(gate_idx).count('1')
                    frame['exist_distance'] = round(pd, 2)
                    frame['exist_energy'] = int(np.clip(self.rng.normal(55, 12), 0, 99))
                    frame['move_energy'] = int(np.clip(self.rng.normal(18, 7), 0, 60))
                    frame['move_distance'] = round(pd, 2)
                    frame['target_status'] = 1
            if t < self.clutter_until:
                frame['move_energy'] = int(np.clip(self.rng.normal(25, 8), 0, 60))
            # 慢速周期干扰源（吊扇）同时抬高运动能量——能量本身也不再是可靠判据
            if self.interf_sensor is not None and abs(ang - self.interf_sensor) < 1e-6:
                frame['move_energy'] = max(frame['move_energy'],
                                          int(np.clip(self.rng.normal(30, 10), 0, 70)))
                frame['move_distance'] = round(float(self.rng.uniform(1.0, 6.0)), 2)

            # --- 人体贡献 ---
            if w > 0.03:
                # 缓慢摆动：人在原地也会有微小位置漂移
                self.t_walk += dt
                sway = 0.25 * math.sin(2 * math.pi * 0.08 * self.t_walk + self.sway_phase)
                dist = max(0.6, self.person_dist + sway * (1.5 if self.mode == 'moving' else 0.35))
                gi = _dist_to_gate(dist, self.spec.gate_bits)
                # 距离门掩码：目标所在门及其相邻门被点亮（同一个人体跨相邻门反射）
                gate_idx = 0
                if gi >= 0:
                    for d in (-1, 0, 1):
                        g = gi + d
                        if 0 <= g < self.spec.gate_bits and self.rng.random() < 0.85:
                            gate_idx |= (1 << g)
                frame['exist_gate_index'] = int(gate_idx)
                frame['gate_count'] = bin(gate_idx).count('1')
                frame['exist_distance'] = round(dist, 2)
                frame['exist_energy'] = int(np.clip(
                    w * (95 - 25 * dist) + self.rng.normal(0, 4), 0, 99))
                frame['exist_count_down'] = int(np.clip(
                    w * 40 + self.rng.normal(0, 3), 0, 65535))

                if self.mode == 'still':
                    # 呼吸：周期性低速微动 + 噪声底。幅值与频率是区分人与空场噪声的关键
                    breath = (self.breath_amp * math.sin(2 * math.pi * self.breath_hz * t)
                              + self.rng.normal(0, 0.8))
                    frame['move_speed'] = int(np.clip(w * breath + 0.3 * speed, -30, 30))
                    frame['move_energy'] = int(np.clip(
                        w * 45 + 18 * abs(math.sin(2 * math.pi * self.breath_hz * t))
                        + self.rng.normal(0, 5), 0, 99))
                    frame['move_distance'] = round(dist, 2)
                    frame['move_direction'] = 1 if breath > 0 else 2
                    frame['target_status'] = 1 if frame['exist_energy'] > 20 else 0
                else:
                    # 走动 / 挥手：大幅速度并有方向反转（叠加噪声底，模拟真实信噪比）
                    v = w * (70 * math.sin(2 * math.pi * 0.7 * t + self.sway_phase)
                             + self.rng.normal(0, 8)) + 0.3 * speed
                    frame['move_speed'] = int(np.clip(v, -150, 150))
                    frame['move_distance'] = round(dist, 2)
                    frame['move_energy'] = int(np.clip(w * 80 + self.rng.normal(0, 8), 0, 99))
                    frame['move_direction'] = 1 if v > 0 else 2
                    frame['target_status'] = 2 if abs(v) > 12 else 1
            else:
                # 空场，或该雷达看不到人：只保留噪声底。
                # 真实 C4002 的 move_speed 从来不是干净的 0（电源纹波 / 风扇 /
                # 桌面微振动都会让它持续小幅非零），父项目为此才设了 2cm/s 死区。
                frame['move_speed'] = int(np.clip(speed, -30, 30))
                frame['move_energy'] = max(frame['move_energy'], int(np.clip(
                    abs(speed) * 2.0 + self.rng.normal(3, 2), 0, 45)))

            # 与 c4002_ext.RawC4002Serial.read_data() 输出保持字段一致：
            # 真实帧一定带 gate_bits 列表，合成帧也补上，避免消费方代码在
            # 「合成 vs 真机」两条路径上出现 KeyError 之类的差异。
            gi = int(frame['exist_gate_index'])
            frame['gate_bits'] = [(gi >> b) & 1 for b in range(self.spec.gate_bits)]
            frames.append(frame)
        return frames


def simulate(spec, mode, seconds, rng, fps=None):
    """生成一段场景的帧序列（list of lists，每项为某一时刻的三路帧）。"""
    fps = fps or spec.fps
    dt = 1.0 / fps
    sim = SessionSim(spec, rng, mode)
    n = int(round(seconds * fps))
    return [sim.step(i * dt, dt) for i in range(n)]


def build_dataset(spec, modes, sessions_per_mode, seconds, seed=0, stride=3):
    """把若干场景片段转成 ``(seq, agg, y, groups)`` 数据集。

    ``stride`` 控制滑窗采样步长（帧）：太小则相邻样本高度相关，训练/验证会虚高。
    出窗条件与推理侧一致，用 ``WindowBuilder.covered()``（缓冲必须覆盖完整时间窗），
    而不是「数够 N 帧」——两者不一致会造成训练/上线分布偏移。

    返回的 ``groups`` 标记样本属于哪一段场景，供 ``group_split`` 按段切分。
    """
    rng = np.random.default_rng(seed)
    seqs, aggs, ys, groups = [], [], [], []
    gid = 0
    for mode in modes:
        label = 0 if mode == 'empty' else 1
        for _ in range(sessions_per_mode):
            frames = simulate(spec, mode, seconds, rng)
            wb = WindowBuilder(spec)
            since_last = stride  # 让第一个可用窗口立即产出
            for group in frames:
                for f in group:
                    wb.add(f)
                since_last += 1
                if not wb.covered():
                    continue
                if since_last % stride:
                    continue
                seq, agg = wb.build()
                if seq is None:
                    continue
                seqs.append(seq)
                aggs.append(agg)
                ys.append(label)
                groups.append(gid)
            gid += 1
    if not seqs:
        raise RuntimeError('未生成任何样本，请加大 --seconds 或减小 --stride')
    return (np.asarray(seqs, dtype=np.float32), np.asarray(aggs, dtype=np.float32),
            np.asarray(ys, dtype=np.int64), np.asarray(groups, dtype=np.int64))


def group_split(groups, y, val_frac=0.3, seed=0):
    """按**片段**切分训练/验证，避免同段场景的相邻窗口同时出现在两边。

    这是时序数据最容易踩的坑：随机按样本切分会让验证集精度虚高
    （相邻窗口几乎一样），上线后表现远低于预期。
    """
    rng = np.random.default_rng(seed)
    uniq = np.unique(groups)
    rng.shuffle(uniq)
    n_val = max(1, int(round(len(uniq) * val_frac)))
    val_groups = set(uniq[:n_val].tolist())
    val_idx = np.array([i for i, g in enumerate(groups) if g in val_groups], dtype=np.int64)
    train_idx = np.array([i for i, g in enumerate(groups) if g not in val_groups], dtype=np.int64)
    return train_idx, val_idx


def save_npz(path, seq, agg, y, groups, spec):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    np.savez_compressed(path, seq=seq, agg=agg, y=y, groups=groups,
                        spec_json=np.asarray(json.dumps(spec.to_dict())))
    return path


def load_npz(path):
    """读取 ``save_npz`` 写出的数据集，返回 ``(seq, agg, y, groups, spec)``。"""
    d = np.load(path, allow_pickle=False)
    spec = WindowSpec.from_dict(json.loads(str(d['spec_json'])))
    groups = d['groups'] if 'groups' in d else np.zeros(len(d['y']), dtype=np.int64)
    return d['seq'], d['agg'], d['y'], groups, spec


def main():
    p = argparse.ArgumentParser(description='生成合成雷达数据集（无硬件）')
    p.add_argument('--out', default='data/synth.npz')
    p.add_argument('--sessions', type=int, default=24, help='每种场景的片段数')
    p.add_argument('--seconds', type=float, default=30.0, help='每段时长 s')
    p.add_argument('--stride', type=int, default=3, help='滑窗采样步长（帧）')
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--window-frames', type=int, default=20)
    p.add_argument('--fps', type=float, default=10.0)
    p.add_argument('--gate-bits', type=int, default=None)
    args = p.parse_args()

    spec = WindowSpec(window_frames=args.window_frames, fps=args.fps,
                      gate_bits=args.gate_bits)
    seq, agg, y, groups = build_dataset(
        spec, modes=['empty', 'still', 'moving'],
        sessions_per_mode=args.sessions, seconds=args.seconds,
        seed=args.seed, stride=args.stride)
    save_npz(args.out, seq, agg, y, groups, spec)
    print(f'窗口 {spec.window_frames} 帧 @ {spec.fps}Hz = {spec.window_seconds:.1f}s，'
          f'seq_dim={spec.seq_dim} agg_dim={spec.agg_dim}')
    print(f'样本 {len(y)}：空场={(y == 0).sum()} 有人={(y == 1).sum()}，'
          f'片段 {len(np.unique(groups))}')
    print(f'已写入 {args.out}')


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    main()
