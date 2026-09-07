import random
import time

class SimulatedRadar:
    """模拟C4002雷达，新增presence字段用于人体存在检测"""
    def __init__(self, angle, sensor_id):
        self.angle = angle
        self.sensor_id = sensor_id

    def read_data(self):
        # 模拟距离：0.5-8米随机
        distance = random.uniform(0.5, 8.0)
        # 模拟人体存在：30%概率有人
        presence = 1 if random.random() < 0.3 else 0
        return {
            'distance': distance,
            'signal': random.randint(50, 200),
            'presence': presence,          # 人体存在标志
            'presence_raw': presence,
            'presence_stable': presence,
            # 与真实 C4002 解析器保持一致：扫描聚合层消费 breath_evidence / motion
            'breath_evidence': presence,
            'motion': presence,
            'target_status': 1 if presence else 0,
            'angle': self.angle,
            'sensor_id': self.sensor_id,
            'valid': True,
            'timestamp': time.time()
        }

class SimulatedUltrasonic:
    """模拟JSN-SR04T超声波（不检测人体）"""
    def __init__(self, angle, sensor_id):
        self.angle = angle
        self.sensor_id = sensor_id

    def read_data(self):
        distance = random.uniform(0.2, 4.0)
        return {
            'distance': distance,
            'presence': 0,                 # 超声波不检测人体
            'angle': self.angle,
            'sensor_id': self.sensor_id,
            'valid': True,
            'timestamp': time.time()
        }

class SimulatedSensorHub:
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
        results = []
        for r in self.radars:
            results.append(r.read_data())
        for u in self.ultrasonics:
            results.append(u.read_data())
        return results
