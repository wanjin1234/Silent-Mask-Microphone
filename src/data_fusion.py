import math
import time
import numpy as np
from collections import deque

class DataFusion:
    def __init__(self):
        self.history = deque(maxlen=20)
        self.extra_sensor_data = {}   # 可扩展：存储其他传感器数据

    def fuse_measurements(self, radar_data, ultrasonic_data, sensor_height=0.8, pitch_deg=-5):
        fused_obstacles = []
        fused_humans = []
        angles = [-45, 0, 45]

        for angle in angles:
            radar_dist = None
            ultra_dist = None
            presence = 0

            # 提取雷达数据
            for d in radar_data:
                if d and d.get('angle') == angle and d.get('valid'):
                    radar_dist = d.get('distance')
                    presence = d.get('presence', 0)
                    break

            # 提取超声波数据
            for d in ultrasonic_data:
                if d and d.get('angle') == angle and d.get('valid'):
                    ultra_dist = d.get('distance')
                    break

            # 融合距离
            if radar_dist is not None and ultra_dist is not None:
                if radar_dist < 1.0:
                    w_radar, w_ultra = 0.3, 0.7
                elif radar_dist < 2.0:
                    w_radar, w_ultra = 0.5, 0.5
                else:
                    w_radar, w_ultra = 0.8, 0.2
                distance = radar_dist * w_radar + ultra_dist * w_ultra
            elif radar_dist is not None:
                distance = radar_dist
            elif ultra_dist is not None:
                distance = ultra_dist
            else:
                continue

            # 世界坐标转换
            x, y, z = self._polar_to_world(distance, angle, sensor_height, pitch_deg)

            obs_entry = {
                'angle': angle,
                'distance': distance,
                'x': x,
                'y': y,
                'z': z,
                'presence': presence,
                'timestamp': time.time()
            }
            fused_obstacles.append(obs_entry)

            # 如果有人，则加入人体列表
            if presence == 1:
                fused_humans.append(obs_entry)

        return fused_obstacles, fused_humans

    def _polar_to_world(self, distance, angle_deg, sensor_height, pitch_deg):
        angle_rad = math.radians(angle_deg)
        pitch_rad = math.radians(pitch_deg)
        horizontal_dist = distance * math.cos(pitch_rad)
        y = sensor_height + distance * math.sin(pitch_rad)
        x = horizontal_dist * math.sin(angle_rad)
        z = horizontal_dist * math.cos(angle_rad)
        return x, y, z

    def temporal_filter(self, obstacles, humans):
        """对障碍物和人体分别进行时间滤波"""
        self.history.append(obstacles)
        filtered_obstacles = self._filter_obstacles()
        filtered_humans = self._filter_humans(humans)
        return filtered_obstacles, filtered_humans

    def _filter_obstacles(self):
        if len(self.history) < 5:
            return list(self.history[-1]) if self.history else []
        recent = list(self.history)[-10:]
        averaged = []
        for angle in [-45, 0, 45]:
            xs, ys, zs, dists, pres = [], [], [], [], []
            for frame in recent:
                for obs in frame:
                    if obs['angle'] == angle:
                        xs.append(obs['x'])
                        ys.append(obs['y'])
                        zs.append(obs['z'])
                        dists.append(obs['distance'])
                        pres.append(obs['presence'])
            if xs:
                avg_x = np.mean(xs)
                avg_y = np.mean(ys)
                avg_z = np.mean(zs)
                avg_dist = np.mean(dists)
                # 最近是否有人：如果历史中任意一帧有人，则保留
                avg_pres = 1 if any(pres) else 0
                averaged.append({
                    'angle': angle,
                    'x': avg_x,
                    'y': avg_y,
                    'z': avg_z,
                    'distance': avg_dist,
                    'presence': avg_pres
                })
        return averaged

    def _filter_humans(self, humans):
        # 简单直接返回当前帧的人体，可增加平滑
        return humans

    def set_extra_sensor_data(self, key, value):
        """存储其他传感器数据"""
        self.extra_sensor_data[key] = value
