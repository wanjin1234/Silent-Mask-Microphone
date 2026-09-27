"""pygame 桌面版上位机（给接了显示器 / VNC 的树莓派用）。

与 Web 版共用同一套数据链路与协议解析，区别只是显示层用 pygame 直接画，
因此在树莓派 4B 上不需要浏览器、不受网络影响，延迟更低。

界面布局::

    ┌────────────────────────────────────────────────┬──────────────┐
    │ 工具栏：状态 / 开始 / 停止 / 清除 / 暂停 / 视图  │  实时统计     │
    ├────────────────────────────────────────────────┤  各类点数     │
    │              3D 点云（拖动旋转，滚轮缩放）       │──────────────│
    ├───────────────┬───────────────┬────────────────┤  点表         │
    │ BEV 矩阵热图  │ XZ 侧视图     │ YZ 正视图       │ （前 N 行）   │
    └───────────────┴───────────────┴────────────────┴──────────────┘
      状态栏：数据源 / 解析统计 / 操作提示

左下角的 **BEV 矩阵热图**直接由 :mod:`radarpi.ndarray_iface` 的
``to_bev()`` 生成，所以它同时是"点云 → ndarray 矩阵"接口的可视化验证：
看到的图像就是下游算法会拿到的那张矩阵。

快捷键::

    空格 暂停/继续    S 开始    X 停止    C 清除    R 录制    N 存矩阵
    1/2/3/4 切换显示  F 全屏    P 截图    Q / ESC 退出
    鼠标拖动 = 旋转 3D 视角，滚轮 = 缩放

用法::

    radarpi view                       # 接真机
    radarpi view --simulate            # 没有雷达时先看效果
    radarpi view --replay out.jsonl --loop
    radarpi view --processor ./my_algo.py:process
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import protocol as P
from .config import Config
from .recorder import Recorder, default_filename
from .serialport import BAUD_RATE

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

# 背景与配色（与 Web 版保持一致，便于对照）
BG = (20, 23, 28)
PANEL = (27, 31, 38)
PANEL_LINE = (44, 50, 60)
FG = (216, 222, 233)
FG_DIM = (139, 149, 165)
ACCENT = (55, 200, 255)
OK = (111, 220, 111)
WARN = (255, 194, 71)
ERR = (255, 93, 93)
GRID = (37, 43, 52)
SELECTED = (42, 64, 85)

GROUP_COLORS_RGB: Tuple[Tuple[int, int, int], ...] = (
    (255, 210, 63),   # 动态高置信度
    (138, 138, 92),   # 动态低置信度
    (55, 200, 255),   # 长时微动高置信度
    (43, 111, 138),   # 长时微动低置信度
    (124, 255, 107),  # 短时微动高置信度
    (63, 122, 58),    # 短时微动低置信度
)

#: 候选中文字体（找不到时界面自动切英文标签，避免显示成方块）
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]

#: 界面文案：有中文字体用中文，否则用英文
LABELS_ZH = {
    "title": "radarpi pygame 上位机",
    "start": "开始 scan start",
    "stop": "停止 scan stop",
    "clear": "清除",
    "pause": "暂停",
    "resume": "继续",
    "record": "开始录制",
    "recording": "停止录制",
    "save": "存矩阵",
    "quit": "退出",
    "view": "显示",
    "view_all": "全部点",
    "view_dyn": "仅动态点",
    "view_micro": "仅微动点",
    "stat_frame": "帧号",
    "stat_fps": "帧率",
    "stat_points": "本帧点数",
    "stat_rate": "数据率",
    "stat_period": "帧周期",
    "stat_bb": "BB处理",
    "stat_postbb": "Post-BB",
    "stat_transfer": "结果传输",
    "stat_array": "矩阵形状",
    "stat_proc": "处理器耗时",
    "groups_title": "各类点数",
    "table_title": "点表",
    "table_head": "   #      X(m)     Y(m)     Z(m)   v(m/s)      SNR",
    "bev_title": "BEV 矩阵（ndarray）",
    "side_xz": "XZ 侧视图",
    "side_yz": "YZ 正视图",
    "view3d": "3D 点云（拖动旋转 / 滚轮缩放）",
    "hint": "空格暂停  S开始  X停止  C清除  R录制  N存矩阵  1-4切换  P截图  Q退出",
    "connected": "已连接",
    "disconnected": "未连接",
    "no_data": "等待数据…",
    "recording_to": "录制中",
    "saved": "已保存",
    "groups": P.GROUP_NAMES,
    "track": "航迹",
}

LABELS_EN = {
    "title": "radarpi pygame console",
    "start": "Start scan",
    "stop": "Stop scan",
    "clear": "Clear",
    "pause": "Pause",
    "resume": "Resume",
    "record": "Record",
    "recording": "Stop rec",
    "save": "Save matrix",
    "quit": "Quit",
    "view": "View",
    "view_all": "All points",
    "view_dyn": "Dynamic",
    "view_micro": "Micro-motion",
    "stat_frame": "Frame",
    "stat_fps": "FPS",
    "stat_points": "Points",
    "stat_rate": "Rate",
    "stat_period": "Period",
    "stat_bb": "BB time",
    "stat_postbb": "Post-BB",
    "stat_transfer": "Transfer",
    "stat_array": "Matrix",
    "stat_proc": "Proc time",
    "groups_title": "Points by class",
    "table_title": "Point table",
    "table_head": "   #      X(m)     Y(m)     Z(m)   v(m/s)      SNR",
    "bev_title": "BEV matrix (ndarray)",
    "side_xz": "Side view XZ",
    "side_yz": "Front view YZ",
    "view3d": "3D point cloud (drag to rotate, wheel to zoom)",
    "hint": "SPACE pause  S start  X stop  C clear  R record  N save  P shot  Q quit",
    "connected": "connected",
    "disconnected": "disconnected",
    "no_data": "waiting for data...",
    "recording_to": "recording",
    "saved": "saved",
    "groups": ("Dyn high", "Dyn low", "LongMicro high", "LongMicro low", "ShortMicro high", "ShortMicro low"),
    "track": "Tracks",
}


class Fonts:
    """字体管理：优先中文字体，找不到就退回默认字体并用英文文案。"""

    def __init__(self) -> None:
        import pygame

        self.pygame = pygame
        # 必须先初始化字体子系统：否则 Font() 会抛 "font not initialized"，
        # 被下面的 except 吞掉后界面会静默退化成英文
        if not pygame.font.get_init():
            pygame.font.init()
        self.path: Optional[str] = None
        for cand in FONT_CANDIDATES:
            if os.path.exists(cand):
                try:
                    pygame.font.Font(cand, 14)
                    self.path = cand
                    break
                except Exception:
                    continue
        self.has_cjk = self.path is not None
        self._cache: Dict[Tuple[int, bool], "pygame.font.Font"] = {}
        self.labels = LABELS_ZH if self.has_cjk else LABELS_EN

    def get(self, size: int, bold: bool = False):
        key = (size, bold)
        if key not in self._cache:
            font = self.pygame.font.Font(self.path, size)
            if bold:
                font.set_bold(True)
            self._cache[key] = font
        return self._cache[key]


class Button:
    """工具栏按钮。"""

    def __init__(self, key: str, action: Callable[[], None], width: int = 104, hint: str = "") -> None:
        self.key = key
        self.action = action
        self.width = width
        self.hint = hint
        self.rect = (0, 0, 0, 0)
        self.active = False
        self.enabled = True

    def draw(self, surf, font, label: str) -> None:
        import pygame

        x, y, w, h = self.rect
        hovered = pygame.mouse.get_pos()[0] >= x and pygame.mouse.get_pos()[0] <= x + w \
            and pygame.mouse.get_pos()[1] >= y and pygame.mouse.get_pos()[1] <= y + h
        bg = SELECTED if self.active else (PANEL_LINE if hovered else (34, 40, 48))
        pygame.draw.rect(surf, bg, self.rect, border_radius=4)
        border = ACCENT if (self.active or hovered) else PANEL_LINE
        pygame.draw.rect(surf, border, self.rect, width=1, border_radius=4)
        text = font.render(label, True, ACCENT if self.active else FG)
        surf.blit(text, (x + (w - text.get_width()) // 2, y + (h - text.get_height()) // 2))

    def hit(self, pos) -> bool:
        x, y, w, h = self.rect
        return x <= pos[0] <= x + w and y <= pos[1] <= y + h


class RadarViewer:
    """pygame 上位机主窗口。"""

    VIEW_FILTERS = ("all", "dynamic", "micro", "tracks")

    def __init__(
        self,
        source,
        config: Optional[Config] = None,
        width: int = 1280,
        height: int = 720,
        fullscreen: bool = False,
        max_points: int = 4000,
        bev_resolution: float = 0.1,
        bev_mode: str = "count",
        bev_x_range: Tuple[float, float] = (-5.0, 5.0),
        bev_y_range: Tuple[float, float] = (0.0, 10.0),
        processor: Optional[Callable] = None,
        processor_name: str = "",
        buffer_frames: int = 30,
        record_format: str = "csv",
        record_dir: str = ".",
        snapshot: Optional[str] = None,
        snapshot_frames: int = 60,
        exit_after: float = 0.0,
    ) -> None:
        import pygame

        self.pygame = pygame
        self.source = source
        self.config = config or Config()
        self.width = width
        self.height = height
        self.fullscreen = fullscreen
        self.max_points = max_points
        self.bev_resolution = bev_resolution
        self.bev_mode = bev_mode
        self.bev_x_range = bev_x_range
        self.bev_y_range = bev_y_range
        self.processor = processor
        self.processor_name = processor_name
        self.buffer_frames = max(buffer_frames, 1)
        self.record_format = record_format
        self.record_dir = record_dir
        self.snapshot_path = snapshot
        self.snapshot_frames = snapshot_frames
        self.exit_after = exit_after

        self.fonts = Fonts()
        self.clock = pygame.time.Clock()
        self.running = True
        self.paused = False
        self.filter = "all"
        self.message = ""
        self.message_until = 0.0
        self.frame: Optional[P.Frame] = None
        self.array = None            # 当前帧 (N, 6) float32 矩阵
        self.bev = None              # 当前帧 BEV 矩阵
        self.proc_ms = 0.0
        self.frames_seen = 0
        self.fps = 0.0
        self._fps_window: List[float] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frame_buffer: List = []      # 最近若干帧的矩阵，供"存矩阵"使用
        self._buffer_meta: List[Dict[str, object]] = []
        self._recorder: Optional[Recorder] = None
        self._saved_count = 0
        self._snapshot_done = False
        self._shots = 0

        # 3D 视角
        self.yaw = -0.62
        self.pitch = 0.5
        self.dist = 7.2
        self.zoom = 1.0
        self.dragging = False
        self.drag_origin = (0, 0)
        self.drag_view = (0.0, 0.0)

        self.buttons: List[Button] = []
        self._build_ui()
        self.screen = None

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        L = self.fonts.labels
        def add(key, label_key, action, width=104):
            btn = Button(key, action, width)
            btn.label_key = label_key
            self.buttons.append(btn)

        add("start", "start", lambda: self.send_command("scan start"))
        add("stop", "stop", lambda: self.send_command("scan stop"))
        add("clear", "clear", self.clear_frame, 70)
        add("pause", "pause", self.toggle_pause, 70)
        add("view", "view", self.cycle_filter, 118)
        add("record", "record", self.toggle_record, 96)
        add("save", "save", self.save_buffer, 84)
        add("quit", "quit", self.stop, 70)

    # ------------------------------------------------------------------
    # 数据链路
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_loop, name="radarpi-viewer-reader", daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        import numpy as _np

        try:
            self.source.send(P.build_command("scan start"))
        except Exception as exc:  # 数据源可能不支持写（回放）
            self._say("scan start 未发送：%s" % exc)
        try:
            for frame in self.source.frames():
                if self._stop.is_set():
                    break
                points = self._to_array(frame)
                bev = None
                proc_ms = 0.0
                if self.processor is not None and _np is not None:
                    t0 = time.perf_counter()
                    try:
                        result = self.processor(points, frame)
                        if result is not None:
                            points = _np.asarray(result, dtype=_np.float32).reshape(-1, 6)
                    except Exception as exc:
                        self._say("处理器出错：%s" % exc, error=True)
                    proc_ms = (time.perf_counter() - t0) * 1000.0
                with self._lock:
                    self.frame = frame
                    self.array = points
                    self.proc_ms = proc_ms
                    self.frames_seen += 1
                    now = time.monotonic()
                    self._fps_window.append(now)
                    if len(self._fps_window) > 60:
                        del self._fps_window[:20]
                    self._frame_buffer.append(points)
                    self._buffer_meta.append(
                        {"frame_id": frame.frame_id, "timestamp": frame.timestamp,
                         "counts": list(frame.header.counts)}
                    )
                    if len(self._frame_buffer) > self.buffer_frames:
                        del self._frame_buffer[0]
                        del self._buffer_meta[0]
                if bev is None and _np is not None:
                    bev = self._make_bev(points)
                    with self._lock:
                        self.bev = bev
                rec = self._recorder
                if rec is not None:
                    try:
                        rec.write(frame)
                    except OSError as exc:
                        self._say("录制写入失败：%s" % exc, error=True)
        except Exception as exc:
            self._say("数据源中断：%s" % exc, error=True)

    def _to_array(self, frame: P.Frame):
        if np is None:
            return None
        from . import ndarray_iface as nd

        try:
            return nd.frame_to_ndarray(frame)
        except Exception:
            return None

    def _make_bev(self, points):
        if np is None or points is None:
            return None
        from . import ndarray_iface as nd

        try:
            return nd.to_bev(points, resolution=self.bev_resolution, mode=self.bev_mode,
                             x_range=self.bev_x_range, y_range=self.bev_y_range)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 交互动作
    # ------------------------------------------------------------------

    def send_command(self, text: str) -> None:
        try:
            self.source.send(P.build_command(text))
            self._say("已发送：%s" % text)
        except Exception as exc:
            self._say("发送失败：%s" % exc, error=True)

    def clear_frame(self) -> None:
        with self._lock:
            self.frame = None
            self.array = None
            self.bev = None
        self._say(self.fonts.labels["clear"])

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self._say(self.fonts.labels["resume" if self.paused else "pause"])

    def cycle_filter(self) -> None:
        index = (self.VIEW_FILTERS.index(self.filter) + 1) % len(self.VIEW_FILTERS)
        self.filter = self.VIEW_FILTERS[index]

    def toggle_record(self) -> None:
        L = self.fonts.labels
        if self._recorder is None:
            fmt = self.record_format
            path = default_filename(self._output_dir(), "jsonl" if fmt.startswith("json") else "csv")
            try:
                self._recorder = Recorder(path, fmt)
                self._say("%s %s" % (L["recording_to"], path))
            except OSError as exc:
                self._say("无法录制：%s" % exc, error=True)
        else:
            info = self._recorder.close()
            self._recorder = None
            self._say("录制结束：%d 帧 / %d 点 → %s" % (info["frames"], info["points"], info["path"]))

    def _output_dir(self) -> str:
        """录制/存矩阵的输出目录：不存在就尝试创建，实在不行退回当前目录。"""
        directory = self.record_dir or "."
        if os.path.isdir(directory):
            return directory
        try:
            os.makedirs(directory, exist_ok=True)
            return directory
        except OSError:
            return "."

    def save_buffer(self) -> None:
        """把缓冲区里的点云矩阵存成 .npz（一行一个矩阵，可直接 np.load 读取）。"""
        if np is None:
            self._say("需要 numpy 才能存矩阵", error=True)
            return
        with self._lock:
            frames = list(self._frame_buffer)
            meta = list(self._buffer_meta)
        if not frames:
            self._say("还没有数据可存", error=True)
            return
        self._saved_count += 1
        directory = self._output_dir()
        path = os.path.join(directory, time.strftime("points_%Y%m%d_%H%M%S.npz", time.localtime()))
        while os.path.exists(path):  # 同一秒内连点两次时避免覆盖
            self._saved_count += 1
            path = os.path.join(directory, time.strftime("points_%%Y%%m%%d_%%H%%M%%S_%d.npz" % self._saved_count,
                                                         time.localtime()))
        payload: Dict[str, object] = {"fields": np.asarray(POINT_FIELDS_SAFE)}
        for i, arr in enumerate(frames):
            payload["points_%04d" % i] = arr if arr is not None else np.zeros((0, 6), dtype=np.float32)
        if meta:
            payload["frame_ids"] = np.asarray([m["frame_id"] for m in meta], dtype=np.int64)
            payload["timestamps"] = np.asarray([m["timestamp"] for m in meta], dtype=np.float64)
            payload["counts"] = np.asarray([m["counts"] for m in meta], dtype=np.int32)
        try:
            np.savez_compressed(path, **payload)
            self._say("%s %d 帧 → %s" % (self.fonts.labels["saved"], len(frames), path))
        except OSError as exc:
            self._say("保存失败：%s" % exc, error=True)

    def save_snapshot(self, path: Optional[str] = None) -> None:
        """把当前画面存成 PNG（Pi 上没接显示器、用 VNC 时也好用）。"""
        import pygame

        if self.screen is None:
            return
        if not path:
            self._shots += 1
            path = os.path.join(self._output_dir(),
                                time.strftime("radarpi_%Y%m%d_%H%M%S.png", time.localtime()))
        try:
            pygame.image.save(self.screen, path)
            self._say("%s %s" % (self.fonts.labels["saved"], path))
        except Exception as exc:
            self._say("截图失败：%s" % exc, error=True)

    def _say(self, text: str, error: bool = False) -> None:
        self.message = ("! " if error else "") + text
        self.message_until = time.time() + 6.0

    def stop(self) -> None:
        self.running = False

    def close(self) -> None:
        self._stop.set()
        if self._recorder is not None:
            try:
                self._recorder.close()
            except Exception:
                pass
            self._recorder = None
        try:
            self.source.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _layout(self):
        """计算各面板矩形，返回 (toolbar, view3d, bev, xz, yz, side, status)。"""
        w, h = self.width, self.height
        tb_h = 40
        status_h = 24
        side_w = max(int(w * 0.26), 300)
        main_w = w - side_w - 3 * 6
        top_h = int((h - tb_h - status_h) * 0.56)
        bottom_h = h - tb_h - status_h - top_h - 18

        toolbar = (0, 0, w, tb_h)
        view3d = (6, tb_h + 6, main_w, top_h)
        bev = (6, tb_h + 6 + top_h + 6, main_w // 2 - 3, bottom_h)
        xz = (6 + main_w // 2 + 3, tb_h + 6 + top_h + 6, main_w // 4 - 3, bottom_h)
        yz = (6 + main_w // 2 + 3 + main_w // 4, tb_h + 6 + top_h + 6, main_w - main_w // 2 - main_w // 4 - 3, bottom_h)
        side = (w - side_w - 6, tb_h + 6, side_w, h - tb_h - status_h - 12)
        status = (0, h - status_h, w, status_h)
        return toolbar, view3d, bev, xz, yz, side, status

    def _panel(self, rect, title: str = "") -> None:
        import pygame

        x, y, w, h = rect
        pygame.draw.rect(self.screen, PANEL, rect, border_radius=6)
        pygame.draw.rect(self.screen, PANEL_LINE, rect, width=1, border_radius=6)
        if title:
            text = self.fonts.get(12).render(title, True, FG_DIM)
            self.screen.blit(text, (x + 8, y + 4))

    def _point_list(self) -> List[P.Point]:
        """按当前过滤器与处理器结果给出要渲染的点。"""
        with self._lock:
            frame, array = self.frame, self.array
        if frame is None:
            return []
        if array is not None and self.processor is not None:
            from . import ndarray_iface as nd

            points = nd.ndarray_to_points(array, frame)
        else:
            points = list(frame.points)
        if self.filter == "dynamic":
            points = [p for p in points if p.group <= 1]
        elif self.filter == "micro":
            points = [p for p in points if p.group >= 2]
        elif self.filter == "tracks":
            points = []
        return points

    def _project(self, x: float, y: float, z: float, rect) -> Optional[Tuple[float, float, float]]:
        """世界坐标 → 屏幕坐标（透视投影），返回 (sx, sy, 深度)。"""
        _, _, w, h = rect
        cy, sy_ = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        px = x * cy - y * sy_
        py = x * sy_ + y * cy
        pz = z - 1.0
        ry = py * cp + pz * sp
        rz = -py * sp + pz * cp
        depth = ry + self.dist
        if depth <= 0.25:
            return None
        focal = min(w, h * 1.35) * 1.05 * self.zoom
        s = focal / depth
        return (rect[0] + w / 2 + px * s, rect[1] + h / 2 - rz * s, depth)

    def _draw_3d(self, rect) -> None:
        import pygame

        self._panel(rect, self.fonts.labels["view3d"])
        clip = self.screen.get_clip()
        self.screen.set_clip(pygame.Rect(rect[0] + 1, rect[1] + 18, rect[2] - 2, rect[3] - 19))

        # 地面网格
        for i in range(-4, 5):
            a = self._project(i, -1.0, 0.0, rect)
            b = self._project(i, 6.0, 0.0, rect)
            if a and b:
                pygame.draw.line(self.screen, GRID, a[:2], b[:2])
            a = self._project(-4.0, float(i), 0.0, rect)
            b = self._project(4.0, float(i), 0.0, rect)
            if a and b:
                pygame.draw.line(self.screen, GRID, a[:2], b[:2])

        # 世界坐标轴
        for (ex, ey, ez), color, name in (
            ((1.2, 0, 0), (224, 108, 117), "X"),
            ((0, 1.2, 0), (152, 195, 121), "Y"),
            ((0, 0, 1.2), (97, 175, 239), "Z"),
        ):
            o = self._project(0, 0, 0, rect)
            e = self._project(ex, ey, ez, rect)
            if o and e:
                pygame.draw.line(self.screen, color, o[:2], e[:2], 2)
                self.screen.blit(self.fonts.get(12).render(name, True, color), (e[0] + 4, e[1] - 14))

        # 点云（按深度排序，远的先画）
        points = self._point_list()[: self.max_points]
        projected = []
        for p in points:
            q = self._project(p.x, p.y, p.z, rect)
            if q:
                projected.append((q[2], q[0], q[1], p))
        projected.sort(key=lambda item: -item[0])
        for depth, sx, sy, p in projected:
            color = GROUP_COLORS_RGB[p.group]
            if p.group % 2 == 1:  # 低置信度画暗一点
                color = tuple(int(c * 0.55) for c in color)
            radius = 2 if depth > 6 else 3
            pygame.draw.circle(self.screen, color, (int(sx), int(sy)), radius)

        # 航迹
        with self._lock:
            frame = self.frame
        if frame and self.filter != "tracks":
            for t in frame.tracks:
                q = self._project(t.x, t.y, t.z, rect)
                if q:
                    pygame.draw.circle(self.screen, (255, 255, 255), (int(q[0]), int(q[1])), 6, 1)
        self.screen.set_clip(clip)

    def _bev_surface(self, rect, bev):
        import pygame
        import pygame.surfarray as surfarray

        if bev is None or bev.size == 0:
            return None
        # 行 0 是最近处 —— 画图时把最近处放到底部（与雷达俯视图的习惯一致）
        data = bev[::-1, :]
        peak = float(data.max()) if data.size else 0.0
        if peak <= 0:
            return None
        norm = np.clip(data / peak, 0.0, 1.0)
        rgb = np.zeros((norm.shape[1], norm.shape[0], 3), dtype=np.uint8)  # surfarray 是 (x, y)
        rgb[..., 1] = (norm.T * 200).astype(np.uint8) + 30   # 绿色通道为主
        rgb[..., 2] = (norm.T * 120).astype(np.uint8)
        surf = surfarray.make_surface(rgb)
        return pygame.transform.smoothscale(surf, (rect[2] - 2, rect[3] - 20))

    def _draw_bev(self, rect) -> None:
        import pygame

        title = "%s  %s %.2fm" % (self.fonts.labels["bev_title"], self.bev_mode, self.bev_resolution)
        self._panel(rect, title)
        with self._lock:
            bev = None if self.bev is None else self.bev.copy()
        surf = self._bev_surface(rect, bev)
        if surf is None:
            text = self.fonts.get(13).render(self.fonts.labels["no_data"], True, FG_DIM)
            self.screen.blit(text, (rect[0] + 10, rect[1] + 30))
            return
        self.screen.blit(surf, (rect[0] + 1, rect[1] + 18))
        # 标注雷达位置（底部中央）与视野范围
        cx = rect[0] + rect[2] // 2
        pygame.draw.circle(self.screen, ACCENT, (cx, rect[1] + rect[3] - 4), 3)
        hint = "X %.0f~%.0f m   Y %.0f~%.0f m" % (
            self.bev_x_range[0], self.bev_x_range[1], self.bev_y_range[0], self.bev_y_range[1])
        self.screen.blit(self.fonts.get(11).render(hint, True, FG_DIM), (rect[0] + 8, rect[1] + 18))

    def _draw_side(self, rect, plane: str) -> None:
        import pygame

        title = self.fonts.labels["side_xz" if plane == "xz" else "side_yz"]
        self._panel(rect, title)
        pad = 24
        if plane == "xz":
            x_range, y_range = self.bev_x_range, (0.0, 3.0)
            def get(p):
                return p.x, p.z
            x_label, y_label = "X(m)", "Z(m)"
        else:
            x_range, y_range = self.bev_y_range, (0.0, 3.0)
            def get(p):
                return p.y, p.z
            x_label, y_label = "Y(m)", "Z(m)"

        px0, py0 = rect[0] + pad, rect[1] + 22
        pw, ph = rect[2] - pad - 8, rect[3] - pad - 26
        sx = pw / max(x_range[1] - x_range[0], 1e-6)
        sy = ph / max(y_range[1] - y_range[0], 1e-6)

        def to_px(a, b):
            return int(px0 + (a - x_range[0]) * sx), int(py0 + ph - (b - y_range[0]) * sy)

        for v in range(int(x_range[0]), int(x_range[1]) + 1):
            pygame.draw.line(self.screen, GRID, to_px(v, y_range[0]), to_px(v, y_range[1]))
        for v in range(int(y_range[0]), int(y_range[1]) + 1):
            pygame.draw.line(self.screen, GRID, to_px(x_range[0], v), to_px(x_range[1], v))

        for p in self._point_list()[: self.max_points]:
            a, b = get(p)
            if not (x_range[0] <= a <= x_range[1] and y_range[0] <= b <= y_range[1]):
                continue
            color = GROUP_COLORS_RGB[p.group]
            if p.group % 2 == 1:
                color = tuple(int(c * 0.55) for c in color)
            pygame.draw.circle(self.screen, color, to_px(a, b), 2)

        self.screen.blit(self.fonts.get(11).render(x_label, True, FG_DIM), (rect[0] + rect[2] - 40, rect[1] + rect[3] - 16))
        self.screen.blit(self.fonts.get(11).render(y_label, True, FG_DIM), (rect[0] + 4, rect[1] + 20))

    def _draw_side_panel(self, rect) -> None:
        import pygame

        self._panel(rect)
        x, y = rect[0] + 10, rect[1] + 8
        L = self.fonts.labels
        f12 = self.fonts.get(12)
        f13 = self.fonts.get(13)
        f11 = self.fonts.get(11)

        status = {}
        try:
            status = self.source.status()
        except Exception:
            status = {}
        connected = bool(status.get("connected"))

        def line(text, color=FG, font=f12, dy=17):
            nonlocal y
            self.screen.blit(font.render(text, True, color), (x, y))
            y += dy

        # 连接状态
        pygame.draw.circle(self.screen, OK if connected else ERR, (x + 5, y + 7), 5)
        dev = status.get("device") or "-"
        line("   %s   %s" % (L["connected"] if connected else L["disconnected"], dev))
        y += 4

        with self._lock:
            frame, array, proc_ms = self.frame, self.array, self.proc_ms
            seen = self.frames_seen
            window = list(self._fps_window)
        if len(window) >= 2:
            span = max(window[-1] - window[0], 1e-6)
            self.fps = (len(window) - 1) / span
        total = status.get("parse", {}).get("points", 0)

        def kv(k, v, color=FG):
            nonlocal y
            self.screen.blit(f11.render(k, True, FG_DIM), (x, y + 1))
            self.screen.blit(f13.render(str(v), True, color), (x + 96, y - 1))
            y += 19

        if frame is not None:
            kv(L["stat_frame"], "#%d" % frame.frame_id)
        kv(L["stat_fps"], "%.1f fps" % self.fps)
        kv(L["stat_points"], len(frame.points) if frame else 0)
        rate = status.get("bytes_per_sec", 0.0)
        kv(L["stat_rate"], ("%.1f KB/s" % (rate / 1024.0)) if rate else "-")
        if frame is not None:
            kv(L["stat_period"], "%d ms" % frame.header.frame_period)
            kv(L["stat_bb"], "%d ms" % frame.header.bb_time)
            kv(L["stat_postbb"], "%d ms" % frame.header.post_bb_time)
            kv(L["stat_transfer"], "%d ms" % frame.header.transfer_time)
        if array is not None:
            kv(L["stat_array"], "%d x %d" % (array.shape[0], array.shape[1]), ACCENT)
        if proc_ms:
            kv(L["stat_proc"], "%.2f ms" % proc_ms, ACCENT)

        y += 6
        self.screen.blit(f12.render(L["groups_title"], True, FG_DIM), (x, y))
        y += 18
        counts = frame.header.counts if frame else (0,) * 6
        for i in range(6):
            pygame.draw.rect(self.screen, GROUP_COLORS_RGB[i], (x, y + 4, 9, 9))
            self.screen.blit(f11.render(L["groups"][i], True, FG), (x + 15, y))
            self.screen.blit(f11.render(str(counts[i]), True, FG), (x + rect[2] - 50, y))
            y += 17
        pygame.draw.rect(self.screen, (255, 255, 255), (x, y + 4, 9, 9), 1)
        self.screen.blit(f11.render(L["track"], True, FG), (x + 15, y))
        self.screen.blit(f11.render(str(frame.header.track_count if frame else 0), True, FG), (x + rect[2] - 50, y))
        y += 22

        # 点表
        self.screen.blit(f12.render(L["table_title"], True, FG_DIM), (x, y))
        y += 18
        self.screen.blit(f11.render(L["table_head"], True, FG_DIM), (x, y))
        y += 16
        points = self._point_list()
        for i, p in enumerate(points[: max((rect[3] - (y - rect[1]) - 8) // 15, 0)]):
            color = GROUP_COLORS_RGB[p.group]
            text = "%4d %8.3f %8.3f %8.3f %7.2f %6d" % (i + 1, p.x, p.y, p.z, p.speed, p.snr)
            self.screen.blit(f11.render(text, True, color), (x, y))
            y += 15

    def _draw_toolbar(self, rect) -> None:
        import pygame

        pygame.draw.rect(self.screen, PANEL, rect)
        pygame.draw.line(self.screen, PANEL_LINE, (0, rect[3]), (self.width, rect[3]))
        L = self.fonts.labels
        f12 = self.fonts.get(12)
        f13 = self.fonts.get(13, bold=True)

        self.screen.blit(f13.render("radarpi", True, ACCENT), (10, rect[3] // 2 - 8))

        status = {}
        try:
            status = self.source.status()
        except Exception:
            status = {}
        connected = bool(status.get("connected"))
        cx = 96
        pygame.draw.circle(self.screen, OK if connected else ERR, (cx, rect[3] // 2), 5)
        dev = str(status.get("device") or status.get("kind") or "-")
        self.screen.blit(f12.render(dev[:24], True, FG_DIM), (cx + 12, rect[3] // 2 - 8))

        # 按钮排布
        bx = cx + 12 + max(90, f12.size(dev[:24])[0] + 16)
        for btn in self.buttons:
            if btn.key == "pause":
                btn.active = self.paused
            elif btn.key == "record":
                btn.active = self._recorder is not None
            btn.rect = (bx, 6, btn.width, rect[3] - 12)
            label = L[btn.label_key]
            if btn.key == "pause" and self.paused:
                label = L["resume"]
            elif btn.key == "record" and self._recorder is not None:
                label = L["recording"]
            elif btn.key == "view":
                label = {"all": L["view_all"], "dynamic": L["view_dyn"],
                         "micro": L["view_micro"], "tracks": L["track"]}[self.filter]
            btn.draw(self.screen, f12, label)
            bx += btn.width + 6

        # 右上角：FPS 与处理器名
        info = "%.1f fps" % self.fps
        if self.processor:
            info = "%s | %s" % (self.processor_name or "processor", info)
        text = f12.render(info, True, FG_DIM)
        self.screen.blit(text, (self.width - text.get_width() - 12, rect[3] // 2 - 8))

    def _draw_statusbar(self, rect) -> None:
        import pygame

        pygame.draw.rect(self.screen, PANEL, rect)
        pygame.draw.line(self.screen, PANEL_LINE, (0, rect[1]), (self.width, rect[1]))
        f11 = self.fonts.get(11)
        L = self.fonts.labels
        status = {}
        try:
            status = self.source.status()
        except Exception:
            status = {}
        parse = status.get("parse", {}) or {}
        text = "src=%s  frames=%s  resync=%s  dropped=%s B" % (
            status.get("kind", "-"), parse.get("frames", 0), parse.get("resyncs", 0),
            parse.get("dropped_bytes", 0))
        self.screen.blit(f11.render(text, True, FG_DIM), (10, rect[1] + 6))

        if time.time() < self.message_until and self.message:
            color = ERR if self.message.startswith("! ") else ACCENT
            msg = self.message[2:] if self.message.startswith("! ") else self.message
            surf = f11.render(msg, True, color)
            self.screen.blit(surf, (self.width - surf.get_width() - 12, rect[1] + 6))
        else:
            hint = f11.render(L["hint"], True, FG_DIM)
            self.screen.blit(hint, (self.width - hint.get_width() - 12, rect[1] + 6))

    def draw(self) -> None:
        import pygame

        rects = self._layout()
        self.screen.fill(BG)
        self._draw_3d(rects[1])
        self._draw_bev(rects[2])
        self._draw_side(rects[3], "xz")
        self._draw_side(rects[4], "yz")
        self._draw_side_panel(rects[5])
        self._draw_toolbar(rects[0])
        self._draw_statusbar(rects[6])
        pygame.display.flip()

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def handle_event(self, event) -> None:
        import pygame

        if event.type == pygame.QUIT:
            self.stop()
        elif event.type == pygame.KEYDOWN:
            key = event.key
            if key in (pygame.K_q, pygame.K_ESCAPE):
                self.stop()
            elif key == pygame.K_SPACE:
                self.toggle_pause()
            elif key == pygame.K_s:
                self.send_command("scan start")
            elif key == pygame.K_x:
                self.send_command("scan stop")
            elif key == pygame.K_c:
                self.clear_frame()
            elif key == pygame.K_r:
                self.toggle_record()
            elif key == pygame.K_n:
                self.save_buffer()
            elif key == pygame.K_p:
                self.save_snapshot()
            elif key == pygame.K_f:
                self.toggle_fullscreen()
            elif key in (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4):
                self.filter = self.VIEW_FILTERS[key - pygame.K_1]
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:
                for btn in self.buttons:
                    if btn.hit(event.pos):
                        btn.action()
                        return
                if self._in_3d(event.pos):
                    self.dragging = True
                    self.drag_origin = event.pos
                    self.drag_view = (self.yaw, self.pitch)
            elif event.button in (4, 5):
                if self._in_3d(event.pos):
                    self.zoom = max(0.3, min(4.0, self.zoom * (1.08 if event.button == 4 else 0.92)))
        elif event.type == pygame.MOUSEBUTTONUP:
            self.dragging = False
        elif event.type == pygame.MOUSEMOTION and self.dragging:
            dx = event.pos[0] - self.drag_origin[0]
            dy = event.pos[1] - self.drag_origin[1]
            self.yaw = self.drag_view[0] + dx * 0.008
            self.pitch = max(-0.4, min(1.4, self.drag_view[1] + dy * 0.006))
        elif event.type == pygame.VIDEORESIZE:
            self.width, self.height = max(event.w, 900), max(event.h, 600)
            self.screen = pygame.display.set_mode((self.width, self.height), pygame.RESIZABLE)

    def _in_3d(self, pos) -> bool:
        _, view3d, *_ = self._layout()
        x, y, w, h = view3d
        return x <= pos[0] <= x + w and y <= pos[1] <= y + h

    def toggle_fullscreen(self) -> None:
        import pygame

        self.fullscreen = not self.fullscreen
        self.screen = pygame.display.set_mode(
            (self.width, self.height), pygame.FULLSCREEN if self.fullscreen else pygame.RESIZABLE)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> int:
        import pygame

        pygame.init()
        if not pygame.font.get_init():
            pygame.font.init()
        flags = pygame.FULLSCREEN if self.fullscreen else pygame.RESIZABLE
        self.screen = pygame.display.set_mode((self.width, self.height), flags)
        pygame.display.set_caption("radarpi · 4D imaging radar")

        self.start()
        started = time.monotonic()
        drawn = 0
        try:
            while self.running:
                for event in pygame.event.get():
                    self.handle_event(event)
                if not self.paused:
                    self.draw()
                    drawn += 1
                self.clock.tick(30 if not self.exit_after else 0)

                # 无显示器环境下的自检：画够帧数就截图退出
                if self.snapshot_path and drawn >= self.snapshot_frames:
                    self.save_snapshot(self.snapshot_path)
                    break
                if self.exit_after and (time.monotonic() - started) >= self.exit_after:
                    break
        finally:
            self.close()
            pygame.quit()
        return 0


POINT_FIELDS_SAFE = ("x", "y", "z", "snr", "v", "group")


def build_source(args, config: Config):
    """与 CLI 其它子命令一致的数据源构造（真机 / 仿真 / 回放）。"""
    from .link import RadarLink, SimulatedLink
    from .recorder import ReplaySource

    if getattr(args, "simulate", False):
        return SimulatedLink(fps=getattr(args, "fps", 10.0) or 10.0)
    if getattr(args, "replay", None):
        return ReplaySource(args.replay, loop=args.loop, speed=args.speed)
    return RadarLink(
        device=args.device or config.get("serial", "device", "auto"),
        baudrate=int(args.baud or config.get_int("serial", "baudrate", BAUD_RATE)),
        profile=config.radar_profile(),
        auto_start=config.get_bool("serial", "auto_start", True),
        reconnect=config.get_bool("serial", "reconnect", True),
    )


def run_viewer(args, config: Config) -> int:
    """CLI 入口：radarpi view"""
    try:
        import pygame  # noqa: F401
    except ImportError:
        print("pygame 未安装。请执行：", file=sys.stderr)
        print("  Raspberry Pi OS:  sudo apt install python3-pygame python3-numpy", file=sys.stderr)
        print("  其它系统:         pip3 install pygame numpy", file=sys.stderr)
        print("也可以改用网页版上位机： radarpi serve", file=sys.stderr)
        return 2
    if np is None:
        print("pygame 界面需要 numpy（矩阵接口依赖它）：", file=sys.stderr)
        print("  Raspberry Pi OS:  sudo apt install python3-numpy", file=sys.stderr)
        return 2

    processor = None
    processor_name = ""
    if getattr(args, "processor", None):
        from . import ndarray_iface as nd

        try:
            processor = nd.load_processor(args.processor)
            processor_name = args.processor
            print("已加载处理器：%s" % args.processor)
        except Exception as exc:
            print("加载处理器失败：%s" % exc, file=sys.stderr)
            return 2

    source = build_source(args, config)
    record_dir = args.record_dir or config.get("record", "directory", ".")
    if not os.path.isdir(record_dir):
        try:
            os.makedirs(record_dir, exist_ok=True)
        except OSError:
            record_dir = "."
    viewer = RadarViewer(
        source,
        config=config,
        width=args.width,
        height=args.height,
        fullscreen=args.fullscreen,
        max_points=args.max_points,
        bev_resolution=args.bev_resolution,
        bev_mode=args.bev_mode,
        processor=processor,
        processor_name=processor_name,
        buffer_frames=args.buffer,
        record_format=args.record_format,
        record_dir=args.record_dir,
        snapshot=args.snapshot,
        snapshot_frames=args.snapshot_frames,
        exit_after=args.exit_after,
    )
    print("pygame 上位机已启动（%dx%d）。按 Q 或 ESC 退出。" % (args.width, args.height))
    return viewer.run()
