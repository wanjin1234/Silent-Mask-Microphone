#!/usr/bin/env python3
"""开箱即用的预训练人体检测（热像 → 伪彩色 → TFLite YOLO11n）。

与现有「从零训练」链路（collect.py / train.py / detect_live.py）**完全独立**，
只新增本文件，不改、不删任何现有代码。两条链路可并行运行、结果再融合。

思路
----
MLX90640 的 32x24 温度帧 → 固定温度窗归一化 → 伪彩色 → 放大到 YOLO 输入尺寸
→ TFLite 推理 COCO 的 ``person`` 类（class 0）→ 复用 ``detect_live.PresenceEngine``
做概率平滑 + 双阈值滞回。

用法
----
    python pretrained_detect.py --model pretrained_models/yolo11n_full_integer_quant.tflite
    python pretrained_detect.py --model ... --source sim --once      # 无硬件自检
    python pretrained_detect.py --model ... --publish /tmp/thermal_presence.json

注意
----
1. ``--input-size`` 必须等于开发机导出时 ``imgsz``（默认 256）。
2. 8Hz 帧率下建议 ``--min-on 6 --min-off 12``（≈0.75s 置位 / ≈1.5s 复位）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preprocess import T_MIN, T_MAX
from detect_live import PresenceEngine, source_real, source_sim

PERSON_CLASS = 0          # COCO 80 类中 person 的索引
DEFAULT_INPUT = 256       # 必须与导出时 imgsz 一致


def thermal_to_rgb(frame, input_size=DEFAULT_INPUT):
    """32x24 摄氏温度帧 → 归一化伪彩色 RGB → 放大到 (input_size, input_size)。

    复用 ``preprocess`` 的固定温度窗 [T_MIN, T_MAX]：人体 30~37°C 落在窗内中上部，
    室温背景中下部、过热物体（>40°C）裁到顶部，与现有归一化语义保持一致。
    """
    try:
        import cv2
    except ImportError as exc:
        raise SystemExit(
            "缺少 OpenCV，请执行：pip install opencv-python-headless") from exc

    a = np.asarray(frame, dtype=np.float32).reshape(24, 32)
    a = np.clip((a - T_MIN) / (T_MAX - T_MIN), 0.0, 1.0)
    gray = (a * 255.0).astype(np.uint8)
    bgr = cv2.applyColorMap(gray, cv2.COLORMAP_TURBO)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if rgb.shape[:2] != (input_size, input_size):
        rgb = cv2.resize(rgb, (input_size, input_size), interpolation=cv2.INTER_LINEAR)
    return rgb


class TFLiteYoloPerson:
    """加载 COCO 检测 TFLite（YOLO11n 等），输出「有人」置信度与粗略位置。"""

    def __init__(self, model_path, input_size=DEFAULT_INPUT, conf_threshold=0.25):
        self.interp = _load_interpreter(model_path)
        self.interp.allocate_tensors()
        self.inp = self.interp.get_input_details()[0]
        self.out = self.interp.get_output_details()[0]
        self.input_size = int(input_size)
        self.conf_threshold = float(conf_threshold)
        self.input_dtype = np.dtype(self.inp["dtype"])
        self.output_dtype = np.dtype(self.out["dtype"])
        print(f"[AI] 已加载 {model_path}")
        print(f"    输入 shape={self.inp['shape']} dtype={self.input_dtype} "
              f"quant={self.inp.get('quantization')}")
        print(f"    输出 shape={self.out['shape']} dtype={self.output_dtype} "
              f"quant={self.out.get('quantization')}")

    def predict(self, frame):
        rgb = thermal_to_rgb(frame, self.input_size)

        # int8 输入减 128 成有符号；uint8 输入直接 0..255；float32 输入除以 255。
        if self.input_dtype == np.int8:
            x = (rgb.astype(np.float32) - 128.0).astype(np.int8)[None]
        elif self.input_dtype == np.uint8:
            x = rgb[None].astype(np.uint8)
        else:
            x = (rgb[None].astype(np.float32) / 255.0).astype(np.float32)

        self.interp.set_tensor(self.inp["index"], x)
        self.interp.invoke()
        raw = np.asarray(self.interp.get_tensor(self.out["index"]))
        if raw.dtype != np.float32:
            raw = _dequantize(raw, self.out)
        o = raw[0]

        # 统一成 (N, 84)：cx,cy,w,h + 80 类分数（兼容 (84,N) 与 (N,84) 两种布局）。
        if o.shape[0] == 84:
            o = o.transpose(1, 0)
        boxes = o[:, :4]
        person = o[:, 4 + PERSON_CLASS]

        k = int(np.argmax(person))
        conf = float(person[k])
        box = boxes[k]

        # ultralytics 的 tflite 输出：box 为像素坐标；个别版本为归一化 [0,1]，自适应。
        if box.max() <= 1.5:
            cx, cy, w, h = box * np.array([32.0, 24.0, 32.0, 24.0])
        else:
            cx, cy, w, h = box
            cx *= 32.0 / self.input_size
            cy *= 24.0 / self.input_size
            w *= 32.0 / self.input_size
            h *= 24.0 / self.input_size

        region = None
        if conf >= self.conf_threshold:
            region = {
                "center_x": round(float(cx), 1),
                "center_y": round(float(cy), 1),
                "w": round(float(w), 1),
                "h": round(float(h), 1),
                "conf": round(conf, 4),
            }
        return conf, region


def _dequantize(raw, details):
    """把 int8/uint8 输出张量按 scale/zero_point 反量化为 float32。"""
    q = details.get("quantization")
    scale, zp = 1.0, 0
    if q:
        if isinstance(q, dict):
            scale = q.get("scale", [1.0])
            zp = q.get("zero_point", [0])
        else:
            scale = q[0] if len(q) > 0 else 1.0
            zp = q[1] if len(q) > 1 else 0
        if isinstance(scale, (list, tuple, np.ndarray)):
            scale = scale[0] if len(scale) else 1.0
        if isinstance(zp, (list, tuple, np.ndarray)):
            zp = zp[0] if len(zp) else 0
    scale = float(scale)
    if scale <= 0:
        scale = 1.0
    return (raw.astype(np.float32) - float(zp)) * scale


def _load_interpreter(path):
    errors = []
    for desc, fn in [
        ("ai_edge_litert", lambda: _imp_ai_edge(path)),
        ("tflite_runtime", lambda: _imp_tflite_runtime(path)),
        ("tensorflow", lambda: _imp_tf(path)),
    ]:
        try:
            return fn()
        except Exception as e:
            errors.append(f"{desc}: {e}")
    print("[ERROR] 无法加载 TFLite 解释器，请安装其一：")
    print("  pip install ai-edge-litert       # 树莓派推荐")
    print("  pip install tflite-runtime")
    for line in errors:
        print("  -", line)
    raise SystemExit(2)


def _imp_ai_edge(path):
    from ai_edge_litert.interpreter import Interpreter
    return Interpreter(model_path=path)


def _imp_tflite_runtime(path):
    from tflite_runtime.interpreter import Interpreter
    return Interpreter(model_path=path)


def _imp_tf(path):
    from tensorflow.lite.python.interpreter import Interpreter
    return Interpreter(model_path=path)


def make_predict_fn(model_path, input_size=DEFAULT_INPUT, conf_threshold=0.25):
    """返回 ``fn(frames) -> (N,)`` 概率数组，接口与 ``detect_live`` 一致。"""
    det = TFLiteYoloPerson(model_path, input_size, conf_threshold)

    def predict_fn(frames):
        out = []
        for f in frames:
            conf, _ = det.predict(f)
            out.append(conf)
        return np.asarray(out, dtype=np.float32)

    return predict_fn


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description="预训练 YOLO 热像人体存在检测")
    p.add_argument("--model", required=True, help="YOLO 的 TFLite 模型路径")
    p.add_argument("--source", default="real", choices=["real", "sim"])
    p.add_argument("--sim-mode", default="mixed", choices=["empty", "human", "hot", "mixed"])
    p.add_argument("--input-size", type=int, default=DEFAULT_INPUT,
                   help="必须等于导出时 imgsz")
    p.add_argument("--conf", type=float, default=0.25, help="person 框置信度下限")
    p.add_argument("--rate", type=float, default=8.0, help="传感器帧率 Hz")
    p.add_argument("--on-threshold", type=float, default=0.5)
    p.add_argument("--off-threshold", type=float, default=0.35)
    p.add_argument("--smooth", type=float, default=0.4)
    p.add_argument("--min-on", type=int, default=6, help="8Hz 下≈0.75s 才置位")
    p.add_argument("--min-off", type=int, default=12, help="8Hz 下≈1.5s 才复位")
    p.add_argument("--publish", default=None, help="原子写入 JSON 状态文件路径")
    p.add_argument("--once", action="store_true", help="只推理一帧")
    args = p.parse_args()

    predict_fn = make_predict_fn(args.model, args.input_size, args.conf)
    engine = PresenceEngine(
        predict_fn=predict_fn,
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
                print(json.dumps({"error": str(exc)}, ensure_ascii=False),
                      file=sys.stderr, flush=True)
                time.sleep(0.05)
                continue
            info = engine.update(frame)
            print(json.dumps(info, ensure_ascii=False), flush=True)
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
