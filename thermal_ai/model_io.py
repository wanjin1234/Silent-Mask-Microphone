# -*- coding: utf-8 -*-
"""模型加载：自动识别 TensorFlow / TFLite / numpy 后端。

查找顺序（与 ``src/test_ai/test_ai/model_io.py`` 同一约定）：

1. ``<dir>/<name>.keras``       → 完整 TensorFlow 后端
2. ``<dir>/<name>.tflite``      → TFLite 后端（树莓派推荐，最轻）
3. ``<dir>/<name>_numpy.npz``   → 纯 numpy 逻辑回归后端

``predict_fn(frames)`` 接收原始温度帧（shape ``(N, 24, 32)`` 或 ``(N, 768)``），
返回 ``(N,)`` 的 ``P(有人)`` 概率数组（内部完成温度归一化）。
"""

from __future__ import annotations

import json
import os

import numpy as np

from preprocess import frame_to_tensor, T_MIN, T_MAX


def load_model(model_dir, name="presence_model"):
    """加载模型与元数据，返回 ``(predict_fn, meta, backend)``。"""
    meta_path = os.path.join(model_dir, name + "_meta.json")
    if not os.path.exists(meta_path):
        raise SystemExit(
            f"找不到 {meta_path}\n"
            f"  请先训练：python train.py --data <数据.npz> --out-dir {model_dir}")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    t_min = float(meta.get("t_min", T_MIN))
    t_max = float(meta.get("t_max", T_MAX))

    keras_path = os.path.join(model_dir, name + ".keras")
    tflite_path = os.path.join(model_dir, name + ".tflite")
    npz_path = os.path.join(model_dir, name + "_numpy.npz")

    if os.path.exists(keras_path):
        try:
            import tensorflow as tf
            model = tf.keras.models.load_model(keras_path)

            def predict(frames):
                return model.predict(frame_to_tensor(frames, t_min, t_max),
                                     verbose=0).ravel().astype(np.float32)
            return predict, meta, "tf"
        except Exception as e:
            print(f"[WARN] TF 模型加载失败（{e}），尝试其它后端")

    if os.path.exists(tflite_path):
        interp_pred = _load_tflite(tflite_path, t_min, t_max)
        if interp_pred is not None:
            return interp_pred, meta, "tflite"

    if os.path.exists(npz_path):
        from thermal_numpy import NumpyLogReg
        model = NumpyLogReg.load(npz_path)

        def predict(frames):
            x = frame_to_tensor(frames, t_min, t_max).reshape(len(frames), -1)
            return model.predict_proba(x)
        return predict, meta, "numpy"

    raise SystemExit(
        f"{model_dir} 下既没有 {name}.keras / {name}.tflite 也没有 {name}_numpy.npz")


def _load_tflite(path, t_min, t_max):
    """加载 TFLite 解释器，返回 ``fn(frames) -> (N,)`` 或 None。

    解释器有多条来路，逐个尝试（实测新版 TF 上 ``from tensorflow.lite import
    Interpreter`` 会失败，必须走 ``tensorflow.lite.python.interpreter``）：

    1. 完整 TF：``tensorflow.lite.Interpreter`` / ``...python.interpreter``
    2. 树莓派轻量替代：``ai_edge_litert``（新版官方包名）
    3. 旧包名：``tflite_runtime``
    """
    interp = None
    errors = []
    try:
        import tensorflow as tf
        interp = tf.lite.Interpreter(model_path=path)
    except Exception as e:
        errors.append(f"tensorflow: {e}")
        try:
            from tensorflow.lite.python.interpreter import Interpreter
            interp = Interpreter(model_path=path)
        except Exception as e2:
            errors.append(f"tensorflow.lite.python: {e2}")
            try:
                from ai_edge_litert.interpreter import Interpreter
                interp = Interpreter(model_path=path)
            except Exception as e3:
                errors.append(f"ai_edge_litert: {e3}")
                try:
                    from tflite_runtime.interpreter import Interpreter
                    interp = Interpreter(model_path=path)
                except Exception as e4:
                    errors.append(f"tflite_runtime: {e4}")
                    print("[WARN] 无法加载 TFLite 解释器，请二选一：")
                    print("  pip install ai-edge-litert      # 树莓派推荐，轻量")
                    print("  pip install tensorflow          # 完整 TF 自带解释器")
                    for line in errors:
                        print(f"  - {line}")
                    return None
    interp.allocate_tensors()
    inp = interp.get_input_details()[0]
    out = interp.get_output_details()[0]

    def predict(frames):
        x = frame_to_tensor(frames, t_min, t_max)
        probs = []
        for i in range(len(x)):
            interp.set_tensor(inp["index"], x[i:i + 1])
            interp.invoke()
            probs.append(float(interp.get_tensor(out["index"]).ravel()[0]))
        return np.asarray(probs, dtype=np.float32)

    return predict
