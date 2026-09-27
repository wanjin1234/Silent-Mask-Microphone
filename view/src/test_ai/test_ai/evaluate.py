# -*- coding: utf-8 -*-
"""在同一批标注数据上比较「神经网络」与「现有规则」。

这是回答「换 TensorFlow 到底值不值」的唯一可信方式：
在真机采集的同一份标注数据上，用相同指标并排比。合成数据上的漂亮数字
不能作为依据（见 README 的诚实说明）。

用法::

    # 只要规则基线的表现
    python evaluate.py --data data/real_val.npz --model-dir models

    # 对比「现行规则」与三种消融下的模型
    python evaluate.py --data data/real_val.npz --model-dir models --compare-ablation

输出为对齐的表格，便于直接贴进比赛报告。
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import Normalizer, ablate, ABLATE_MODES, WindowSpec
from metrics import format_report, roc_auc, prf, best_threshold
from model_io import load_model
from rule_baseline import rule_scores, RULE_MODES
from synth import load_npz


def main():
    p = argparse.ArgumentParser(description='模型 vs 现行规则，同一数据上对比')
    p.add_argument('--data', required=True, help='标注数据 npz（建议用真机验证集）')
    p.add_argument('--model-dir', default='models')
    p.add_argument('--name', default='presence_model')
    p.add_argument('--rule-mode', default='motion', choices=list(RULE_MODES),
                   help='现行规则基线：motion=部署逻辑，breath=呼吸周期，motion_or_breath=取两者上界')
    p.add_argument('--compare-ablation', action='store_true',
                   help='对比不同消融下**各自重新训练**的模型（放在 <model-dir>/abl_<模式>/）')
    p.add_argument('--ablation-name', default='presence_model',
                   help='消融模型的文件名前缀')
    p.add_argument('--out', default=None, help='把结果表写入 json')
    args = p.parse_args()

    seq, agg, y, groups, spec = load_npz(args.data)
    print(f'数据 {args.data}：{len(y)} 窗口（正 {int(y.sum())} / 负 {int((1 - y).sum())}），'
          f'片段 {len(np.unique(groups))}')
    print(f'窗口 {spec.window_frames} 帧 @ {spec.fps}Hz，seq_dim={spec.seq_dim}，'
          f'agg_dim={spec.agg_dim}\n')

    rows = {}

    # ---- 基线 1：现行规则 ----
    dec, score = rule_scores(seq, spec, mode=args.rule_mode, agg=agg)
    m_rule_hard = prf(y, dec.astype(float), 0.5)
    rule_auc = roc_auc(y, score)
    print(f'--- 现行规则基线：{args.rule_mode} ---')
    print(f'部署阈值下的硬判定：准确率 {m_rule_hard["acc"]:.4f}  精确率 {m_rule_hard["precision"]:.4f}  '
          f'召回率 {m_rule_hard["recall"]:.4f}  F1 {m_rule_hard["f1"]:.4f}  '
          f'特异度 {m_rule_hard["specificity"]:.4f}  '
          f'(TP={m_rule_hard["tp"]} FP={m_rule_hard["fp"]} '
          f'TN={m_rule_hard["tn"]} FN={m_rule_hard["fn"]})')
    print(f'连续分数 AUC：{rule_auc:.4f}')
    # 规则也能调阈值：给出「规则调到最好」的分数，作为更严格的比较对象，
    # 否则容易被「AI 赢了没调参的规则」这种不公平对比误导。
    # 注意规则的连续分数单位是 cm/s，不能套用 0~1 的概率网格，
    # 这里用观测值的分位数作为候选阈值（数据驱动，且不假设取值范围）。
    rule_grid = np.unique(np.round(np.quantile(score, np.linspace(0, 1, 101)), 4))
    rule_thr, _ = best_threshold(y, score, metric='f1', grid=rule_grid)
    m_rule_tuned = prf(y, score, rule_thr)
    print(f'规则在本数据上重选阈值 {rule_thr:.2f}（速度 cm/s）后：'
          f'F1 {m_rule_tuned["f1"]:.4f}  召回 {m_rule_tuned["recall"]:.4f}  '
          f'精确 {m_rule_tuned["precision"]:.4f}')
    rows[f'rule_{args.rule_mode}'] = {'hard': m_rule_hard, 'auc': round(rule_auc, 4),
                                      'tuned': {'threshold': round(rule_thr, 3), **m_rule_tuned}}

    # ---- 基线 2：随机猜测（下界参考）----
    rng = np.random.default_rng(0)
    rows['random'] = {'auc': round(roc_auc(y, rng.random(len(y))), 4),
                      'hard': prf(y, rng.random(len(y)), 0.5)}

    # ---- 模型 ----
    predict, meta, backend = load_model(args.model_dir, args.name)
    thr = float(meta.get('threshold', 0.5))
    print(f'\n--- 神经网络模型（{meta.get("arch")} / {backend} 后端，'
          f'消融={meta.get("ablate", "none")}）---')
    print(f'训练时保存的判定阈值：{thr:.2f}')
    p_model = predict(seq, agg)
    rep, m_model = format_report(y, p_model, 0.5, '模型 @0.5')
    print(rep)
    rep2, m_model_thr = format_report(y, p_model, thr, f'模型 @{thr:.2f}')
    print(rep2)
    rows['model'] = {'at_0.5': m_model, 'at_saved_threshold': m_model_thr,
                     'auc': round(roc_auc(y, p_model), 4),
                     'ablate': meta.get('ablate', 'none'), 'backend': backend}

    # ---- 公平比较：在验证数据上重选阈值，看模型的可达上界 ----
    tuned_thr, _ = best_threshold(y, p_model, metric='f1')
    m_tuned = prf(y, p_model, tuned_thr)
    rows['model_tuned'] = {'threshold': round(tuned_thr, 3), **m_tuned}
    print(f'\n模型在**本数据**上重选阈值 {tuned_thr:.2f} 后：'
          f'F1 {m_tuned["f1"]:.4f}（注意：这属于在验证集上调参，'
          f'真实上线前应在另一批独立数据上确认）')

    # ---- 消融对比 ----
    # 关键：消融必须在**各自重新训练**的模型上比较。把单个已训练模型
    # 直接喂入被置零的特征属于分布外输入，输出无意义（实测会得到 F1=0 的假结果）。
    if args.compare_ablation:
        print('\n--- 特征消融对比（每个模式各自重新训练）---')
        print(f'{"消融模式":<10}{"AUC":>8}{"准确率":>9}{"精确率":>9}{"召回率":>9}{"F1":>8}')
        for mode in ('none', 'legacy', 'gates', 'agg'):
            sub = os.path.join(args.model_dir, f'abl_{mode}')
            try:
                pred2, meta2, backend2 = load_model(sub, args.ablation_name)
            except SystemExit:
                print(f'{mode:<10}   [跳过] 缺少 {sub}，'
                      f'请先跑 train.py --ablate {mode} --out-dir {sub}')
                continue
            pm = pred2(seq, agg)
            t2, _ = best_threshold(y, pm, metric='f1')
            mm = prf(y, pm, t2)
            auc = roc_auc(y, pm)
            rows[f'ablation_{mode}'] = {'auc': round(auc, 4), 'f1': mm['f1'],
                                        'recall': mm['recall'],
                                        'precision': mm['precision'],
                                        'acc': mm['acc'], 'threshold': round(t2, 3)}
            print(f'{mode:<10}{auc:>8.4f}{mm["acc"]:>9.4f}{mm["precision"]:>9.4f}'
                  f'{mm["recall"]:>9.4f}{mm["f1"]:>8.4f}')
        print('说明：legacy=只用旧解析器暴露的 5 个标量（距离门/倒计时/方向/光强全部屏蔽）；')
        print('      gates=去掉距离门掩码；agg=去掉窗口统计量。')
        print('      none 为完整特征，是消融实验的上界。')

    # ---- 结论行 ----
    print('\n=== 结论 ===')
    print(f'现行规则（部署阈值）  F1={m_rule_hard["f1"]:.4f}  召回={m_rule_hard["recall"]:.4f}  '
          f'误报={m_rule_hard["fp"]}  漏检={m_rule_hard["fn"]}')
    print(f'现行规则（阈值调优）  F1={m_rule_tuned["f1"]:.4f}  召回={m_rule_tuned["recall"]:.4f}  '
          f'误报={m_rule_tuned["fp"]}  漏检={m_rule_tuned["fn"]}')
    print(f'神经网络模型          F1={m_model["f1"]:.4f}  召回={m_model["recall"]:.4f}  '
          f'误报={m_model["fp"]}  漏检={m_model["fn"]}')
    # 与「规则调优后」比较才是公平的：规则同样可以调阈值
    delta = m_model['f1'] - m_rule_tuned['f1']
    print()
    if delta > 0.02:
        print(f'→ 模型 F1 比调优后的规则高 {delta:+.4f}，且特征维度并未增加，'
              f'说明神经网络确实学到了阈值规则表达不了的判据（典型是'
              f'「距离门稳定性 + 多雷达一致性 + 时序形态」的组合）。')
    elif delta > -0.02:
        print(f'→ 与调优后的规则相差 {delta:+.4f}，两者相当。此时不必强行上模型，'
              f'除非模型在**固定误报率下召回更高**（看 ROC 工作点）。')
    else:
        print(f'→ 模型比调优后的规则低 {delta:+.4f}。请先检查：标注是否可靠、'
              f'验证集是否与训练集同场次（必须分场次）、样本量是否够。'
              f'不要仅凭一次结果否定方案。')
    print('\n注意：以上数字若来自合成数据（synth.py）只能说明流水线通畅，'
          '不能作为方案有效性依据；请用 collect.py 采集真机标注数据后重跑。')

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f'\n结果已写入 {args.out}')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
