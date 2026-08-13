import time
import pygame
from simulated_sensors import SimulatedSensorHub
from data_fusion import DataFusion
from stereo_ar_display import StereoARDisplay

def main():
    # 初始化模拟传感器
    sensor_hub = SimulatedSensorHub()
    # 初始化数据融合
    fusion = DataFusion()
    # 初始化立体显示
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
                    # 切换视图模式
                    if display.view_mode == "stereo":
                        display.view_mode = "top"
                    else:
                        display.view_mode = "stereo"

        # 获取模拟传感器数据
        radar_data = [r.read_data() for r in sensor_hub.radars]
        ultrasonic_data = [u.read_data() for u in sensor_hub.ultrasonics]

        # 融合数据（输出世界坐标）
        fused = fusion.fuse_measurements(radar_data, ultrasonic_data)
        # 时间滤波
        obstacles = fusion.temporal_filter(fused)

        # 绘制
        display.draw_obstacles(obstacles)

        clock.tick(10)  # 10 FPS

    pygame.quit()

if __name__ == "__main__":
    main()
