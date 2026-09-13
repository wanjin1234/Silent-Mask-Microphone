# -*- coding: utf-8 -*-
"""模型加载：自动识别 TensorFlow / numpy 后端。

被 ``evaluate.py`` 与 ``detect_live.py`` 共用，避免两边各写一份加载逻辑
（两处不一致会导致「评估用的是 A 模型、上线用的是 B 模型」这类隐蔽问题）。
"""

import json
import os

from features import Normalizer


def load_model(model_dir, name='presence_model'):
    """加载模型与元数据。

    返回 ``(predict_fn, meta, backend)``，其中
    ``predict_fn(seq, agg) -> 概率数组``（内部完成标准化）。

    查找顺序：
      1. ``<dir>/<name>.keras``      → TensorFlow 后端
      2. ``<dir>/<name>.tflite``     → TFLite 后端（树莓派常用；无 TF 时用解释器）
      3. ``<dir>/<name>_numpy.npz``  → numpy 逻辑回归后端
    """
    meta_path = os.path.join(model_dir, name + '_meta.json')
    if not os.path.exists(meta_path):
        raise SystemExit(
            f'找不到 {meta_path}\n'
            f'  请先训练：python train.py --data <数据.npz> --out-dir {model_dir}')
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    norm = Normalizer.from_dict(meta['normalizer'])

    keras_path = os.path.join(model_dir, name + '.keras')
    tflite_path = os.path.join(model_dir, name + '.tflite')
    npz_path = os.path.join(model_dir, name + '_numpy.npz')

    if os.path.exists(keras_path):
        try:
            from tensorflow import keras
            model = keras.models.load_model(keras_path)

            def predict(seq, agg):
                s, a = norm.transform(seq, agg)
                return model.predict([s, a], verbose=0).ravel()
            return predict, meta, 'tf'
        except Exception as e:
            print(f'[WARN] TF 模型加载失败（{e}），尝试其它后端')

    if os.path.exists(tflite_path):
        interp_pred = _load_tflite(tflite_path)
        if interp_pred is not None:
            def predict(seq, agg):
                s, a = norm.transform(seq, agg)
                return interp_pred(s, a)
            return predict, meta, 'tflite'

    if os.path.exists(npz_path):
        from baseline_numpy import NumpyLogReg, flatten
        model = NumpyLogReg.load(npz_path)

        def predict(seq, agg):
            s, a = norm.transform(seq, agg)
            return model.predict_proba(flatten(s, a))
        return predict, meta, 'numpy'

    raise SystemExit(
        f'{model_dir} 下既没有 {name}.keras / {name}.tflite 也没有 {name}_numpy.npz')


def _load_tflite(path):
    """加载 TFLite 解释器；返回 ``fn(seq, agg) -> 概率`` 或 None。

    解释器有多条来路，逐个尝试（实测 TF 2.21 上 ``from tensorflow.lite import
    Interpreter`` 会失败，必须走 ``tensorflow.lite.python.interpreter``）：

    1. 完整 TF 环境：``tensorflow.lite.Interpreter`` / ``...python.interpreter``
    2. 树莓派轻量替代：``ai_edge_litert``（新版官方包名）
    3. 旧包名：``tflite_runtime``
    """
    interp = None
    errors = []
    try:
        import tensorflow as tf
        interp = tf.lite.Interpreter(model_path=path)
    except Exception as e:
        errors.append(f'tensorflow: {e}')
        try:
            from tensorflow.lite.python.interpreter import Interpreter
            interp = Interpreter(model_path=path)
        except Exception as e2:
            errors.append(f'tensorflow.lite.python: {e2}')
            try:
                from ai_edge_litert.interpreter import Interpreter
                interp = Interpreter(model_path=path)
            except Exception as e3:
                errors.append(f'ai_edge_litert: {e3}')
                try:
                    from tflite_runtime.interpreter import Interpreter
                    interp = Interpreter(model_path=path)
                except Exception as e4:
                    errors.append(f'tflite_runtime: {e4}')
                    print('[WARN] 无法加载 TFLite 解释器，请二选一：')
                    print('  pip install ai-edge-litert      # 树莓派推荐，轻量')
                    print('  pip install tensorflow          # 完整 TF 自带解释器')
                    for line in errors:
                        print(f'  - {line}')
                    return None
    interp.allocate_tensors()
    inp = interp.get_input_details()
    out = interp.get_output_details()
    # 输入顺序按 meta 里保存的签名固定为 [seq, agg]；这里按 shape 维度做一次校验
    in_by_rank = sorted(inp, key=lambda d: len(d['shape']), reverse=True)
    seq_detail, agg_detail = in_by_rank[0], in_by_rank[1]

    def predict(seq, agg):
        probs = []
        for i in range(len(seq)):
            interp.set_tensor(seq_detail['index'], seq[i:i + 1].astype('float32'))
            interp.set_tensor(agg_detail['index'], agg[i:i + 1].astype('float32'))
            interp.invoke()
            probs.append(float(interp.get_tensor(out[0]['index']).ravel()[0]))
        import numpy as np
        return np.asarray(probs, dtype=np.float32)
    return predict
