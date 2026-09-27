#!/usr/bin/env python3
"""在**独立场次**的验证数据上评估热成像人体检测模型。

为什么需要本脚本
----------------
``train.py`` 只打印验证集 AUC 与最优阈值，既不给出混淆矩阵，也不暴露
「某一段被整体判错」的系统性问题。本脚本用**从未参与训练**的场次（session）
数据做最终评估，输出：

1. AUC / 最优阈值 / 精确率 / 召回率 / F1 / 准确率；
2. 选定阈值下的混淆矩阵（真阳/假阳/真阴/假阴）；
3. 逐片段（session）错误分布，暴露「某个场景被整体判错」的系统性问题。

用法::

    python evaluate.py --data data/heldout.npz --model-dir models
    python evaluate.py --data data/heldout.npz --model-dir models --threshold 0.5
    python evaluate.py --data data/heldout.npz --model-dir models --sweep
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from model_io import load_model
from synth import load_npz
from train import _auc, _best_threshold


def _confusion(y, pred):
    y = np.asarray(y, dtype=int)
    pred = np.asarray(pred, dtype=int)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    return tp, fp, tn, fn


def _prf_acc(y, pred):
    tp, fp, tn, fn = _confusion(y, pred)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    acc = (tp + tn) / (tp + fp + tn + fn) if (tp + fp + tn + fn) else 0.0
    return prec, rec, f1, acc


def _report_table(p, y, threshold):
    pred = (p >= threshold).astype(int)
    tp, fp, tn, fn = _confusion(y, pred)
    prec, rec, f1, acc = _prf_acc(y, pred)
    print(f"阈值 {threshold:.3f}")
    print(f"  AUC        {_auc(y, p):.4f}")
    print(f"  准确率      {acc:.4f}")
    print(f"  精确率      {prec:.4f}（判「有人」里真正有人的比例）")
    print(f"  召回率      {rec:.4f}（真正有人里被检出的比例）")
    print(f"  F1         {f1:.4f}")
    print(f"  混淆矩阵    TP={tp}  FP={fp}  TN={tn}  FN={fn}")
    return tp, fp, tn, fn


def _per_session(groups, y, p, threshold):
    print("\n逐片段（session）错误分布：")
    n_bad = 0
    for g in sorted(set(groups.tolist())):
        idx = groups == g
        label = int(y[idx][0])
        n = int(idx.sum())
        mean_p = float(p[idx].mean())
        pred = 1 if mean_p >= threshold else 0
        flag = "OK" if pred == label else "!!"
        if flag == "!!":
            n_bad += 1
        print(f"  session {g:3d}  标签 {label}  帧数 {n:3d}  "
              f"平均概率 {mean_p:.3f}  判定 {pred}  {flag}")
    print(f"  （共 {len(set(groups.tolist()))} 段，{n_bad} 段被整体判错）")


def main():
    p = argparse.ArgumentParser(description="评估热成像人体检测模型")
    p.add_argument("--data", required=True, help="独立验证 npz（未参与训练的场次）")
    p.add_argument("--model-dir", default="models")
    p.add_argument("--name", default="presence_model")
    p.add_argument("--threshold", type=float, default=None,
                   help="覆盖模型元数据里的阈值")
    p.add_argument("--sweep", action="store_true", help="打印阈值扫描表")
    args = p.parse_args()

    predict_fn, meta, backend = load_model(args.model_dir, args.name)
    print(f"模型后端：{backend}")

    frames, y, groups, _ = load_npz(args.data)
    print(f"验证数据：{len(y)} 帧（正 {int(y.sum())} / 负 {int((1 - y).sum())}），"
          f"共 {len(set(groups.tolist()))} 段")

    # 注意：predict_fn 接收的是**原始温度帧**，归一化在其内部完成（见 model_io）
    p_prob = np.asarray(predict_fn(frames), dtype=np.float32).ravel()

    threshold = args.threshold if args.threshold is not None else float(meta.get("threshold", 0.5))
    _report_table(p_prob, y, threshold)
    _per_session(groups, y, p_prob, threshold)

    if args.sweep:
        print("\n阈值扫描（精确率/召回率/F1）：")
        for t in np.arange(0.1, 1.0, 0.1):
            prec, rec, f1, acc = _prf_acc(y, (p_prob >= t).astype(int))
            print(f"  {t:.2f}  精确率 {prec:.3f}  召回率 {rec:.3f}  F1 {f1:.3f}  准确率 {acc:.3f}")
        best = _best_threshold(y, p_prob)
        print(f"\n本数据上最优阈值(F1)：{best:.3f}")

    print("\n判读建议：")
    print("  - 召回率低：漏报多，检查是否有人距离太远/穿厚外套/姿态未被覆盖")
    print("  - 精确率低：误报多，检查负样本是否覆盖了热饮/电暖器/阳光热斑等干扰")
    print("  - 逐片段有 !!：说明模型在某个具体场景系统性失效，优先补采该场景")


if __name__ == "__main__":
    main()
