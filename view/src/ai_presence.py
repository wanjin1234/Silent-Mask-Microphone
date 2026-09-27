# -*- coding: utf-8 -*-
"""AI 人体存在检测 —— 与主程序 main_stereo.py 的粘合层。

为什么必须同进程、共用同一个雷达 hub
------------------------------------
C4002 通过 USB 串口接入，一个串口不能被两个进程同时可靠地读（会互相抢帧）。
``detect_live.py`` 的"独立进程 + JSON 文件"方案只适合雷达不在同一台机器上的
场景；在本项目的实际部署里，AI 与主程序必须共用同一个 ``RawRadarHub``。

本模块负责：
1. 打开 ``RawRadarHub``（拿到距离门掩码 ``exist_gate_index`` 等扩展字段），
   并补上超声波，供主程序避障显示继续使用；
2. 加载训练好的模型（找不到时退回规则后端）得到 ``PresenceEngine``；
3. 提供 ``raw_to_legacy()``，把原始帧转成旧解析器字段，供主程序已有的
   "移动扫描 + 障碍物融合"链路继续使用，避免改动那条稳定链路。
"""

import os
import sys

# 把 src/ 与 src/test_ai/test_ai/ 都加进 sys.path：
#   - src/                让 c4002_ext.py 能 import c4002_parser
#   - src/test_ai/test_ai/ 让本模块能 import detect_live / features / model_io 等
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, 'test_ai', 'test_ai')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _speed_deadzone():
    return float(os.getenv('C4002_SPEED_DEADZONE', '2'))


def _motion_speed_min():
    return float(os.getenv('C4002_MOTION_SPEED_MIN', '5'))


def raw_to_legacy(frame):
    """``RawC4002Serial.read_data()`` 原始帧 → 旧 ``C4002Serial`` 关键字段。

    原始帧含 ``move_speed`` / ``move_distance`` / ``exist_distance`` 等，但没有旧字段
    ``distance`` / ``motion`` / ``presence_stable``。这里按旧逻辑补出，供扫描聚合与
    ``data_fusion`` 障碍物融合继续使用。

    注意：运动判定依旧只依赖多普勒速度（与旧逻辑一致），不依赖固件的
    presence/target_status（这些字段在陌生环境未校准时不可信）。
    """
    if not frame or not frame.get('valid'):
        return {'valid': False}

    speed = int(frame.get('move_speed') or 0)
    if abs(speed) < _speed_deadzone():
        speed = 0
    motion = 1 if abs(speed) > _motion_speed_min() else 0

    exist_d = float(frame.get('exist_distance') or 0.0)
    move_d = float(frame.get('move_distance') or 0.0)
    dist = exist_d if exist_d > 0 else (move_d if move_d > 0 else 0.0)

    return {
        'distance': dist,
        'valid': dist > 0,
        'motion': motion,
        'angle': frame.get('angle'),
        'sensor_id': frame.get('sensor_id'),
        'timestamp': frame.get('timestamp', 0.0),
        # 人体显示已由"移动扫描 + AI"驱动，这里给 data_fusion 一个占位即可
        'presence': 0,
        'presence_raw': 0,
        'presence_stable': 0,
    }


def attach_ultrasonics(hub):
    """给 ``RawRadarHub`` 补上超声波（复用 ``RealSensorHub`` 的 pigpio 初始化）。"""
    hub.ultrasonics = []
    hub._ultra_backend = None
    try:
        from ultrasonic_rpigpio import RpiUltrasonic, UltrasonicSensor
        hub._ultra_backend = RpiUltrasonic()
        for trig, echo, ang in zip([22, 24, 5], [23, 25, 6], [-45, 0, 45]):
            hub.ultrasonics.append(UltrasonicSensor(hub._ultra_backend, trig, echo, ang))
    except Exception as e:
        print(f"超声波初始化失败（pigpiod 未启动或 RPi.GPIO 不可用）: {e}")
        hub.ultrasonics = []
    return hub


def build_ai_hub(ports, angles, baud=115200):
    """打开 ``RawRadarHub``（扩展字段）+ 超声波。失败抛异常由调用方兜底。"""
    from c4002_ext import RawRadarHub
    hub = RawRadarHub(ports=ports, angles=angles, baud=baud)
    return attach_ultrasonics(hub)


def _build_rule_engine(angles):
    """无模型时的规则后端（motion_or_breath），保证集成链路始终可用。"""
    import numpy as np

    from detect_live import PresenceEngine
    from features import WindowSpec
    from rule_baseline import rule_scores

    spec = WindowSpec(window_frames=20, fps=10.0, angles=[float(a) for a in angles])
    mode = os.getenv('C4002_AI_RULE_MODE', 'motion_or_breath')
    engine = PresenceEngine(
        spec=spec, predict_fn=None, threshold=0.5,
        smooth=float(os.getenv('C4002_AI_SMOOTH', '0.4')),
        min_on=int(os.getenv('C4002_AI_MIN_ON', '2')),
        min_off=int(os.getenv('C4002_AI_MIN_OFF', '3')),
        window_stride_s=float(os.getenv('C4002_AI_STRIDE_S', '0.5')))

    def rule_predict(seq, agg):
        dec, _ = rule_scores(seq, spec, mode=mode, agg=agg)
        return dec.astype(np.float32)

    engine.predict_fn = rule_predict
    engine.on_threshold = 0.5
    engine.off_threshold = 0.5
    return engine, spec, 'rule', False


def build_ai_engine(angles, model_dir=None, name=None):
    """加载 AI 引擎。返回 ``(engine, spec, backend, is_model)``。

    优先加载神经网络模型（tf/tflite/numpy 后端）；找不到模型时退回规则后端
    （motion_or_breath），保证集成链路在真机数据采集/训练完成前也能跑通。
    """
    from detect_live import PresenceEngine
    from features import WindowSpec
    from model_io import load_model

    model_dir = model_dir or os.getenv('C4002_AI_MODEL_DIR', 'models')
    name = name or os.getenv('C4002_AI_NAME', 'presence_model')

    try:
        predict_fn, meta, backend = load_model(model_dir, name)
        spec = WindowSpec.from_dict(meta['spec'])
        threshold = float(meta.get('threshold', 0.5))
        engine = PresenceEngine(
            spec=spec, predict_fn=predict_fn, threshold=threshold,
            on_threshold=os.getenv('C4002_AI_ON_THRESHOLD'),
            off_threshold=os.getenv('C4002_AI_OFF_THRESHOLD'),
            smooth=float(os.getenv('C4002_AI_SMOOTH', '0.4')),
            min_on=int(os.getenv('C4002_AI_MIN_ON', '2')),
            min_off=int(os.getenv('C4002_AI_MIN_OFF', '3')),
            window_stride_s=float(os.getenv('C4002_AI_STRIDE_S', '0.5')))
        print(f"[AI] 已加载模型 {model_dir}/{name}（{backend}，阈值 {threshold:.2f}）")
        return engine, spec, backend, True
    except SystemExit as e:
        print(f"[AI] 未找到可用模型（{e}），退回到规则后端（motion_or_breath）")
        return _build_rule_engine(angles)
    except Exception as e:
        print(f"[AI] 模型加载失败（{e}），退回到规则后端（motion_or_breath）")
        return _build_rule_engine(angles)
