#!/usr/bin/env python3
"""thermal_ai 自检：一条命令跑完整条链路（无需硬件）。

覆盖：
1. 温度归一化/热区统计（``preprocess``）
2. 合成数据维度（``synth``）
3. 纯 numpy 后端（``thermal_numpy``）
4. 训练 + TFLite 导出 + 推理（``train`` + ``model_io``，若装 TensorFlow）
5. 实时推理行为：空场概率低、有人概率高（``detect_live``）

用法::

    python smoke_test.py             # 需要 TensorFlow（推荐在开发机跑）
    python smoke_test.py --no-tf     # 只测数据链路，秒级，只需要 numpy
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_preprocess():
    from preprocess import frame_to_tensor, frame_stats, human_region, WIDTH, HEIGHT
    rng = np.random.default_rng(0)
    f = 20.0 + rng.normal(0, 0.5, (HEIGHT, WIDTH)).astype(np.float32)
    f[12, 16] = 35.0
    f[12, 17] = 34.0
    t = frame_to_tensor(f)
    assert t.shape == (1, HEIGHT, WIDTH, 1), t.shape
    assert 0.0 <= t.min() and t.max() <= 1.0
    s = frame_stats(f)
    assert s["max_c"] >= 35.0
    r = human_region(f)
    assert r is not None and r["pixels"] >= 2
    print("[OK] preprocess：归一化 + 热区统计")


def test_synth():
    from synth import synthesize_dataset, MODES
    frames, y, groups = synthesize_dataset(n_per_class=20, seed=1)
    assert frames.shape[1:] == (24, 32)
    assert set(y.tolist()) <= {0, 1}
    assert len(set(groups.tolist())) > 1
    assert (y == 1).any() and (y == 0).any()
    print(f"[OK] synth：{frames.shape[0]} 帧，含 {MODES} 场景")


def test_numpy_backend():
    from preprocess import frame_to_tensor
    from synth import synthesize_dataset
    from thermal_numpy import NumpyLogReg
    frames, y, _ = synthesize_dataset(n_per_class=40, seed=2)
    x = frame_to_tensor(frames).reshape(len(frames), -1)
    model = NumpyLogReg.fit(x, y, epochs=100)
    p = model.predict_proba(x)
    assert p.shape == (len(y),)
    assert ((p >= 0) & (p <= 1)).all()
    # 训练数据上应能明显区分
    auc_ish = ((p[y == 1].mean()) > (p[y == 0].mean()))
    assert auc_ish
    print("[OK] thermal_numpy：逻辑回归后端可训练、可推理")


def test_train_tflite(tmpdir):
    try:
        import tensorflow  # noqa: F401
    except Exception:
        print("[SKIP] train/tflite：未安装 TensorFlow")
        return

    from synth import synthesize_dataset, save_npz
    from preprocess import T_MIN, T_MAX
    data = os.path.join(tmpdir, "synth.npz")
    frames, y, groups = synthesize_dataset(n_per_class=30, seed=3)
    save_npz(data, frames, y, groups, T_MIN, T_MAX)

    from train import _import_tf, build_model, _prepare, _auc, _best_threshold
    from preprocess import frame_to_tensor
    tf = _import_tf()
    f, yy, gg = _prepare([data])
    x = frame_to_tensor(f, T_MIN, T_MAX)
    model = build_model(tf, arch="cnn", lr=1e-3)
    model.fit(x, yy, epochs=5, batch_size=16, verbose=0)
    p = model.predict(x, verbose=0).ravel()
    auc = _auc(yy, p)
    assert auc > 0.9, f"AUC 过低: {auc}"
    thr = _best_threshold(yy, p)
    print(f"[OK] train：CNN 训练/评估（AUC {auc:.4f}，阈值 {thr:.3f}）")

    # TFLite 导出 + 解释器推理
    tfl = os.path.join(tmpdir, "presence_model.tflite")
    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    with open(tfl, "wb") as fh:
        fh.write(conv.convert())

    from model_io import _load_tflite
    pred = _load_tflite(tfl, T_MIN, T_MAX)
    assert pred is not None
    probs = pred(frames[:8])
    assert probs.shape == (8,)
    print(f"[OK] train：TFLite 导出 {os.path.getsize(tfl)//1024} KB + 解释器推理")


def test_detect_behavior():
    from detect_live import PresenceEngine, source_sim

    def rule_predict(frames):
        from preprocess import human_region
        return np.asarray([1.0 if human_region(f) else 0.0
                           for f in frames], dtype=np.float32)

    engine = PresenceEngine(predict_fn=rule_predict, threshold=0.5,
                            smooth=0.4, min_on=2, min_off=3)

    # 空场：应保持「无人」
    sim_empty = source_sim("empty")
    for _ in range(30):
        engine.update(sim_empty.read_frame())
    assert engine.present is False, "空场不应判有人"
    assert engine.prob_ema < 0.5

    # 有人：应转为「有人」
    engine.reset()
    sim_human = source_sim("human")
    for _ in range(30):
        engine.update(sim_human.read_frame())
    assert engine.present is True, "有人场景应判有人"
    assert engine.prob_ema > 0.5
    print("[OK] detect_live：空场概率低、有人概率高、滞回生效")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-tf", action="store_true")
    args = p.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="thermal_ai_smoke_")
    try:
        test_preprocess()
        test_synth()
        test_numpy_backend()
        test_detect_behavior()
        if not args.no_tf:
            test_train_tflite(tmpdir)
        print("\n全部自检通过。")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
