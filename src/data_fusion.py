import math
import time
import numpy as np
from collections import deque

class DataFusion:
    def __init__(self):
        self.history = deque(maxlen=20)

    def fuse_measurements(self, radar_data, ultrasonic_data, sensor_height=0.8, pitch_deg=-5):
        """
        融合雷达和超声波数据，输出世界坐标
        radar_data: list of dicts with 'angle','distance','valid'
        ultrasonic_data: same
        返回: list of dicts with 'x','y','z','distance','angle','confidence'
        """
        fused = []
        angles = [-45, 0, 45]

        for angle in angles:
            radar_dist = None
            ultra_dist = None

            # 提取雷达数据
            for d in radar_data:
                if d and d.get('angle') == angle and d.get('valid'):
                    radar_dist = d.get('distance')
                    break

            # 提取超声波数据
            for d in ultrasonic_data:
                if d and d.get('angle') == angle and d.get('valid'):
                    ultra_dist = d.get('distance')
                    break

            # 融合逻辑
            if radar_dist is not None and ultra_dist is not None:
                if radar_dist < 1.0:
                    w_radar, w_ultra = 0.3, 0.7
                elif radar_dist < 2.0:
                    w_radar, w_ultra = 0.5, 0.5
                else:
                    w_radar, w_ultra = 0.8, 0.2
                distance = radar_dist * w_radar + ultra_dist * w_ultra
                confidence = 0.9
            elif radar_dist is not None:
                distance = radar_dist
                confidence = 0.7
            elif ultra_dist is not None:
                distance = ultra_dist
                confidence = 0.7
            else:
                continue

            # 转换为世界坐标 (x向右, y向上, z向前)
            x, y, z = self._polar_to_world(distance, angle, sensor_height, pitch_deg)

            fused.append({
                'angle': angle,
                'distance': distance,
                'x': x,
                'y': y,
                'z': z,
                'confidence': confidence,
                'timestamp': time.time()
            })

        return fused

    def _polar_to_world(self, distance, angle_deg, sensor_height, pitch_deg):
        """将距离和角度转换为世界坐标"""
        angle_rad = math.radians(angle_deg)
        pitch_rad = math.radians(pitch_deg)

        # 水平距离（地面投影）
        horizontal_dist = distance * math.cos(pitch_rad)
        # 高度：传感器高度 + 距离*sin(俯仰角)
        y = sensor_height + distance * math.sin(pitch_rad)

        # 横向和纵向
        x = horizontal_dist * math.sin(angle_rad)
        z = horizontal_dist * math.cos(angle_rad)

        return x, y, z

    def temporal_filter(self, fused_data):
        """时间滤波，返回稳定的障碍物列表（保留世界坐标）"""
        self.history.append(fused_data)
        if len(self.history) < 5:
            return fused_data

        # 取最近10帧平均
        recent = list(self.history)[-10:]
        averaged = []
        for angle in [-45, 0, 45]:
            xs, ys, zs, dists, confs = [], [], [], [], []
            for frame in recent:
                for obs in frame:
                    if obs['angle'] == angle:
                        xs.append(obs['x'])
                        ys.append(obs['y'])
                        zs.append(obs['z'])
                        dists.append(obs['distance'])
                        confs.append(obs['confidence'])
            if xs:
                avg_x = np.mean(xs)
                avg_y = np.mean(ys)
                avg_z = np.mean(zs)
                avg_dist = np.mean(dists)
                avg_conf = np.mean(confs)
                averaged.append({
                    'angle': angle,
                    'x': avg_x,
                    'y': avg_y,
                    'z': avg_z,
                    'distance': avg_dist,
                    'confidence': avg_conf
                })
        return averaged

