import time
import pygame
from real_sensors import RealSensorHub
from data_fusion import DataFusion
from stereo_ar_display import StereoARDisplay

def main():
    # 使用真实传感器（雷达端口根据实际调整）
    sensor_hub = RealSensorHub(
        radar_ports=['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2'],
        radar_angles=[-45, 0, 45],
        debug=True
    )
    fusion = DataFusion()
    display = StereoARDisplay(1920, 1080)

    running = True
    clock = pygame.time.Clock()

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_v:
                    display.view_mode = "top" if display.view_mode == "stereo" else "stereo"

        # 读取雷达数据
        radar_data = sensor_hub.read_radars()
        ultrasonic_data = []   # 暂时禁用超声波

        # 融合雷达和空超声波数据
        obstacles_raw, humans_raw = fusion.fuse_measurements(radar_data, ultrasonic_data)
        obstacles, humans = fusion.temporal_filter(obstacles_raw, humans_raw)

        # 显示
        display.draw_obstacles(obstacles, humans)

        clock.tick(10)   # 10 FPS

    sensor_hub.close()
    pygame.quit()

if __name__ == "__main__":
    main()
