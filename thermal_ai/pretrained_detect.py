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
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preprocess import T_MIN, T_MAX
from detect_live import PresenceEngine, source_real, source_sim

PERSON_CLASS = 0          # COCO 80 类中 person 的索引
DEFAULT_INPUT = 256       # 必须与导出时 imgsz 一致

# 与 mlx90640-thermal/continuos_graphic.py 的显示对齐：jet 色带 + 固定窗 [20,40]，
# 保证「检测器看到的伪彩色」和你屏幕上看到的一致（人体 30~37°C → 红/橙，背景 → 蓝）。
DISPLAY_T_MIN = 20.0
DISPLAY_T_MAX = 40.0

# 温度门：帧内峰值温度超过此值（热饮/电暖器/火苗等热源 >39°C，人体体表 ~35~37°C）
# 判定为非人，抑制该帧置信度。热源比人更热是绝对物理量，比纯形态判断更可靠。
TEMP_GATE_MAX = 39.0


def thermal_to_rgb(frame, input_size=DEFAULT_INPUT, colormap="jet",
                   t_min=DISPLAY_T_MIN, t_max=DISPLAY_T_MAX):
    """32x24 摄氏温度帧 → 归一化伪彩色 RGB → 放大到 (input_size, input_size)。

    默认 ``jet`` 色带 + ``[20,40]`` 窗口，与 ``continuos_graphic.py`` 显示一致；
    也支持 ``turbo`` / ``inferno`` 便于实验。
    """
    try:
        import cv2
    except ImportError as exc:
        raise SystemExit(
            "缺少 OpenCV，请执行：pip install opencv-python-headless") from exc

    cmaps = {"jet": cv2.COLORMAP_JET, "turbo": cv2.COLORMAP_TURBO,
             "inferno": cv2.COLORMAP_INFERNO}
    if colormap not in cmaps:
        raise ValueError(f"未知色带 {colormap}，可选 {list(cmaps)}")

    a = np.asarray(frame, dtype=np.float32).reshape(24, 32)
    a = np.clip((a - t_min) / (t_max - t_min), 0.0, 1.0)
    gray = (a * 255.0).astype(np.uint8)
    bgr = cv2.applyColorMap(gray, cmaps[colormap])
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if rgb.shape[:2] != (input_size, input_size):
        rgb = cv2.resize(rgb, (input_size, input_size), interpolation=cv2.INTER_LINEAR)
    return rgb


class TFLiteYoloPerson:
    """加载 COCO 检测 TFLite（YOLO11n 等），输出「有人」置信度与粗略位置。"""

    def __init__(self, model_path, input_size=DEFAULT_INPUT, conf_threshold=0.25,
                 colormap="jet", t_min=DISPLAY_T_MIN, t_max=DISPLAY_T_MAX):
        self.interp = _load_interpreter(model_path)
        self.interp.allocate_tensors()
        self.inp = self.interp.get_input_details()[0]
        self.out = self.interp.get_output_details()[0]
        self.input_size = int(input_size)
        self.conf_threshold = float(conf_threshold)
        self.colormap = colormap
        self.t_min = float(t_min)
        self.t_max = float(t_max)
        self.input_dtype = np.dtype(self.inp["dtype"])
        self.output_dtype = np.dtype(self.out["dtype"])
        # 输入布局：ultralytics 传统 tflite 是 NHWC [1,H,W,3]；新版 litert-torch
        # 导出是 NCHW [1,3,H,W]。依据「通道维在 shape 里的位置」自动判断。
        in_shape = list(self.inp["shape"])
        self.nchw = len(in_shape) == 4 and in_shape[1] == 3
        print(f"[AI] 已加载 {model_path}")
        print(f"    输入 shape={self.inp['shape']} dtype={self.input_dtype} "
              f"layout={'NCHW' if self.nchw else 'NHWC'}")
        print(f"    输出 shape={self.out['shape']} dtype={self.output_dtype} "
              f"quant={self.out.get('quantization')}")

    def predict(self, frame):
        rgb = thermal_to_rgb(frame, self.input_size, self.colormap,
                             self.t_min, self.t_max)   # (H, W, 3) RGB uint8

        # 转 float32 并归一化到 [0,1]（YOLO 输入约定）；量化张量再按需转 int/uint。
        x = rgb.astype(np.float32) / 255.0
        if self.nchw:
            x = x.transpose(2, 0, 1)                   # HWC -> CHW
        if self.input_dtype == np.int8:
            x = (x * 255.0 - 128.0).astype(np.int8)
        elif self.input_dtype == np.uint8:
            x = (x * 255.0).astype(np.uint8)
        else:
            x = x.astype(np.float32)
        x = x[None]                                     # 加 batch 维

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


def _big_human_frame():
    """构造一个「又大又明显的人形热斑」：大身体 + 显眼头部。

    用于回答「是不是合成人太小/没人形导致 YOLO 认不出」——若连这个明显目标
    YOLO 都认不出，说明是热像模态与自然 RGB 训练集的本质不匹配，而非样本问题。
    """
    bg = np.full((24, 32), 21.0, np.float32)
    yy, xx = np.mgrid[0:24, 0:32]
    cx, cy = 16.0, 14.0
    body = 15.0 * np.exp(-((xx - cx) ** 2 / (2 * 5.0 ** 2) + (yy - cy) ** 2 / (2 * 9.0 ** 2)))
    head = 16.0 * np.exp(-((xx - cx) ** 2 / (2 * 2.5 ** 2) + (yy - (cy - 12.0)) ** 2 / (2 * 2.5 ** 2)))
    bg += body.astype(np.float32) + head.astype(np.float32)
    return bg.reshape(-1).tolist()


class _BigHumanSim:
    def read_frame(self):
        return _big_human_frame()

    def close(self):
        pass


def make_predict_fn(model_path, input_size=DEFAULT_INPUT, conf_threshold=0.25,
                    colormap="jet", t_min=DISPLAY_T_MIN, t_max=DISPLAY_T_MAX,
                    peak_window=8, temp_gate=TEMP_GATE_MAX):
    """返回 ``fn(frames) -> (N,)`` 概率数组，接口与 ``detect_live`` 一致。

    两道把关：
    1. **温度门**：帧内峰值温度 > ``temp_gate``（默认 39°C）判为非人，置信度置 0。
       热源（热饮/电暖器/火苗）比人体更热，这是绝对物理量，能干净区分热源与人体。
    2. **滑动窗口峰值聚合**：返回最近 ``peak_window`` 帧中 person 置信度的最大值。
       YOLO 单帧置信度抖动很大，窗口峰值把「有人时的短时高峰」抬出来。
    """
    det = TFLiteYoloPerson(model_path, input_size, conf_threshold,
                           colormap, t_min, t_max)
    peak_buf = deque(maxlen=max(1, int(peak_window)))
    gate = float(temp_gate)

    def predict_fn(frames):
        last = 0.0
        for f in frames:
            arr = np.asarray(f, dtype=np.float32).reshape(-1)
            peak_c = float(arr.max())
            conf, _ = det.predict(f)
            if peak_c > gate:              # 温度门：过热 → 非人
                conf = 0.0
            peak_buf.append(float(conf))
            last = max(peak_buf)
        return np.asarray([last], dtype=np.float32)

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
    p.add_argument("--sim-mode", default="mixed",
                   choices=["empty", "human", "hot", "mixed", "human_big"])
    p.add_argument("--input-size", type=int, default=DEFAULT_INPUT,
                   help="必须等于导出时 imgsz")
    p.add_argument("--conf", type=float, default=0.25, help="person 框置信度下限")
    p.add_argument("--colormap", default="jet", choices=["jet", "turbo", "inferno"],
                   help="伪彩色色带（默认 jet，与显示一致）")
    p.add_argument("--t-min", type=float, default=DISPLAY_T_MIN)
    p.add_argument("--t-max", type=float, default=DISPLAY_T_MAX)
    p.add_argument("--rate", type=float, default=8.0, help="传感器帧率 Hz")
    p.add_argument("--peak-window", type=int, default=8,
                   help="滑动窗口峰值聚合的帧数（8Hz 下 8 帧≈1s）")
    p.add_argument("--temp-gate", type=float, default=TEMP_GATE_MAX,
                   help="温度门：峰值温度超过此值判为非人（默认 39°C）")
    p.add_argument("--on-threshold", type=float, default=0.15)
    p.add_argument("--off-threshold", type=float, default=0.10)
    p.add_argument("--smooth", type=float, default=0.4)
    p.add_argument("--min-on", type=int, default=6, help="8Hz 下≈0.75s 才置位")
    p.add_argument("--min-off", type=int, default=12, help="8Hz 下≈1.5s 才复位")
    p.add_argument("--publish", default=None, help="原子写入 JSON 状态文件路径")
    p.add_argument("--once", action="store_true", help="只推理一帧")
    args = p.parse_args()

    predict_fn = make_predict_fn(args.model, args.input_size, args.conf,
                                 args.colormap, args.t_min, args.t_max,
                                 args.peak_window, args.temp_gate)
    engine = PresenceEngine(
        predict_fn=predict_fn,
        on_threshold=args.on_threshold, off_threshold=args.off_threshold,
        smooth=args.smooth, min_on=args.min_on, min_off=args.min_off)

    if args.source == "real":
        source = source_real(args.rate)
    elif args.sim_mode == "human_big":
        source = _BigHumanSim()
    else:
        source = source_sim(args.sim_mode)

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
