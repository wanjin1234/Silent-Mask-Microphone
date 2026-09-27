# -*- coding: utf-8 -*-
"""真机标注采集：把 C4002 原始帧+标签写成训练用 npz。

为什么采集脚本必须独立存在
--------------------------
模型上限由数据决定。这个项目的关键难点（吊扇、幻影目标、呼吸级微动）
只能靠**真机、带标签、且分场次**的数据来学，任何合成数据都替代不了。

采集要求（照做，否则训出来的模型上线会崩）
------------------------------------------
1. **分场次多次采集**：同一次录制里的相邻窗口高度相似。每个类别至少采 10 段，
   每段 20~60s，''--session'' 每次换一个名字（或直接让脚本自动编号）。
   训练/验证按段切分（``train.py`` 的 ``group_split`` 就是干这个的）。
2. **负样本要包含「像人」的干扰**：空场但开着吊扇/空调/有人走动经过；
   有人在附近但不该触发（如隔墙、隔壁房间）。
3. **正样本要覆盖难例**：静止只呼吸的人（本项目当前测不到的那种）、
   缓慢挥手、坐着轻微活动、以及正常走动。
4. 采集时环境尽量与比赛/实际使用一致，换房间要重新采一遍。

用法::

    # 树莓派上（先确认雷达端口映射，参考 run.sh）
    export RADAR_PORTS=/dev/serial/by-path/...,/dev/serial/by-path/...,/dev/serial/by-path/...
    export RADAR_ANGLES=-45,0,45

    # 空场 30 秒（无人、但开着风扇）
    python collect.py --label empty --seconds 30 --session empty_fan_01

    # 有人静止只呼吸 30 秒
    python collect.py --label human --seconds 30 --session still_breath_01

    # 每采一段会自动追加到同一个 npz，用 --out 指定
    python collect.py --label human --seconds 30 --session wave_01 --out data/real.npz

安全提示：脚本会打印每帧的实时统计，采集过程中请留意「有效帧数」，若长时间为 0
说明串口映射错了，先跑 ``probe_gates.py`` 确认。
"""

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import WindowSpec, WindowBuilder
from synth import save_npz, load_npz


class _SimRadar:
    """模拟雷达：从 ``_SimHub`` 取当前时刻的帧。仅用于无硬件试跑采集流程。"""

    def __init__(self, hub, angle, sensor_id):
        self.hub = hub
        self.angle = angle
        self.sensor_id = sensor_id
        self.port = f'sim:{angle:+.0f}'
        self.debug = False

    def read_data(self):
        return self.hub.frames_now().get(self.angle, {'valid': False})


class _SimHub:
    """用 ``synth.SessionSim`` 冒充雷达 hub，便于在 Windows/无雷达时验证流程。

    时间按真实墙钟推进（``--sim-speed`` 可加速），因此采样率、丢帧行为与实机一致。
    """

    def __init__(self, spec, mode, seed=0, speed=1.0):
        import numpy as np
        from synth import SessionSim
        self.spec = spec
        self.speed = float(speed)
        self.rng = np.random.default_rng(seed)
        self.sim = SessionSim(spec, self.rng, mode)
        self.dt = 1.0 / spec.fps
        self.errors = []
        self._idx = -1
        self._frames = {}
        self._wall0 = None
        self.radars = [_SimRadar(self, a, i) for i, a in enumerate(spec.angles)]

    def frames_now(self):
        now = time.time()
        if self._wall0 is None:
            self._wall0 = now
        idx = int((now - self._wall0) * self.speed * self.spec.fps)
        if idx != self._idx:
            self._idx = idx
            self._frames = {f['angle']: f
                            for f in self.sim.step(idx * self.dt, self.dt)}
        return self._frames


def open_hub(ports, angles, gate_bits, baud=115200):
    """打开雷达；失败时给出可操作的排查提示。"""
    try:
        from c4002_ext import RawRadarHub
    except Exception as e:
        raise SystemExit(f'无法导入 c4002_ext（缺少 pyserial 或不在 src 下运行）：{e}')
    try:
        return RawRadarHub(ports=ports, angles=angles, baud=baud, gate_bits=gate_bits)
    except Exception as e:
        wait = _wait_for_hub(ports, angles, baud, gate_bits, str(e))
        if wait is None:
            raise SystemExit(f'打开雷达失败：{e}')
        return wait


def _wait_for_hub(ports, angles, baud, gate_bits, first_error):
    """插拔重试：若一个雷达都读不到帧，提示检查端口映射后重试。

    采集往往在现场进行、USB 口容易插错，直接退出会浪费时间。
    """
    from c4002_ext import RawRadarHub
    print(f'打开雷达失败：{first_error}')
    while True:
        ans = input('检查 USB 与端口映射后按回车重试，输入 q 退出：').strip().lower()
        if ans == 'q':
            return None
        try:
            return RawRadarHub(ports=ports, angles=angles, baud=baud, gate_bits=gate_bits)
        except Exception as e:
            print(f'仍然失败：{e}')


def collect(spec, hub, label, seconds, session, stride, verbose=True):
    """采集一段并返回 ``(seq, agg, y, groups)``。

    窗口按**帧时间戳**产出（每 ``stride`` 个采样周期一个），而不是按读取次数。
    这点很关键：主循环读串口的频率远高于雷达上报率（模拟场景下尤甚），
    若按读取次数计数，同一个采样时刻会反复产出几乎相同的窗口，
    训练集会被大量重复样本灌水，验证指标虚高。
    """
    wb = WindowBuilder(spec)
    seqs, aggs = [], []
    dt = 1.0 / max(spec.fps, 1e-6)
    min_gap = stride * dt
    t0 = time.time()
    n_reads = 0
    n_valid = 0
    n_steps = 0
    last_window_ts = None
    newest_ts = None
    last_print = 0.0
    while time.time() - t0 < seconds:
        for r in hub.radars:
            d = r.read_data()
            if not d:
                continue
            n_reads += 1
            if not d.get('valid'):
                continue
            n_valid += 1
            wb.add(d)
            ts = d.get('timestamp')
            if ts is not None:
                ts = float(ts)
                if newest_ts is None or ts > newest_ts:
                    newest_ts = ts
                    n_steps += 1  # 采样时刻推进一次 = 真正收到一批新帧
        if wb.covered() and newest_ts is not None:
            if last_window_ts is None or (newest_ts - last_window_ts) >= min_gap:
                s, a = wb.build()
                if s is not None:
                    seqs.append(s)
                    aggs.append(a)
                    last_window_ts = newest_ts
        if verbose and time.time() - last_print > 1.0:
            last_print = time.time()
            el = time.time() - t0
            print(f'\r  [{session}] {el:5.1f}/{seconds:.0f}s  '
                  f'采样 {n_steps}（有效帧 {n_valid}）  窗口 {len(seqs)}',
                  end='', flush=True)
    if verbose:
        print()
    if not seqs:
        print(f'  [警告] {session} 未产出任何窗口。常见原因：雷达没出帧、'
              f'端口映射错误、--seconds 太短。')
        return None
    seq = np.asarray(seqs, dtype=np.float32)
    agg = np.asarray(aggs, dtype=np.float32)
    y = np.full(len(seq), 1 if label == 'human' else 0, dtype=np.int64)
    groups = np.zeros(len(seq), dtype=np.int64)  # 同一段同组，追加时重编号
    print(f'  {session}: {len(seq)} 窗口（标签 {label}），'
          f'采样 {n_steps} 次（{n_steps / max(seconds, 1e-6):.1f} Hz），有效帧 {n_valid}')
    return seq, agg, y, groups


def append_to_npz(path, seq, agg, y, groups, spec):
    """把新采集的一段追加到已有 npz（组号递增，保证分段信息不丢）。"""
    if os.path.exists(path):
        oseq, oagg, oy, og, ospec = load_npz(path)
        if (ospec.window_frames, ospec.gate_bits, tuple(ospec.angles)) != \
           (spec.window_frames, spec.gate_bits, tuple(spec.angles)):
            raise SystemExit(
                f'{path} 的窗口规格 ({ospec.window_frames}帧/{ospec.gate_bits}门/'
                f'{ospec.angles}) 与本次 ({spec.window_frames}/{spec.gate_bits}/'
                f'{spec.angles}) 不一致，请换 --out 或删除旧文件。')
        groups = groups + (int(og.max()) + 1 if len(og) else 0)
        seq = np.concatenate([oseq, seq])
        agg = np.concatenate([oagg, agg])
        y = np.concatenate([oy, y])
        groups = np.concatenate([og, groups])
        print(f'  追加到 {path}（累计 {len(y)} 窗口，'
              f'{len(np.unique(groups))} 段）')
    save_npz(path, seq, agg, y, groups, spec)
    return path


def main():
    p = argparse.ArgumentParser(description='采集真机标注数据')
    p.add_argument('--label', required=True, choices=['empty', 'human'],
                   help='empty=空场（无人），human=有人')
    p.add_argument('--seconds', type=float, default=30.0)
    p.add_argument('--session', default=None, help='本次采集的名字（写入日志，便于回溯）')
    p.add_argument('--out', default='data/real.npz')
    p.add_argument('--ports', default=None, help='逗号分隔，默认取环境变量 RADAR_PORTS')
    p.add_argument('--angles', default=None, help='逗号分隔，默认取环境变量 RADAR_ANGLES')
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--gate-bits', type=int, default=None)
    p.add_argument('--window-frames', type=int, default=20)
    p.add_argument('--fps', type=float, default=10.0)
    p.add_argument('--stride', type=int, default=5,
                   help='产出窗口的采样步长（帧）。越小样本越多但相关性越强')
    p.add_argument('--no-append', action='store_true', help='覆盖而不是追加')
    p.add_argument('--simulate', action='store_true',
                   help='不连雷达，用合成场景试跑采集/追加/训练流程（无硬件时验证用）')
    p.add_argument('--sim-speed', type=float, default=1.0,
                   help='--simulate 时的时间倍速，调大可以更快跑完')
    p.add_argument('--sim-mode', default=None, choices=['empty', 'still', 'moving'],
                   help='--simulate 时的场景，默认按 --label 推断')
    args = p.parse_args()

    ports = (args.ports or os.getenv('RADAR_PORTS') or '').strip()
    ports = [x.strip() for x in ports.split(',') if x.strip()] if ports else None
    angles = (args.angles or os.getenv('RADAR_ANGLES') or '').strip()
    angles = [float(x) for x in angles.split(',') if x.strip()] if angles else None
    if not ports and not args.simulate:
        print('未提供 --ports，且环境变量 RADAR_PORTS 为空。')
        print('示例：export RADAR_PORTS=/dev/serial/by-path/pci-...-0:1.1:1.0-port0,...')
        print('     export RADAR_ANGLES=-45,0,45')
        print('（建议用 ls -l /dev/serial/by-path/ 得到的稳定路径，参考 deploy/README.md）')
        return 2

    spec = WindowSpec(window_frames=args.window_frames, fps=args.fps,
                      gate_bits=args.gate_bits, angles=angles or (-45.0, 0.0, 45.0))
    session = args.session or f'{args.label}_{time.strftime("%Y%m%d_%H%M%S")}'

    hub = None
    if args.simulate:
        sim_mode = args.sim_mode or ('empty' if args.label == 'empty' else 'moving')
        hub = _SimHub(spec, sim_mode, seed=abs(hash(session)) % (2 ** 31),
                      speed=max(args.sim_speed, 1e-6))
        print(f'[模拟模式] 场景={sim_mode}  倍速={args.sim_speed}  '
              f'（不会打开任何串口）')
    else:
        hub = open_hub(ports, spec.angles, spec.gate_bits, args.baud)
    print(f'已打开 {len(hub.radars)} 个雷达：'
          f'{[(r.angle, getattr(r, "port", "?")) for r in hub.radars]}')
    if hub.errors:
        print(f'（部分初始化告警：{hub.errors}）')
    if args.label == 'empty':
        print('→ 空场采集：请确保场景内无人（但可以开风扇/空调等干扰源）')
    else:
        print('→ 有人采集：请让被测者在雷达覆盖范围内活动/静坐呼吸')
    print(f'开始采集 {session}，{args.seconds:.0f}s …')

    got = collect(spec, hub, args.label, args.seconds, session, args.stride)
    if got is None:
        return 1
    seq, agg, y, groups = got
    if args.no_append:
        save_npz(args.out, seq, agg, y, groups, spec)
        print(f'已写入 {args.out}')
    else:
        append_to_npz(args.out, seq, agg, y, groups, spec)

    print('\n提示：')
    print('  1) 换一个场景/位置再采一段，标签相同的多段不要混在同一次录制里；')
    print('  2) 采完两个类别后再训练：python train.py --data ' + args.out)
    print('  3) 想验证「新字段有没有用」：python evaluate.py --data ' + args.out +
          ' --compare-ablation（需先按各消融模式各训一次）')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
