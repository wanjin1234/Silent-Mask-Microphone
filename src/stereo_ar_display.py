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

        # 颜色定义
        self.colors = {
            'bg': (20, 20, 30),
            'grid': (60, 60, 70),
            'sector': (0, 200, 255, 30),
            'text': (255, 255, 255),
            # 新增 HUD 浅蓝色
            'hud_blue': (0, 200, 255),
        }

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

        # 2. 解析障碍物和人体数据
        left_dist = center_dist = right_dist = None
        human_left = human_center = human_right = None

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

            obj_type = obs.get('type', 'obstacle')

            direction = None
            if -50 <= ang <= -40:
                direction = 'left'
            elif 40 <= ang <= 50:
                direction = 'right'
            elif -10 <= ang <= 10:
                direction = 'center'
            else:
                continue

            if obj_type == 'human':
                if direction == 'left':
                    human_left = dist
                elif direction == 'center':
                    human_center = dist
                elif direction == 'right':
                    human_right = dist
            else:
                if direction == 'left':
                    left_dist = dist
                elif direction == 'center':
                    center_dist = dist
                elif direction == 'right':
                    right_dist = dist

        # 3. 中心浅蓝色小十字
        cross_size = 12
        cross_gap = 4
        hud_blue = self.colors['hud_blue']
        for center_x in [self.center_x_left, self.center_x_right]:
            cx = center_x
            cy = self.center_y
            pygame.draw.line(self.screen, hud_blue, (cx - cross_size, cy), (cx - cross_gap, cy), 2)
            pygame.draw.line(self.screen, hud_blue, (cx + cross_gap, cy), (cx + cross_size, cy), 2)
            pygame.draw.line(self.screen, hud_blue, (cx, cy - cross_size), (cx, cy - cross_gap), 2)
            pygame.draw.line(self.screen, hud_blue, (cx, cy + cross_gap), (cx, cy + cross_size), 2)

        # 4. 上方等距刻度线（浅蓝色）
        total_width = int(self.width * 0.5)
        bar_width = total_width // 3
        gap = 6
        num_ticks = 7
        tick_height = 20
        tick_y_top = 40

        for center_x in [self.center_x_left, self.center_x_right]:
            local_start = center_x - total_width // 2
            for i in range(num_ticks):
                x_pos = local_start + (i / (num_ticks - 1)) * total_width
                pygame.draw.line(self.screen, hud_blue,
                                 (x_pos, tick_y_top),
                                 (x_pos, tick_y_top + tick_height), 2)

        # 5. 人体信号图标（刻度线下方，对齐三个方向）
        icon_size = 30
        icon_y = tick_y_top + tick_height + 15

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

        for center_x in [self.center_x_left, self.center_x_right]:
            segment_width = total_width / 3
            x_positions = [
                local_start + segment_width * 0.5,
                local_start + segment_width * 1.5,
                local_start + segment_width * 2.5
            ]
            human_dists = [human_left, human_center, human_right]
            for idx, dist in enumerate(human_dists):
                if dist is not None and dist <= 5.0:
                    draw_human_icon(self.screen, x_positions[idx], icon_y, icon_size, dist)

        # 6. 下方三个矩形条（普通障碍物距离）
        bar_height = 30
        bar_y = self.height - 70
        color_near = (255, 50, 50)
        color_mid = (255, 200, 50)
        dirs = [('L', left_dist), ('C', center_dist), ('R', right_dist)]

        for center_x in [self.center_x_left, self.center_x_right]:
            local_start = center_x - total_width // 2
            for idx, (label, dist) in enumerate(dirs):
                x = local_start + idx * (bar_width + gap)
                if dist is None or dist > 2.5:
                    continue
                fill_color = color_near if dist <= 1.2 else color_mid
                rect_rect = (x, bar_y, bar_width, bar_height)
                pygame.draw.rect(self.screen, fill_color, rect_rect)
                pygame.draw.rect(self.screen, (220, 220, 220), rect_rect, 1)

                # 条内距离文字蓝色（红黄底上清晰）
                font = pygame.font.Font(None, 28)
                text_str = f"{dist:.1f}m"
                text_surf = font.render(text_str, True, self.colors['hud_blue'])
                shadow_surf = font.render(text_str, True, (0, 0, 0))
                text_rect = text_surf.get_rect(center=(x + bar_width//2, bar_y + bar_height//2))
                self.screen.blit(shadow_surf, (text_rect.x + 2, text_rect.y + 2))
                self.screen.blit(text_surf, text_rect)

        # 7. 模式提示
        self._draw_mode_hint()

    # ---------- 俯视图模式（保持不变） ----------
    def draw_top_view(self, obstacles):
        self.screen.fill((10, 10, 18))
        cx = self.width // 2
        cy = self.height - 100
        scale = 80

        sector_surf = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        pts = [(cx, cy)]
        for angle in range(-60, 61, 5):
            rad = math.radians(angle)
            pts.append((cx + 500 * math.sin(rad), cy - 500 * math.cos(rad)))
        pts.append((cx, cy))
        pygame.draw.polygon(sector_surf, self.colors['sector'], pts)
        self.screen.blit(sector_surf, (0, 0))

        for r in range(1, 6):
            radius = r * scale
            pygame.draw.circle(self.screen, (50, 55, 70), (cx, cy), radius, 1)
            font = pygame.font.Font(None, 22)
            text = font.render(f"{r}m", True, (160, 170, 180))
            self.screen.blit(text, (cx + 6, cy - radius - 18))

        dirs = [(-45, (255, 100, 100), "L45"), (0, (100, 255, 100), "C"), (45, (100, 200, 255), "R45")]
        for angle, color, label in dirs:
            rad = math.radians(angle)
            end_x = cx + 550 * math.sin(rad)
            end_y = cy - 550 * math.cos(rad)
            pygame.draw.line(self.screen, color, (cx, cy), (end_x, end_y), 2)
            font = pygame.font.Font(None, 24)
            self.screen.blit(font.render(label, True, color), (end_x - 15, end_y - 20))

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

        pygame.draw.circle(self.screen, (0, 200, 255), (cx, cy), 8)
        pygame.draw.line(self.screen, (0, 200, 255), (cx, cy), (cx, cy - 30), 3)
        pygame.draw.polygon(self.screen, (0, 200, 255), [(cx - 5, cy - 25), (cx + 5, cy - 25), (cx, cy - 35)])

        self._draw_mode_hint()

    # ---------- 模式提示 ----------
    def _draw_mode_hint(self):
        font = pygame.font.Font(None, 30)
        mode_text = "俯视图模式" if self.view_mode == "top" else "立体分屏模式"
        hint = f"{mode_text}  按V切换  ESC退出"
        text = font.render(hint, True, (200, 210, 220))
        bg = pygame.Surface((text.get_width() + 20, text.get_height() + 10), pygame.SRCALPHA)
        bg.fill((0, 0, 0, 150))
        self.screen.blit(bg, (10, 10))
        self.screen.blit(text, (20, 15))

    # ---------- 主绘制入口 ----------
    def draw_obstacles(self, obstacles, humans):
        """根据当前模式绘制，合并障碍物与人体数据"""
        merged = list(obstacles)
        for h in humans:
            h_copy = dict(h)
            h_copy['type'] = 'human'
            merged.append(h_copy)

        if self.view_mode == "stereo":
            self.draw_stereo(merged)
        else:
            self.draw_top_view(merged)
        pygame.display.flip()
