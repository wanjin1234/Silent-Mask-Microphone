# -*- coding: utf-8 -*-
"""实时人体存在检测：滑动窗口推理 + 滞回输出，可发布给主程序。

为什么推理不能直接用 0.5 阈值逐窗口判定
--------------------------------------
滑窗推理每秒产出 1~2 个概率，单帧概率抖动会导致 HUD 图标闪烁。这里做两件事：

1. **概率平滑**（``--smooth``，EMA）抑制单窗口抖动；
2. **双阈值滞回**：升到 ``--on-threshold`` 才置「有人」，降到 ``--off-threshold``
   才复位，且需连续 ``--min-on`` / ``--min-off`` 个窗口确认。
   这与父项目 ``c4002_parser.py`` 里 presence_on/off 计数的思路一致——
   误报和漏报的代价不对称时，滞回是必须的。

与主程序集成（两种方式，互不干扰）
----------------------------------
1. **独立进程 + JSON 文件**（推荐，零侵入）::

       python detect_live.py --source real --publish /tmp/presence.json

   主程序读该文件即可，不必改动 ``main_stereo.py``。

2. **同进程调用**：``from test_ai.detect_live import PresenceEngine``，
   把 ``engine.update(frame)`` 的返回值接到显示层。

三种数据源：``real``（真雷达）/ ``sim``（合成，无硬件演示）/ ``csv``（回放录制）。
``--backend rule`` 可切换到现行规则，用于现场 A/B 对比同一段输入的表现。
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import WindowSpec, WindowBuilder
from model_io import load_model
from rule_baseline import rule_scores


class PresenceEngine:
    """滑动窗口 + 平滑 + 滞回的存在判定引擎（可在主程序中直接实例化）。"""

    def __init__(self, spec=None, predict_fn=None, threshold=0.5,
                 on_threshold=None, off_threshold=None, smooth=0.4,
                 min_on=2, min_off=3, window_stride_s=0.5):
        self.spec = spec or WindowSpec()
        self.predict_fn = predict_fn
        self.on_threshold = float(on_threshold if on_threshold is not None else threshold)
        # 关阈值默认为开阈值的 0.7 倍，形成滞回带
        self.off_threshold = float(off_threshold if off_threshold is not None
                                   else max(self.on_threshold * 0.7, 0.01))
        self.smooth = float(smooth)
        self.min_on = int(min_on)
        self.min_off = int(min_off)
        self.stride_s = float(window_stride_s)
        self.wb = WindowBuilder(self.spec)
        self.prob_ema = None
        self.present = False
        self.on_streak = 0
        self.off_streak = 0
        self.n_windows = 0
        self._last_window_ts = None
        self.last_info = {}

    def reset(self):
        self.wb.reset()
        self.prob_ema = None
        self.present = False
        self.on_streak = self.off_streak = 0
        self.n_windows = 0
        self._last_window_ts = None

    def _should_run(self, newest_ts):
        if newest_ts is None:
            return False
        if self._last_window_ts is None:
            return True
        return (newest_ts - self._last_window_ts) >= self.stride_s

    def update(self, frames):
        """喂入一批帧（同一时刻的多路），返回本步的状态字典。

        未到推理间隔时返回上一次的状态（``updated=False``），调用方无需关心节奏。
        """
        newest = None
        for f in frames or []:
            if not f:
                continue
            self.wb.add(f)
            if f.get('valid'):
                ts = f.get('timestamp')
                if ts is not None:
                    ts = float(ts)
                    if newest is None or ts > newest:
                        newest = ts
        if not self.wb.covered() or not self._should_run(newest):
            return self._status(updated=False)
        seq, agg = self.wb.build()
        if seq is None:
            return self._status(updated=False)
        self._last_window_ts = newest
        self.n_windows += 1

        if self.predict_fn is None:
            raise RuntimeError('未提供 predict_fn')
        prob = float(np.asarray(self.predict_fn(seq[None, ...], agg[None, ...])).ravel()[0])
        self.prob_ema = prob if self.prob_ema is None else \
            (self.smooth * prob + (1 - self.smooth) * self.prob_ema)

        # 滞回 + 连续确认
        if not self.present:
            if self.prob_ema >= self.on_threshold:
                self.on_streak += 1
                if self.on_streak >= self.min_on:
                    self.present = True
                    self.off_streak = 0
            else:
                self.on_streak = 0
        else:
            if self.prob_ema <= self.off_threshold:
                self.off_streak += 1
                if self.off_streak >= self.min_off:
                    self.present = False
                    self.on_streak = 0
            else:
                self.off_streak = 0
        return self._status(updated=True, prob=prob, seq=seq)

    def _status(self, updated=False, prob=None, seq=None):
        info = {
            'present': bool(self.present),
            'updated': bool(updated),
            'prob': None if self.prob_ema is None else round(float(self.prob_ema), 4),
            'prob_raw': None if prob is None else round(float(prob), 4),
            'windows': self.n_windows,
            'timestamp': time.time(),
        }
        if updated:
            info.update(self._estimate_target(seq))
        self.last_info = info
        return info

    def _estimate_target(self, seq):
        """顺带给出「最近的疑似目标」距离与方位，便于 HUD 显示。

        取窗口内最后一个时间步，选存在能量最大的那个传感器。
        注意：这只是**展示用**的粗略估计，判定本身由模型完成。
        """
        from features import SCALAR_NAMES, DIST_SCALE, ENERGY_SCALE
        best = None
        for si, ang in enumerate(self.spec.angles):
            base = si * (len(SCALAR_NAMES) + self.spec.gate_bits)
            e_idx = base + SCALAR_NAMES.index('exist_energy')
            d_idx = base + SCALAR_NAMES.index('exist_distance')
            m_idx = base + SCALAR_NAMES.index('move_distance')
            energy = float(seq[-1, e_idx]) * ENERGY_SCALE
            dist = float(seq[-1, d_idx]) * DIST_SCALE
            if dist <= 0:
                dist = float(seq[-1, m_idx]) * DIST_SCALE
            if best is None or energy > best[0]:
                best = (energy, ang, dist)
        return {'target_angle': best[1], 'target_distance': round(best[2], 2),
                'target_energy': round(best[0], 1)}


# ----------------------------------------------------------------------
# 数据源
# ----------------------------------------------------------------------
def source_real(spec, ports, angles, baud):
    from c4002_ext import RawRadarHub
    return RawRadarHub(ports=ports, angles=angles, baud=baud, gate_bits=spec.gate_bits)


def source_sim(spec, mode='mixed', speed=1.0, seed=0):
    """合成数据源：无硬件时演示/调试显示与逻辑。"""
    from synth import SessionSim

    class _Radar:
        def __init__(self, hub, angle, i):
            self.hub, self.angle, self.sensor_id = hub, angle, i
            self.port = f'sim:{angle:+.0f}'
            self.debug = False

        def read_data(self):
            return self.hub.frames().get(self.angle, {'valid': False})

        def reset_detection(self):
            pass

    class _Hub:
        def __init__(self):
            self.rng = np.random.default_rng(seed)
            self.sim = None
            self.mode = mode
            self.speed = speed
            self.dt = 1.0 / spec.fps
            self.idx = -1
            self._frames = {}
            self._wall0 = None
            self.errors = []
            self.radars = [_Radar(self, a, i) for i, a in enumerate(spec.angles)]
            self._maybe_switch(0)

        def _maybe_switch(self, idx):
            # 'mixed' 场景每 40s 切换一次，方便一屏看到有人/没人交替
            block = idx // int(40 * spec.fps)
            if self.sim is None or block != getattr(self, '_block', None):
                self._block = block
                m = ['empty', 'still', 'moving'][block % 3] if self.mode == 'mixed' else self.mode
                self.sim = SessionSim(spec, self.rng, m)

        def frames(self):
            now = time.time()
            if self._wall0 is None:
                self._wall0 = now
            idx = int((now - self._wall0) * self.speed * spec.fps)
            if idx != self.idx:
                self.idx = idx
                self._maybe_switch(idx)
                self._frames = {f['angle']: f
                                for f in self.sim.step(idx * self.dt, self.dt)}
            return self._frames

    return _Hub()


def source_csv(spec, path, speed=1.0):
    """回放逐帧 CSV（``capture_radar_signal.py`` 的产出，或自己录的同类文件）。

    缺少的列按 0 处理：旧版 CSV 只有 ``move_speed/move_*_energy/exist_*``，
    没有距离门掩码等新字段，因此回放时这些特征恒为 0，判定会比真机保守。
    想要完整回放，请用 ``probe_gates.py --csv`` 录一份含新字段的 CSV。
    """
    import csv

    rows = []
    with open(path, 'r', newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append(r)

    def _f(r, key, default=0.0):
        v = r.get(key)
        try:
            return float(v) if v not in (None, '') else default
        except (TypeError, ValueError):
            return default

    class _Radar:
        def __init__(self, hub, angle, i):
            self.hub, self.angle, self.sensor_id = hub, angle, i
            self.port = f'csv:{path}'
            self.debug = False

        def read_data(self):
            return self.hub.frames().get(self.angle, {'valid': False})

        def reset_detection(self):
            pass

    class _Hub:
        def __init__(self):
            self.speed = speed
            self.t0 = None
            self.errors = []
            angles = sorted({_f(r, 'angle') for r in rows}) or list(spec.angles)
            self.radars = [_Radar(self, a, i) for i, a in enumerate(angles)]
            self._rows_by_angle = {}
            for r in rows:
                self._rows_by_angle.setdefault(_f(r, 'angle'), []).append(r)

        def frames(self):
            if not rows:
                return {}
            now = time.time()
            if self.t0 is None:
                self.t0 = now
            elapsed = (now - self.t0) * self.speed
            out = {}
            for ang, rs in self._rows_by_angle.items():
                ts0 = _f(rs[0], 'timestamp')
                idx = 0
                for i, r in enumerate(rs):
                    if _f(r, 'timestamp') - ts0 <= elapsed:
                        idx = i
                    else:
                        break
                r = rs[idx]
                # 关键：帧时间戳必须沿用 **CSV 里的相对时间**，不能填当前墙钟。
                # 若每帧都盖同一个墙钟时间，窗口对齐会认为「2 秒窗内只有一帧」，
                # 构建出的窗口退化成「前面全是空档 + 最后一格有数据」，
                # 模型输出恒为 0（实测如此）。用 t0 + 录制偏移量则能保持
                # 原始 0.1s 采样间隔，且回放倍速不影响特征（窗口始终是 2 个"录制秒"）。
                ts_play = self.t0 + (_f(r, 'timestamp') - ts0)
                gi = _f(r, 'exist_gate_index')
                out[ang] = {
                    'angle': ang, 'valid': True,
                    'timestamp': ts_play,
                    'target_status': _f(r, 'target_status'),
                    'light': _f(r, 'light'),
                    'exist_gate_index': gi,
                    'gate_count': bin(int(gi)).count('1'),
                    'gate_bits': [(int(gi) >> b) & 1 for b in range(spec.gate_bits)],
                    'exist_count_down': _f(r, 'exist_count_down'),
                    'exist_distance': _f(r, 'exist_distance'),
                    'exist_energy': _f(r, 'exist_energy'),
                    'move_distance': _f(r, 'move_distance'),
                    'move_speed': _f(r, 'move_speed'),
                    'move_energy': _f(r, 'move_energy'),
                    'move_direction': _f(r, 'move_direction'),
                }
            return out

    return _Hub()


def main():
    p = argparse.ArgumentParser(description='实时人体存在检测（模型推理 + 滞回）')
    p.add_argument('--model-dir', default='models')
    p.add_argument('--name', default='presence_model')
    p.add_argument('--backend', default='auto', choices=['auto', 'rule'],
                   help='auto=加载训练好的模型；rule=改用现行规则（现场 A/B 对比用）')
    p.add_argument('--rule-mode', default='motion',
                   choices=['motion', 'breath', 'motion_or_breath'])
    p.add_argument('--source', default='sim', choices=['real', 'sim', 'csv'])
    p.add_argument('--csv', default=None, help='--source csv 时的文件路径')
    p.add_argument('--ports', default=None, help='逗号分隔，默认取 RADAR_PORTS')
    p.add_argument('--angles', default=None, help='逗号分隔，默认取 RADAR_ANGLES')
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--sim-mode', default='mixed',
                   choices=['mixed', 'empty', 'still', 'moving'])
    p.add_argument('--sim-speed', type=float, default=1.0)
    p.add_argument('--seconds', type=float, default=0, help='0=一直运行')
    p.add_argument('--window-frames', type=int, default=None,
                   help='默认取模型 meta 里的值，切勿手改（会与训练不一致）')
    p.add_argument('--fps', type=float, default=None)
    p.add_argument('--on-threshold', type=float, default=None,
                   help='判「有人」的概率阈值，默认取模型 meta 里的 threshold')
    p.add_argument('--off-threshold', type=float, default=None,
                   help='判「无人」的概率阈值，默认取开阈值的 0.7 倍（形成滞回）')
    p.add_argument('--smooth', type=float, default=0.4, help='概率 EMA 系数')
    p.add_argument('--min-on', type=int, default=2)
    p.add_argument('--min-off', type=int, default=3)
    p.add_argument('--stride-s', type=float, default=0.5, help='推理间隔（秒）')
    p.add_argument('--publish', default=None,
                   help='把状态写到该 JSON 文件，供主程序读取（零侵入集成）')
    p.add_argument('--log', default=None, help='把每步概率写入 CSV，便于事后分析')
    args = p.parse_args()

    # 模型优先：窗口规格必须与训练一致，否则输入维度对不上
    predict_fn = None
    meta = None
    if args.backend == 'auto':
        predict_fn, meta, backend = load_model(args.model_dir, args.name)
        spec = WindowSpec.from_dict(meta['spec'])
        if args.window_frames and args.window_frames != spec.window_frames:
            print(f'[WARN] --window-frames={args.window_frames} 与模型训练时'
                  f'（{spec.window_frames}）不一致，已强制使用模型的值')
        if args.fps and abs(args.fps - spec.fps) > 1e-6:
            print(f'[WARN] --fps={args.fps} 与模型训练时（{spec.fps}）不一致，'
                  f'已强制使用模型的值')
        threshold = (args.on_threshold if args.on_threshold is not None
                     else float(meta.get('threshold', 0.5)))
        print(f'已加载模型：{args.model_dir}/{args.name}（{meta.get("arch")} / {backend} 后端）')
    else:
        backend = 'rule'
        angles = (args.angles or os.getenv('RADAR_ANGLES') or '-45,0,45')
        spec = WindowSpec(window_frames=args.window_frames or 20,
                          fps=args.fps or 10.0,
                          angles=[float(x) for x in angles.split(',') if x.strip()])
        threshold = args.on_threshold if args.on_threshold is not None else 0.5
        print(f'使用现行规则基线（{args.rule_mode}），不加载模型')

    print(f'窗口 {spec.window_frames} 帧 @ {spec.fps}Hz = {spec.window_seconds:.1f}s，'
          f'距离门 {spec.gate_bits} 位，方位 {spec.angles}')
    print(f'开阈值 {threshold:.2f}  关阈值 '
          f'{(args.off_threshold if args.off_threshold is not None else threshold * 0.7):.2f}  '
          f'平滑 {args.smooth}  确认 {args.min_on}/{args.min_off}')

    # 数据源
    if args.source == 'real':
        ports = (args.ports or os.getenv('RADAR_PORTS') or '').strip()
        ports = [x.strip() for x in ports.split(',') if x.strip()] if ports else None
        if not ports:
            print('--source real 需要 --ports 或环境变量 RADAR_PORTS')
            return 2
        hub = source_real(spec, ports, spec.angles, args.baud)
    elif args.source == 'csv':
        if not args.csv:
            print('--source csv 需要 --csv <路径>')
            return 2
        hub = source_csv(spec, args.csv, speed=args.sim_speed)
    else:
        hub = source_sim(spec, mode=args.sim_mode, speed=args.sim_speed)
    print(f'数据源：{args.source}（{len(hub.radars)} 路雷达）\n')

    engine = PresenceEngine(
        spec=spec, predict_fn=predict_fn, threshold=threshold,
        on_threshold=args.on_threshold, off_threshold=args.off_threshold,
        smooth=args.smooth, min_on=args.min_on, min_off=args.min_off,
        window_stride_s=args.stride_s)

    log_f = None
    if args.log:
        log_f = open(args.log, 'w', newline='', encoding='utf-8')
        log_f.write('wall_time,present,prob,prob_raw,target_angle,target_distance,'
                    'target_energy\n')
    t0 = time.time()
    last_line = 0.0
    try:
        while True:
            if args.seconds and (time.time() - t0) >= args.seconds:
                break
            frames = [r.read_data() for r in hub.radars]
            if backend == 'rule':
                # 规则后端：攒够一个窗口就对整窗跑一次规则，与模型口径一致
                st = _update_rule(engine, frames, spec, args.rule_mode)
            else:
                st = engine.update(frames)
            if st.get('updated'):
                if log_f:
                    log_f.write(f'{time.time():.3f},{int(st["present"])},'
                                f'{st.get("prob")},{st.get("prob_raw")},'
                                f'{st.get("target_angle")},{st.get("target_distance")},'
                                f'{st.get("target_energy")}\n')
                if args.publish:
                    _publish(args.publish, st, spec)
            now = time.time()
            if now - last_line > 0.25:
                last_line = now
                mark = '有人' if st.get('present') else '无人'
                prob = st.get('prob')
                prob_s = '  --  ' if prob is None else f'{prob:5.3f}'
                dist = st.get('target_distance')
                dist_s = '  --' if dist is None else f'{dist:4.2f}m'
                ang = st.get('target_angle')
                ang_s = '   --' if ang is None else f'{ang:+5.0f}°'
                el = time.time() - t0
                print(f'\r[{el:6.1f}s] {mark}  P(有人)={prob_s}  目标 {ang_s} {dist_s}  '
                      f'窗口 {st.get("windows", 0)}', end='', flush=True)
    except KeyboardInterrupt:
        print('\n已中断')
    finally:
        if log_f:
            log_f.close()
        print()
    return 0


def _update_rule(engine, frames, spec, rule_mode):
    """规则后端的适配层：复用 PresenceEngine 的窗口/滞回，只替换概率来源。"""
    if engine.predict_fn is None:
        def rule_predict(seq, agg):
            dec, _ = rule_scores(seq, spec, mode=rule_mode, agg=agg)
            # 规则输出的是 0/1 硬判定，直接当「概率」用；滞回仍由 engine 统一负责
            return dec.astype(np.float32)
        engine.predict_fn = rule_predict
        engine.on_threshold = 0.5
        engine.off_threshold = 0.5
    return engine.update(frames)


def _publish(path, st, spec):
    """原子写 JSON，避免主程序读到写了一半的文件。"""
    payload = {
        'present': st.get('present'),
        'prob': st.get('prob'),
        'target_angle': st.get('target_angle'),
        'target_distance': st.get('target_distance'),
        'target_energy': st.get('target_energy'),
        'windows': st.get('windows'),
        'timestamp': st.get('timestamp'),
        'angles': spec.angles,
    }
    tmp = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f'\n[WARN] 发布状态失败：{e}')


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
