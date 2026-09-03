import pygame
import math
import time

class StereoARDisplay:
    def __init__(self, width=1920, height=1080, ipd_cm=6.5):
        pygame.init()
        self.screen = pygame.display.set_mode((width, height))
        pygame.display.set_caption("火场AR调试 - 按V切换俯视/立体")
        self.width = width
        self.height = height
        self.half_width = width // 2
        self.center_y = height // 2
        self.focal_length = 700
        self.ipd = ipd_cm / 100.0

        self.center_x_left = self.half_width // 2
        self.center_x_right = self.half_width + self.half_width // 2

        self.view_mode = "stereo"

        # 扫描状态提示（人体静止扫描）
        self.scan_status = None
        self.scan_status_color = (0, 200, 255)
        self.scan_results = []   # 固定扫描结果：[{'angle', 'detected', 'distance'}]

        # 颜色定义
        self.colors = {
            'bg': (20, 20, 30),
            'grid': (60, 60, 70),
            'sector': (0, 200, 255, 30),
            'text': (255, 255, 255),
            # 新增 HUD 浅蓝色
            'hud_blue': (0, 200, 255),
        }

        # 2.5D 伪立体 HUD 参数（可调）
        self.arc_half = 430        # HUD 带从中心向两侧展开的半宽(px)
        self.arc_bow = 45          # 弧线弯曲幅度(px)，越大弧越弯
        self.tick_base_y = 60      # 上方刻度基线 y（中心处）
        self.icon_base_y = 95      # 人体图标基线 y（中心处）
        self.bar_base_y = 1020     # 下方距离条基线 y（中心处）
        self.bar_half_h = 15       # 距离条半高(px)
        self.divider_len = 130     # 分割线向纵深延伸的长度(px)
        self.divider_color = (0, 140, 200)  # 分割线颜色（略暗，作为纵深元素）
        self.depth_fade = 0.5      # 深度衰减强度(0~1)：越大，边缘(远处)越暗越细

    # ---------- 投影函数（保留） ----------
    def project_point(self, x, y, z, eye):
        if eye == 'left':
            cam_x = -self.ipd / 2
            center_x = self.center_x_left
        else:
            cam_x = self.ipd / 2
            center_x = self.center_x_right
        x_rel = x - cam_x
        z_rel = z
        if z_rel <= 0.1:
            return None
        screen_x = center_x + (x_rel * self.focal_length / z_rel)
        screen_y = self.center_y - (y * self.focal_length / z_rel)
        if 0 <= screen_x < self.width and 0 <= screen_y < self.height:
            return (int(screen_x), int(screen_y))
        return None

    # ---------- 立体分屏模式（新UI） ----------
    def draw_stereo(self, obstacles):
        """
        立体分屏模式：
        - 背景纯黑（透明）
        - 中心浅蓝色小十字（左右各一）
        - 上方等距刻度线（浅蓝色竖线，无数字）
        - 刻度线下方：人体信号图标（感叹号+黄色三角形+浅蓝色距离）
        - 下方三个矩形条（普通障碍物距离），红/黄/隐藏，条内距离数字白色
        """
        # 1. 纯黑背景（AR透明）
        self.screen.fill((0, 0, 0))

        # 2. 解析障碍物数据（人体不再来自实时数据，只来自 scan_results 固定结果）
        left_dist = center_dist = right_dist = None

        for obs in obstacles:
            if 'angle' in obs and 'distance' in obs:
                ang = obs['angle']
                dist = obs['distance']
            else:
                x = obs.get('x', 0)
                z = obs.get('z', 1)
                if z <= 0.1:
                    continue
                ang = math.degrees(math.atan2(x, z))
                dist = obs.get('distance', math.sqrt(x*x + z*z))

            direction = None
            if -50 <= ang <= -40:
                direction = 'left'
            elif 40 <= ang <= 50:
                direction = 'right'
            elif -10 <= ang <= 10:
                direction = 'center'
            else:
                continue

            if direction == 'left':
                left_dist = dist
            elif direction == 'center':
                center_dist = dist
            elif direction == 'right':
                right_dist = dist

        # 人体图标完全由固定扫描结果驱动（保持到下次扫描）
        human_left = human_center = human_right = None
        for r in self.scan_results:
            if not r.get('detected'):
                continue
            ang = r.get('angle', 0)
            dist = r.get('distance', 0.0)
            if -50 <= ang <= -40:
                human_left = dist
            elif -10 <= ang <= 10:
                human_center = dist
            elif 40 <= ang <= 50:
                human_right = dist

        # 3. 中心浅蓝色小十字
        cross_size = 12
        cross_gap = 4
        hud_blue = self.colors['hud_blue']

        def fade(color, f):
            # f: 0=全黑(远景) ~ 1=原始色(近景)
            if f >= 1.0:
                return color
            if f <= 0.0:
                return (0, 0, 0)
            return (int(color[0] * f), int(color[1] * f), int(color[2] * f))
        for center_x in [self.center_x_left, self.center_x_right]:
            cx = center_x
            cy = self.center_y
            pygame.draw.line(self.screen, hud_blue, (cx - cross_size, cy), (cx - cross_gap, cy), 2)
            pygame.draw.line(self.screen, hud_blue, (cx + cross_gap, cy), (cx + cross_size, cy), 2)
            pygame.draw.line(self.screen, hud_blue, (cx, cy - cross_size), (cx, cy - cross_gap), 2)
            pygame.draw.line(self.screen, hud_blue, (cx, cy + cross_gap), (cx, cy + cross_size), 2)

        # 4. 上方等距刻度线（浅蓝色，沿纵深方向弧形排布）
        num_ticks = 7
        tick_len = 20
        arc_half = self.arc_half
        arc_bow = self.arc_bow

        for center_x in [self.center_x_left, self.center_x_right]:
            for i in range(num_ticks):
                t = -1.0 + 2.0 * i / (num_ticks - 1)
                x_pos = center_x + t * arc_half
                y_top = self.tick_base_y - arc_bow * t * t
                f = 1.0 - self.depth_fade * t * t
                tick_w = 2 if f > 0.7 else 1
                pygame.draw.line(self.screen, fade(hud_blue, f),
                                 (x_pos, y_top),
                                 (x_pos, y_top + tick_len), tick_w)

        # 5. 人体信号图标（沿弧线排布，对齐三个方向）
        icon_size = 30

        def draw_human_icon(surface, x, y, size, distance):
            half = size // 2
            # 黄色三角形
            points = [(x, y - half), (x - half, y + half//2), (x + half, y + half//2)]
            pygame.draw.polygon(surface, (255, 255, 0), points)
            pygame.draw.polygon(surface, (200, 200, 0), points, 2)

            # 感叹号 "!" 改为浅蓝色
            font = pygame.font.Font(None, size)
            exclaim = font.render("!", True, hud_blue)
            text_rect = exclaim.get_rect(center=(x, y))
            surface.blit(exclaim, text_rect)

            # 距离数值改为浅蓝色
            font_dist = pygame.font.Font(None, 20)
            dist_text = font_dist.render(f"{distance:.1f}m", True, hud_blue)
            dist_rect = dist_text.get_rect(center=(x, y + half + 15))
            surface.blit(dist_text, dist_rect)

        icon_t_centers = [-2.0 / 3.0, 0.0, 2.0 / 3.0]
        human_dists = [human_left, human_center, human_right]
        for center_x in [self.center_x_left, self.center_x_right]:
            for idx, dist in enumerate(human_dists):
                if dist is not None and dist <= 5.0:
                    t = icon_t_centers[idx]
                    x = center_x + t * arc_half
                    y = self.icon_base_y - arc_bow * t * t
                    draw_human_icon(self.screen, x, y, icon_size, dist)

        # 6. 分割线（向纵深远方延伸）+ 下方弧形距离条
        color_near = (255, 50, 50)
        color_mid = (255, 200, 50)
        dirs = [('L', left_dist), ('C', center_dist), ('R', right_dist)]
        bar_t_centers = [-2.0 / 3.0, 0.0, 2.0 / 3.0]
        bar_half_w = 0.28

        for center_x in [self.center_x_left, self.center_x_right]:
            # 6a. 三个方向条之间的分割线，向纵深（画面中心/消失点）延伸
            for gap_t in (-1.0 / 3.0, 1.0 / 3.0):
                sx0 = center_x + gap_t * arc_half
                sy0 = self.bar_base_y + arc_bow * gap_t * gap_t
                dx = center_x - sx0
                dy = self.center_y - sy0
                length = math.hypot(dx, dy)
                if length == 0:
                    continue
                ux, uy = dx / length, dy / length
                sx1 = sx0 + ux * self.divider_len
                sy1 = sy0 + uy * self.divider_len
                # 向远端渐暗：分段绘制，越远颜色越淡
                seg_n = 8
                for k in range(seg_n):
                    p0 = k / seg_n
                    p1 = (k + 1) / seg_n
                    f0 = 1.0 - self.depth_fade * p0
                    ax = sx0 + (sx1 - sx0) * p0
                    ay = sy0 + (sy1 - sy0) * p0
                    bx = sx0 + (sx1 - sx0) * p1
                    by = sy0 + (sy1 - sy0) * p1
                    pygame.draw.line(self.screen, fade(self.divider_color, f0),
                                     (ax, ay), (bx, by), 2)

            # 6b. 弧形距离条（红/黄），条内距离数字
            for idx, (label, dist) in enumerate(dirs):
                if dist is None or dist > 6.0:
                    continue
                tc = bar_t_centers[idx]
                base_color = color_near if dist <= 1.2 else color_mid
                fill_color = fade(base_color, 1.0 - self.depth_fade * tc * tc)
                pts_top = []
                pts_bot = []
                seg = 12
                for j in range(seg + 1):
                    t = tc + bar_half_w * (-1.0 + 2.0 * j / seg)
                    sx = center_x + t * arc_half
                    yc = self.bar_base_y + arc_bow * t * t
                    pts_top.append((sx, yc - self.bar_half_h))
                    pts_bot.append((sx, yc + self.bar_half_h))
                poly = pts_top + pts_bot[::-1]
                pygame.draw.polygon(self.screen, fill_color, poly)
                pygame.draw.polygon(self.screen, (220, 220, 220), poly, 1)

                # 条内距离文字（蓝色，红黄底上清晰）
                font = pygame.font.Font(None, 28)
                text_str = f"{dist:.1f}m"
                text_surf = font.render(text_str, True, self.colors['hud_blue'])
                shadow_surf = font.render(text_str, True, (0, 0, 0))
                text_rect = text_surf.get_rect(
                    center=(center_x + tc * arc_half,
                            self.bar_base_y + arc_bow * tc * tc))
                self.screen.blit(shadow_surf, (text_rect.x + 2, text_rect.y + 2))
                self.screen.blit(text_surf, text_rect)



    # ---------- 俯视图模式（保持不变） ----------
    def draw_top_view(self, obstacles):
        """俯视图模式：左右分屏显示，无立体视差"""
        self.screen.fill((10, 10, 18))

        # 左半屏俯视图
        self._draw_top_view_at(obstacles, self.center_x_left, self.height - 100)
        # 右半屏俯视图（内容完全相同）
        self._draw_top_view_at(obstacles, self.center_x_right, self.height - 100)

        
        pygame.display.flip()

    def _draw_top_view_at(self, obstacles, cx, cy):
        """在指定中心绘制俯视雷达图"""
        scale = 80

        # 扇形探测区域
        sector_surf = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        pts = [(cx, cy)]
        for angle in range(-60, 61, 5):
            rad = math.radians(angle)
            pts.append((cx + 500 * math.sin(rad), cy - 500 * math.cos(rad)))
        pts.append((cx, cy))
        pygame.draw.polygon(sector_surf, self.colors['sector'], pts)
        self.screen.blit(sector_surf, (0, 0))

        # 距离环
        for r in range(1, 6):
            radius = r * scale
            pygame.draw.circle(self.screen, (50, 55, 70), (cx, cy), radius, 1)
            font = pygame.font.Font(None, 22)
            text = font.render(f"{r}m", True, (160, 170, 180))
            self.screen.blit(text, (cx + 6, cy - radius - 18))

        # 高亮三方向线
        dirs = [(-45, (255, 100, 100), "L45"), (0, (100, 255, 100), "C"), (45, (100, 200, 255), "R45")]
        for angle, color, label in dirs:
            rad = math.radians(angle)
            end_x = cx + 550 * math.sin(rad)
            end_y = cy - 550 * math.cos(rad)
            pygame.draw.line(self.screen, color, (cx, cy), (end_x, end_y), 2)
            font = pygame.font.Font(None, 24)
            self.screen.blit(font.render(label, True, color), (end_x - 15, end_y - 20))

        # 绘制障碍物（含人体）
        for obs in obstacles:
            if 'angle' in obs and 'distance' in obs:
                ang = obs['angle']
                dist = obs['distance']
            else:
                x = obs.get('x', 0)
                z = obs.get('z', 1)
                dist = math.sqrt(x*x + z*z)
                ang = math.degrees(math.atan2(x, z)) if z > 0.1 else 0
            if dist > 6:
                continue
            rad = math.radians(ang)
            screen_x = cx + int(dist * scale * math.sin(rad))
            screen_y = cy - int(dist * scale * math.cos(rad))

            obj_type = obs.get('type', 'obstacle')
            if obj_type == 'human':
                color = (255, 255, 0)
                radius = 8
                font = pygame.font.Font(None, 16)
                self.screen.blit(font.render("!", True, (255,255,255)), (screen_x-4, screen_y-10))
            else:
                if dist < 1.2:
                    color = (255, 60, 60)
                    radius = 10
                elif dist < 2.5:
                    color = (255, 200, 50)
                    radius = 7
                else:
                    color = (50, 255, 50)
                    radius = 5
            pygame.draw.circle(self.screen, color, (screen_x, screen_y), radius)
            pygame.draw.circle(self.screen, (255, 255, 255), (screen_x, screen_y), radius, 1)
            font = pygame.font.Font(None, 20)
            self.screen.blit(font.render(f"{dist:.1f}m", True, (220, 230, 255)), (screen_x + 14, screen_y - 10))

        # 头部位置标识
        pygame.draw.circle(self.screen, (0, 200, 255), (cx, cy), 8)
        pygame.draw.line(self.screen, (0, 200, 255), (cx, cy), (cx, cy - 30), 3)
        pygame.draw.polygon(self.screen, (0, 200, 255), [(cx - 5, cy - 25), (cx + 5, cy - 25), (cx, cy - 35)])

    
   
    # ---------- 主绘制入口 ----------
    def draw_obstacles(self, obstacles):
        """根据当前模式绘制障碍物；人体图标由 scan_results 固定驱动。"""
        if self.view_mode == "stereo":
            self.draw_stereo(obstacles)
        else:
            self.draw_top_view(obstacles)

        # 绘制扫描状态提示
        if self.scan_status:
            font = pygame.font.Font(None, 44)
            surf = font.render(self.scan_status, True, self.scan_status_color)
            shadow = font.render(self.scan_status, True, (0, 0, 0))
            self.screen.blit(shadow, (22, 22))
            self.screen.blit(surf, (20, 20))

        pygame.display.flip()

    def set_scan_status(self, text, color=(0, 200, 255)):
        """设置/清除人体扫描状态提示。text 为 None 时清除。"""
        self.scan_status = text
        self.scan_status_color = color

    def set_scan_results(self, results):
        """设置固定的逐雷达扫描结果，保持显示到下次更新。"""
        self.scan_results = list(results) if results else []
