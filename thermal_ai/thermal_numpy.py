# -*- coding: utf-8 -*-
"""纯 numpy 逻辑回归后端（TensorFlow 完全装不上时的保底）。

输入为展平的 24x32 归一化帧（768 维），输出 ``P(有人)``。训练与 ``train.py``
的 ``--export-numpy-only`` 一起使用，完全不依赖 TensorFlow。数据量小、特征只有
768 维时，L2 逻辑回归常能拿到不差的基线，代价是看不到空间结构（把每个像素当
独立特征），上限低于 CNN。
"""

from __future__ import annotations

import numpy as np

DIM = 32 * 24


class NumpyLogReg:
    def __init__(self, w: np.ndarray, b: float):
        self.w = np.asarray(w, dtype=np.float32).reshape(-1)
        self.b = float(b)

    @staticmethod
    def _sigmoid(z):
        z = np.clip(z, -30.0, 30.0)
        return 1.0 / (1.0 + np.exp(-z))

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).reshape(len(x), -1)
        return self._sigmoid(x @ self.w + self.b).astype(np.float32)

    @classmethod
    def fit(cls, x: np.ndarray, y: np.ndarray, l2: float = 1.0, epochs: int = 400,
            lr: float = 0.5) -> "NumpyLogReg":
        x = np.asarray(x, dtype=np.float32).reshape(len(x), -1)
        y = np.asarray(y, dtype=np.float32)
        w = np.zeros(x.shape[1], dtype=np.float32)
        b = 0.0
        for _ in range(epochs):
            p = cls._sigmoid(x @ w + b)
            g = p - y
            w_grad = (x.T @ g) / len(y) + l2 * w
            b_grad = g.mean()
            w -= lr * w_grad
            b -= lr * b_grad
        return cls(w, b)

    def save(self, path: str) -> None:
        np.savez_compressed(path, w=self.w, b=np.float32(self.b))

    @classmethod
    def load(cls, path: str) -> "NumpyLogReg":
        d = np.load(path, allow_pickle=False)
        return cls(d["w"], float(d["b"]))
