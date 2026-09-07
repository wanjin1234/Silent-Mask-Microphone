import time
import pygame
import os
import threading
from simulated_sensors import SimulatedSensorHub
# try to use real C4002 driver if available; set RADAR_PORTS env var to comma-separated ports (e.g. /dev/ttyUSB0,/dev/ttyUSB1,/dev/ttyUSB2)
try:
    from c4002_parser import RealSensorHub
    ports_env = os.getenv('RADAR_PORTS')
    # RADAR_ANGLES：显式指定每个端口对应的角度（逗号分隔），顺序与 RADAR_PORTS 一致。
    # 例如 RADAR_PORTS=/dev/serial/by-path/p1,/dev/serial/by-path/p2,/dev/serial/by-path/p3
    #     RADAR_ANGLES=-45,0,45
    angles_env = os.getenv('RADAR_ANGLES')
    angles = None
    if angles_env:
        try:
            angles = [float(a.strip()) for a in angles_env.split(',') if a.strip()]
        except Exception:
            angles = None
    if ports_env:
        ports = [p.strip() for p in ports_env.split(',') if p.strip()]
        sensor_hub = RealSensorHub(ports=ports, angles=angles)
    else:
        # default to common USB ports for 3 sensors if running on Linux/embedded (user confirmed /dev/ttyUSB0-2)
        try:
            if os.name != 'nt':
                fallback_ports = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2']
                sensor_hub = RealSensorHub(ports=fallback_ports, angles=angles)
            else:
                sensor_hub = SimulatedSensorHub()
        except Exception:
            sensor_hub = SimulatedSensorHub()
except Exception:
    sensor_hub = SimulatedSensorHub()
from data_fusion import DataFusion
from stereo_ar_display import StereoARDisplay
try:
    from gpio_button import GpioButton
except Exception:
    GpioButton = None

# 人体静止扫描参数（均可通过环境变量覆盖）
SCAN_DURATION = float(os.getenv('C4002_SCAN_DURATION', '2.0'))       # 扫描时长 s
SCAN_BREATH_MIN = int(os.getenv('C4002_SCAN_BREATH_MIN', '2'))       # 单雷达呼吸证据命中帧数阈值
SCAN_MOTION_MIN = int(os.getenv('C4002_SCAN_MOTION_MIN', '2'))       # 单雷达运动证据命中帧数阈值


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

def sensor_worker(lock, state, scan_trigger, stop_event):
    """后台采集线程：串口/超声波读取、扫描证据聚合、融合均在此执行。

    超声波 measure() 每次最多阻塞 50~80ms、三路串行可达 150ms+，若放在渲染
    主循环会导致帧率骤降、扫描弧动画"跳格"。移入独立线程后，渲染主循环只做
    读取共享状态 + 绘制，稳定跑满 60fps。
    """
    fusion = DataFusion()
    SENSOR_HZ = float(os.getenv('C4002_SENSOR_HZ', '20'))
    SENSOR_INTERVAL = 1.0 / SENSOR_HZ

    scan_active = False
    scan_start = 0.0
    scan_stats = {}
    scan_results = []

    last_t = 0.0
    while not stop_event.is_set():
        # 处理扫描触发请求（来自主线程 SPACE / GPIO 按钮）
        if scan_trigger.is_set():
            scan_trigger.clear()
            for r in sensor_hub.radars:
                if hasattr(r, 'reset_detection'):
                    r.reset_detection()
            scan_active = True
            scan_start = time.time()
            scan_stats = {}
            scan_results = []

        now = time.time()
        if now - last_t < SENSOR_INTERVAL:
            time.sleep(0.001)
            continue
        last_t = now

        # 获取雷达与超声波数据
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

        # 融合：返回障碍物（人体检测已改为手动扫描 + 固定显示，不再走实时融合）
        obstacles_raw, _ = fusion.fuse_measurements(radar_data, ultrasonic_data)
        # 时间滤波：仅障碍物
        obstacles, _ = fusion.temporal_filter(obstacles_raw, [])

        # 发布共享状态（供渲染主循环非阻塞读取）
        with lock:
            state['obstacles'] = obstacles
            state['scan_results'] = list(scan_results)
            state['scan_active'] = scan_active
            state['scan_start_time'] = scan_start


def main():
    # reuse sensor_hub created at module import time (RealSensorHub or SimulatedSensorHub)
    display = StereoARDisplay(1920, 1080)

    running = True
    clock = pygame.time.Clock()

    # 共享状态 + 后台采集线程
    lock = threading.Lock()
    state = {
        'obstacles': [],
        'scan_results': [],
        'scan_active': False,
        'scan_start_time': 0.0,
    }
    scan_trigger = threading.Event()
    stop_event = threading.Event()
    worker = threading.Thread(
        target=sensor_worker, args=(lock, state, scan_trigger, stop_event), daemon=True)
    worker.start()

    # FPS 统计：每 0.5s 更新一次实测帧率，并绘制到画面左上角（设 C4002_SHOW_FPS=0 关闭）
    show_fps = os.getenv('C4002_SHOW_FPS', '1') != '0'
    fps_frames = 0
    fps_t0 = time.time()
    fps_current = 0.0

    # 空闲 GPIO 按钮：按一次触发一次人体存在扫描
    button = None
    if GpioButton is not None:
        try:
            candidate = GpioButton(gpio=os.getenv('BUTTON_GPIO'))
            if candidate.enabled:
                button = candidate
        except Exception:
            button = None

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
                    scan_trigger.set()

        # GPIO 按钮触发扫描
        if button is not None and button.poll():
            scan_trigger.set()

        # 读取最新共享状态（非阻塞）
        with lock:
            obstacles = state['obstacles']
            scan_results = state['scan_results']
            scan_active = state['scan_active']
            scan_start_time = state['scan_start_time']

        # 把扫描进行状态/起始时间传给显示层（触发扫描推进弧动画）
        display.set_scan_active(scan_active, scan_start_time if scan_active else None)
        display.set_scan_results(scan_results)

        # FPS 统计：每 0.5s 重新计算一次实测帧率
        if show_fps:
            fps_frames += 1
            now_f = time.time()
            if now_f - fps_t0 >= 0.5:
                fps_current = fps_frames / (now_f - fps_t0)
                fps_frames = 0
                fps_t0 = now_f

        # 显示：障碍物 + 固定扫描人体图标（fps 叠加到画面左上角）
        display.draw_obstacles(obstacles, fps_current if show_fps else None)

        clock.tick(30)

    stop_event.set()
    # 等待后台采集线程退出：雷达/超声波一次测距最多阻塞几十~上百毫秒，
    # 留足时间确保线程在解释器退出前结束，避免守护线程在 shutdown 阶段
    # 还持有 stderr 缓冲锁导致 "could not acquire lock for stderr" 致命错误。
    worker.join(timeout=5.0)
    pygame.quit()

if __name__ == "__main__":
    main()
