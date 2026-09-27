# -*- coding: utf-8 -*-
"""一条命令跑完所有入口的自检（无需硬件）。

为什么值得单独写一个：本项目的数据链路上有一类**静默失败**——时间戳语义、
多传感器对齐、窗口缓冲去重、CSV 回放基准时间。这些地方出错时不会抛异常，
只会让模型输出退化成常数或恒为 0，看日志完全正常。开发过程中就踩到了三处：

1. ``WindowBuilder`` 未按时间戳去重 → 主循环把同一帧塞满缓冲，概率恒为 1.0；
2. ``frame.get('timestamp') or time.time()`` 把合法的 0.0 当成缺失 → 后续帧全被丢弃；
3. CSV 回放用墙钟时间戳 → 每帧时间几乎相同，窗口退化成「只有最后一格有数据」。

因此自检里除了「跑通不报错」，还包含**行为断言**：空场概率必须低、
移动人体概率必须高。否则「能跑」毫无意义。

用法::

    python smoke_test.py            # 完整自检（约 1~3 分钟，需要 TensorFlow）
    python smoke_test.py --no-tf    # 跳过训练相关项，只测数据链路（秒级，只需 numpy）
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _run(mod_args, timeout=900):
    """以子进程运行某个脚本，返回 ``(returncode, stdout+stderr)``。

    用子进程而不是函数调用：能同时验证 CLI 参数解析、退出码与打印输出，
    这些正是用户实际接触的接口。
    """
    cmd = [sys.executable] + mod_args
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, 'TIMEOUT'


def t_parser_selftest(tmp):
    """字节级解析自检：验证字段偏移（不依赖串口）。"""
    rc, out = _run(['c4002_ext.py'])
    return rc == 0 and 'PASS' in out, out.strip().splitlines()[-1] if out else ''


def t_builder_alignment(tmp):
    """窗口对齐与去重：同一帧重复喂入不得污染窗口。"""
    from features import WindowSpec, WindowBuilder
    spec = WindowSpec(window_frames=10, fps=10.0)
    wb = WindowBuilder(spec)
    # 模拟主循环：每个采样时刻被反复读取 50 次
    for i in range(60):
        for ang in spec.angles:
            frame = {'angle': ang, 'valid': True, 'timestamp': i * 0.1,
                     'move_speed': 30, 'move_distance': 2.0, 'move_energy': 60,
                     'exist_distance': 2.0, 'exist_energy': 70, 'exist_count_down': 40,
                     'exist_gate_index': 0b110, 'gate_count': 2, 'target_status': 2,
                     'move_direction': 1, 'light': 100}
            for _ in range(50):
                wb.add(frame)
    seq, agg = wb.build()
    fresh = seq[:, 10]  # SCALAR_NAMES 里 fresh 的下标
    ok = bool(np.all(fresh > 0.5)) and seq.shape == (10, spec.seq_dim)
    detail = f'fresh 全为实测={bool(np.all(fresh > 0.5))}, 去重计数={wb.duplicate_frames}'
    return ok, detail


def t_synth_and_features(tmp):
    """合成数据 + 特征构建：维度、类别、窗口长度都要对。"""
    from synth import build_dataset
    from features import WindowSpec, AGG_NAMES
    spec = WindowSpec()
    seq, agg, y, g = build_dataset(spec, ['empty', 'still', 'moving'], 2, 12,
                                   seed=1, stride=4)
    ok = (seq.shape[1:] == (spec.window_frames, spec.seq_dim)
          and agg.shape[1] == spec.agg_dim
          and len(np.unique(y)) == 2 and len(np.unique(g)) == 6)
    # 空场窗口的有效帧占比不应为 0（为 0 说明对齐/去重失效，窗口退化成空档）
    empty = y == 0
    frac = float(agg[empty][:, AGG_NAMES.index('valid_frac')].mean())
    ok = ok and frac > 0
    return ok, (f'seq{seq.shape} agg{agg.shape} 类别={np.unique(y)} '
                f'片段={len(np.unique(g))} 空场有效帧占比={frac:.2f}')


def t_train_eval(tmp):
    """训练 → 评估 → 与规则对比，全链路。"""
    data = os.path.join(tmp, 'smoke.npz')
    # 数据量要够：本项目的难点（吊扇干扰、幻影目标）只在部分场景里出现，
    # 段数太少时模型可能一个都没见过，出现「离线指标满分、实时全错」的假象。
    rc, out = _run(['synth.py', '--out', data, '--sessions', '14',
                    '--seconds', '24', '--stride', '5'])
    if rc != 0:
        return False, f'synth 失败 rc={rc}'
    mdir = os.path.join(tmp, 'models')
    os.makedirs(mdir, exist_ok=True)
    rc, out1 = _run(['train.py', '--data', data, '--out-dir', mdir,
                     '--epochs', '10', '--arch', 'cnn', '--tflite'])
    if rc != 0:
        return False, f'train 失败 rc={rc}\n{out1[-800:]}'
    if not os.path.exists(os.path.join(mdir, 'presence_model_meta.json')):
        return False, '未写出 meta.json'
    if not os.path.exists(os.path.join(mdir, 'presence_model.tflite')):
        return False, '未导出 tflite'
    rc, out2 = _run(['evaluate.py', '--data', data, '--model-dir', mdir])
    if rc != 0:
        return False, f'evaluate 失败 rc={rc}\n{out2[-800:]}'
    keep = [l for l in out2.splitlines() if '现行规则（部署' in l or '神经网络模型  ' in l]
    return True, ' | '.join(s.strip() for s in keep)[:160]


def t_numpy_backend(tmp):
    """纯 numpy 后端（TF 不可用时的备选）必须能训能评。"""
    data = os.path.join(tmp, 'smoke.npz')
    mdir = os.path.join(tmp, 'models_np')
    os.makedirs(mdir, exist_ok=True)
    rc, out = _run(['baseline_numpy.py', '--data', data, '--out-dir', mdir,
                    '--epochs', '300'])
    if rc != 0:
        return False, f'baseline_numpy 失败 rc={rc}\n{out[-600:]}'
    ok = os.path.exists(os.path.join(mdir, 'presence_model_numpy.npz'))
    auc = ''
    for l in out.splitlines():
        if 'val @' in l and 'AUC' in l:
            auc = l.strip()
            break
    return ok, auc


def t_live_behaviour(tmp):
    """**行为断言**：空场概率必须低、有人的场景概率必须高。

    只测「能跑」是不够的——前述时间戳/窗口覆盖类 bug 都能"正常跑完"但输出常数。
    判据放在中位数上并留足余量：合成场景每次随机生成，个别场景判错是正常的。
    """
    mdir = os.path.join(tmp, 'models')
    results = {}
    for scene in ('empty', 'moving', 'still'):
        log = os.path.join(tmp, f'live_{scene}.csv')
        rc, out = _run(['detect_live.py', '--model-dir', mdir,
                        '--name', 'presence_model', '--source', 'sim',
                        '--sim-mode', scene, '--sim-speed', '10',
                        '--seconds', '6', '--stride-s', '0.5', '--log', log])
        if rc != 0 or not os.path.exists(log):
            return False, f'场景 {scene} 推理失败 rc={rc}\n{out[-500:]}'
        with open(log, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return False, f'场景 {scene} 未产出任何推理步'
        ps = [float(r['prob']) for r in rows]
        results[scene] = (float(np.mean(ps)), float(np.median(ps)))
    # 判据是**相对关系**为主、绝对值为辅：自检只训十几段合成数据、10 个 epoch，
    # 不该用来评判模型质量（每次随机初始化+随机场景，分数会有波动）。
    # 要抓的三类 bug 分别对应下面三条：
    #   「恒判有人」→ 空场中位数偏高（emp < 0.6 兜底）
    #   「恒判无人」→ 有人中位数偏低（hum > 0.5 兜底）
    #   「输出常数 / 窗口退化」→ 三个场景中位数拉不开差距（核心判据 spread > 0.3）
    emp = results['empty'][1]
    hum = max(results['still'][1], results['moving'][1])
    spread = hum - emp
    ok = (emp < 0.6) and (hum > 0.5) and (spread > 0.3)
    detail = ('  '.join(f'{k}: P中位={v[1]:.3f}' for k, v in results.items())
              + f'   人-空 差距={spread:.3f}（需 >0.3）')
    return ok, detail


def t_csv_replay(tmp):
    """CSV 回放：帧时间戳必须沿用录制时间，否则窗口退化、输出恒 0。"""
    from features import WindowSpec
    from synth import SessionSim
    spec = WindowSpec()
    path = os.path.join(tmp, 'frames.csv')
    rng = np.random.default_rng(3)
    sim = SessionSim(spec, rng, 'moving')
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['timestamp', 'angle', 'target_status', 'light',
                    'exist_gate_index', 'gate_count', 'exist_count_down',
                    'exist_distance', 'exist_energy', 'move_distance',
                    'move_speed', 'move_energy', 'move_direction', 'data_len']
                   + [f'gate{i}' for i in range(spec.gate_bits)])
        for i in range(200):
            t = i * 0.1
            for fr in sim.step(t, 0.1):
                w.writerow([round(t, 4), fr['angle'], fr['target_status'], fr['light'],
                            fr['exist_gate_index'], fr['gate_count'],
                            fr['exist_count_down'], fr['exist_distance'],
                            fr['exist_energy'], fr['move_distance'], fr['move_speed'],
                            fr['move_energy'], fr['move_direction'], 30]
                           + list(fr['gate_bits']))
    mdir = os.path.join(tmp, 'models')
    log = os.path.join(tmp, 'csv_replay.csv')
    rc, out = _run(['detect_live.py', '--model-dir', mdir, '--name', 'presence_model',
                    '--source', 'csv', '--csv', path, '--sim-speed', '6',
                    '--seconds', '10', '--stride-s', '0.5', '--log', log])
    if rc != 0 or not os.path.exists(log):
        return False, f'CSV 回放失败 rc={rc}\n{out[-500:]}'
    with open(log, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return False, 'CSV 回放未产出推理步'
    med = float(np.median([float(r['prob']) for r in rows]))
    return med > 0.5, f'有人走动录像回放 P中位={med:.3f}（应 >0.5）'


def main():
    p = argparse.ArgumentParser(description='test_ai 全链路自检（无需硬件）')
    p.add_argument('--no-tf', action='store_true',
                   help='跳过需要 TensorFlow 的项（训练/推理），只测数据链路')
    args = p.parse_args()

    tests = [('字节级解析字段偏移', t_parser_selftest, False),
             ('窗口对齐与去重', t_builder_alignment, False),
             ('合成数据与特征维度', t_synth_and_features, False)]
    if not args.no_tf:
        tests += [('训练+评估+TFLite 导出', t_train_eval, True),
                  ('纯 numpy 后端', t_numpy_backend, True),
                  ('实时推理行为断言', t_live_behaviour, True),
                  ('CSV 回放', t_csv_replay, True)]

    tmp = tempfile.mkdtemp(prefix='test_ai_smoke_')
    passed, failed = 0, 0
    print(f'=== test_ai 自检（需要 TensorFlow 的项：{"跳过" if args.no_tf else "运行"}）===')
    print(f'临时目录 {tmp}\n')
    try:
        for name, fn, needs_tf in tests:
            try:
                if needs_tf:
                    ok, detail = fn(tmp)
                else:
                    ok, detail = fn(tmp)
            except Exception as e:
                ok, detail = False, f'异常 {type(e).__name__}: {e}'
            flag = 'PASS' if ok else 'FAIL'
            print(f'[{flag}] {name}')
            if detail:
                print(f'       {detail}')
            passed += bool(ok)
            failed += (not ok)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f'\n=== 结果：{passed} 通过 / {failed} 失败 ===')
    if failed:
        print('有失败项时不要上机采集数据——先修好数据链路，否则采到的数据不可用。')
        return 1
    print('全部通过。下一步请按 README 第四节，先跑 probe_gates.py 上机验证距离门字段。')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
