import time
import pygame
import os
from simulated_sensors import SimulatedSensorHub
# try to use real C4002 driver if available; set RADAR_PORTS env var to comma-separated ports (e.g. /dev/ttyUSB0,/dev/ttyUSB1,/dev/ttyUSB2)
try:
    from c4002_parser import RealSensorHub
    ports_env = os.getenv('RADAR_PORTS')
    if ports_env:
        ports = [p.strip() for p in ports_env.split(',') if p.strip()]
        sensor_hub = RealSensorHub(ports=ports)
    else:
        # default to common USB ports for 3 sensors if running on Linux/embedded (user confirmed /dev/ttyUSB0-2)
        try:
            if os.name != 'nt':
                fallback_ports = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2']
                sensor_hub = RealSensorHub(ports=fallback_ports)
            else:
                sensor_hub = SimulatedSensorHub()
        except Exception:
            sensor_hub = SimulatedSensorHub()
except Exception:
    sensor_hub = SimulatedSensorHub()
from data_fusion import DataFusion
from stereo_ar_display import StereoARDisplay

# 人体静止扫描参数（均可通过环境变量覆盖）
SCAN_DURATION = float(os.getenv('C4002_SCAN_DURATION', '2.0'))       # 扫描时长 s
SCAN_BREATH_MIN = int(os.getenv('C4002_SCAN_BREATH_MIN', '2'))       # 单雷达呼吸证据命中帧数阈值
SCAN_MOTION_MIN = int(os.getenv('C4002_SCAN_MOTION_MIN', '3'))       # 单雷达运动证据命中帧数阈值


def _robust_distance(dists):
    """IQR 离群剔除后取中位数，作为一次扫描的单点距离估计。

    场景：静止扫描约 2 秒，读入一二十帧距离，大部分接近真值、少量因多径跳变。
    中位数绝对稳健、IQR 自动适应数据分布，无需人工设阈值，专治"大部分准、少量离群"。
    """
    if not dists:
        return 0.0
    sd = sorted(dists)
    n = len(sd)

    def quantile(p):
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return sd[lo] * (1.0 - frac) + sd[hi] * frac

    q1 = quantile(0.25)
    q3 = quantile(0.75)
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    kept = [d for d in sd if lo <= d <= hi]
    if not kept:            # 极端情况全部被剔除，退回原始样本
        kept = sd
    return kept[len(kept) // 2]

def main():
    # reuse sensor_hub created at module import time (RealSensorHub or SimulatedSensorHub)
    fusion = DataFusion()
    display = StereoARDisplay(1920, 1080)

    running = True
    clock = pygame.time.Clock()

    # 人体扫描状态（逐雷达）
    scan_active = False
    scan_start = 0.0
    # key: angle -> {'breath': 呼吸证据帧数, 'motion': 运动证据帧数, 'dists': [有效距离列表]}
    scan_stats = {}
    scan_results = []   # 固定结果：[{'angle', 'detected', 'distance'}]

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_v:
                    display.view_mode = "top" if display.view_mode == "stereo" else "stereo"
                elif event.key == pygame.K_SPACE:
                    # 开始一次"静止扫描"：清空各雷达检测计数器，保证扫描窗口干净
                    for r in sensor_hub.radars:
                        if hasattr(r, 'reset_detection'):
                            r.reset_detection()
                    scan_active = True
                    scan_start = time.time()
                    scan_stats = {}
                    scan_results = []

        # 获取雷达与超声波数据：
        # C4002 现在只用于"人体静止扫描"，因此仅在扫描窗口内才读取串口；
        # 障碍物距离完全交给超声波（radar_data 传空，融合层只走超声波路径）。
        if scan_active:
            radar_data = [r.read_data() for r in sensor_hub.radars]
        else:
            radar_data = []
        ultrasonic_data = [u.read_data() for u in sensor_hub.ultrasonics]

        # 扫描期间逐雷达累计证据：呼吸证据（单帧）+ 运动证据（单帧），并收集有效距离
        if scan_active:
            for d in radar_data:
                if not d or not d.get('valid'):
                    continue
                ang = d.get('angle', 0)
                st = scan_stats.setdefault(ang, {'breath': 0, 'motion': 0, 'dists': []})
                # 单帧证据，滞后统一在扫描结束时按"命中帧数"判定，避免双层滞后
                if d.get('breath_evidence') == 1:
                    st['breath'] += 1
                if d.get('motion') == 1:
                    st['motion'] += 1
                dist = d.get('distance')
                if dist and dist > 0:
                    st['dists'].append(dist)

            if time.time() - scan_start >= SCAN_DURATION:
                scan_active = False
                # 每个雷达输出一个固定结果：呼吸或运动任一达标即判"有人"
                for ang in sorted(scan_stats.keys()):
                    st = scan_stats[ang]
                    detected = st['breath'] >= SCAN_BREATH_MIN or st['motion'] >= SCAN_MOTION_MIN
                    scan_results.append({
                        'angle': ang,
                        'detected': detected,
                        'distance': _robust_distance(st['dists'])
                    })

        # 更新扫描状态提示
        if scan_active:
            remain = max(0.0, SCAN_DURATION - (time.time() - scan_start))
            display.set_scan_status(f"扫描中... {remain:.1f}s 请保持静止", (0, 200, 255))
        elif scan_results:
            n = sum(1 for r in scan_results if r['detected'])
            if n > 0:
                display.set_scan_status(f"检测到 {n} 个方向有幸存者", (0, 255, 80))
            else:
                display.set_scan_status("未检测到幸存者", (255, 90, 90))
        else:
            display.set_scan_status(None)

        # 把固定扫描结果传给显示层（直到下次扫描前保持不变）
        display.set_scan_results(scan_results)

        # 融合：返回障碍物（人体检测已改为手动扫描 + 固定显示，不再走实时融合）
        obstacles_raw, _ = fusion.fuse_measurements(radar_data, ultrasonic_data)
        # 时间滤波：仅障碍物
        obstacles, _ = fusion.temporal_filter(obstacles_raw, [])

        # 显示：障碍物 + 固定扫描人体图标
        display.draw_obstacles(obstacles)

        clock.tick(20)

    pygame.quit()

if __name__ == "__main__":
    main()
