import time
import pygame
from simulated_sensors import SimulatedSensorHub
from data_fusion import DataFusion
from stereo_ar_display import StereoARDisplay

def main():
    sensor_hub = SimulatedSensorHub()
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

        # 获取模拟数据
        radar_data = [r.read_data() for r in sensor_hub.radars]
        ultrasonic_data = [u.read_data() for u in sensor_hub.ultrasonics]

        # 融合：返回障碍物和人体
        obstacles_raw, humans_raw = fusion.fuse_measurements(radar_data, ultrasonic_data)
        # 时间滤波：需要同时传入障碍物和人体
        obstacles, humans = fusion.temporal_filter(obstacles_raw, humans_raw)

        # 显示：需要同时传入障碍物和人体
        display.draw_obstacles(obstacles, humans)

        clock.tick(10)

    pygame.quit()

if __name__ == "__main__":
    main()
