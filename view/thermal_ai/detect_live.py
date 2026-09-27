#!/usr/bin/env python3
"""实时人体存在检测（热成像）：逐帧推理 + 概率平滑 + 双阈值滞回。

为什么不能只用 0.5 阈值逐帧判定
------------------------------
MLX90640 帧率通常 1~2 Hz，单帧概率抖动会导致 HUD 图标闪烁、误报。这里做：

1. **概率平滑**（EMA）抑制单帧抖动；
2. **双阈值滞回**：升到 ``--on-threshold`` 才置「有人」，降到 ``--off-threshold``
   才复位，且需连续 ``--min-on`` / ``--min-off`` 帧确认。这与雷达模型里
   ``PresenceEngine`` 的滞回思路一致。

与主程序集成（两种方式，互不干扰）
----------------------------------
1. **独立进程 + JSON 文件**（推荐，零侵入）::

       python detect_live.py --publish /tmp/thermal_presence.json

   ``main_stereo.py`` 主循环读该文件即可把人体图标画出来。
2. **同进程调用**：``from detect_live import PresenceEngine``，每帧调用
   ``engine.update(frame)``，把返回的状态字典接到显示层。

数据源：``real``（MLX90640）/ ``sim``（合成，无硬件演示）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from model_io import load_model
from preprocess import frame_stats, human_region


class PresenceEngine:
    """平滑 + 滞回的人体存在判定引擎（可在主程序中直接实例化）。"""

    def __init__(self, predict_fn=None, threshold=0.5, on_threshold=None,
                 off_threshold=None, smooth=0.4, min_on=2, min_off=3):
        self.predict_fn = predict_fn
        self.on_threshold = float(on_threshold if on_threshold is not None else threshold)
        self.off_threshold = float(
            off_threshold if off_threshold is not None
            else max(self.on_threshold * 0.7, 0.01))
        self.smooth = float(smooth)
        self.min_on = int(min_on)
        self.min_off = int(min_off)
        self.prob_ema = None
        self.present = False
        self.on_streak = 0
        self.off_streak = 0
        self.n_frames = 0
        self.last_info = {}

    def reset(self):
        self.prob_ema = None
        self.present = False
        self.on_streak = self.off_streak = 0
        self.n_frames = 0

    def update(self, frame, raw=None):
        """喂入一帧原始温度（768 个值），返回本步状态字典。

        ``predict_fn`` 接收**原始温度帧**（shape ``(N, 24, 32)`` 或 ``(N, 768)``），
        返回 ``(N,)`` 概率。归一化由 ``predict_fn`` 内部完成（见 ``model_io``）。
        ``raw`` 可选传入原始数组，避免重复转换。
        """
        if self.predict_fn is None:
            raise RuntimeError("未提供 predict_fn")
        batch = np.asarray([frame], dtype=np.float32)
        prob = float(np.asarray(self.predict_fn(batch)).ravel()[0])
        self.prob_ema = prob if self.prob_ema is None else \
            (self.smooth * prob + (1 - self.smooth) * self.prob_ema)
        self.n_frames += 1

        if not self.present:
            if self.prob_ema >= self.on_threshold:
                self.on_streak += 1
                if self.on_streak >= self.min_on:
                    self.present = True
                    self.off_streak = 0
            else:
                self.on_streak = 0
        else:
            if self.prob_ema <= self.off_threshold:
                self.off_streak += 1
                if self.off_streak >= self.min_off:
                    self.present = False
                    self.on_streak = 0
            else:
                self.off_streak = 0

        region = human_region(frame)
        info = {
            "present": bool(self.present),
            "prob": round(float(self.prob_ema), 4),
            "prob_raw": round(float(prob), 4),
            "frames": self.n_frames,
            "region": region,
            "stats": frame_stats(frame),
            "timestamp": time.time(),
        }
        self.last_info = info
        return info


def source_real(rate_hz):
    from thermal_camera import ThermalCamera
    return ThermalCamera(rate_hz=rate_hz)


def source_sim(mode="mixed", seed=0):
    from thermal_camera import synthetic_frame

    class _Sim:
        def __init__(self):
            self.step = 0
            self.seed = seed

        def read_frame(self):
            block = self.step // 60
            m = ["empty", "human", "hot"][block % 3] if mode == "mixed" else mode
            f = synthetic_frame(self.step, m, self.seed)
            self.step += 1
            return f

        def close(self):
            pass

    return _Sim()


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description="热成像实时人体存在检测")
    p.add_argument("--source", default="real", choices=["real", "sim"])
    p.add_argument("--sim-mode", default="mixed", choices=["empty", "human", "hot", "mixed"])
    p.add_argument("--model-dir", default="models")
    p.add_argument("--name", default="presence_model")
    p.add_argument("--rate", type=float, default=2.0, help="传感器帧率 Hz")
    p.add_argument("--on-threshold", type=float, default=None)
    p.add_argument("--off-threshold", type=float, default=None)
    p.add_argument("--smooth", type=float, default=0.4)
    p.add_argument("--min-on", type=int, default=2)
    p.add_argument("--min-off", type=int, default=3)
    p.add_argument("--publish", default=None, help="原子写入 JSON 状态文件路径")
    p.add_argument("--once", action="store_true", help="只推理一帧")
    args = p.parse_args()

    # 加载模型（若只有规则无模型，则退化到「热区存在即有人」的简单规则）
    try:
        predict_fn, meta, backend = load_model(args.model_dir, args.name)
        threshold = float(meta.get("threshold", 0.5))
        print(f"[AI] 已加载模型 {args.model_dir}/{args.name}（{backend}，阈值 {threshold:.2f}）")
    except SystemExit as e:
        print(f"[AI] 未找到可用模型（{e}），回退到简单热区规则")
        predict_fn = None
        backend = "rule"
        threshold = 0.5

    if predict_fn is None:
        def predict_fn(frames):
            return np.asarray([1.0 if human_region(f) else 0.0
                               for f in frames], dtype=np.float32)

    engine = PresenceEngine(
        predict_fn=predict_fn, threshold=threshold,
        on_threshold=args.on_threshold, off_threshold=args.off_threshold,
        smooth=args.smooth, min_on=args.min_on, min_off=args.min_off)

    source = source_real(args.rate) if args.source == "real" else source_sim(args.sim_mode)

    try:
        interval = 1.0 / max(args.rate, 1e-6)
        while True:
            t0 = time.monotonic()
            try:
                frame = source.read_frame()
            except RuntimeError as exc:
                print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
                time.sleep(0.05)
                continue
            info = engine.update(frame)
            line = json.dumps(info, ensure_ascii=False)
            print(line, flush=True)
            if args.publish:
                _atomic_write(args.publish, info)
            if args.once:
                break
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
