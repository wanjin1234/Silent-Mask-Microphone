import pygame
import pygame.gfxdraw
import math
import time
import os


def _clamp(value, low, high):
    return low if value < low else high if value > high else value


def _temperature_to_rgb(value, min_t, max_t):
    """把摄氏温度映射到冷->热 RGB（蓝->青->绿->黄->红），供热成像模式复用。"""
    if max_t <= min_t:
        return (128, 128, 128)
    a = min_t + (max_t - min_t) * 0.2121
    b = min_t + (max_t - min_t) * 0.3182
    c = min_t + (max_t - min_t) * 0.4242
    d = min_t + (max_t - min_t) * 0.8182

    red = _clamp(255.0 * (value - b) / (c - b), 0.0, 255.0)
    if value < a:
        green = _clamp(255.0 * (value - min_t) / (a - min_t), 0.0, 255.0)
    elif value <= c:
        green = 255.0
    else:
        green = _clamp(255.0 * (value - d) / (c - d), 0.0, 255.0)

    if value <= b:
        blue = _clamp(255.0 * (value - b) / (a - b), 0.0, 255.0)
    elif value <= d:
        blue = 0.0
    else:
        blue = _clamp(240.0 * (value - d) / (max_t - d), 0.0, 240.0)
    return (round(red), round(green), round(blue))


def _build_thermal_lut(min_t, max_t, levels=256):
    """按温度范围生成 RGB 查找表。"""
    if levels < 2:
        raise ValueError("levels must be at least 2")
    return [_temperature_to_rgb(min_t + (max_t - min_t) * i / (levels - 1),
                                min_t, max_t) for i in range(levels)]


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

        # UPS 电池状态（{voltage, current_ma, power_w, percent, charging}）
        self.battery = None
        try:
            self.battery_low_percent = float(os.getenv('UPS_LOW_PERCENT', '20'))
        except Exception:
            self.battery_low_percent = 20.0
        self._battery_bottom = 20   # 电池图标下边缘 y（FPS 文字据此下移避让）

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

    def project_point_mono(self, x, y, z):
        """单视口投影（AR 眼镜自己渲染双目，我们只输出整屏单视角）。

        用 2 倍焦距保持与单眼相同的水平 FOV，画面填满整个屏幕。
        """
        z_rel = z
        if z_rel <= 0.1:
            return None
        f = self.focal_length * 2.0
        screen_x = self.width // 2 + (x * f / z_rel)
        screen_y = self.center_y - (y * f / z_rel)
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

    
   
    # ---------- 点云透视视图（4D 成像雷达） ----------
    def draw_pointcloud(self, points, humans, obstacles, frame_stats=None):
        """点云透视（X 光 / 透视）视图：左右眼分屏，点云 + 人体辉光标记。

        "透视效果"的实现手法（纯软件渲染，树莓派 4B 可 20~30fps）：
          - 深度排序：远点先画、近点后画，近处遮挡远处，形成纵深；
          - 深度渐隐：远处点更暗更小、向灭点收敛，营造"看穿进房间"的深度感；
          - 地面网格：单向透视网格（水平深度线 + 向灭点汇聚的径向线）；
          - 人体辉光：多层同心圆模拟光晕，在黑烟/黑暗背景下有"穿透"观感。

        参数均为显示坐标系（x=左右, y=高度, z=前方，单位 m）：
          points    : [{'x','y','z','v','cls'}, ...]
          humans    : [{'x','y','z','v','state'}, ...]
          obstacles : [{'x','y','z','n_points'}, ...]
          frame_stats: 可选，帧头统计信息 dict
        """
        self.screen.fill((5, 5, 10))

        cls_color = {
            'dyn_hi': (255, 205, 60),
            'dyn_lo': (180, 150, 50),
            'long_hi': (40, 220, 255),
            'short_hi': (120, 255, 180),
            'long_lo': (30, 150, 180),
            'short_lo': (90, 180, 140),
        }

        for eye in ('left', 'right'):
            center_x = self.center_x_left if eye == 'left' else self.center_x_right
            self._draw_ground_grid(center_x)

            # 深度排序：远处(z 大)先画，近处后画（近处遮挡远处）
            ordered = sorted(points, key=lambda p: p.get('z', 0.0), reverse=True)
            for p in ordered:
                pos = self.project_point(p['x'], p['y'], p['z'], eye)
                if pos is None:
                    continue
                sx, sy = pos
                z = max(0.1, p['z'])
                fade = max(0.0, min(1.0, 1.0 - z / 12.0))
                base = cls_color.get(p.get('cls', 'dyn_lo'), (180, 180, 180))
                c = (int(base[0] * fade), int(base[1] * fade), int(base[2] * fade))
                r = max(1, int(3 * self.ui_scale * 8.0 / z))
                pygame.draw.circle(self.screen, c, (sx, sy), r)

            # 障碍点（更暗更小，避免与人体争抢注意力）
            for o in obstacles:
                pos = self.project_point(o['x'], o['y'], o['z'], eye)
                if pos is None:
                    continue
                sx, sy = pos
                z = max(0.1, o['z'])
                fade = max(0.0, min(1.0, 1.0 - z / 12.0))
                c = (int(120 * fade), int(120 * fade), int(140 * fade))
                pygame.draw.circle(self.screen, c, (sx, sy),
                                   max(1, int(2 * self.ui_scale * 6.0 / z)))

            # 人体辉光标记（最上层）
            for h in humans:
                pos = self.project_point(h['x'], h['y'], h['z'], eye)
                if pos is None:
                    continue
                sx, sy = pos
                state = h.get('state', 'breathing')
                col = (255, 80, 60) if state == 'moving' else (40, 220, 255)
                self._draw_glow_marker(sx, sy, h['z'], col)
                # 距离标签
                font = self._font(max(16, int(24 * self.ui_scale)))
                label = font.render(f"{h['z']:.1f}m", True, col)
                self.screen.blit(label, (sx + int(10 * self.ui_scale),
                                         sy - int(10 * self.ui_scale)))

        if frame_stats:
            self._draw_frame_stats(frame_stats)

    def draw_pointcloud_mono(self, points, humans, obstacles, frame_stats=None,
                             ultrasonic=None):
        """单视口透视视图：点云 + 顶部角度刻度 + 底部三弧形距离条。

        参数：
          points    : [{'x','y','z','v','cls'}, ...] 显示系坐标
          humans    : [{'x','y','z','state'}, ...]
          obstacles : [{'x','y','z','n_points'}, ...]
          ultrasonic: [{'angle':-45/0/45,'distance':m}, ...] 底部弧形条数据源
        """
        self.screen.fill((5, 5, 10))

        cls_color = {
            'dyn_hi': (255, 205, 60),
            'dyn_lo': (180, 150, 50),
            'long_hi': (40, 220, 255),
            'short_hi': (120, 255, 180),
            'long_lo': (30, 150, 180),
            'short_lo': (90, 180, 140),
        }

        center_x = self.width // 2
        self._draw_ground_grid(center_x, half=self.width // 2,
                               f=self.focal_length * 2.0)

        # 顶部角度刻度区：刻度线从 tick_base_y 向下延伸 tick_len，点云不可侵入
        tick_len = int(20 * self.ui_scale)
        tick_bottom = self.tick_base_y + tick_len + int(12 * self.ui_scale)
        # 底部距离条区：bar_base_y 上下 bar_half_h，点云不可越界（穿模）
        bar_top = self.bar_base_y - self.bar_half_h - int(16 * self.ui_scale)

        # 深度排序：远处(z 大)先画，近处后画（近处遮挡远处）
        ordered = sorted(points, key=lambda p: p.get('z', 0.0), reverse=True)
        for p in ordered:
            pos = self.project_point_mono(p['x'], p['y'], p['z'])
            if pos is None:
                continue
            sx, sy = pos
            # 过滤：与顶部刻度重合的点（太靠上）与穿模到距离条的点（太靠下）
            if sy < tick_bottom or sy > bar_top:
                continue
            z = max(0.1, p['z'])
            fade = max(0.0, min(1.0, 1.0 - z / 12.0))
            base = cls_color.get(p.get('cls', 'dyn_lo'), (180, 180, 180))
            c = (int(base[0] * fade), int(base[1] * fade), int(base[2] * fade))
            r = max(1, int(3 * self.ui_scale * 8.0 / z))
            pygame.draw.circle(self.screen, c, (sx, sy), r)

        # 障碍点（更暗更小）
        for o in obstacles:
            pos = self.project_point_mono(o['x'], o['y'], o['z'])
            if pos is None:
                continue
            sx, sy = pos
            if sy < tick_bottom or sy > bar_top:
                continue
            z = max(0.1, o['z'])
            fade = max(0.0, min(1.0, 1.0 - z / 12.0))
            c = (int(120 * fade), int(120 * fade), int(140 * fade))
            pygame.draw.circle(self.screen, c, (sx, sy),
                               max(1, int(2 * self.ui_scale * 6.0 / z)))

        # 人体包围框（框住疑似人体的点云，最上层）
        for h in humans:
            state = h.get('state', 'breathing')
            col = (255, 80, 60) if state == 'moving' else (40, 220, 255)
            self._draw_bbox_mono(h, col)

        # HUD：顶部角度刻度 + 底部三弧形距离条（后画，压住点云）
        self._draw_mono_hud(center_x, ultrasonic)

        if frame_stats:
            self._draw_frame_stats(frame_stats)

    def _draw_bbox_mono(self, h, color):
        """在单视口透视视图里画一个 3D 包围框，框住疑似人体点云。

        h 需含显示系坐标：x=左右, y=高度, z=前方距离，以及可选边界
        x_min/x_max/y_min/y_max/z_min/z_max。缺边界时用中心点 ± 固定尺寸兜底。
        """
        cx = h.get('x', 0.0)
        cy = h.get('y', 1.2)   # 高度
        cz = h.get('z', 2.0)   # 前方距离
        # 兜底尺寸：人体约 0.5m 宽、1.6m 高、0.5m 深
        x_min = h.get('x_min', cx - 0.25)
        x_max = h.get('x_max', cx + 0.25)
        y_min = h.get('y_min', max(0.0, cy - 0.8))
        y_max = h.get('y_max', cy + 0.8)
        z_min = h.get('z_min', max(0.2, cz - 0.25))
        z_max = h.get('z_max', cz + 0.25)

        # 8 个顶点（近面 z_min 和远面 z_max）
        corners = [
            (x_min, y_min, z_min), (x_max, y_min, z_min),
            (x_max, y_max, z_min), (x_min, y_max, z_min),
            (x_min, y_min, z_max), (x_max, y_min, z_max),
            (x_max, y_max, z_max), (x_min, y_max, z_max),
        ]
        pts = []
        for c in corners:
            p = self.project_point_mono(c[0], c[1], c[2])
            pts.append(p)
        if any(p is None for p in pts):
            return
        p = pts
        # 12 条边
        edges = [
            (0, 1), (1, 2), (2, 3), (3, 0),   # 近面
            (4, 5), (5, 6), (6, 7), (7, 4),   # 远面
            (0, 4), (1, 5), (2, 6), (3, 7),   # 连接
        ]
        for a, b in edges:
            pygame.draw.line(self.screen, color, p[a], p[b], 2)

        # 距离标签画在框顶部中点（近面）
        font = self._font(max(16, int(24 * self.ui_scale)))
        label = font.render(f"{cz:.1f}m", True, color)
        label_pos = ((p[2][0] + p[3][0]) // 2, p[2][1] - label.get_height() - 4)
        self.screen.blit(label, label_pos)

    def _draw_mono_hud(self, center_x, ultrasonic=None):
        """单视口 HUD：顶部等距角度刻度 + 底部三弧形距离条。"""
        hud_blue = self.colors['hud_blue']
        arc_half = self.arc_half
        arc_bow = self.arc_bow

        # ---- 顶部角度刻度（与 _build_hud 相同的弧形排布）----
        num_ticks = 7
        tick_len = int(20 * self.ui_scale)
        for i in range(num_ticks):
            t = -1.0 + 2.0 * i / (num_ticks - 1)
            x_pos = int(center_x + t * arc_half)
            y_top = int(self.tick_base_y - arc_bow * t * t)
            f = 1.0 - self.depth_fade * t * t
            col = (int(hud_blue[0] * f), int(hud_blue[1] * f), int(hud_blue[2] * f))
            tick_w = 2 if f > 0.7 else 1
            pygame.draw.line(self.screen, col, (x_pos, y_top),
                             (x_pos, y_top + tick_len), tick_w)

        # ---- 底部三弧形距离条（左/中/右，超声波距离）----
        if not ultrasonic:
            return
        dist_map = {'left': None, 'center': None, 'right': None}
        for u in ultrasonic:
            ang = u.get('angle')
            d = u.get('distance')
            if d is None or d <= 0:
                continue
            if -50 <= ang <= -30:
                dist_map['left'] = d
            elif 30 <= ang <= 50:
                dist_map['right'] = d
            elif -15 <= ang <= 15:
                dist_map['center'] = d

        color_near = (255, 50, 50)
        color_mid = (255, 200, 50)
        bar_t_centers = [-2.0 / 3.0, 0.0, 2.0 / 3.0]
        bar_half_w = 0.28
        dirs = [('L', dist_map['left']), ('C', dist_map['center']),
                ('R', dist_map['right'])]
        for idx, (label, dist) in enumerate(dirs):
            if dist is None or dist > 6.0:
                continue
            tc = bar_t_centers[idx]
            base_color = color_near if dist <= 1.2 else color_mid
            fade = 1.0 - self.depth_fade * tc * tc
            fill_color = (int(base_color[0] * fade), int(base_color[1] * fade),
                          int(base_color[2] * fade))
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
            pygame.gfxdraw.filled_polygon(self.screen, poly, fill_color)
            pygame.gfxdraw.aapolygon(self.screen, poly, (220, 220, 220))
            font = self._font(max(16, int(28 * self.ui_scale)))
            text_surf = font.render(f"{dist:.1f}m", True, hud_blue)
            shadow_surf = font.render(f"{dist:.1f}m", True, (0, 0, 0))
            text_rect = text_surf.get_rect(
                center=(center_x + tc * arc_half,
                        self.bar_base_y + arc_bow * tc * tc))
            self.screen.blit(shadow_surf, (text_rect.x + 2, text_rect.y + 2))
            self.screen.blit(text_surf, text_rect)

    def draw_thermal(self, temperatures, low, high, center_label=None, smooth=True):
        """热成像模式：把 32x24 温度帧渲染成铺满屏幕的平滑热图。

        与旧版裸色块不同，这里先渲染成 32x24 小图，再用 smoothscale 平滑
        插值放大到全屏，效果接近 show_graph.py 的 bicubic 插值，但无需
        matplotlib，树莓派上更轻量。

        参数：
          temperatures : 768 个摄氏温度值（MLX90640 32x24 原始帧，行优先）
          low/high     : 色阶范围
          center_label : 可选，叠加在底部的文字（如中心/峰值温度）
          smooth       : True=平滑插值，False=保留像元马赛克
        """
        if len(temperatures) != 768:
            return
        self.screen.fill((0, 0, 0))
        lut = _build_thermal_lut(low, high)
        span = high - low

        # 1. 渲染 32x24 小图（每个像元一个像素）
        small = pygame.Surface((32, 24))
        for y in range(24):
            for x in range(32):
                v = temperatures[y * 32 + x]
                idx = int(_clamp((v - low) * 255.0 / span, 0.0, 255.0))
                small.set_at((x, y), lut[idx])

        # 2. 平滑插值放大到全屏（保持 4:3，居中）
        scale = min(self.width / 32, self.height / 24)
        w = int(32 * scale)
        h = int(24 * scale)
        if smooth:
            try:
                big = pygame.transform.smoothscale(small, (w, h))
            except Exception:
                big = pygame.transform.scale(small, (w, h))
        else:
            big = pygame.transform.scale(small, (w, h))
        self.screen.blit(big, ((self.width - w) // 2, (self.height - h) // 2))

        if center_label:
            font = self._font(max(18, int(24 * self.ui_scale)))
            s = font.render(center_label, True, (255, 255, 255))
            self.screen.blit(s, (8, self.height - s.get_height() - 8))

    def _draw_ground_grid(self, center_x, cam_h=1.6, max_z=12.0, half=None, f=None):
        """绘制单向透视地面网格：水平深度线 + 向灭点汇聚的径向线。"""
        grid = (36, 42, 52)
        f = self.focal_length if f is None else f
        half = self.half_width // 2 if half is None else half   # 单眼半宽（设计 1920 时 = 480px）
        # 水平深度线（z 固定，地面 y = -cam_h）
        for z0 in (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0):
            sy = self.center_y + (cam_h * f / z0)
            if sy < 0 or sy > self.height:
                continue
            fade = max(0.25, 1.0 - z0 / max_z)
            c = (int(grid[0] * fade), int(grid[1] * fade), int(grid[2] * fade))
            pygame.draw.line(self.screen, c,
                             (max(0, center_x - half), int(sy)),
                             (min(self.width, center_x + half), int(sy)), 1)
        # 径向线（x 固定，从近端向灭点汇聚）
        for xi in (-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0):
            z_near = 0.5
            sx_near = center_x + (xi * f / z_near)
            sy_near = self.center_y + (cam_h * f / z_near)
            pygame.draw.line(self.screen, grid,
                             (int(sx_near), int(sy_near)),
                             (center_x, self.center_y), 1)

    def _draw_glow_marker(self, sx, sy, z, color, size_scale=1.0):
        """多层同心圆模拟光晕，无 alpha 混合，树莓派软渲染友好。"""
        zc = max(0.5, z)
        r = max(3, int(6 * self.ui_scale * 6.0 / zc * size_scale))
        pygame.draw.circle(self.screen,
                           (int(color[0] * 0.25), int(color[1] * 0.25), int(color[2] * 0.25)),
                           (sx, sy), int(r * 2.2))
        pygame.draw.circle(self.screen,
                           (int(color[0] * 0.55), int(color[1] * 0.55), int(color[2] * 0.55)),
                           (sx, sy), int(r * 1.5))
        pygame.draw.circle(self.screen, color, (sx, sy), r)
        pygame.draw.circle(self.screen, (255, 255, 255), (sx, sy), max(1, r // 3))

    def _draw_frame_stats(self, stats):
        """左上角叠加帧头统计（帧号/点数/处理耗时），调试用。"""
        font = self._font(20)
        lines = [
            f"frame {stats.get('frame_id', '?')}  "
            f"pts {stats.get('n_points', '?')}  tracks {stats.get('n_tracks', '?')}",
        ]
        if stats.get('bb_ms') is not None:
            lines.append(f"bb {stats['bb_ms']}ms post {stats.get('postbb_ms')} "
                         f"tx {stats.get('tx_ms')} int {stats.get('interval_ms')}ms")
        y = 10
        for ln in lines:
            s = font.render(ln, True, self.colors['hud_blue'])
            self.screen.blit(s, (10, y))
            y += 24

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

        self._draw_battery()

        if fps is not None:
            self._draw_fps(fps)
        t1 = time.perf_counter()

        pygame.display.flip()
        t2 = time.perf_counter()

        # 平滑记录（诊断用）：draw 含所有绘图与叠加，flip 为显示同步/驱动耗时
        self.draw_ms = self.draw_ms * 0.9 + (t1 - t0) * 1000 * 0.1
        self.flip_ms = self.flip_ms * 0.9 + (t2 - t1) * 1000 * 0.1

    def _draw_fps(self, fps):
        """在画面左上角绘制实测帧率与分阶段耗时（带阴影，避让电池图标）。"""
        font = pygame.font.Font(None, 32)
        text = f"{fps:.1f} fps   draw {self.draw_ms:.0f}ms   flip {self.flip_ms:.0f}ms"
        shadow = font.render(text, True, (0, 0, 0))
        surf = font.render(text, True, (255, 255, 255))
        y = self._battery_bottom + 8
        self.screen.blit(shadow, (22, y + 2))
        self.screen.blit(surf, (20, y))

    def set_scan_status(self, text, color=(0, 200, 255)):
        """设置/清除人体扫描状态提示。text 为 None 时清除。"""
        self.scan_status = text
        self.scan_status_color = color

    def set_scan_results(self, results):
        """设置固定的逐雷达扫描结果，保持显示到下次更新。"""
        self.scan_results = list(results) if results else []

    def set_battery(self, status):
        """设置 UPS 电池状态（{voltage, current_ma, power_w, percent, charging}）。"""
        self.battery = dict(status) if status else None

    def _draw_battery(self):
        """左上角叠加 UPS 电池图标：外框 + 内部矩形格子，低电量闪烁。

        充电时外框与正极凸起变青色；电量 >50% 绿 / 20%~50% 黄 / <=20% 红；
        低电量（<=UPS_LOW_PERCENT）时整个图标以约 2Hz 闪烁。
        """
        st = self.battery
        if not st:
            self._battery_bottom = 20
            return
        percent = max(0.0, min(100.0, float(st.get('percent', 0.0))))
        charging = bool(st.get('charging', False))
        low = percent <= self.battery_low_percent
        # 低电量：每 0.5s 切换一次显示/隐藏，实现闪烁
        if low and (int(time.time() * 2) % 2 == 1):
            self._battery_bottom = 20
            return

        u = self.ui_scale
        bw = max(44, int(66 * u))     # 电池主体宽
        bh = max(24, int(34 * u))     # 电池主体高
        nub_w = max(3, int(4 * u))    # 正极凸起宽
        nub_h = max(12, int(18 * u))  # 正极凸起高
        x0, y0 = 20, 20

        outline = (90, 220, 255) if charging else (200, 220, 235)
        pygame.draw.rect(self.screen, outline, (x0, y0, bw, bh), 2)
        pygame.draw.rect(self.screen, outline,
                         (x0 + bw, y0 + (bh - nub_h) // 2, nub_w, nub_h))

        # 内部矩形格子（5 格）
        cells = 5
        gap = max(1, int(2 * u))
        margin = 3
        seg_w = (bw - 2 * margin - (cells - 1) * gap) // cells
        seg_h = bh - 2 * margin
        if percent > 50:
            color = (0, 200, 80)
        elif percent > self.battery_low_percent:
            color = (255, 200, 0)
        else:
            color = (255, 60, 60)
        filled = int(round(percent / 100.0 * cells))
        seg_x = x0 + margin
        for i in range(cells):
            if i < filled:
                pygame.draw.rect(self.screen, color,
                                 (seg_x, y0 + margin, seg_w, seg_h))
            seg_x += seg_w + gap

        # 右侧百分比文字（带阴影）
        font = self._font(max(18, int(26 * u)))
        text = f"{percent:.0f}%"
        shadow = font.render(text, True, (0, 0, 0))
        surf = font.render(text, True, (255, 255, 255))
        tx = x0 + bw + nub_w + 16
        ty = y0 + (bh - surf.get_height()) // 2
        self.screen.blit(shadow, (tx + 2, ty + 2))
        self.screen.blit(surf, (tx, ty))

        # 充电时在百分比文字左侧画一个闪电标记
        if charging:
            bx = x0 + bw + nub_w + 4
            cy = y0 + bh // 2
            bolt = [(bx + 5, cy - 8), (bx, cy), (bx + 3, cy),
                    (bx + 2, cy + 8), (bx + 9, cy - 1), (bx + 5, cy - 1)]
            pygame.draw.polygon(self.screen, (255, 240, 100), bolt)

        self._battery_bottom = y0 + bh

    def set_scan_active(self, active, start_time=None):
        """设置扫描进行状态，用于触发扫描推进弧动画。

        start_time 为扫描实际开始的时间戳；传入后动画与真实扫描窗口精确同步，
        避免因主循环读取延迟造成动画起始错位。
        """
        if active and not self.scan_active:
            self.scan_start_time = start_time if start_time is not None else time.time()
        self.scan_active = active
