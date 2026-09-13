# -*- coding: utf-8 -*-
"""训练人体存在检测模型（TensorFlow / Keras）。

模型结构（双路输入）
--------------------
::

    seq (T, F) ──► Conv1D×2 + 池化 ──► 全局平均池化 ──┐
                                                      ├─► Dense ─► Dropout ─► sigmoid ─► P(有人)
    agg (A,)   ──────────────────► Dense ─────────────┘

* ``seq`` 支路看**时序形态**：速度是周期起伏（呼吸）、正负交替（走动）
  还是孤立突发（吊扇杂波）——卷积核直接在时间轴上匹配这些模式。
* ``agg`` 支路看**窗口统计量**：速度 RMS、过零率、距离方差、门掩码稳定性等，
  也就是父项目 ``breath_detector.py`` 里人手写死的那些判据。把它显式喂进去，
  在几百个样本的量级上比让网络从原始序列里自己悟要稳得多。

为什么用 ``--arch mlp`` 也能跑：数据量很小时（一场比赛采不了几小时数据），
卷积/循环层的收益有限，先用 MLP 拿基线更实在。三个结构都留着便于对比。

用法::

    python train.py --data data/synth.npz --out-dir models
    python train.py --data data/real_all.npz --val-data data/real_val.npz --arch cnn
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import WindowSpec, Normalizer, feature_names, ablate, ABLATE_MODES
from metrics import prf, roc_auc, best_threshold, format_report
from synth import load_npz, group_split


def _import_tf():
    """延迟导入 TensorFlow，缺依赖时给出可操作的提示而不是堆栈。"""
    try:
        import tensorflow as tf
    except Exception as e:  # pragma: no cover
        print('[ERROR] 未能导入 TensorFlow：%s' % e)
        print('  安装：pip install tensorflow')
        print('  若树莓派上装不上（aarch64 无官方 wheel），请改用：')
        print('    python train.py --export-numpy-only   # 导出纯 numpy 权重')
        print('    python baseline_numpy.py              # 纯 numpy 逻辑回归基线')
        raise SystemExit(2)
    return tf


def build_model(tf, spec, arch='cnn', lr=1e-3, l2=1e-4):
    """构造双路输入模型。规模刻意压小：样本量小、要跑在树莓派 CPU 上。"""
    from tensorflow.keras import layers, regularizers

    reg = regularizers.l2(l2) if l2 else None
    seq_in = layers.Input(shape=(spec.window_frames, spec.seq_dim), name='seq')
    agg_in = layers.Input(shape=(spec.agg_dim,), name='agg')

    if arch == 'cnn':
        x = layers.Conv1D(32, 5, padding='same', kernel_regularizer=reg)(seq_in)
        x = layers.BatchNormalization()(x)
        x = layers.Activation('relu')(x)
        x = layers.MaxPooling1D(2)(x)
        x = layers.Conv1D(64, 3, padding='same', kernel_regularizer=reg)(x)
        x = layers.BatchNormalization()(x)
        x = layers.Activation('relu')(x)
        x = layers.GlobalAveragePooling1D()(x)
    elif arch == 'gru':
        x = layers.Bidirectional(layers.GRU(32, return_sequences=False))(seq_in)
    elif arch == 'mlp':
        x = layers.Flatten()(seq_in)
        x = layers.Dense(64, activation='relu', kernel_regularizer=reg)(x)
    else:
        raise ValueError(f'未知结构: {arch}')

    a = layers.Dense(32, activation='relu', kernel_regularizer=reg)(agg_in)
    z = layers.Concatenate()([x, a])
    z = layers.Dense(32, activation='relu', kernel_regularizer=reg)(z)
    z = layers.Dropout(0.3)(z)
    out = layers.Dense(1, activation='sigmoid', name='present')(z)

    model = tf.keras.Model(inputs=[seq_in, agg_in], outputs=out)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(lr),
        loss='binary_crossentropy',
        metrics=['accuracy', tf.keras.metrics.AUC(name='auc')],
    )
    return model


def prepare(data_paths, spec=None):
    """读取并拼接多个 npz，检查窗口规格一致。"""
    seqs, aggs, ys, groups = [], [], [], []
    gid_offset = 0
    for p in data_paths:
        s, a, y, g, sp = load_npz(p)
        if spec is None:
            spec = sp
        elif (sp.window_frames, sp.gate_bits, tuple(sp.angles)) != \
             (spec.window_frames, spec.gate_bits, tuple(spec.angles)):
            raise SystemExit(
                f'{p} 的窗口规格与其它数据集不一致：'
                f'{sp.window_frames}/{sp.gate_bits}/{sp.angles} vs '
                f'{spec.window_frames}/{spec.gate_bits}/{spec.angles}')
        seqs.append(s)
        aggs.append(a)
        ys.append(y)
        groups.append(g + gid_offset)
        gid_offset += int(g.max()) + 1 if len(g) else 0
        print(f'  载入 {p}: {len(y)} 样本（正 {int(y.sum())} / 负 {int((1 - y).sum())}）')
    return (np.concatenate(seqs), np.concatenate(aggs),
            np.concatenate(ys), np.concatenate(groups), spec)


def main():
    p = argparse.ArgumentParser(description='训练人体存在检测模型')
    p.add_argument('--data', required=True,
                   help='训练集 npz，多个用逗号分隔（如 data/a.npz,data/b.npz）')
    p.add_argument('--val-data', default=None,
                   help='单独验证集 npz（强烈建议用**不同场次**的真机数据）')
    p.add_argument('--out-dir', default='models')
    p.add_argument('--arch', default='cnn', choices=['cnn', 'gru', 'mlp'])
    p.add_argument('--epochs', type=int, default=120)
    p.add_argument('--batch', type=int, default=32)
    p.add_argument('--lr', type=float, default=1e-3)
    p.add_argument('--val-frac', type=float, default=0.3,
                   help='未指定 --val-data 时按片段切分的验证集比例')
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--threshold-metric', default='f1', choices=['f1', 'acc', 'recall'])
    p.add_argument('--tflite', action='store_true', help='同时导出 TFLite（树莓派推荐）')
    p.add_argument('--ablate', default='none', choices=list(ABLATE_MODES),
                   help='特征消融：legacy=只用旧解析器暴露的 5 个标量；'
                        'gates=去掉距离门掩码；agg=去掉窗口统计量')
    p.add_argument('--name', default='presence_model')
    args = p.parse_args()

    tf = _import_tf()
    tf.keras.utils.set_random_seed(args.seed)
    np.random.seed(args.seed)

    data_paths = [x.strip() for x in args.data.split(',') if x.strip()]
    print('载入训练数据：')
    seq, agg, y, groups, spec = prepare(data_paths)

    if args.val_data:
        print('载入验证数据：')
        vseq, vagg, vy, vgroups, vspec = prepare(
            [x.strip() for x in args.val_data.split(',') if x.strip()], spec=spec)
        tr_idx = np.arange(len(y))
        va_idx = np.arange(len(vy))
    else:
        tr_idx, va_idx = group_split(groups, y, val_frac=args.val_frac, seed=args.seed)
        vseq, vagg, vy = seq[va_idx], agg[va_idx], y[va_idx]

    print(f'窗口规格：{spec.window_frames} 帧 @ {spec.fps}Hz = '
          f'{spec.window_seconds:.1f}s，seq_dim={spec.seq_dim}，agg_dim={spec.agg_dim}')
    print(f'训练 {len(tr_idx)} 样本 / 验证 {va_idx.size} 样本')

    # 标准化参数**只用训练集**拟合，避免验证集信息泄漏
    norm = Normalizer.fit(seq[tr_idx], agg[tr_idx])
    tr_seq, tr_agg = norm.transform(seq[tr_idx], agg[tr_idx])
    va_seq, va_agg = norm.transform(vseq, vagg)

    # 特征消融（在标准化之后置零，等价于「这些特征不可用」）
    if args.ablate != 'none':
        tr_seq, tr_agg = ablate(tr_seq, tr_agg, spec, args.ablate)
        va_seq, va_agg = ablate(va_seq, va_agg, spec, args.ablate)
        print(f'[消融实验] 模式 {args.ablate}：已屏蔽对应特征')

    # 类别不平衡：比赛里「空场」采集时长往往远大于「有人」
    n_pos = int((y[tr_idx] == 1).sum())
    n_neg = int((y[tr_idx] == 0).sum())
    class_weight = {0: 1.0, 1: 1.0}
    if n_pos and n_neg:
        class_weight = {0: len(tr_idx) / (2.0 * n_neg), 1: len(tr_idx) / (2.0 * n_pos)}
    print(f'类别权重：{ {k: round(v, 3) for k, v in class_weight.items()} }')

    model = build_model(tf, spec, arch=args.arch, lr=args.lr)
    model.summary()

    os.makedirs(args.out_dir, exist_ok=True)
    ckpt = os.path.join(args.out_dir, args.name + '.keras')
    callbacks = [
        tf.keras.callbacks.EarlyStopping(monitor='val_auc', mode='max',
                                        patience=20, restore_best_weights=True),
        tf.keras.callbacks.ReduceLROnPlateau(monitor='val_loss', factor=0.5,
                                            patience=8, min_lr=1e-5),
        tf.keras.callbacks.ModelCheckpoint(ckpt, monitor='val_auc', mode='max',
                                          save_best_only=True, verbose=0),
    ]
    model.fit([tr_seq, tr_agg], y[tr_idx],
              validation_data=([va_seq, va_agg], vy),
              epochs=args.epochs, batch_size=args.batch,
              class_weight=class_weight, callbacks=callbacks, verbose=2)

    # ---- 评估与阈值选择 ----
    print('\n训练集评估：')
    p_tr = model.predict([tr_seq, tr_agg], verbose=0).ravel()
    rep_tr, _ = format_report(y[tr_idx], p_tr, 0.5, 'train @0.5')
    print(rep_tr)
    print('\n验证集评估：')
    p_va = model.predict([va_seq, va_agg], verbose=0).ravel()
    rep_va, m05 = format_report(vy, p_va, 0.5, 'val @0.5')
    print(rep_va)

    thr, best_val = best_threshold(vy, p_va, metric=args.threshold_metric)
    rep_thr, mbest = format_report(vy, p_va, thr, f'val @{thr:.2f}（按 {args.threshold_metric} 选优）')
    print(rep_thr)

    seq_names, agg_names = feature_names(spec)
    meta = {
        'arch': args.arch,
        'ablate': args.ablate,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'spec': spec.to_dict(),
        'normalizer': norm.to_dict(),
        'threshold': float(thr),
        'threshold_metric': args.threshold_metric,
        'class_weight': class_weight,
        'train_samples': int(len(tr_idx)),
        'val_samples': int(va_idx.size),
        'metrics_val_at_0.5': m05,
        'metrics_val_at_threshold': mbest,
        'auc_val': roc_auc(vy, p_va),
        'auc_train': roc_auc(y[tr_idx], p_tr),
        'seq_feature_names': seq_names,
        'agg_feature_names': agg_names,
        'data': data_paths,
        'val_data': args.val_data,
    }
    with open(os.path.join(args.out_dir, args.name + '_meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f'\n模型已保存：{ckpt}')
    print(f'元数据已保存：{os.path.join(args.out_dir, args.name + "_meta.json")}')

    # ---- TFLite 导出（树莓派推理更快、依赖更少）----
    if args.tflite:
        tfl = os.path.join(args.out_dir, args.name + '.tflite')
        conv = tf.lite.TFLiteConverter.from_keras_model(model)
        conv.optimizations = [tf.lite.Optimize.DEFAULT]
        with open(tfl, 'wb') as f:
            f.write(conv.convert())
        print(f'TFLite 已导出：{tfl}（{os.path.getsize(tfl) / 1024:.1f} KB）')

    if roc_auc(vy, p_va) < 0.7:
        print('\n[警告] 验证集 AUC < 0.7，模型基本没学到东西。常见原因：')
        print('  1) 正负样本来自同一段录制（用按片段切分，别按窗口随机切）；')
        print('  2) 样本太少（建议每个类别至少 200 个窗口 / 10 段以上独立录制）；')
        print('  3) 真机数据里「有人」和「空场」的特征分布本身重叠严重。')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
