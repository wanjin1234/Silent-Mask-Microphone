import random
import time
import math

class SimulatedRadar:
    """模拟C4002雷达"""
    def __init__(self, angle, sensor_id):
        self.angle = angle
        self.sensor_id = sensor_id
        self.data = None
        
    def read_data(self):
        # 模拟距离数据（0.5-8米随机变化），偶尔产生近处障碍
        if random.random() < 0.3:
            distance = random.uniform(0.5, 1.5)  # 近处
        else:
            distance = random.uniform(2.0, 6.0)
        signal = random.randint(50, 200)
        return {
            'distance': distance,
            'signal': signal,
            'angle': self.angle,
            'sensor_id': self.sensor_id,
            'valid': True,
            'timestamp': time.time()
        }

class SimulatedUltrasonic:
    """模拟JSN-SR04T超声波"""
    def __init__(self, angle, sensor_id):
        self.angle = angle
        self.sensor_id = sensor_id
        
    def read_data(self):
        # 模拟近距离数据（0.2-2米）
        distance = random.uniform(0.2, 2.0)
        return {
            'distance': distance,
            'angle': self.angle,
            'sensor_id': self.sensor_id,
            'valid': True,
            'timestamp': time.time()
        }

class SimulatedSensorHub:
    """模拟整个传感器阵列"""
    def __init__(self):
        self.radars = [
            SimulatedRadar(-45, 0),
            SimulatedRadar(0, 1),
            SimulatedRadar(45, 2)
        ]
        self.ultrasonics = [
            SimulatedUltrasonic(-45, 0),
            SimulatedUltrasonic(0, 1),
            SimulatedUltrasonic(45, 2)
        ]
        
    def scan_all(self):
        """模拟一次完整扫描"""
        results = []
        for r in self.radars:
            results.append(r.read_data())
        for u in self.ultrasonics:
            results.append(u.read_data())
        return results
