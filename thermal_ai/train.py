#!/usr/bin/env python3
"""训练热成像「有人/无人」检测模型（TensorFlow / Keras），导出 TFLite。

模型：小型 CNN（32x24 灰度输入 → 几层卷积 → 全局池化 → sigmoid）。
规模刻意压小：热像只有 768 个低分辨率像素，样本量小，且要跑在树莓派 CPU 上。

用法::

    python train.py --data data/synth.npz --out-dir models --tflite
    python train.py --data data/real.npz --val-data data/real_val.npz --arch mlp
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preprocess import frame_to_tensor, group_split, T_MIN, T_MAX
from synth import load_npz


def _import_tf():
    try:
        import tensorflow as tf
    except Exception as e:
        print("[ERROR] 未能导入 TensorFlow：%s" % e)
        print("  安装：pip install tensorflow")
        print("  若树莓派上装不上（aarch64 无官方 wheel），请在开发机训练导出 TFLite，")
        print("  树莓派只用 ai-edge-litert 跑推理；或改用纯 numpy 后端。")
        raise SystemExit(2)
    return tf


def build_model(tf, arch="cnn", lr=1e-3, l2=1e-4, dropout=0.3):
    from tensorflow.keras import layers, regularizers

    reg = regularizers.l2(l2) if l2 else None
    inp = layers.Input(shape=(24, 32, 1), name="thermal")

    if arch == "cnn":
        x = layers.Conv2D(16, 3, padding="same", kernel_regularizer=reg)(inp)
        x = layers.BatchNormalization()(x)
        x = layers.Activation("relu")(x)
        x = layers.MaxPooling2D(2)(x)
        x = layers.Conv2D(32, 3, padding="same", kernel_regularizer=reg)(x)
        x = layers.BatchNormalization()(x)
        x = layers.Activation("relu")(x)
        x = layers.MaxPooling2D(2)(x)
        x = layers.Conv2D(64, 3, padding="same", kernel_regularizer=reg)(x)
        x = layers.BatchNormalization()(x)
        x = layers.Activation("relu")(x)
        x = layers.GlobalAveragePooling2D()(x)
    elif arch == "mlp":
        x = layers.Flatten()(inp)
        x = layers.Dense(128, activation="relu", kernel_regularizer=reg)(x)
        x = layers.Dropout(dropout)(x)
        x = layers.Dense(64, activation="relu", kernel_regularizer=reg)(x)
    else:
        raise ValueError(f"未知结构: {arch}")

    out = layers.Dense(1, activation="sigmoid", name="present")(x)
    model = tf.keras.Model(inputs=inp, outputs=out)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(lr),
        loss="binary_crossentropy",
        metrics=["accuracy", tf.keras.metrics.AUC(name="auc")],
    )
    return model


def _auc(y, p):
    """纯 numpy AUC（不依赖 sklearn），供元数据记录。

    用「升序排序后正样本的秩」计算：分数越高、秩越大。完美模型 AUC=1。
    """
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    # 按分数升序排，正样本应集中在秩的高位（分数高的地方）
    order = np.argsort(p)
    y_sorted = y[order]
    ranks = np.arange(1, len(y) + 1)[y_sorted == 1]
    return (float(ranks.sum()) - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _best_threshold(y, p):
    """按 F1 搜索最优阈值（纯 numpy）。"""
    best_t, best_f1 = 0.5, -1.0
    for t in np.linspace(0.05, 0.95, 91):
        pred = (p >= t).astype(int)
        tp = int(((pred == 1) & (y == 1)).sum())
        fp = int(((pred == 1) & (y == 0)).sum())
        fn = int(((pred == 0) & (y == 1)).sum())
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        if f1 > best_f1:
            best_f1, best_t = f1, t
    return best_t


def _prepare(paths):
    frames, ys, groups = [], [], []
    g_offset = 0
    for pth in paths:
        f, y, g, meta = load_npz(pth)
        frames.append(f)
        ys.append(y)
        groups.append(g + g_offset)
        g_offset += int(g.max()) + 1 if len(g) else 0
        print(f"  载入 {pth}: {len(y)} 帧（正 {int(y.sum())} / 负 {int((1 - y).sum())}）")
    return (np.concatenate(frames), np.concatenate(ys), np.concatenate(groups))


def main():
    p = argparse.ArgumentParser(description="训练热成像人体检测模型")
    p.add_argument("--data", required=True, help="训练 npz，多个用逗号分隔")
    p.add_argument("--val-data", default=None, help="单独验证 npz（推荐不同场次）")
    p.add_argument("--out-dir", default="models")
    p.add_argument("--arch", default="cnn", choices=["cnn", "mlp"])
    p.add_argument("--epochs", type=int, default=80)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--val-frac", type=float, default=0.3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--tflite", action="store_true", help="导出 TFLite（树莓派推荐）")
    p.add_argument("--export-numpy-only", action="store_true",
                   help="只导出纯 numpy 逻辑回归后端（不装 TF 时的保底）")
    p.add_argument("--name", default="presence_model")
    args = p.parse_args()

    paths = [x.strip() for x in args.data.split(",") if x.strip()]
    print("载入数据：")
    frames, y, groups = _prepare(paths)

    # 纯 numpy 后端：不依赖 TF，直接训练逻辑回归
    if args.export_numpy_only:
        from thermal_numpy import NumpyLogReg
        x = frame_to_tensor(frames).reshape(len(frames), -1)
        tr_idx, va_idx = group_split(groups, y, args.val_frac, args.seed)
        model = NumpyLogReg.fit(x[tr_idx], y[tr_idx])
        p_va = model.predict_proba(x[va_idx])
        thr = _best_threshold(y[va_idx], p_va)
        os.makedirs(args.out_dir, exist_ok=True)
        model.save(os.path.join(args.out_dir, args.name + "_numpy.npz"))
        meta = {
            "arch": "numpy", "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "t_min": T_MIN, "t_max": T_MAX, "threshold": float(thr),
            "auc_val": _auc(y[va_idx], p_va),
            "train_samples": int(len(tr_idx)), "val_samples": int(len(va_idx)),
        }
        with open(os.path.join(args.out_dir, args.name + "_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"numpy 后端已保存：{args.name}_numpy.npz（val AUC {meta['auc_val']:.4f}）")
        return 0

    tf = _import_tf()
    tf.keras.utils.set_random_seed(args.seed)
    np.random.seed(args.seed)

    x = frame_to_tensor(frames, T_MIN, T_MAX)

    if args.val_data:
        vf, vy, vg = _prepare([x.strip() for x in args.val_data.split(",") if x.strip()])
        vx = frame_to_tensor(vf, T_MIN, T_MAX)
        tr_idx = np.arange(len(y))
        va_idx = np.arange(len(vy))
        x_va, y_va = vx, vy
    else:
        tr_idx, va_idx = group_split(groups, y, args.val_frac, args.seed)
        x_va, y_va = x[va_idx], y[va_idx]

    x_tr, y_tr = x[tr_idx], y[tr_idx]
    print(f"训练 {len(tr_idx)} 帧 / 验证 {len(va_idx)} 帧")

    n_pos = int(y_tr.sum())
    n_neg = len(y_tr) - n_pos
    class_weight = {0: len(y_tr) / (2.0 * n_neg), 1: len(y_tr) / (2.0 * n_pos)} \
        if n_pos and n_neg else None
    print(f"类别权重：{ {k: round(v, 3) for k, v in class_weight.items()} if class_weight else None}")

    model = build_model(tf, arch=args.arch, lr=args.lr)
    model.summary()

    os.makedirs(args.out_dir, exist_ok=True)
    ckpt = os.path.join(args.out_dir, args.name + ".keras")
    callbacks = [
        tf.keras.callbacks.EarlyStopping(monitor="val_auc", mode="max",
                                         patience=15, restore_best_weights=True),
        tf.keras.callbacks.ReduceLROnPlateau(monitor="val_loss", factor=0.5,
                                             patience=6, min_lr=1e-5),
        tf.keras.callbacks.ModelCheckpoint(ckpt, monitor="val_auc", mode="max",
                                           save_best_only=True, verbose=0),
    ]
    model.fit(x_tr, y_tr, validation_data=(x_va, y_va), epochs=args.epochs,
              batch_size=args.batch, class_weight=class_weight,
              callbacks=callbacks, verbose=2)

    p_va = model.predict(x_va, verbose=0).ravel()
    thr = _best_threshold(y_va, p_va)
    print(f"验证集：AUC {_auc(y_va, p_va):.4f}，最优阈值(F1) {thr:.3f}")

    meta = {
        "arch": args.arch, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "t_min": T_MIN, "t_max": T_MAX, "threshold": float(thr),
        "auc_val": float(_auc(y_va, p_va)),
        "train_samples": int(len(tr_idx)), "val_samples": int(len(va_idx)),
        "data": paths, "val_data": args.val_data,
    }
    with open(os.path.join(args.out_dir, args.name + "_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"模型已保存：{ckpt}")
    print(f"元数据已保存：{args.name}_meta.json")

    if args.tflite:
        tfl = os.path.join(args.out_dir, args.name + ".tflite")
        conv = tf.lite.TFLiteConverter.from_keras_model(model)
        conv.optimizations = [tf.lite.Optimize.DEFAULT]
        with open(tfl, "wb") as f:
            f.write(conv.convert())
        print(f"TFLite 已导出：{tfl}（{os.path.getsize(tfl) / 1024:.1f} KB）")

    if _auc(y_va, p_va) < 0.7:
        print("\n[警告] 验证集 AUC < 0.7，建议：")
        print("  1. 检查正负样本是否都覆盖了难例（热物体/热反射/不同距离的人）")
        print("  2. 换 --arch mlp 先看基线；数据量小优先用简单模型")
        print("  3. 采集更多分场次数据后重训")


if __name__ == "__main__":
    main()
