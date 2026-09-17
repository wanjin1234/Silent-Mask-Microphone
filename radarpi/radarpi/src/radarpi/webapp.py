"""内置 Web 上位机。

树莓派通常是无头（headless）运行，所以这里用一个纯标准库的 HTTP 服务替代
Windows 端的图形上位机：手机或电脑浏览器打开 ``http://<树莓派IP>:8080`` 即可
看到 3D 点云、三个投影视图、点表与实时统计。

接口一览
--------
``GET  /``                上位机页面
``GET  /api/status``      链路与解析状态
``GET  /api/frame``       最近一帧（一次性 JSON，便于用 curl 排查）
``GET  /api/stream``      SSE 实时帧流
``POST /api/command``     发送指令，例如 ``{"cmd": "scan start"}``
``GET  /api/config``      读取配置
``POST /api/config``      修改配置
``POST /api/record``      开始/停止录制
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import protocol as P
from .config import Config
from .recorder import Recorder, default_filename

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_SSE_QUEUE = 8


class RadarHub:
    """把数据源、广播、录制与状态管理集中在一处。"""

    def __init__(
        self,
        source,
        config: Config,
        max_points_sent: Optional[int] = None,
        max_fps: Optional[float] = None,
        with_tracks: Optional[bool] = None,
    ) -> None:
        self.source = source
        self.config = config
        self.max_points_sent = max_points_sent if max_points_sent is not None else config.get_int("web", "max_points_sent", 4000)
        self.max_fps = max_fps if max_fps is not None else config.get_float("web", "max_fps", 15.0)
        self.with_tracks = with_tracks if with_tracks is not None else config.get_bool("web", "tracks", True)

        self._subscribers: List[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._latest: Optional[dict] = None
        self._frame_events = 0
        self._publish_times: List[float] = []
        self._last_publish = 0.0
        self._frame_fps = 0.0
        self._recorder: Optional[Recorder] = None
        self._recorder_lock = threading.Lock()
        self._record_info: Dict[str, object] = {}
        self._started_at = time.time()

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="radarpi-reader", daemon=True)
        self._thread.start()
        if self.config.get_bool("record", "autostart", False):
            self.record_start()

    def stop(self) -> None:
        self._stop.set()
        self.record_stop()
        try:
            self.source.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            for frame in self.source.frames():
                if self._stop.is_set():
                    break
                self._handle_frame(frame)
        except Exception as exc:  # 数据源异常不应导致整站退出
            self._publish({"type": "error", "message": str(exc)})

    def _handle_frame(self, frame: P.Frame) -> None:
        with self._recorder_lock:
            recorder = self._recorder
        if recorder is not None:
            try:
                recorder.write(frame)
            except OSError as exc:
                self._publish({"type": "error", "message": "录制写入失败：%s" % exc})

        now = time.monotonic()
        self._frame_events += 1
        self._publish_times.append(now)
        if len(self._publish_times) > 60:
            del self._publish_times[:20]

        # 限流：既要保证浏览器跟得上，也要保证不把树莓派和网络带宽吃满
        if self.max_fps > 0 and (now - self._last_publish) < (1.0 / self.max_fps):
            return
        self._last_publish = now
        payload = frame.as_dict(max_points=self.max_points_sent or None, with_tracks=self.with_tracks)
        self._latest = payload
        self._publish({"type": "frame", "data": payload})

    def _publish(self, message: dict) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(message)
            except queue.Full:
                # 浏览器卡顿时丢掉最旧的一帧，保证画面始终追得上最新数据
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except queue.Empty:
                    pass

    # -- 订阅 -------------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=MAX_SSE_QUEUE)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    @property
    def subscribers(self) -> int:
        with self._sub_lock:
            return len(self._subscribers)

    # -- 指令与状态 -------------------------------------------------------

    def send_command(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "指令为空"}
        try:
            self.source.send(P.build_command(text))
        except Exception as exc:
            return {"ok": False, "error": str(exc), "cmd": text}
        return {"ok": True, "cmd": text, "at": time.time()}

    def send_key(self, key: str, values: Optional[List[str]] = None) -> dict:
        try:
            data = P.command_from_key(key, values or [])
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        try:
            self.source.send(data)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "cmd": data.decode("ascii", "replace").strip()}
        return {"ok": True, "cmd": data.decode("ascii", "replace").strip()}

    def status(self) -> dict:
        src = {}
        try:
            src = self.source.status()
        except Exception as exc:
            src = {"connected": False, "last_error": str(exc)}
        now = time.monotonic()
        recent = [t for t in self._publish_times if now - t <= 5.0]
        self._frame_fps = len(recent) / 5.0 if recent else 0.0
        with self._recorder_lock:
            rec = self._record_info
        return {
            "source": src,
            "web": {
                "clients": self.subscribers,
                "frame_fps": round(self._frame_fps, 2),
                "max_points_sent": self.max_points_sent,
                "max_fps": self.max_fps,
                "with_tracks": self.with_tracks,
                "uptime": round(time.time() - self._started_at, 1),
            },
            "record": rec,
            "config_path": self.config.path,
            "commands": [c.as_dict() for c in P.COMMANDS],
            "groups": [
                {"index": i, "key": k, "name": n, "short": s, "color": c}
                for i, (k, n, s, c) in enumerate(zip(P.GROUP_KEYS, P.GROUP_NAMES, P.GROUP_SHORT_NAMES, P.GROUP_COLORS))
            ],
        }

    def latest(self) -> Optional[dict]:
        return self._latest

    # -- 录制 -------------------------------------------------------------

    def record_start(self, fmt: Optional[str] = None, path: Optional[str] = None) -> dict:
        with self._recorder_lock:
            if self._recorder is not None:
                return {"ok": False, "error": "已在录制中", "path": self._recorder.path}
            fmt = (fmt or self.config.get("record", "format", "csv")).lower()
            directory = self.config.get("record", "directory", ".")
            if not path:
                try:
                    path = default_filename(directory, "jsonl" if fmt.startswith("json") else "csv")
                except OSError as exc:
                    return {"ok": False, "error": "无法创建录制目录 %s：%s" % (directory, exc)}
            try:
                self._recorder = Recorder(path, fmt)
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            self._record_info = {"recording": True, "path": self._recorder.path, "format": fmt}
            return {"ok": True, "path": self._recorder.path, "format": fmt}

    def record_stop(self) -> dict:
        with self._recorder_lock:
            rec = self._recorder
            self._recorder = None
            if rec is None:
                return {"ok": False, "error": "当前没有在录制"}
            info = rec.close()
            self._record_info = {"recording": False, **info}
            return {"ok": True, **info}

    @property
    def record_info(self) -> dict:
        with self._recorder_lock:
            return dict(self._record_info)


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "radarpi"
    protocol_version = "HTTP/1.1"

    hub: RadarHub  # 由 create_server 注入

    # -- 工具 -------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # 默认日志太吵，只在 debug 下输出
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _send_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._send_json({"error": "not found", "path": path}, 404)
            return
        ctype, _ = mimetypes.guess_type(path)
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", (ctype or "application/octet-stream") + ("; charset=utf-8" if ctype and ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send_file(os.path.join(STATIC_DIR, "index.html"))
        elif route.startswith("/static/"):
            name = os.path.basename(route)
            self._send_file(os.path.join(STATIC_DIR, name))
        elif route == "/api/status":
            self._send_json(self.hub.status())
        elif route == "/api/frame":
            frame = self.hub.latest()
            if frame is None:
                self._send_json({"ok": False, "error": "还没有收到任何帧"}, 503)
            else:
                self._send_json({"ok": True, "frame": frame})
        elif route == "/api/config":
            self._send_json({"ok": True, "path": self.hub.config.path, "items": self.hub.config.describe()})
        elif route == "/api/stream":
            self._stream(parse_qs(parsed.query))
        elif route == "/api/port":
            from .serialport import list_ports

            self._send_json({"ok": True, "ports": [p.as_dict() for p in list_ports()]})
        else:
            self._send_json({"error": "unknown route", "path": route}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/api/command":
            if "key" in body:
                result = self.hub.send_key(body["key"], body.get("values") or [])
            else:
                result = self.hub.send_command(body.get("cmd", ""))
            self._send_json(result, 200 if result.get("ok") else 400)
        elif parsed.path == "/api/config":
            changed = []
            for item in body.get("items", []):
                try:
                    self.hub.config.set(item["section"], item["key"], item["value"])
                    changed.append("%s.%s" % (item["section"], item["key"]))
                except (KeyError, TypeError):
                    continue
            if body.get("save"):
                try:
                    path = self.hub.config.save()
                except OSError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, 500)
                    return
                self._send_json({"ok": True, "saved": path, "changed": changed})
            else:
                self._send_json({"ok": True, "changed": changed})
        elif parsed.path == "/api/record":
            action = body.get("action", "start")
            if action == "start":
                self._send_json(self.hub.record_start(body.get("format"), body.get("path")), 200)
            else:
                self._send_json(self.hub.record_stop(), 200)
        else:
            self._send_json({"error": "unknown route", "path": parsed.path}, 404)

    # -- SSE --------------------------------------------------------------

    def _stream(self, query: Dict[str, List[str]]) -> None:
        max_points = None
        if "max_points" in query:
            try:
                max_points = max(int(query["max_points"][0]), 0)
            except ValueError:
                max_points = None
        fps = None
        if "fps" in query:
            try:
                fps = float(query["fps"][0])
            except ValueError:
                fps = None
        if max_points is not None:
            self.hub.max_points_sent = max_points
        if fps is not None:
            self.hub.max_fps = fps

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = self.hub.subscribe()
        try:
            self._sse_event("hello", self.hub.status())
            last_status = time.monotonic()
            while True:
                try:
                    message = q.get(timeout=1.0)
                except queue.Empty:
                    # 心跳，防止代理/浏览器判定连接超时
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    if time.monotonic() - last_status > 2.0:
                        self._sse_event("status", self.hub.status())
                        last_status = time.monotonic()
                    continue
                self._sse_event(message.get("type", "message"), message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            pass
        except Exception:
            pass
        finally:
            self.hub.unsubscribe(q)

    def _sse_event(self, event: str, payload) -> None:
        if event == "frame" and isinstance(payload, dict) and "data" in payload:
            payload = payload["data"]
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        chunk = ("event: %s\ndata: %s\n\n" % (event, body)).encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()


def create_server(hub: RadarHub, host: str = "0.0.0.0", port: int = 8080, verbose: bool = False) -> ThreadingHTTPServer:
    handler = type("RadarHandler", (_Handler,), {"hub": hub})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.verbose = verbose
    return server


def serve(hub: RadarHub, host: str = "0.0.0.0", port: int = 8080, verbose: bool = False) -> None:
    """阻塞式启动 Web 服务。"""
    hub.start()
    server = create_server(hub, host, port, verbose)
    print("radarpi Web 上位机已启动： http://%s:%d/" % (detect_lan_ip() if host == "0.0.0.0" else host, port))
    print("按 Ctrl+C 退出。")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        server.shutdown()
        server.server_close()
        hub.stop()


def detect_lan_ip() -> str:
    """猜一个局域网地址，用于打印访问地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"
