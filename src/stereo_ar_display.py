import pygame
import pygame.gfxdraw
import math
import time
import os

class StereoARDisplay:
    def __init__(self, width=1920, height=1080, ipd_cm=6.5, fullscreen=None):
        # 平滑缩放提示需在 pygame.init 与 set_mode 之前设置，放大时减少锯齿
        os.environ.setdefault('SDL_HINT_RENDER_SCALE_QUALITY', '1')
        pygame.init()
        self.design_w = width
        self.design_h = height

        # 内部渲染分辨率：默认保持设计分辨率（1920x1080），不做降采样。
        # 覆盖方式：C4002_RENDER_W/H 直接指定，或 C4002_RENDER_SCALE 按比例。
        try:
            render_w = int(os.getenv('C4002_RENDER_W', '0'))
        except Exception:
            render_w = 0
        try:
            render_h = int(os.getenv('C4002_RENDER_H', '0'))
        except Exception:
            render_h = 0
        if render_w <= 0 or render_h <= 0:
            try:
                scale = float(os.getenv('C4002_RENDER_SCALE', '1.0'))
            except Exception:
                scale = 1.0
            render_w = max(1, int(width * scale))
            render_h = max(1, int(height * scale))

        if fullscreen is None:
            fullscreen = os.getenv('C4002_FULLSCREEN', '0') == '1'

        # SCALED：让 SDL 把内部 surface 上采样到窗口/全屏，硬件可用时走硬件缩放
        flags = pygame.SCALED
        if fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((render_w, render_h), flags)
        pygame.display.set_caption("火场AR调试 - 按V切换俯视/立体")

        # 统一缩放因子：布局基于 1920x1080 设计，按内部渲染分辨率等比缩放
        self.ui_scale = min(render_w / width, render_h / height)

        self.width = render_w
        self.height = render_h
        self.half_width = render_w // 2
        self.center_y = render_h // 2
        self.focal_length = 700 * self.ui_scale
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

        # 2.5D 伪立体 HUD 参数（基于 1920x1080 设计，按 ui_scale 缩放）
        self.arc_half = 430 * self.ui_scale    # HUD 带从中心向两侧展开的半宽(px)
        self.arc_bow = 45 * self.ui_scale      # 弧线弯曲幅度(px)，越大弧越弯
        self.tick_base_y = 60 * self.ui_scale  # 上方刻度基线 y（中心处）
        self.icon_base_y = 95 * self.ui_scale  # 人体图标基线 y（中心处）
        self.bar_base_y = 1020 * self.ui_scale # 下方距离条基线 y（中心处）
        self.bar_half_h = 15 * self.ui_scale   # 距离条半高(px)
        self.divider_len = 130 * self.ui_scale # 分割线向纵深延伸的长度(px)
        self.divider_color = (0, 140, 200)  # 分割线颜色（略暗，作为纵深元素）
        self.depth_fade = 0.5      # 深度衰减强度(0~1)：越大，边缘(远处)越暗越细

        # 扫描波纹单次扩散耗时，与人体静止扫描时长保持一致（s）。
        # 必须与 main_stereo.SCAN_DURATION 默认值一致（只测移动的人，3s）。
        self.scan_duration = float(os.getenv('C4002_SCAN_DURATION', '3.0'))

        # 扫描推进弧动画状态（仅在扫描期间显示）
        self.scan_active = False
        self.scan_start_time = 0.0

        # 字体缓存：避免每帧重复创建 Font 对象
        self._fonts = {}
        # 静态 HUD 缓存：黑底不透明表面。AR 背景本就是纯黑（透明），HUD 元素
        # 全为不透明颜色，画到黑底表面后整屏 blit 与直接绘制视觉完全一致，
        # 但 blit 走不透明 memcpy 快路径，比每帧重绘(gfxdraw/字体)快得多。
        self._hud_surface = pygame.Surface((self.width, self.height))
        self._hud_key = None
        # 扫描弧叠加层：只覆盖扫描弧可能出现的纵向区间（灭点~距离条），
        # 缩成窄条后逐帧 blit 的 alpha 混合量大幅减少，扫描时帧率显著提升。
        self._overlay_top = int(self.center_y * 0.5)
        self._overlay_h = int(self.bar_base_y + 60 * self.ui_scale) - self._overlay_top
        self._overlay = pygame.Surface((self.width, self._overlay_h), pygame.SRCALPHA)

        # 性能诊断：记录每帧绘制耗时与 flip 耗时（仅诊断用）
        self.draw_ms = 0.0
        self.flip_ms = 0.0

    def _font(self, size):
        f = self._fonts.get(size)
        if f is None:
            f = pygame.font.Font(None, size)
            self._fonts[size] = f
        return f

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
        立体分屏模式（AR 透明背景，纯图形无文字）：
        - 背景纯黑
        - 中心浅蓝色小十字（左右各一）
        - 上方等距刻度线（浅蓝色竖线，无数字）
        - 刻度线下方：人体信号图标（黄色三角形 + 中心浅蓝色光点）
        - 下方三个矩形条（普通障碍物距离），红/黄/隐藏
        - 扫描波纹：从底部距离条向灭点缓慢扩散、逐渐淡出的半透明弧线
        """
        # 1. 解析障碍物数据（人体不再来自实时数据，只来自 scan_results 固定结果）
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

        # 2. 缓存 HUD：数据变化（约20Hz）时才重建，逐帧仅整屏 memcpy blit。
        #    距离取 0.1m 精度（与显示一致）作为缓存键，避免 float 微抖导致每帧重建。
        key = (
            None if left_dist is None else round(left_dist, 1),
            None if center_dist is None else round(center_dist, 1),
            None if right_dist is None else round(right_dist, 1),
            None if human_left is None else round(human_left, 1),
            None if human_center is None else round(human_center, 1),
            None if human_right is None else round(human_right, 1),
        )
        if self._hud_key != key:
            self._build_hud(left_dist, center_dist, right_dist,
                            human_left, human_center, human_right)
            self._hud_key = key
        self.screen.blit(self._hud_surface, (0, 0))

        if self.scan_active:
            elapsed = time.time() - self.scan_start_time
            p = min(1.0, max(0.0, elapsed / self.scan_duration))
            self._overlay.fill((0, 0, 0, 0))
            for center_x in [self.center_x_left, self.center_x_right]:
                self._draw_scan_sweep(self._overlay, center_x, p)
            self.screen.blit(self._overlay, (0, self._overlay_top))


    def _build_hud(self, left_dist, center_dist, right_dist,
                   human_left, human_center, human_right):
        """把静态 HUD（十字/刻度/人体图标/分割线/距离条）绘制到黑底缓存表面。"""
        hud_blue = self.colors['hud_blue']
        arc_half = self.arc_half
        arc_bow = self.arc_bow
        surf = self._hud_surface
        surf.fill((0, 0, 0))   # 黑底（AR 透明背景）

        def fade(color, f):
            # f: 0=全黑(远景) ~ 1=原始色(近景)
            if f >= 1.0:
                return color
            if f <= 0.0:
                return (0, 0, 0)
            return (int(color[0] * f), int(color[1] * f), int(color[2] * f))

        # 中心浅蓝色小十字
        cross_size = int(12 * self.ui_scale)
        cross_gap = max(2, int(4 * self.ui_scale))
        for center_x in [self.center_x_left, self.center_x_right]:
            cx = center_x
            cy = self.center_y
            pygame.draw.line(surf, hud_blue, (cx - cross_size, cy), (cx - cross_gap, cy), 2)
            pygame.draw.line(surf, hud_blue, (cx + cross_gap, cy), (cx + cross_size, cy), 2)
            pygame.draw.line(surf, hud_blue, (cx, cy - cross_size), (cx, cy - cross_gap), 2)
            pygame.draw.line(surf, hud_blue, (cx, cy + cross_gap), (cx, cy + cross_size), 2)

        # 上方等距刻度线（浅蓝色，沿纵深方向弧形排布）
        num_ticks = 7
        tick_len = int(20 * self.ui_scale)
        for center_x in [self.center_x_left, self.center_x_right]:
            for i in range(num_ticks):
                t = -1.0 + 2.0 * i / (num_ticks - 1)
                x_pos = center_x + t * arc_half
                y_top = self.tick_base_y - arc_bow * t * t
                f = 1.0 - self.depth_fade * t * t
                tick_w = 2 if f > 0.7 else 1
                pygame.draw.line(surf, fade(hud_blue, f),
                                 (x_pos, y_top),
                                 (x_pos, y_top + tick_len), tick_w)

        # 人体信号图标（沿弧线排布，对齐三个方向）
        icon_size = int(45 * self.ui_scale)

        def draw_human_icon(surface, x, y, size, distance):
            half = size // 2
            # 黄色三角形（人体信号标识）
            points = [(x, y - half), (x - half, y + half // 2), (x + half, y + half // 2)]
            pygame.draw.polygon(surface, (255, 255, 0), points)
            pygame.draw.polygon(surface, (200, 200, 0), points, 2)
            # 感叹号 "!"（浅蓝色，居中）
            font_ex = self._font(max(16, size))
            exclaim = font_ex.render("!", True, hud_blue)
            ex_rect = exclaim.get_rect(center=(x, y))
            surface.blit(exclaim, ex_rect)

            # 距离数值（浅蓝色，紧挨图标下方且与图标中心对齐）
            font_dist = self._font(max(18, int(30 * self.ui_scale)))
            dist_text = font_dist.render(f"{distance:.1f}m", True, hud_blue)
            dist_rect = dist_text.get_rect(center=(x, y + half + int(17 * self.ui_scale)))
            surface.blit(dist_text, dist_rect)

        icon_t_centers = [-2.0 / 3.0, 0.0, 2.0 / 3.0]
        human_dists = [human_left, human_center, human_right]
        for center_x in [self.center_x_left, self.center_x_right]:
            for idx, dist in enumerate(human_dists):
                if dist is not None and dist <= 5.0:
                    t = icon_t_centers[idx]
                    x = center_x + t * arc_half
                    y = self.icon_base_y - arc_bow * t * t
                    draw_human_icon(surf, x, y, icon_size, dist)

        # 分割线（向纵深远方延伸）+ 下方弧形距离条
        color_near = (255, 50, 50)
        color_mid = (255, 200, 50)
        dirs = [('L', left_dist), ('C', center_dist), ('R', right_dist)]
        bar_t_centers = [-2.0 / 3.0, 0.0, 2.0 / 3.0]
        bar_half_w = 0.28

        for center_x in [self.center_x_left, self.center_x_right]:
            # 三个方向条之间的分割线，向纵深（画面中心/消失点）延伸
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
                    pygame.draw.line(surf, fade(self.divider_color, f0),
                                     (ax, ay), (bx, by), 2)

            # 弧形距离条（红/黄），用 gfxdraw 抗锯齿消除锯齿点
            for idx, (label, dist) in enumerate(dirs):
                if dist is None or dist > 6.0:
                    continue
                tc = bar_t_centers[idx]
                base_color = color_near if dist <= 1.2 else color_mid
                fill_color = fade(base_color, 1.0 - self.depth_fade * tc * tc)
                pts_top = []
                pts_bot = []
                seg = 24
                for j in range(seg + 1):
                    t = tc + bar_half_w * (-1.0 + 2.0 * j / seg)
                    sx = int(round(center_x + t * arc_half))
                    yc = self.bar_base_y + arc_bow * t * t
                    pts_top.append((sx, int(round(yc - self.bar_half_h))))
                    pts_bot.append((sx, int(round(yc + self.bar_half_h))))
                poly = pts_top + pts_bot[::-1]
                pygame.gfxdraw.filled_polygon(surf, poly, fill_color)
                pygame.gfxdraw.aapolygon(surf, poly, (220, 220, 220))

                # 条内距离文字（蓝色，红黄底上清晰）
                font = self._font(max(16, int(28 * self.ui_scale)))
                text_str = f"{dist:.1f}m"
                text_surf = font.render(text_str, True, self.colors['hud_blue'])
                shadow_surf = font.render(text_str, True, (0, 0, 0))
                text_rect = text_surf.get_rect(
                    center=(center_x + tc * arc_half,
                            self.bar_base_y + arc_bow * tc * tc))
                surf.blit(shadow_surf, (text_rect.x + 2, text_rect.y + 2))
                surf.blit(text_surf, text_rect)


    def _draw_scan_sweep(self, overlay, center_x, p):
        """扫描推进弧：淡黄色弧形条，从底部向灭点推进并淡出。

        弧形条有一定厚度；顶部（向灭点一侧）半透明，底部逐渐透明直至消失。
        """
        if p <= 0.0 or p >= 1.0:
            return
        # 弧条中心位置：从底部距离条(bar_base_y)向灭点(center_y)推进
        # 注意：坐标相对 overlay 左上角（overlay 顶部对应 self._overlay_top）
        y = self.bar_base_y - self._overlay_top + (self.center_y - self.bar_base_y) * p
        # 横向半宽与弧度随纵深收缩，到达灭点时收敛为一点
        half = self.arc_half * (1.0 - p)
        bow = self.arc_bow * (1.0 - p)
        # 整体随推进淡出
        base_alpha = 200 * (1.0 - p)
        if base_alpha <= 0:
            return
        thickness = int(50 * self.ui_scale)   # 弧形条厚度(px)
        steps = 20              # 厚度方向渐变分段数
        seg = 48                # 横向分段数
        for s in range(steps):
            v = s / (steps - 1)          # 0=顶部(向灭点一侧) ~ 1=底部(渐透明)
            local_alpha = base_alpha * (1.0 - v)
            if local_alpha <= 0:
                continue
            yy = (y - thickness * 0.5) + thickness * v
            pts = []
            for j in range(seg + 1):
                t = -1.0 + 2.0 * j / seg
                pts.append((center_x + t * half, yy + bow * t * t))
            color = (255, 240, 150, int(local_alpha))
            pygame.draw.lines(overlay, color, False, pts, 1)

    # ---------- 俯视图模式（保持不变） ----------
    def draw_top_view(self, obstacles):
        """俯视图模式：左右分屏显示，无立体视差"""
        self.screen.fill((10, 10, 18))

        # 左半屏俯视图
        self._draw_top_view_at(obstacles, self.center_x_left, self.height - 100)
        # 右半屏俯视图（内容完全相同）
        self._draw_top_view_at(obstacles, self.center_x_right, self.height - 100)

    def _draw_top_view_at(self, obstacles, cx, cy):
        """在指定中心绘制俯视雷达图"""
        scale = 80 * self.ui_scale

        # 扇形探测区域
        sector_surf = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        R = 500 * self.ui_scale
        pts = [(cx, cy)]
        for angle in range(-60, 61, 5):
            rad = math.radians(angle)
            pts.append((cx + R * math.sin(rad), cy - R * math.cos(rad)))
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
            end_x = cx + 550 * self.ui_scale * math.sin(rad)
            end_y = cy - 550 * self.ui_scale * math.cos(rad)
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
    def draw_obstacles(self, obstacles, fps=None):
        """根据当前模式绘制障碍物；人体图标由 scan_results 固定驱动。

        fps 非空时在画面左上角叠加实测帧率（调试用，设 C4002_SHOW_FPS=0 关闭）。
        """
        t0 = time.perf_counter()
        if self.view_mode == "stereo":
            self.draw_stereo(obstacles)
        else:
            self.draw_top_view(obstacles)

        if fps is not None:
            self._draw_fps(fps)
        t1 = time.perf_counter()

        pygame.display.flip()
        t2 = time.perf_counter()

        # 平滑记录（诊断用）：draw 含所有绘图与叠加，flip 为显示同步/驱动耗时
        self.draw_ms = self.draw_ms * 0.9 + (t1 - t0) * 1000 * 0.1
        self.flip_ms = self.flip_ms * 0.9 + (t2 - t1) * 1000 * 0.1

    def _draw_fps(self, fps):
        """在画面左上角绘制实测帧率与分阶段耗时（带阴影）。"""
        font = pygame.font.Font(None, 32)
        text = f"{fps:.1f} fps   draw {self.draw_ms:.0f}ms   flip {self.flip_ms:.0f}ms"
        shadow = font.render(text, True, (0, 0, 0))
        surf = font.render(text, True, (255, 255, 255))
        self.screen.blit(shadow, (22, 22))
        self.screen.blit(surf, (20, 20))

    def set_scan_status(self, text, color=(0, 200, 255)):
        """设置/清除人体扫描状态提示。text 为 None 时清除。"""
        self.scan_status = text
        self.scan_status_color = color

    def set_scan_results(self, results):
        """设置固定的逐雷达扫描结果，保持显示到下次更新。"""
        self.scan_results = list(results) if results else []

    def set_scan_active(self, active, start_time=None):
        """设置扫描进行状态，用于触发扫描推进弧动画。

        start_time 为扫描实际开始的时间戳；传入后动画与真实扫描窗口精确同步，
        避免因主循环读取延迟造成动画起始错位。
        """
        if active and not self.scan_active:
            self.scan_start_time = start_time if start_time is not None else time.time()
        self.scan_active = active
