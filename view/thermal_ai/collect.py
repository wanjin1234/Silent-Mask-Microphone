#!/usr/bin/env python3
"""真机标注采集：把 MLX90640 温度帧 + 标签写成训练用 npz。

模型上限由数据决定。热成像人类检测最难的点不是「看见热斑」，而是
**把 30~37°C 的人形斑块，与 >40°C 的高温物体（热饮/散热器/火苗）、以及
人体周边的热辐射反射区分开**。因此采集必须覆盖这些难例。

采集要求（照做，否则训出来的模型上线会崩）
------------------------------------------
1. **分场次多次采集**：每次录制相邻帧高度相似。每个类别至少采 10 段、每段
   20~60s，``--session`` 每次换名字（或直接让脚本自动编号）。
2. **负样本要包含「像人但不是人」的干扰**：热饮、电暖器、刚关掉的显示器、
   窗边的阳光热斑、墙面热反射。
3. **正样本要覆盖难例**：人站不同距离（近/远）、侧身/背身、坐下/蹲下、
   只露出身体一部分、穿厚外套（表面温度较低）。
4. **换房间/换摆位要重新采**——热像强依赖背景温度与反射环境。

用法::

    # 树莓派上（先确认 I2C 能读到 0x33）
    export MLX90640_I2C_BUS=1
    python collect.py --label empty --seconds 30 --session empty_room_01
    python collect.py --label human --seconds 30 --session person_near_01 --out data/real.npz
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from preprocess import T_MIN, T_MAX
from synth import save_npz, load_npz
from thermal_camera import ThermalCamera, PIXEL_COUNT


class _SimCamera:
    """模拟相机：用 ``thermal_camera.synthetic_frame`` 冒充硬件，便于无硬件试跑。"""

    def __init__(self, mode, seed=0):
        self.mode = mode
        self.step = 0
        self.seed = seed
        self.serial_number = (0xDE, 0xAD)

    def read_frame(self):
        from thermal_camera import synthetic_frame
        f = synthetic_frame(self.step, self.mode, self.seed)
        self.step += 1
        return f

    def close(self):
        pass


def open_camera(rate_hz, simulate=False):
    if simulate:
        return _SimCamera("human" if os.getenv("THERMAL_SIM_MODE", "human") else "human")
    return ThermalCamera(rate_hz=rate_hz)


def collect(camera, label, seconds, session, rate_hz=2.0, verbose=True):
    """采集一段，返回 ``(frames, y, groups)``。帧率按传感器刷新率近似。"""
    frames, ys, groups = [], [], []
    interval = 1.0 / max(rate_hz, 1e-6)
    t0 = time.time()
    n_frames = 0
    last_print = 0.0
    while time.time() - t0 < seconds:
        try:
            frame = camera.read_frame()
        except RuntimeError as exc:
            if verbose:
                print(f"  [警告] 读帧失败：{exc}")
            time.sleep(0.05)
            continue
        frames.append(np.asarray(frame, dtype=np.float32))
        ys.append(1 if label == "human" else 0)
        groups.append(0)  # 追加时统一重编号
        n_frames += 1
        if verbose and time.time() - last_print > 1.0:
            last_print = time.time()
            el = time.time() - t0
            print(f"\r  [{session}] {el:5.1f}/{seconds:.0f}s  帧数 {n_frames}",
                  end="", flush=True)
        time.sleep(max(0.0, interval - (time.time() - t0) % interval))
    if verbose:
        print()
    if not frames:
        print(f"  [警告] {session} 未采到任何帧，检查 I2C 接线。")
        return None
    return (np.asarray(frames, dtype=np.float32),
            np.asarray(ys, dtype=np.int64),
            np.zeros(len(frames), dtype=np.int64))


def append_to_npz(path, frames, y, groups, t_min, t_max):
    if os.path.exists(path):
        oframes, oy, ogroups, meta = load_npz(path)
        offset = int(ogroups.max()) + 1 if len(ogroups) else 0
        groups = groups + offset
        frames = np.concatenate([oframes, frames], axis=0)
        y = np.concatenate([oy, y], axis=0)
        groups = np.concatenate([ogroups, groups], axis=0)
    save_npz(path, frames, y, groups, t_min, t_max)


def main():
    p = argparse.ArgumentParser(description="采集 MLX90640 热像标注数据")
    p.add_argument("--label", required=True, choices=["empty", "human"],
                   help="该段标签：empty=无人 / human=有人")
    p.add_argument("--seconds", type=float, default=30.0)
    p.add_argument("--session", default=None, help="片段名（默认自动用时间戳）")
    p.add_argument("--out", default="data/real.npz")
    p.add_argument("--rate", type=float, default=2.0, help="传感器帧率 Hz")
    p.add_argument("--simulate", action="store_true", help="无硬件试跑")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    session = args.session or time.strftime("%Y%m%d_%H%M%S")
    camera = open_camera(args.rate, args.simulate)
    try:
        result = collect(camera, args.label, args.seconds, session,
                         args.rate, verbose=not args.quiet)
    finally:
        camera.close()
    if result is None:
        return 2
    frames, y, groups = result
    append_to_npz(args.out, frames, y, groups, T_MIN, T_MAX)


if __name__ == "__main__":
    raise SystemExit(main())
