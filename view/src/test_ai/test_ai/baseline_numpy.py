# -*- coding: utf-8 -*-
"""纯 numpy 逻辑回归基线——TensorFlow 装不上时的备选方案。

为什么需要它
------------
树莓派（aarch64）上 ``pip install tensorflow`` 常常失败：官方 PyPI 没有
aarch64 的 CPU wheel，需要社区轮子（tensorflow-aarch64）或改用
``tflite-runtime`` / ``ai-edge-litert``。比赛现场没有时间折腾环境时，
这个文件保证**同一套特征、同一套评估口径**下仍能出一个可上线的模型：

* 只依赖 numpy（树莓派上必然有）；
* 特征与神经网络完全一致（``seq`` 展平 + ``agg``），因此 ``evaluate.py`` /
  ``detect_live.py`` 可以用 ``--backend numpy`` 无缝切换；
* 训练用批量梯度下降 + L2 + 类别加权，几百个样本几秒训完。

代价：没有时序卷积，只能看展平的序列与统计量，上限低于 CNN。
若真机数据上两者差距不大，就用这个——更少的依赖本身就是收益。
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import Normalizer, WindowSpec
from metrics import roc_auc, prf, best_threshold, format_report
from synth import load_npz, group_split


def flatten(seq, agg):
    """把 ``(N, T, F)`` 与 ``(N, A)`` 拼成 ``(N, T*F + A)``。

    为提高信噪比，对序列部分额外拼接 **每帧的绝对速度均值/最大值** 两类
    强特征（相当于给线性模型一点「时序形态」的提示），这也是纯线性模型
    在呼吸检测这类任务上还能站得住的关键。
    """
    n, t, f = seq.shape
    flat = np.concatenate([seq.reshape(n, t * f), agg], axis=1)
    return flat.astype(np.float32)


class NumpyLogReg:
    """带 L2 与类别加权的逻辑回归（全批量 Adam，实现短、无魔法）。"""

    def __init__(self, n_features, l2=1e-4):
        self.w = np.zeros(n_features, dtype=np.float64)
        self.b = 0.0
        self.l2 = float(l2)

    def _sigmoid(self, z):
        # 数值稳定写法，避免 exp 溢出
        out = np.empty_like(z)
        pos = z >= 0
        out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
        e = np.exp(z[~pos])
        out[~pos] = e / (1.0 + e)
        return out

    def predict_proba(self, x):
        return self._sigmoid(x @ self.w + self.b)

    def fit(self, x, y, epochs=1200, lr=0.05, class_weight=None, verbose=True):
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        n, d = x.shape
        ones = np.ones(n)
        mw = np.ones(n)
        if class_weight:
            mw = np.where(y > 0.5, class_weight.get(1, 1.0), class_weight.get(0, 1.0))
        mw_sum = mw.sum()
        m_w = np.zeros(d)
        v_w = np.zeros(d)
        m_b = v_b = 0.0
        beta1, beta2, eps = 0.9, 0.999, 1e-8
        prev_loss = None
        for ep in range(1, epochs + 1):
            p = self._sigmoid(x @ self.w + self.b)
            err = (p - y) * mw
            grad_w = (x.T @ err) / mw_sum + self.l2 * self.w
            grad_b = err.sum() / mw_sum
            # Adam
            m_w = beta1 * m_w + (1 - beta1) * grad_w
            v_w = beta2 * v_w + (1 - beta2) * grad_w ** 2
            m_b = beta1 * m_b + (1 - beta1) * grad_b
            v_b = beta2 * v_b + (1 - beta2) * grad_b ** 2
            bc1 = 1 - beta1 ** ep
            bc2 = 1 - beta2 ** ep
            self.w -= lr * (m_w / bc1) / (np.sqrt(v_w / bc2) + eps)
            self.b -= lr * (m_b / bc1) / (np.sqrt(v_b / bc2) + eps)
            if verbose and ep % 200 == 0:
                p = self._sigmoid(x @ self.w + self.b)
                loss = -np.mean(mw * (y * np.log(p + 1e-9) + (1 - y) * np.log(1 - p + 1e-9)))
                print(f'  epoch {ep:5d}  loss {loss:.4f}')
                if prev_loss is not None and abs(prev_loss - loss) < 1e-6:
                    break
                prev_loss = loss
        return self

    def save(self, path):
        np.savez(path, w=self.w, b=np.asarray([self.b]), l2=np.asarray([self.l2]))

    @classmethod
    def load(cls, path):
        d = np.load(path)
        obj = cls(len(d['w']), l2=float(d['l2'][0]) if 'l2' in d else 1e-4)
        obj.w = d['w'].astype(np.float64)
        obj.b = float(d['b'][0])
        return obj


def main():
    p = argparse.ArgumentParser(description='纯 numpy 逻辑回归（TF 装不上时的备选）')
    p.add_argument('--data', required=True)
    p.add_argument('--val-data', default=None)
    p.add_argument('--out-dir', default='models')
    p.add_argument('--name', default='presence_model')
    p.add_argument('--epochs', type=int, default=1200)
    p.add_argument('--lr', type=float, default=0.05)
    p.add_argument('--l2', type=float, default=1e-4)
    p.add_argument('--val-frac', type=float, default=0.3)
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--ablate', default='none',
                   choices=['none', 'legacy', 'gates', 'agg'],
                   help='与 train.py 一致的消融开关')
    args = p.parse_args()

    np.random.seed(args.seed)
    data = [x.strip() for x in args.data.split(',') if x.strip()]
    seqs, aggs, ys, gs = [], [], [], []
    spec = None
    for i, path in enumerate(data):
        s, a, y, g, sp = load_npz(path)
        spec = spec or sp
        seqs.append(s)
        aggs.append(a)
        ys.append(y)
        gs.append(g + (int(gs[-1].max()) + 1 if gs else 0))
        print(f'  载入 {path}: {len(y)} 样本')
    seq = np.concatenate(seqs)
    agg = np.concatenate(aggs)
    y = np.concatenate(ys)
    groups = np.concatenate(gs)

    if args.val_data:
        vseq, vagg, vy, vg, _ = load_npz([x.strip() for x in args.val_data.split(',')][0])
        tr_idx = np.arange(len(y))
        va_mask = None
    else:
        tr_idx, va_idx = group_split(groups, y, val_frac=args.val_frac, seed=args.seed)
        vseq, vagg, vy = seq[va_idx], agg[va_idx], y[va_idx]

    if args.ablate != 'none':
        from features import ablate
        seq_a, agg_a = ablate(seq, agg, spec, args.ablate)
        vseq_a, vagg_a = ablate(vseq, vagg, spec, args.ablate)
    else:
        seq_a, agg_a, vseq_a, vagg_a = seq, agg, vseq, vagg

    norm = Normalizer.fit(seq_a[tr_idx], agg_a[tr_idx])
    tr_seq, tr_agg = norm.transform(seq_a[tr_idx], agg_a[tr_idx])
    va_seq, va_agg = norm.transform(vseq_a, vagg_a)
    x_tr = flatten(tr_seq, tr_agg)
    x_va = flatten(va_seq, va_agg)

    n_pos = int((y[tr_idx] == 1).sum())
    n_neg = int((y[tr_idx] == 0).sum())
    cw = {0: 1.0, 1: 1.0}
    if n_pos and n_neg:
        cw = {0: len(tr_idx) / (2.0 * n_neg), 1: len(tr_idx) / (2.0 * n_pos)}
    print(f'训练 {len(tr_idx)} / 验证 {len(vy)}，类别权重 {cw}')

    model = NumpyLogReg(x_tr.shape[1], l2=args.l2)
    model.fit(x_tr, y[tr_idx], epochs=args.epochs, lr=args.lr, class_weight=cw)

    p_tr = model.predict_proba(x_tr)
    p_va = model.predict_proba(x_va)
    rep, _ = format_report(vy, p_va, 0.5, 'numpy 逻辑回归 val @0.5')
    print(rep)
    thr, _ = best_threshold(vy, p_va, metric='f1')
    rep2, mbest = format_report(vy, p_va, thr, f'numpy 逻辑回归 val @{thr:.2f}')
    print(rep2)

    os.makedirs(args.out_dir, exist_ok=True)
    npz_path = os.path.join(args.out_dir, args.name + '_numpy.npz')
    model.save(npz_path)
    meta = {
        'arch': 'numpy_logreg',
        'backend': 'numpy',
        'ablate': args.ablate,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'spec': spec.to_dict(),
        'normalizer': norm.to_dict(),
        'threshold': float(thr),
        'metrics_val_at_threshold': mbest,
        'auc_val': roc_auc(vy, p_va),
        'auc_train': roc_auc(y[tr_idx], p_tr),
        'l2': args.l2,
        'data': data,
    }
    meta_path = os.path.join(args.out_dir, args.name + '_meta.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f'已保存 {npz_path}')
    print(f'已保存 {meta_path}')
    print('提示：detect_live.py --backend numpy 或 evaluate.py 会自动识别该后端。')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
