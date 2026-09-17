"""配置文件读写。

默认路径：``/etc/radarpi/radarpi.conf``（系统安装），普通用户可用
``~/.config/radarpi/radarpi.conf`` 覆盖。文件为 INI 格式，便于手工编辑。
"""

from __future__ import annotations

import configparser
import os
from typing import Dict, List, Optional

SYSTEM_CONF = "/etc/radarpi/radarpi.conf"
USER_CONF = os.path.join(os.path.expanduser("~"), ".config", "radarpi", "radarpi.conf")

DEFAULTS: Dict[str, Dict[str, str]] = {
    "serial": {
        "device": "auto",
        "baudrate": "3000000",
        "read_timeout": "0.2",
        "auto_start": "true",
        "reconnect": "true",
        "keepalive": "5.0",
    },
    "radar": {
        # 以下各项与手册「成像雷达启动配置指令说明」一一对应
        # 注意：已实测下发这些 set 指令会让雷达运行一段时间后挂死（元凶待逐条排查），
        # 故默认关闭自动下发，仅发 scan start 保活即可长期稳定运行。
        "apply_on_connect": "false",
        "radar_height": "1.7",
        "radar_inclination": "30",
        "boundary": "-3 3 0.8 0.2 5",
        "mmsinterval": "2 34",
        "cfar_coeff": "33 44 55",
    },
    "web": {
        "host": "0.0.0.0",
        "port": "8080",
        "max_points_sent": "4000",
        "max_fps": "15",
        "tracks": "true",
    },
    "record": {
        "directory": "/var/lib/radarpi",
        "format": "csv",
        "autostart": "false",
    },
}

SECTION_TITLES = {
    "serial": "串口",
    "radar": "雷达参数",
    "web": "Web 上位机",
    "record": "录制",
}

#: 会下发给雷达的配置项（顺序即为上电后的下发顺序）
RADAR_COMMAND_ORDER = ("radar_height", "radar_inclination", "boundary", "mmsinterval", "cfar_coeff")


def _to_bool(text: str, default: bool = False) -> bool:
    if text is None:
        return default
    return str(text).strip().lower() in ("1", "true", "yes", "on", "y")


class Config:
    """雷达上位机配置。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or self.default_path()
        self._cp = configparser.ConfigParser()
        for section, values in DEFAULTS.items():
            self._cp[section] = dict(values)
        self.loaded_from: Optional[str] = None
        self.load()

    # -- 路径 -------------------------------------------------------------

    @staticmethod
    def default_path() -> str:
        if os.path.exists(SYSTEM_CONF):
            return SYSTEM_CONF
        if os.path.exists(USER_CONF):
            return USER_CONF
        return SYSTEM_CONF

    # -- 读写 -------------------------------------------------------------

    def load(self, path: Optional[str] = None) -> None:
        target = path or self.path
        if target and os.path.exists(target):
            try:
                self._cp.read(target, encoding="utf-8")
                self.loaded_from = target
                self.path = target
            except (OSError, configparser.Error) as exc:
                raise SystemExit("读取配置 %s 失败：%s" % (target, exc))
        elif path:
            raise SystemExit("配置文件不存在：%s" % target)

    def save(self, path: Optional[str] = None) -> str:
        target = path or self.path
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("# radarpi 配置文件\n")
            fh.write("# 由 `radarpi config set` 或手工编辑，修改后需重启服务生效\n\n")
            self._cp.write(fh)
        self.path = target
        return target

    # -- 访问 -------------------------------------------------------------

    def get(self, section: str, key: str, fallback: str = "") -> str:
        return self._cp.get(section, key, fallback=fallback)

    def get_bool(self, section: str, key: str, fallback: bool = False) -> bool:
        return _to_bool(self._cp.get(section, key, fallback=str(fallback)), fallback)

    def get_int(self, section: str, key: str, fallback: int = 0) -> int:
        try:
            return int(float(self._cp.get(section, key, fallback=str(fallback))))
        except (TypeError, ValueError):
            return fallback

    def get_float(self, section: str, key: str, fallback: float = 0.0) -> float:
        try:
            return float(self._cp.get(section, key, fallback=str(fallback)))
        except (TypeError, ValueError):
            return fallback

    def set(self, section: str, key: str, value: object) -> None:
        if section not in self._cp:
            self._cp[section] = {}
        self._cp[section][key] = str(value)

    def as_dict(self) -> Dict[str, Dict[str, str]]:
        return {s: dict(self._cp[s]) for s in self._cp.sections()}

    def describe(self) -> List[dict]:
        """带中文说明的配置项列表，供 Web 界面直接渲染。"""
        hints = {
            ("serial", "device"): "串口设备，auto 表示自动识别",
            ("serial", "baudrate"): "波特率，手册要求 3000000",
            ("serial", "auto_start"): "连上后自动下发 scan start",
            ("serial", "reconnect"): "断线自动重连",
            ("serial", "keepalive"): "保活间隔（秒），周期性重发 scan start 防雷达扫描超时，0 关闭",
            ("serial", "read_timeout"): "串口读超时（秒）",
            ("radar", "apply_on_connect"): "连接后自动下发下面各项雷达参数",
            ("radar", "radar_height"): "雷达安装高度（m）",
            ("radar", "radar_inclination"): "安装倾斜角度（度）",
            ("radar", "boundary"): "点云输出范围，5 个数值",
            ("radar", "mmsinterval"): "长时/短时微动点处理间隔（帧）",
            ("radar", "cfar_coeff"): "动态/短时微动/长时微动门限系数",
            ("web", "host"): "Web 监听地址，0.0.0.0 表示允许局域网访问",
            ("web", "port"): "Web 监听端口",
            ("web", "max_points_sent"): "每帧最多推送给浏览器的点数",
            ("web", "max_fps"): "推送给浏览器的最高帧率",
            ("web", "tracks"): "是否推送航迹包",
            ("record", "directory"): "录制文件默认目录",
            ("record", "format"): "录制格式 csv 或 jsonl",
            ("record", "autostart"): "服务启动后立即开始录制",
        }
        out: List[dict] = []
        for section in self._cp.sections():
            for key in self._cp[section]:
                out.append(
                    {
                        "section": section,
                        "section_title": SECTION_TITLES.get(section, section),
                        "key": key,
                        "value": self._cp.get(section, key),
                        "hint": hints.get((section, key), ""),
                    }
                )
        return out

    def radar_profile(self) -> List[str]:
        """按手册格式生成需要下发给雷达的指令文本列表。"""
        if not self.get_bool("radar", "apply_on_connect", True):
            return []
        profile: List[str] = []
        for key in RADAR_COMMAND_ORDER:
            value = self.get("radar", key, "").strip()
            if not value:
                continue
            if key == "radar_height":
                profile.append("set radar_height %s" % value)
            elif key == "radar_inclination":
                profile.append("set radar_inclination %s" % value)
            elif key == "boundary":
                profile.append("set boundary %s" % value)
            elif key == "mmsinterval":
                profile.append("set_mmsinterval %s" % value)
            elif key == "cfar_coeff":
                profile.append("set_cfar_coeff %s" % value)
        return profile
