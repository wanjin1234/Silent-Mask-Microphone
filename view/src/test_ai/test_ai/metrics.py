# -*- coding: utf-8 -*-
"""纯 numpy 评估指标。

不依赖 scikit-learn：树莓派部署环境装 sklearn 代价不小，而这些指标本身很短。
被 ``train.py`` / ``baseline_numpy.py`` / ``evaluate.py`` 共用。
"""

import numpy as np


def confusion(y, p, thr=0.5):
    """返回 ``(tp, fp, tn, fn)``。"""
    y = np.asarray(y).astype(np.int64)
    pred = (np.asarray(p) >= thr).astype(np.int64)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    return tp, fp, tn, fn


def prf(y, p, thr=0.5):
    """准确率 / 精确率 / 召回率 / F1 / 特异度。分母为 0 时该项取 0。"""
    tp, fp, tn, fn = confusion(y, p, thr)
    total = tp + fp + tn + fn
    acc = (tp + tn) / total if total else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        'acc': round(acc, 4), 'precision': round(precision, 4),
        'recall': round(recall, 4), 'f1': round(f1, 4),
        'specificity': round(specificity, 4),
        'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn, 'n': total,
    }


def roc_auc(y, scores):
    """秩和法 ROC-AUC。样本出现并列分数时用平均秩处理。

    AUC 对判定阈值不敏感，是挑选模型/早停最稳的指标；
    部署时关心的 F1 / 召回率再单独按阈值算。
    """
    y = np.asarray(y).astype(np.int64)
    s = np.asarray(scores, dtype=np.float64)
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return float('nan')
    order = np.argsort(s, kind='mergesort')
    ranks = np.empty(len(s), dtype=np.float64)
    ranks[order] = np.arange(1, len(s) + 1)
    # 并列分数取平均秩
    s_sorted = s[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    rank_sum = ranks[y == 1].sum()
    return float((rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def best_threshold(y, scores, metric='f1', grid=None, tolerance=0.005):
    """在候选阈值上挑最优判定阈值（默认最大化 F1）。

    为什么必须做：模型输出的是概率，0.5 未必是「有人/没人」的最佳分界。
    比赛场景更怕漏检（漏掉幸存者），可以用 ``metric='recall'`` 配一个
    最低精确率约束，或直接在 meta 里指定阈值。

    **为什么取「近优区间的中位数」而不是 argmax**：当验证集几乎可完美分离时，
    F1 会在一大片阈值区间内并列最优，argmax 会返回网格最左端（例如 0.02）。
    这个阈值在部署时非常危险——噪声让概率轻微抬头就会误报。
    实测：空场窗口概率约 0.02~0.11，被 0.02 的阈值判成「有人」占 53%。
    取近优区间的中位数能让阈值落在分离带中间，留出两侧余量。
    """
    if grid is None:
        grid = np.unique(np.round(np.linspace(0.02, 0.98, 97), 4))
    vals = []
    for thr in grid:
        m = prf(y, scores, thr)
        if metric == 'recall':
            # 召回率优先，但精确率不能低于 0.5，否则会把空场全判成有人
            val = m['recall'] if m['precision'] >= 0.5 else m['recall'] * m['precision']
        elif metric == 'acc':
            val = m['acc']
        else:
            val = m['f1']
        vals.append(val)
    vals = np.asarray(vals, dtype=np.float64)
    best_val = float(vals.max())
    good = np.asarray(grid)[vals >= best_val - tolerance]
    return float(np.median(good)), best_val


def format_report(y, p, thr=0.5, title=''):
    m = prf(y, p, thr)
    auc = roc_auc(y, p)
    lines = []
    if title:
        lines.append(f'--- {title} ---')
    lines.append(f'样本 {m["n"]}（正 {m["tp"] + m["fn"]} / 负 {m["tn"] + m["fp"]}）  '
                 f'阈值 {thr:.2f}  AUC {auc:.4f}')
    lines.append(f'准确率 {m["acc"]:.4f}  精确率 {m["precision"]:.4f}  '
                 f'召回率 {m["recall"]:.4f}  F1 {m["f1"]:.4f}  特异度 {m["specificity"]:.4f}')
    lines.append(f'混淆矩阵  TP={m["tp"]}  FP={m["fp"]}  TN={m["tn"]}  FN={m["fn"]}')
    return '\n'.join(lines), {'auc': round(auc, 4), **m}
