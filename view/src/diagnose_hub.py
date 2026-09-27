"""诊断 main_stereo.py 里超声波数据为什么没显示。

模拟 main 的传感器初始化流程，打印关键信息，定位数据断在哪一环。
"""
import os
import time


def main():
    print("=== 1. 依赖检查 ===")
    try:
        import serial
        print(f"  pyserial 可用 (version {serial.__version__})")
    except Exception as e:
        print(f"  pyserial 不可用: {e}")
    try:
        import pyftdi
        print(f"  pyftdi 可用")
    except Exception as e:
        print(f"  pyftdi 不可用: {e}")

    print("\n=== 2. 传感器 hub 初始化（复现 main 逻辑） ===")
    sensor_hub = None
    hub_type = None
    try:
        from c4002_parser import RealSensorHub
        ports_env = os.getenv('RADAR_PORTS')
        if ports_env:
            ports = [p.strip() for p in ports_env.split(',') if p.strip()]
            sensor_hub = RealSensorHub(ports=ports)
        else:
            if os.name != 'nt':
                sensor_hub = RealSensorHub(ports=['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2'])
            else:
                from simulated_sensors import SimulatedSensorHub
                sensor_hub = SimulatedSensorHub()
        hub_type = type(sensor_hub).__name__
    except Exception as e:
        print(f"  RealSensorHub 导入/初始化失败: {e}")
        from simulated_sensors import SimulatedSensorHub
        sensor_hub = SimulatedSensorHub()
        hub_type = type(sensor_hub).__name__

    print(f"  sensor_hub 类型 = {hub_type}")
    print(f"  超声波传感器数量 = {len(sensor_hub.ultrasonics)}")
    for i, u in enumerate(sensor_hub.ultrasonics):
        print(f"    [{i}] {type(u).__name__}")

    if not sensor_hub.ultrasonics:
        print("\n  !!! ultrasonics 为空 -> 超声波数据永远不会出现")
        print("  可能原因：FT232H 未连接 / pyftdi 缺失 / 初始化时抛异常")
        return

    print("\n=== 3. 连续读取 5 帧超声波数据 ===")
    for frame in range(5):
        rows = []
        for u in sensor_hub.ultrasonics:
            try:
                rows.append(u.read_data())
            except Exception as e:
                rows.append({'angle': '?', 'error': str(e)})
        print(f"  帧{frame}:")
        for r in rows:
            if 'error' in r:
                print(f"    错误: {r['error']}")
            else:
                ang = r.get('angle')
                dist = r.get('distance')
                valid = r.get('valid')
                print(f"    angle={ang:+d}°  distance={dist if dist is not None else None}  valid={valid}")
        time.sleep(0.2)

    print("\n=== 4. 结论判断 ===")
    any_valid = any(
        r.get('valid') for r in [u.read_data() for u in sensor_hub.ultrasonics]
    )
    if hub_type != 'RealSensorHub':
        print("  hub 不是 RealSensorHub -> 用的是模拟数据，真实超声波未接入")
        print("  检查：pyserial 是否已装 (pip install pyserial)")
    elif not any_valid:
        print("  RealSensorHub 但超声波都 valid=False -> 无回波或超量程")
        print("  检查：FT232H 是否插好、障碍物是否在 20-600cm 且正对探头")
    else:
        print("  超声波数据有效 -> 问题在显示层/融合层，不在传感器")
        print("  检查：障碍物是否 >2.5m（显示层会过滤 >2.5m 的距离条）")


if __name__ == "__main__":
    main()
