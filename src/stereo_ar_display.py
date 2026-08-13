import pygame
import math

class StereoARDisplay:
    def __init__(self, width=1920, height=1080, ipd_cm=6.5):
        pygame.init()
        self.screen = pygame.display.set_mode((width, height))
        pygame.display.set_caption("火场AR调试 - 按V切换俯视/立体")
        self.width = width
        self.height = height
        self.half_width = width // 2
        self.center_y = height // 2
        self.focal_length = 700          # 像素焦距，影响视野范围
        self.ipd = ipd_cm / 100.0        # 瞳距（米）

        # 左右眼屏幕中心（SBS分屏）
        self.center_x_left = self.half_width // 2
        self.center_x_right = self.half_width + self.half_width // 2

        self.view_mode = "stereo"        # 当前视图模式："stereo"或"top"

        # 颜色定义
        self.colors = {
            'bg': (20, 20, 30),          # 立体模式背景（模拟黑暗/浓烟）
            'grid': (60, 60, 70),        # 网格线颜色
            'near': (255, 50, 50, 180),  # 近处障碍：红色半透明
            'mid': (255, 200, 50, 180),  # 中距离：黄色半透明
            'far': (50, 255, 50, 180),   # 远处：绿色半透明
            'text': (255, 255, 255),     # 文字白色
            'sector': (0, 200, 255, 30)  # 扇形区域半透明填充（俯视图）
        }

    # ---------- 投影函数：将世界坐标映射到指定眼睛的屏幕坐标 ----------
    def project_point(self, x, y, z, eye):
        """
        透视投影：近大远小
        x: 横向偏移（右正左负）
        y: 高度（向上正）
        z: 前方距离（必须>0.1）
        eye: 'left' 或 'right'
        """
        if eye == 'left':
            cam_x = -self.ipd / 2
            center_x = self.center_x_left
        else:
            cam_x = self.ipd / 2
            center_x = self.center_x_right

        # 相对相机坐标
        x_rel = x - cam_x
        z_rel = z
        if z_rel <= 0.1:   # 太近不显示（避免除零）
            return None

        # 透视投影公式
        screen_x = center_x + (x_rel * self.focal_length / z_rel)
        screen_y = self.center_y - (y * self.focal_length / z_rel)

        # 边界检查
        if 0 <= screen_x < self.width and 0 <= screen_y < self.height:
            return (int(screen_x), int(screen_y))
        return None

    # ---------- 立体分屏模式绘制（模拟AR眼镜第一人称） ----------
    def draw_stereo(self, obstacles):
        """绘制立体分屏视图，障碍物用半透明色块叠加"""
        # 背景填充为深色，模拟浓烟环境
        self.screen.fill(self.colors['bg'])

        # 绘制参考网格（地面距离线）
        self._draw_ground_grid_stereo()

        # 绘制障碍物：使用半透明色块（模拟AR叠加效果）
        for obs in obstacles:
            x, y, z = obs['x'], obs['y'], obs['z']
            dist = obs['distance']

            # 根据距离选择颜色和大小
            if dist < 1.0:
                color = self.colors['near']    # 红色半透明
                size = 40                      # 大尺寸表示危险
            elif dist < 2.0:
                color = self.colors['mid']     # 黄色半透明
                size = 30
            else:
                color = self.colors['far']     # 绿色半透明
                size = 20

            # 创建半透明表面
            alpha_surface = pygame.Surface((size, size), pygame.SRCALPHA)
            # 在表面中央绘制半透明圆形（或矩形，这里用圆形代表障碍物）
            pygame.draw.circle(alpha_surface, color, (size//2, size//2), size//2)
            # 可选：绘制边框增加可见性
            pygame.draw.circle(alpha_surface, (255,255,255,255), (size//2, size//2), size//2, 2)

            # 将半透明表面投射到左右眼
            left_pos = self.project_point(x, y, z, 'left')
            right_pos = self.project_point(x, y, z, 'right')
            if left_pos:
                # 将表面左上角定位到投影点（使中心对齐障碍物位置）
                self.screen.blit(alpha_surface, (left_pos[0]-size//2, left_pos[1]-size//2))
            if right_pos:
                self.screen.blit(alpha_surface, (right_pos[0]-size//2, right_pos[1]-size//2))

            # 显示距离文字
            if left_pos:
                self._draw_distance_text(left_pos, dist, size)
            if right_pos:
                self._draw_distance_text(right_pos, dist, size)

        self._draw_mode_hint()

    def _draw_ground_grid_stereo(self):
        """在左右眼视图中绘制地面距离参考线"""
        for eye in ['left', 'right']:
            if eye == 'left':
                center_x = self.center_x_left
            else:
                center_x = self.center_x_right

            # 绘制水平线，表示不同距离的地面位置（简化）
            for d in [1, 2, 3, 4, 5]:
                z = d
                # 地面高度y=0，投影到屏幕
                screen_y = self.center_y - (0 * self.focal_length / z)  # y=0时屏幕y=center_y
                # 画一条水平线，表示该距离的地面参考
                pygame.draw.line(self.screen, self.colors['grid'],
                                 (center_x - 200, screen_y),
                                 (center_x + 200, screen_y), 1)
                font = pygame.font.Font(None, 20)
                text = font.render(f"{d}m", True, self.colors['text'])
                self.screen.blit(text, (center_x + 210, screen_y - 10))

    def _draw_distance_text(self, pos, dist, size):
        """在障碍物旁边显示距离"""
        font = pygame.font.Font(None, 24)
        text = font.render(f"{dist:.1f}m", True, (255, 255, 255))
        self.screen.blit(text, (pos[0] + size//2 + 5, pos[1] - 10))

    # ---------- 俯视图模式绘制（用于调试） ----------
    def draw_top_view(self, obstacles):
        """绘制俯视雷达图：中心为头部，上方为前方，障碍物用彩色圆点表示"""
        self.screen.fill((15, 15, 25))   # 深色背景
        cx = self.width // 2
        cy = self.height - 100           # 头部位置在屏幕下方
        scale = 80                       # 每米像素数

        # 1. 扇形探测区域（半透明）
        sector_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        sector_points = [(cx, cy)]
        for angle in range(-60, 61, 5):  # 覆盖-60°到60°
            rad = math.radians(angle)
            end_x = cx + 500 * math.sin(rad)
            end_y = cy - 500 * math.cos(rad)
            sector_points.append((end_x, end_y))
        sector_points.append((cx, cy))
        pygame.draw.polygon(sector_surface, self.colors['sector'], sector_points)
        self.screen.blit(sector_surface, (0, 0))

        # 2. 距离环（1m,2m,...）
        for r in range(1, 6):
            radius = r * scale
            pygame.draw.circle(self.screen, (60,60,70), (cx, cy), radius, 1)
            font = pygame.font.Font(None, 22)
            text = font.render(f"{r}m", True, (200,200,200))
            self.screen.blit(text, (cx + 5, cy - radius - 18))

        # 3. 角度参考线（-45°, 0°, 45°）
        for angle in [-45, 0, 45]:
            rad = math.radians(angle)
            end_x = cx + 500 * math.sin(rad)
            end_y = cy - 500 * math.cos(rad)
            pygame.draw.line(self.screen, (80,80,90), (cx, cy), (end_x, end_y), 1)
            label_x = cx + 550 * math.sin(rad)
            label_y = cy - 550 * math.cos(rad)
            font = pygame.font.Font(None, 22)
            text = font.render(f"{angle}°", True, (150,150,160))
            self.screen.blit(text, (label_x - 10, label_y - 10))

        # 4. 障碍物绘制
        for obs in obstacles:
            x = obs['x']
            z = obs['z']   # z为前方距离
            screen_x = cx + int(x * scale)
            screen_y = cy - int(z * scale)

            dist = obs['distance']
            # 颜色编码：红<1m，黄1-2m，绿>2m
            if dist < 1.0:
                color = (255, 60, 60)
                radius = 10
            elif dist < 2.0:
                color = (255, 200, 50)
                radius = 7
            else:
                color = (50, 255, 50)
                radius = 5

            # 圆点 + 白色描边
            pygame.draw.circle(self.screen, color, (screen_x, screen_y), radius)
            pygame.draw.circle(self.screen, (255,255,255), (screen_x, screen_y), radius, 1)

            # 距离标注
            font = pygame.font.Font(None, 24)
            text = font.render(f"{dist:.1f}m", True, (255,255,255))
            self.screen.blit(text, (screen_x + 12, screen_y - 12))

        # 5. 头部位置标识（中心点+方向箭头）
        pygame.draw.circle(self.screen, (0,200,255), (cx, cy), 8)
        pygame.draw.line(self.screen, (0,200,255), (cx, cy), (cx, cy-30), 3)
        pygame.draw.polygon(self.screen, (0,200,255), [(cx-5, cy-25), (cx+5, cy-25), (cx, cy-35)])

        self._draw_mode_hint()

    def _draw_mode_hint(self):
        """显示模式提示和操作说明"""
        font = pygame.font.Font(None, 30)
        mode_text = "俯视图模式" if self.view_mode == "top" else "立体分屏模式"
        hint = f"{mode_text}  按V切换  ESC退出"
        text = font.render(hint, True, self.colors['text'])
        self.screen.blit(text, (20, 20))

    def draw_obstacles(self, obstacles):
        """根据当前模式绘制"""
        if self.view_mode == "stereo":
            self.draw_stereo(obstacles)
        else:
            self.draw_top_view(obstacles)
        pygame.display.flip()
