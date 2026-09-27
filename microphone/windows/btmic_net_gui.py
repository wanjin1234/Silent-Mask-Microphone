#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
树莓派网络麦克风 · Windows 端（图形界面）

和树莓派上的 final_btmic.py --net 配套使用：

    树莓派                                      Windows（本程序）
    reSpeaker 麦克风 → 降噪 → 增益 ──上行──▶  播放（可选录音到 WAV）
    耳机口 ◀──下行── 电脑声音（系统回环 或 麦克风）

两种连接方式（树莓派侧都由它主动发起，因为 4G 是运营商大内网、外面连不进去）：
  * TCP 直连：本程序监听端口，树莓派用 "电脑的 IP + 端口" 连过来。两边在同一个
    局域网（同一 WiFi/热点）时最省事、延迟最低；要跨互联网则需要电脑有公网 IP
    或路由器把端口映射进来。
  * MQTT 中继：两边都主动连一个 MQTT broker（默认公共 broker.emqx.io），因此
    **不需要公网 IP、不需要端口映射**，4G 侧和电脑都在 NAT 后面也能互通。公共
    broker 只做转发，令牌 = 主题名，避免和别人串音。

依赖：pip install soundcard numpy   （tkinter 是 Python 自带的）
运行：python btmic_net_gui.py
"""

import json
import math
import os
import queue
import random
import select
import socket
import struct
import sys
import threading
import time
import wave
from collections import deque

# 打包成 --noconsole 的 exe 后没有控制台：sys.stdout/sys.stderr 是 None，
# 任何 print/写 stderr 都会抛 AttributeError 直接把程序搞崩。所以在最前面兜住，
# 顺便把输出落一份到 exe 旁边的启动日志，出问题时有据可查。
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w")
    sys.stdout = sys.stdout or _devnull
    sys.stderr = sys.stderr or _devnull

try:
    import tkinter as tk
    from tkinter import filedialog, ttk
except ImportError:  # 极少见：Python 未带 tkinter
    print("当前 Python 没有 tkinter，无法启动图形界面。")
    print("Windows 官方安装包默认自带；若用的是精简版 Python，请改装 python.org 版本。")
    sys.exit(1)

APP_TITLE = "树莓派网络麦克风 · Windows 端"
def _app_dir():
    """程序所在目录。

    打包成单文件 exe 时，__file__ 指向临时解包目录（退出即删），配置写在那里会丢；
    所以冻结状态下用 exe 自己的目录，配置/录音都落在 exe 旁边。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


CONF_PATH = os.path.join(_app_dir(), "btmic_net_gui.json")

DEFAULT_CONF = {
    "transport": "tcp",
    "tcp_port": 5010,
    "mqtt_host": "broker.emqx.io",
    "mqtt_port": 1883,
    "mqtt_host2": "broker.emqx.io",
    "mqtt_port2": 1883,
    "mqtt_prefix": "btmic",
    "token": "",
    "play_device": "",
    "play_volume": 80,
    "play_mute": 0,
    "rec_device": "",
    "rec_volume": 100,
    "rec_mute": 0,
    "remote_gain": 15,
    "record_wav": 0,
    "wav_path": "",
    "auto_start": 0,
    "down_rate": 48000,
    "down_channels": 2,
}

# ---- 与树莓派 net_bridge.py 完全一致的帧格式 ----
MAGIC = b"BMND"
T_AUDIO = 1
T_JSON = 2
HEADER_LEN = 12
MAX_PAYLOAD = 4 * 1024 * 1024
DEFAULT_KEEPALIVE = 20
PEER_TIMEOUT = 12.0     # 秒：多久收不到树莓派任何数据就认为它掉线（它每秒发一次统计）
TARGET_MS = 150.0       # 上行播放的目标缓冲深度（越低越跟手，越容易断音）
TX_QUEUE_MS = 600.0     # 待发送队列上限：链路卡顿时丢最旧，不拖慢采集
TRIM_MS = 450.0         # 上行积压超过这个深度就裁剪回去（见 BaseLink.take_audio）     # 秒：多久收不到树莓派任何数据就认为它掉线（它每秒发一次统计）
HB_INTERVAL = 1.0       # 秒：自己给树莓派发心跳的间隔


def now_str():
    return time.strftime("%H:%M:%S")


def log_dB(level):
    if level is None or level <= 0:
        return "-inf"
    return "%.1f" % (20.0 * math.log10(level / 32768.0))


# ---------------- 帧编解码（与 net_bridge.py 同一套） ----------------


def pack_frame(ftype, payload):
    return struct.pack("<4sBBHI", MAGIC, ftype, 0, 0, len(payload)) + payload


def pack_json(obj):
    return pack_frame(T_JSON, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def pack_audio(payload):
    return pack_frame(T_AUDIO, payload)


class FrameParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            if len(self.buf) < HEADER_LEN:
                return out
            if bytes(self.buf[:4]) != MAGIC:
                del self.buf[0]
                continue
            ftype, _flags, _seq, length = struct.unpack_from("<BBHI", self.buf, 4)
            if length > MAX_PAYLOAD:
                del self.buf[0]
                continue
            if len(self.buf) < HEADER_LEN + length:
                return out
            payload = bytes(self.buf[HEADER_LEN : HEADER_LEN + length])
            del self.buf[: HEADER_LEN + length]
            out.append((ftype, payload))


# ---------------- MQTT 3.1.1 最小客户端（只用 QoS0，与树莓派侧同一实现） ----------------


def _varint(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class MqttParser:
    @staticmethod
    def _decode_len(buf, idx):
        mult, length, i = 1, 0, idx
        for _ in range(4):
            if i >= len(buf):
                return None, i
            b = buf[i]
            i += 1
            length += (b & 0x7F) * mult
            if not (b & 0x80):
                return length, i
            mult *= 128
        return -1, idx

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while self.buf:
            length, idx = self._decode_len(self.buf, 1)
            if length is None:
                return out
            if length < 0:
                del self.buf[0]
                continue
            if len(self.buf) < idx + length:
                return out
            head = self.buf[0]
            body = bytes(self.buf[idx : idx + length])
            del self.buf[: idx + length]
            out.append((head, body))
        return out


class MiniMQTT:
    def __init__(self, host, port, client_id, keepalive=DEFAULT_KEEPALIVE):
        self.host, self.port = host, int(port)
        self.client_id = client_id
        self.keepalive = keepalive
        self.sock = None
        self.parser = MqttParser()
        self.packets = deque()
        self.messages = deque()
        self.lock = threading.Lock()
        self.last_send = 0.0
        self.last_recv = 0.0

    def _send_raw(self, raw):
        with self.lock:
            if self.sock is None:
                raise OSError("MQTT 未连接")
            self.sock.sendall(raw)
            self.last_send = time.time()

    def _pump_one(self, timeout=0.5):
        try:
            ready, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            return False
        if not ready:
            return True
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return True
        except OSError:
            return False
        if not data:
            return False
        self.last_recv = time.time()
        for pkt in self.parser.feed(data):
            self.packets.append(pkt)
        return True

    def connect(self, timeout=10):
        self.sock = socket.create_connection((self.host, self.port), timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 加大收发缓冲：音频线程是"C 里带 sleep 的忙等"，会占住 GIL，读线程偶尔
        # 被饿一下；缓冲太小就直接丢消息（公共 broker 对慢消费者不走 QoS0 重发）
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        except OSError:
            pass
        self.sock.settimeout(timeout)   # 握手期间用超时
        cid = self.client_id.encode("utf-8")
        var = b"\x00\x04MQTT\x04" + b"\x02" + struct.pack("!H", self.keepalive)
        payload = struct.pack("!H", len(cid)) + cid
        self._send_raw(b"\x10" + _varint(len(var) + len(payload)) + var + payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.packets:
                head, body = self.packets.popleft()
                if head >> 4 != 2:
                    raise OSError("CONNACK 期望值不符: 0x%02x" % head)
                if len(body) >= 2 and body[1] != 0:
                    raise OSError("MQTT 连接被拒绝（返回码 %d）" % body[1])
                # 握手后切回阻塞，读线程改用 select（settimeout 是整条 socket
                # 的属性，会波及另一个线程的 sendall → 发送被误判为超时失败）
                self.sock.settimeout(None)
                return True
            if not self._pump_one(0.5):
                raise OSError("连接 broker 后立刻断开")
        raise OSError("等待 CONNACK 超时")

    def subscribe(self, topics, pid=1):
        if isinstance(topics, str):
            topics = [topics]
        var = struct.pack("!H", pid)
        for t in topics:
            raw = t.encode("utf-8")
            var += struct.pack("!H", len(raw)) + raw + b"\x00"
        self._send_raw(b"\x82" + _varint(len(var)) + var)

    def publish(self, topic, payload):
        raw = topic.encode("utf-8")
        var = struct.pack("!H", len(raw)) + raw + payload
        self._send_raw(b"\x30" + _varint(len(var)) + var)

    def ping(self):
        self._send_raw(b"\xc0\x00")

    def maybe_ping(self):
        if time.time() - self.last_send > max(5.0, self.keepalive * 0.6):
            try:
                self.ping()
            except OSError:
                return False
        return True

    def pump(self, timeout=0.5):
        if self.sock is None:
            return False
        if not self._pump_one(timeout):
            return False
        while self.packets:
            head, body = self.packets.popleft()
            kind = head >> 4
            if kind == 3 and len(body) >= 2:
                qos = (head >> 1) & 0x03
                tlen = struct.unpack_from("!H", body, 0)[0]
                topic = body[2 : 2 + tlen].decode("utf-8", "replace")
                rest = body[2 + tlen :]
                if qos > 0:
                    rest = rest[2:]
                self.messages.append((topic, rest))
            elif kind == 13:
                try:
                    self._send_raw(b"\xd0\x00")
                except OSError:
                    return False
        return True

    def close(self):
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


# ---------------- 链路（树莓派 → 电脑 / 电脑 → 树莓派） ----------------


class BaseLink:
    """一条通道：把音频与控制帧收进队列，提供线程安全的发送。"""

    kind = "?"

    def __init__(self, channel, on_hello, on_event):
        self.channel = channel          # "up" = 树莓派发音频给电脑；"down" = 反过来
        self.on_hello = on_hello        # 收到树莓派 hello 时回调（携带音频格式）
        self.on_event = on_event        # 状态文本（写日志）
        self.audio_q = deque()
        self.ctrl_q = deque()
        self.audio_lock = threading.Lock()
        self.audio_cap = 25           # 音频队列上限（帧）；知道帧长后按 1 秒折算
        self.trim_at = 12             # 积压超过这么多帧就裁剪（≈450ms，set_fmt 里折算）
        self.trim_to = 4              # 裁剪到这么多帧（≈150ms）
        # 待发送队列：采集线程只管入队，发送由独立线程做。publish()/sendall()
        # 在链路慢时会阻塞几百毫秒，直接放在采集线程里会连采集一起拖慢（实测
        # 采集速率掉到 5.5 帧/秒，本该 8.3），声音反而越攒越晚
        self.tx_q = deque()
        self.tx_cap = 6               # 最多压 ~600ms，超了丢最旧
        self.tx_dropped = 0
        self.tx_bytes = 0
        self._sender = None
        self.dead = threading.Event()
        self.handshaked = False
        self.last_peer = 0.0
        self.peer = None
        self.fmt = {}          # 树莓派宣布的音频格式（hello 里带）
        self.sent_frames = 0
        self.sent_bytes = 0
        self.dropped = 0
        self.recv_frames = 0
        self.last_level = None
        self.rtt_ms = None
        self.stat = {}
        self.start_t = time.time()

    # ---- 队列 ----
    def push_audio(self, payload, cap=None):
        with self.audio_lock:
            self.audio_q.append(payload)
            limit = cap or self.audio_cap
            while len(self.audio_q) > limit:
                self.audio_q.popleft()
                self.dropped += 1

    def push_ctrl(self, obj):
        if obj.get("t") == "hello":
            self.peer = obj.get("name") or self.peer
        if obj.get("t") == "stat":
            self.stat = obj
            if obj.get("level_db") is not None:
                self.last_level = obj["level_db"]
        if obj.get("t") == "pong" and obj.get("ts"):
            self.rtt_ms = int(time.time() * 1000) - int(obj["ts"])
        self.last_peer = time.time()
        with self.audio_lock:
            self.ctrl_q.append(obj)
            while len(self.ctrl_q) > 200:
                self.ctrl_q.popleft()

    def start_sender(self):
        if self._sender is None:
            self._sender = threading.Thread(target=self._send_loop, daemon=True)
            self._sender.start()

    def _send_loop(self):
        while not self.dead.is_set():
            with self.audio_lock:
                data = self.tx_q.popleft() if self.tx_q else None
            if data is None:
                time.sleep(0.005)
                continue
            try:
                if not self._send_audio_now(data):
                    # 没发出去就别计入"已发送"（否则界面上帧数好看、对端收不到）
                    time.sleep(0.05)
                    with self.audio_lock:
                        self.tx_q.appendleft(data)
                        self.tx_dropped += 1
                        while len(self.tx_q) > self.tx_cap:
                            self.tx_q.pop()
                            self.tx_dropped += 1
            except OSError as exc:
                self.on_event("发送失败（%s）" % exc)
                with self.audio_lock:
                    self.tx_q.appendleft(data)
                time.sleep(0.2)

    def _send_audio_now(self, data):
        raise NotImplementedError

    def send_audio(self, data):
        """入队即返回（真正的发送在发送线程里做）。"""
        with self.audio_lock:
            self.tx_q.append(data)
            while len(self.tx_q) > self.tx_cap:
                self.tx_q.popleft()
                self.tx_dropped += 1

    def set_fmt(self, fmt):
        """记住树莓派宣布的音频格式，并按帧长折算队列长度与裁剪阈值。

        上限不能按"帧数"写死：帧长 40ms 时 25 帧是 1 秒，若对方用更短的帧，
        固定帧数会变成几百毫秒的抖动缓冲。按 frame_ms 折算才是"最多压 1 秒"。
        """
        self.fmt = fmt or {}
        frame_ms = int(self.fmt.get("frame_ms") or 40)
        self.audio_cap = max(4, int(1000.0 / max(5, frame_ms)) + 2)   # 队列上限 ≈1s
        self.trim_at = max(2, int(TRIM_MS / max(5, frame_ms)))        # 超过就裁剪
        self.trim_to = max(1, int(TARGET_MS / max(5, frame_ms)))      # 裁到这么多
        self.tx_cap = max(3, int(TX_QUEUE_MS / max(5, frame_ms)))     # 发送队列 ≈600ms

    def take_audio(self, timeout=0.05):
        end = time.time() + timeout
        while True:
            with self.audio_lock:
                # 积压太深就一次性丢掉多余的最旧帧，把常驻延迟压回目标值。
                # 播放线程启动要 0.5~1 秒（声卡初始化），这期间到达的音频会一直
                # 堆在队列里变成"永久延迟"——播放速度与到达速度一样快，靠自然
                # 追赶永远追不回来。宁可丢一次（听感是一处跳音），也不让延迟常驻：
                # 不这么做时实测常驻延迟稳定停在 1 秒左右。
                if len(self.audio_q) > self.trim_at:
                    while len(self.audio_q) > self.trim_to:
                        self.audio_q.popleft()
                        self.dropped += 1
                if self.audio_q:
                    return self.audio_q.popleft()
            if self.dead.is_set() or time.time() >= end:
                return None
            time.sleep(0.005)

    def take_ctrl(self, timeout=0.0):
        end = time.time() + timeout
        while True:
            with self.audio_lock:
                if self.ctrl_q:
                    return self.ctrl_q.popleft()
            if self.dead.is_set() or time.time() >= end:
                return None
            time.sleep(0.005)

    def peer_online(self):
        return self.handshaked and not self.dead.is_set() and (
            time.time() - self.last_peer < PEER_TIMEOUT
        )

    def describe(self):
        return self.kind


class TcpServerLink(BaseLink):
    """TCP 直连的服务端一侧：监听 → accept → 读 hello → 回 welcome。"""

    kind = "tcp"

    def __init__(self, sock, addr, token, **kw):
        BaseLink.__init__(self, kw.pop("channel"), kw.pop("on_hello"), kw.pop("on_event"))
        self.sock = sock
        self.addr = addr
        self.token = token
        self.parser = FrameParser()
        self.send_lock = threading.Lock()
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 发送缓冲压到 64KB：缓冲堆起来延迟就跟着涨，宁可丢帧也不让延迟滑走
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        except OSError:
            pass
        # 阻塞模式：读线程用 select 等可读。别用 settimeout——那是整条 socket 的
        # 属性，会让发送线程的 sendall 也"超时即失败"（MQTT 那边就踩过这个坑）
        self.sock.settimeout(None)

    def start(self):
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.start_sender()
        return self

    def _read_loop(self):
        while not self.dead.is_set():
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = self.sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            for ftype, payload in self.parser.feed(data):
                if ftype == T_AUDIO:
                    self.recv_frames += 1
                    self.last_peer = time.time()
                    self.push_audio(payload)
                else:
                    try:
                        self.push_ctrl(json.loads(payload.decode("utf-8", "replace")))
                    except ValueError:
                        pass
        self.dead.set()
        self.on_event("%s 通道断开（%s）" % (self.channel, self.addr[0]))

    def _send_audio_now(self, data):
        with self.send_lock:
            if self.sock is None or not self.handshaked:
                return False
            self.sock.sendall(pack_audio(data))
        self.sent_frames += 1
        self.sent_bytes += len(data)
        return True

    def send_ctrl(self, obj):
        with self.send_lock:
            if self.sock is None or not self.handshaked:
                return False
            try:
                self.sock.sendall(pack_json(obj))
            except OSError:
                return False
        return True

    def hello_done(self, obj):
        """读到 hello、校验令牌、回 welcome。返回是否放行。"""
        fmt = obj.get("audio") or {}
        if self.token and obj.get("token") != self.token:
            self.send_ctrl({"t": "welcome", "ok": False, "error": "令牌不一致"})
            time.sleep(0.3)
            self.on_event("拒绝连接 %s：令牌不一致（树莓派与电脑上的令牌要一样）"
                          % (self.addr[0],))
            return False
        self.set_fmt(fmt)
        self.handshaked = True
        self.last_peer = time.time()
        self.peer = obj.get("name")
        self.send_ctrl({
            "t": "welcome", "ok": True, "peer": self.addr[0], "name": "windows",
            "ch": self.channel, "audio": fmt,
        })
        self.on_hello(self, True)
        return True

    def describe(self):
        return "TCP %s" % (self.addr[0],)

    def close(self):
        self.dead.set()
        with self.send_lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


class MqttClientLink(BaseLink):
    """MQTT 中继的一侧：一条到 broker 的连接 + 两个主题（音频/控制）。"""

    kind = "mqtt"

    def __init__(self, host, port, prefix, token, channel, on_hello, on_event,
                 peer_timeout=PEER_TIMEOUT):
        BaseLink.__init__(self, channel, on_hello, on_event)
        self.host, self.port = host, int(port)
        # token 必须存下来：读循环里要用它校验树莓派的 hello。少这一行时
        # 读线程会抛 AttributeError 直接死掉、再自动重连——每次重连期间收到
        # 的音频都丢（QoS0 无会话），表现就是"能连上但只收到 60% 的音频"
        self.token = token
        base = prefix.rstrip("/")
        if token:
            base = "%s/%s" % (base, token)
        self.base = base
        self.ctrl_tx = base + "/c2p"      # 电脑 → 树莓派（控制）
        self.ctrl_rx = base + "/p2c"      # 树莓派 → 电脑（hello/统计）
        self.audio_tx = base + "/down" if channel == "down" else None
        self.audio_rx = base + "/up" if channel == "up" else None
        self.subs = [self.ctrl_rx] + ([self.audio_rx] if self.audio_rx else [])
        self.client = None
        self.peer_timeout = peer_timeout
        self.client_id = "btmic-win-%s-%04x" % (channel, random.getrandbits(16))
        self._lock2 = threading.Lock()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        self.start_sender()
        return self

    def _connect(self):
        self.client = MiniMQTT(self.host, self.port, self.client_id)
        self.client.connect(10)
        self.client.subscribe(self.subs)
        self.handshaked = True
        # last_peer 不在"连上 broker"时刷新：broker 通 ≠ 树莓派在线，
        # 要等真的收到树莓派的 hello/统计/音频才算它在线，否则会在树莓派
        # 没开机时先起播放/采集线程，十几秒后再收工
        self.last_peer = 0.0
        self.on_event("MQTT 已连上 %s:%d（%s）"
                      % (self.host, self.port, self.base))

    def _loop(self):
        backoff = 2.0
        while not self.dead.is_set():
            try:
                if self.client is None:
                    self._connect()
                    backoff = 2.0
                if not self.client.maybe_ping() or not self.client.pump(timeout=0.5):
                    raise OSError("MQTT 连接断开")
                while self.client.messages:
                    topic, payload = self.client.messages.popleft()
                    if topic == self.audio_rx:
                        self.recv_frames += 1
                        self.last_peer = time.time()
                        self.push_audio(payload)
                        continue
                    try:
                        obj = json.loads(payload.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    # 上行/下行两个子进程都会往 p2c 发 hello 和统计，只认自己这条
                    # 通道的（否则 up 通道会拿到下行的格式，播放采样率就错了）
                    if obj.get("t") in ("hello", "stat") and obj.get("ch") != self.channel:
                        continue
                    if obj.get("t") == "hello":
                        if self.token and obj.get("token") != self.token:
                            self.on_event("忽略令牌不一致的 hello（来自 %s）"
                                          % obj.get("name"))
                            continue
                        new_fmt = obj.get("audio") or {}
                        changed = (self.fmt.get("rate"), self.fmt.get("channels")) != \
                                  (new_fmt.get("rate"), new_fmt.get("channels"))
                        self.fmt = new_fmt
                        self.last_peer = time.time()
                        self.on_hello(self, changed)
                    self.push_ctrl(obj)
            except OSError as exc:
                self.handshaked = False
                self.client = None
                if not self.dead.is_set():
                    self.on_event("MQTT 断开（%s），%.0f 秒后重连"
                                  % (exc, backoff))
                    time.sleep(backoff)
                    backoff = min(30.0, backoff * 1.6)
            except Exception as exc:  # noqa: BLE001
                # 读线程里任何意外异常都会让这条通道静默死掉（界面上只看到
                # "收 0 帧"），必须打出来——实测自检时被 NoneType.get 坑过一次
                self.on_event("!! MQTT 通道异常：%s: %s" % (type(exc).__name__, exc))
                self.client = None
                time.sleep(1.0)

    def _send_audio_now(self, data):
        if self.client is None or self.audio_tx is None:
            return False
        self.client.publish(self.audio_tx, data)
        self.sent_frames += 1
        self.sent_bytes += len(data)
        return True

    def send_ctrl(self, obj):
        if self.client is None:
            return False
        try:
            self.client.publish(self.ctrl_tx,
                                json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        except OSError:
            return False
        return True

    def describe(self):
        return "MQTT %s" % self.base

    def close(self):
        self.dead.set()
        with self._lock2:
            if self.client is not None:
                self.client.close()
                self.client = None


# ---------------- 音频（soundcard） ----------------


class AudioIO:
    """把 soundcard 的设备列表/播放/采集包起来，顺便做 int16 与 float32 的转换。

    采样率交给 Windows 自己转换（WASAPI 共享模式的音频引擎会转），实测 16k/44.1k/
    48k 都能直接打开并按时长返回；万一某个设备拒绝目标采样率，会退回到能打开的
    采样率并用 numpy 线性重采样顶上（音质略降但能出声）。
    """

    def __init__(self, log):
        self.log = log
        self.sc = None
        self.np = None
        self.speakers = []
        self.mics = []
        self.loopbacks = {}

    def load(self):
        import numpy  # noqa: F401

        import soundcard

        self.np = numpy
        self.sc = soundcard
        self.refresh()
        return True

    def refresh(self):
        self.speakers = list(self.sc.all_speakers())
        self.mics = list(self.sc.all_microphones(include_loopback=False))
        self.loopbacks = {}
        for spk in self.speakers:
            try:
                self.loopbacks[spk.name] = self.sc.get_microphone(
                    id=str(spk.name), include_loopback=True)
            except Exception:
                pass

    # ---- 设备描述 ----
    def output_names(self):
        return ["%s（%d 声道）" % (s.name, s.channels) for s in self.speakers]

    def input_names(self):
        """采集来源：每个扬声器的"系统声音（回环）"+ 每个真实麦克风。

        默认（第一项）放"默认扬声器的系统声音"——最常用的场景是"电脑里正在放的
        声音原样送到树莓派的耳机口"，选项放第一个省得每次挑。
        """
        names = []
        try:
            default_name = self.sc.default_speaker().name
        except Exception:  # noqa: BLE001
            default_name = None
        order = list(self.speakers)
        if default_name:
            order.sort(key=lambda s: 0 if s.name == default_name else 1)
        for spk in order:
            if spk.name in self.loopbacks:
                names.append("系统声音 · %s（回环）" % spk.name)
        for m in self.mics:
            names.append("麦克风 · %s" % m.name)
        return names

    def default_output_label(self):
        """默认扬声器在下拉框里的标签（找不到就退回第一项）。"""
        try:
            name = self.sc.default_speaker().name
        except Exception:  # noqa: BLE001
            name = None
        for label in self.output_names():
            if name and label.startswith(name):
                return label
        return self.output_names()[0] if self.speakers else ""

    def default_input_label(self):
        """默认扬声器的"系统声音（回环）"标签：最常见的用法就是这个。"""
        try:
            name = self.sc.default_speaker().name
        except Exception:  # noqa: BLE001
            name = None
        for label in self.input_names():
            if name and label.startswith("系统声音 · %s" % name):
                return label
        return self.input_names()[0] if self.mics or self.loopbacks else ""

    def resolve_output(self, label):
        if not label:
            return self.sc.default_speaker() if self.speakers else None
        for s in self.speakers:
            if label.startswith(s.name) or label == "%s（%d 声道）" % (s.name, s.channels):
                return s
        return self.speakers[0] if self.speakers else None

    def resolve_input(self, label):
        if not label:
            # 没指定就用"默认扬声器的系统声音回环"。GUI 里这个下拉框总会被填上，
            # 但无界面/命令行用法（比如联调脚本）可能给空串——早先直接返回 None，
            # 结果下行一声不响、日志里只有一句"找不到采集设备"（真机联调时踩到）
            try:
                fallback = self.default_input_label()
            except Exception:  # noqa: BLE001
                fallback = ""
            if not fallback:
                return None, False
            return self.resolve_input(fallback)
        for spk in self.speakers:
            if label.startswith("系统声音 · ") and spk.name in label:
                return self.loopbacks.get(spk.name), True
        for m in self.mics:
            if label.endswith(m.name):
                return m, False
        return (self.mics[0], False) if self.mics else (None, False)

    # ---- 播放 ----
    def open_player(self, device, rate, channels):
        for r in (rate, 48000, 44100, 32000, 16000):
            try:
                return device.player(samplerate=int(r),
                                     channels=min(channels, device.channels or channels)), r
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise OSError("播放设备打不开：%s" % last)

    # ---- 采集 ----
    def open_recorder(self, device, rate, channels):
        ch = min(channels, getattr(device, "channels", channels) or channels)
        last = None
        for r in (rate, 48000, 44100, 32000, 16000):
            try:
                rec = device.recorder(samplerate=int(r), channels=max(1, ch))
                return rec, int(r), max(1, ch)
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise OSError("采集设备打不开：%s" % last)

    @staticmethod
    def resample(x, src_rate, dst_rate, np):
        """重采样（仅在设备拒绝目标采样率时的兜底路径上用）。

        降采样（比如下行的 48kHz → 8kHz）**必须先抗混叠低通**：线性插值直接
        抽点会把超过目标奈奎斯特的内容折回可听频段，听感是沙哑/刺啦的失真。
        这里用窗函数 sinc 低通（单边 32 抽头）+ 线性插值，块内足够干净。
        上采样时线性插值本身够用，保持原来的轻量做法。
        """
        if src_rate == dst_rate:
            return x
        n_in, ch = x.shape
        n_out = int(round(n_in * dst_rate / float(src_rate)))
        if n_out <= 1:
            return x[:1]
        y = x
        if dst_rate < src_rate and n_in > 8:
            taps = 256                     # 单边 128：过渡带够窄，才有足够的
                                           # 阻带衰减压住混叠（64 抽头实测只剩
                                           # 约 -10dB，5kHz 折回 3kHz 还是听得见）
            fc = 0.42 * dst_rate / float(src_rate)
            n = np.arange(taps) - (taps - 1) / 2.0
            h = (2 * fc * np.sinc(2 * fc * n) * np.hamming(taps)).astype("float32")
            h /= h.sum()
            pad = taps // 2
            padded = np.concatenate([np.repeat(x[:1], pad, axis=0), x,
                                     np.repeat(x[-1:], pad, axis=0)])
            y = np.empty_like(x)
            for c in range(ch):
                y[:, c] = np.convolve(padded[:, c], h, mode="valid")[:n_in]
        idx = np.linspace(0, n_in - 1, n_out)
        i0 = np.floor(idx).astype(np.int64)
        i1 = np.clip(i0 + 1, 0, n_in - 1)
        frac = (idx - i0)[:, None]
        return (y[i0] * (1 - frac) + y[i1] * frac).astype("float32")


class Player:
    """上行播放：树莓派送来的 int16 PCM → 声卡。

    数据直接从链路队列取（link.take_audio），play() 会把数据写进 WASAPI 缓冲并按
    播放速度阻塞，天然起到"按实时节奏取数据"的作用；链路队列积压超过上限就丢
    最旧（在 BaseLink.push_audio 里做），避免延迟越堆越大。
    """

    def __init__(self, io, link, device, rate, channels, volume, on_level, log):
        self.io = io
        self.link = link
        self.device = device
        self.rate = rate
        self.channels = channels
        self.volume = volume / 100.0
        self.mute = False
        self.on_level = on_level
        self.log = log
        self.stop = threading.Event()
        self.frames = 0
        self.dropped = 0
        self.started = False
        self.wav = None
        self.wav_path = None

    def start_wav(self, path):
        self.stop_wav()
        self.wav = wave.open(path, "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.rate)
        self.wav_path = path

    def stop_wav(self):
        if self.wav is not None:
            try:
                self.wav.close()
            except Exception:  # noqa: BLE001
                pass
            self.wav = None

    def run(self):
        np = self.io.np
        try:
            player, actual = self.io.open_player(self.device, self.rate, self.channels)
        except OSError as exc:
            self.log("上行播放打不开设备：%s" % exc)
            return
        if actual != self.rate:
            self.log("设备不接受 %d Hz，改用 %d Hz 并重采样" % (self.rate, actual))
        self.started = True
        try:
            with player:
                while not self.stop.is_set():
                    data = self.link.take_audio(timeout=0.05)
                    if data is None:
                        continue
                    x = np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0
                    ch = self.channels if self.channels > 0 else 1
                    if x.size % ch == 0 and ch > 1:
                        x = x.reshape(-1, ch)
                    else:
                        x = x.reshape(-1, 1)
                    if self.wav is not None:
                        # 录的是"收到的原始数据"，不受播放音量/静音影响：音量滑块
                        # 是给现场监听用的，存下来的应当是树莓派送来的音频本身
                        mono = (x.mean(axis=1) if x.shape[1] > 1 else x[:, 0])
                        self.wav.writeframes(
                            (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes())
                    gain = 0.0 if self.mute else self.volume
                    if gain != 1.0:
                        x = x * gain
                    if actual != self.rate:
                        x = self.io.resample(x, self.rate, actual, np)
                    peak = float(np.max(np.abs(x))) if x.size else 0.0
                    self.on_level(peak * 32768.0)
                    try:
                        player.play(x)
                    except Exception as exc:  # noqa: BLE001
                        self.log("播放出错：%s" % exc)
                        break
                    self.frames += 1
        finally:
            self.started = False
            self.stop_wav()


class Recorder:
    """下行采集：声卡（系统声音回环 或 麦克风）→ int16 PCM 给树莓派。"""

    def __init__(self, io, device, is_loopback, wire_rate, wire_channels, volume,
                 on_level, log, block_ms=40):
        self.io = io
        self.device = device
        self.is_loopback = is_loopback
        self.wire_rate = wire_rate
        self.wire_channels = wire_channels
        self.volume = volume / 100.0
        self.mute = False
        self.on_level = on_level
        self.log = log
        self.block_ms = block_ms
        self.stop = threading.Event()
        self.frames = 0
        self.started = False

    def run(self, out_cb):
        np = self.io.np
        try:
            rec, actual, ch = self.io.open_recorder(self.device, self.wire_rate,
                                                    self.wire_channels)
        except OSError as exc:
            self.log("下行采集打不开设备：%s" % exc)
            return
        if actual != self.wire_rate:
            self.log("采集设备不接受 %d Hz，改用 %d Hz 并重采样"
                     % (self.wire_rate, actual))
        block = max(1, int(actual * self.block_ms / 1000))
        self.started = True
        try:
            with rec:
                while not self.stop.is_set():
                    try:
                        x = rec.record(numframes=block)
                    except Exception as exc:  # noqa: BLE001
                        self.log("采集出错：%s" % exc)
                        break
                    x = np.asarray(x, dtype="float32")
                    if x.ndim == 1:
                        x = x[:, None]
                    if x.shape[1] > 1 and self.wire_channels == 1:
                        x = x.mean(axis=1, keepdims=True)
                    elif x.shape[1] == 1 and self.wire_channels > 1:
                        x = np.tile(x, (1, self.wire_channels))
                    gain = 0.0 if self.mute else self.volume
                    if gain != 1.0:
                        x = x * gain
                    peak = float(np.max(np.abs(x))) if x.size else 0.0
                    self.on_level(peak * 32768.0)
                    if actual != self.wire_rate:
                        x = self.io.resample(x, actual, self.wire_rate, np)
                    out_cb((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
                    self.frames += 1
        except Exception as exc:  # noqa: BLE001
            self.log("采集设备异常：%s" % exc)
        self.started = False


# ---------------- 服务端 ----------------


class NetServer:
    """把"树莓派 ↔ 电脑"的两条通道管起来，并按需启停播放/采集线程。

    TCP 模式：监听端口，每来一条连接读 hello 认领通道（up/down）。
    MQTT 模式：建两条到 broker 的连接（up/down），树莓派上线后会发 hello。
    """

    def __init__(self, io, get_conf, on_event, on_state, on_level, on_stat):
        self.io = io
        self.get_conf = get_conf
        self.on_event = on_event
        self.on_state = on_state
        self.on_level = on_level
        self.on_stat = on_stat
        self.stop = threading.Event()
        self.links = {}          # channel -> link
        self.player = None
        self.player_thread = None
        self.recorder = None
        self.recorder_thread = None
        self.listener = None
        self.running = False
        # 播放/采集线程的启停可能被三个地方同时触发（收连接的回调、看门狗线程、
        # 界面线程停止服务），用一个可重入锁串起来，避免"一边启动一边停止"
        self.engine_lock = threading.RLock()
        self.sessions = 0        # 树莓派连上来的次数
        self.bytes_in = 0
        self.bytes_out = 0
        self.last_peer_addr = "-"

    # ---- 生命周期 ----
    def start(self):
        if self.running:
            return
        conf = self.get_conf()
        self.running = True
        self.stop.clear()
        if conf["transport"] == "mqtt":
            # 上行、下行各连一个 broker：公共 broker 实测每小时只能扛 ~8 条/秒、
            # 双向同时跑时会掉帧；两个方向分开走，容量就够一路语音 + 一路音乐了。
            # 两个地址填成同一个也能跑（只是容量共用）。
            for ch, host, port in (
                ("up", conf["mqtt_host"], conf["mqtt_port"]),
                ("down", conf.get("mqtt_host2") or conf["mqtt_host"],
                 conf.get("mqtt_port2") or conf["mqtt_port"]),
            ):
                link = MqttClientLink(host, port, conf["mqtt_prefix"],
                                      conf["token"], ch,
                                      self._on_hello, self._event)
                link.start()
                self.links[ch] = link
            self._event("已启动 MQTT 中继：上行 %s:%d，下行 %s:%d，主题 %s/%s"
                        % (conf["mqtt_host"], conf["mqtt_port"],
                           conf.get("mqtt_host2") or conf["mqtt_host"],
                           conf.get("mqtt_port2") or conf["mqtt_port"],
                           conf["mqtt_prefix"], conf["token"] or "(无令牌)"))
        else:
            try:
                self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind(("0.0.0.0", int(conf["tcp_port"])))
                self.listener.listen(8)
                self.listener.settimeout(0.5)
            except OSError as exc:
                self.running = False
                self._event("!! 监听端口 %s 失败：%s" % (conf["tcp_port"], exc))
                return
            threading.Thread(target=self._accept_loop, daemon=True).start()
            self._event("已启动 TCP 监听：端口 %d（树莓派填 %s）"
                        % (conf["tcp_port"], self._local_ips()))
        threading.Thread(target=self._supervise_loop, daemon=True).start()
        self.on_state(True)

    def _local_ips(self):
        ips = []
        try:
            host = socket.gethostname()
            for info in socket.getaddrinfo(host, None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip not in ips:
                ips.insert(0, ip)
            s.close()
        except OSError:
            pass
        return " / ".join(ips) if ips else "本机 IP"

    def stop_all(self):
        self.running = False
        self.stop.set()
        self._stop_engines()
        for link in list(self.links.values()):
            try:
                link.send_ctrl({"t": "bye"})
            except Exception:  # noqa: BLE001
                pass
            link.close()
        self.links = {}
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        self.on_state(False)

    # ---- TCP 服务端 ----
    def _accept_loop(self):
        while not self.stop.is_set():
            try:
                sock, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_socket, args=(sock, addr),
                             daemon=True).start()

    def _handle_socket(self, sock, addr):
        """一条新连接：读第一个帧，按 hello 里的 ch 认领 up/down 通道。"""
        conf = self.get_conf()
        parser = FrameParser()
        sock.settimeout(8.0)
        deadline = time.time() + 8
        obj = None
        try:
            while time.time() < deadline:
                data = sock.recv(4096)
                if not data:
                    sock.close()
                    return
                for ftype, payload in parser.feed(data):
                    if ftype == T_JSON:
                        obj = json.loads(payload.decode("utf-8", "replace"))
                        break
                if obj:
                    break
        except (OSError, ValueError):
            sock.close()
            return
        if not obj or obj.get("t") != "hello" or obj.get("ch") not in ("up", "down"):
            self._event("来自 %s 的连接没有按协议发 hello，已断开" % addr[0])
            try:
                sock.close()
            except OSError:
                pass
            return
        ch = obj["ch"]
        # 同一通道的旧连接先断掉（树莓派重连时不会两条并存）
        old = self.links.get(ch)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        link = TcpServerLink(sock, addr, conf["token"], channel=ch,
                             on_hello=self._on_hello, on_event=self._event)
        # hello 之后紧跟着的音频字节可能已经在同一个 recv 里到了：交给 link 的
        # 解析器，否则启动瞬间会掉一帧（连续 PCM 里听不出来，但白丢数据）
        if parser.buf:
            link.parser.buf += bytes(parser.buf)
        if not link.hello_done(obj):
            try:
                sock.close()
            except OSError:
                pass
            return
        self.links[ch] = link
        link.start()
        self.last_peer_addr = addr[0]
        self.sessions += 1
        self._event("树莓派已连接：%s 通道（来自 %s，音频 %s）"
                    % (ch, addr[0], link.fmt))
        self.on_stat()

    # ---- 回调 ----
    def _event(self, msg):
        self.on_event(msg)

    def _on_hello(self, link, changed=True):
        self.on_stat()
        # 回一条 welcome：TCP 下是握手的应答，MQTT 下让树莓派不用干等下一次心跳
        link.send_ctrl({"t": "welcome", "ok": True, "name": "windows",
                        "ch": link.channel, "peer": "windows"})
        # 树莓派上线/换了格式：把当前滑块值推过去（麦克风增益），并重起播放/采集
        if link.channel == "up":
            if self.player is None or changed:
                self._start_player(link)
        elif link.channel == "down":
            # 下行（电脑声音 → 树莓派）必须**等 hello 里的格式到了**再起采集：
            # 树莓派要拿这些数据在它的声卡上放出来，采样率/声道/帧长都以它宣布的
            # 为准。以前没有这一支，采集线程由 _supervise_loop 按 peer_online()
            # 起，而"对端在线"会被树莓派的控制帧（pong）提前点亮——hello 还没到
            # 就开采集，于是用界面里那套（比如 16kHz）发过去，树莓派按 8kHz 播：
            # **半速 + 低八度**（听感"失真、非常低沉"），数据量还翻倍、抖动缓冲
            # 丢一半的帧。2026-09-27 实测定位到这一处。
            if self.recorder is None or changed:
                self._start_recorder(link)
        self.send_gain(self.get_conf().get("remote_gain", 15))

    # ---- 音频引擎管理 ----
    def _supervise_loop(self):
        """按通道状态启停播放/采集：树莓派掉线就收工等它回来。"""
        last_ping = 0.0
        while not self.stop.is_set():
            up = self.links.get("up")
            down = self.links.get("down")
            # 上行：树莓派在线（且已经宣布了格式）→ 起播放线程
            if (up is not None and up.fmt.get("rate") and up.peer_online()
                    and self.player is None):
                self._start_player(up)
            if self.player is not None and (up is None or not up.peer_online()):
                self._stop_player()
            # 下行：树莓派在线**并且宣布了格式**→ 起采集线程，把电脑声音发过去。
            # 注意这里必须等 fmt：没有格式时起的采集会用界面上的设置值，与树莓派
            # 的实际播放格式不一致（变调/半速，见 _on_hello 里的说明）。
            if (down is not None and down.fmt.get("rate") and down.peer_online()
                    and self.recorder is None):
                self._start_recorder(down)
            want_rate = int(down.fmt.get("rate") or 0) if down is not None else 0
            want_ch = int(down.fmt.get("channels") or 1) if down is not None else 0
            if self.recorder is not None and (
                    down is None or not down.peer_online()
                    or not want_rate
                    or (self.recorder.wire_rate, self.recorder.wire_channels)
                    != (want_rate, want_ch)):
                self._stop_recorder()
            # 控制帧：取出 hello 之类的通知（ping/pong 的往返在 push_ctrl 里算）
            for ch, link in list(self.links.items()):
                for _ in range(8):
                    obj = link.take_ctrl(timeout=0)
                    if obj is None:
                        break
                    if obj.get("t") == "hello":
                        self._event("树莓派 %s 通道就绪（音频 %s，版本 %s）"
                                    % (ch, obj.get("audio"), obj.get("ver")))
            # 心跳/探测发给所有"通道已建立"的链路——**不管树莓派有没有先出声**。
            # MQTT 下尤其关键：树莓派要靠电脑的心跳才能确认"电脑在线"，若这边
            # 等它先说话、它又等这边先说话，双方就互相干等（实测卡死在这里）。
            now = time.time()
            for link in list(self.links.values()):
                if not link.handshaked:
                    continue
                if now - last_ping >= 2.0 and link.peer_online():
                    link.send_ctrl({"t": "ping", "ts": int(now * 1000)})
                else:
                    link.send_ctrl({"t": "hb", "peer": "windows",
                                    "ts": int(now * 1000)})
            if now - last_ping >= 2.0:
                last_ping = now
            self.on_stat()
            time.sleep(HB_INTERVAL if self.links else 0.5)

    def _start_player(self, up_link):
        with self.engine_lock:
            self._start_player_locked(up_link)

    def _start_player_locked(self, up_link):
        self._stop_player()
        conf = self.get_conf()
        dev = self.io.resolve_output(conf["play_device"])
        if dev is None:
            self._event("!! 找不到播放设备，上行音频无法播放")
            return
        fmt = up_link.fmt or {}
        rate = int(fmt.get("rate") or 16000)
        ch = int(fmt.get("channels") or 1)
        self.player = Player(self.io, up_link, dev, rate, ch, conf["play_volume"],
                             self._level_up, self._event)
        if conf.get("record_wav"):
            path = conf.get("wav_path") or os.path.join(
                os.path.dirname(CONF_PATH),
                "降噪录音_%s.wav" % time.strftime("%Y%m%d_%H%M%S"))
            try:
                self.player.start_wav(path)
                self._event("正在录音到 %s" % path)
            except OSError as exc:
                self._event("录音文件打不开（%s），继续播放" % exc)
        self.player_thread = threading.Thread(target=self.player.run, daemon=True)
        self.player_thread.start()
        self._event("开始播放树莓派音频：%s @ %d Hz %d 声道" % (dev.name, rate, ch))

    def _stop_player(self):
        if self.player is None:
            return
        with self.engine_lock:
            self._stop_player_locked()

    def _stop_player_locked(self):
        if self.player is not None:
            self.player.stop.set()
            if self.player_thread is not None:
                self.player_thread.join(timeout=1.5)
            self.player = None
            self.player_thread = None
            self.on_level("up", 0.0)

    def _start_recorder(self, down_link):
        with self.engine_lock:
            self._start_recorder_locked(down_link)

    def _start_recorder_locked(self, down_link):
        self._stop_recorder()
        conf = self.get_conf()
        dev, is_loop = self.io.resolve_input(conf["rec_device"])
        if dev is None:
            self._event("!! 找不到采集设备，无法把电脑声音发给树莓派")
            return
        # 下行格式**以树莓派为准**：它要在自己的声卡上放出来，采样率/声道数/帧长
        # 由它定（NET_DOWNLINK_RATE / NET_DOWNLINK_CHANNELS / NET_FRAME_MS）。
        # 界面上的"下行格式"只在树莓派还没宣布时当兜底——两边不一致的后果是
        # 树莓派按错的格式播，听感是变调/半速的怪声。
        fmt = down_link.fmt or {}
        wire_rate = int(fmt.get("rate") or conf["down_rate"])
        wire_ch = int(fmt.get("channels") or conf["down_channels"])
        # 帧长同理：公共 broker 大约只扛得住 8 条/秒，中继必须用大帧（默认 120ms）。
        # 按 40ms 发等于 25 条/秒，broker 会把两个方向的消息一起丢掉（实测上行只
        # 到 35%）。树莓派还没宣布帧长时按传输方式的默认值兜底。
        fallback_ms = 120 if self.get_conf()["transport"] == "mqtt" else 40
        block_ms = int(fmt.get("frame_ms") or fallback_ms)
        self.recorder = Recorder(self.io, dev, is_loop, wire_rate, wire_ch,
                                 conf["rec_volume"], self._level_down, self._event,
                                 block_ms=block_ms)
        self.recorder_thread = threading.Thread(
            target=self.recorder.run,
            args=(self._send_downlink,), daemon=True)
        self.recorder_thread.start()
        self._event("开始采集并发送给树莓派：%s @ %d Hz %d 声道，每帧 %dms（%s）"
                    % (dev.name, wire_rate, wire_ch, block_ms,
                       "按树莓派宣布的格式" if fmt.get("rate")
                       else "树莓派还没宣布格式，用界面上的设置值"))

    def _stop_recorder(self):
        if self.recorder is None:
            return
        with self.engine_lock:
            self._stop_recorder_locked()

    def _stop_recorder_locked(self):
        if self.recorder is not None:
            self.recorder.stop.set()
            if self.recorder_thread is not None:
                self.recorder_thread.join(timeout=1.5)
            self.recorder = None
            self.recorder_thread = None
            self.on_level("down", 0.0)

    def _stop_engines(self):
        with self.engine_lock:
            self._stop_player_locked()
            self._stop_recorder_locked()

    def _send_downlink(self, data):
        link = self.links.get("down")
        if link is None:
            return
        if link.send_audio(data):
            self.bytes_out += len(data)

    # ---- GUI 更新 ----
    def _level_up(self, peak):
        self.on_level("up", peak)

    def _level_down(self, peak):
        self.on_level("down", peak)

    # ---- 外部调用（GUI 线程） ----
    def apply_conf(self):
        """音量/静音/设备改变后即时生效。"""
        conf = self.get_conf()
        if self.player is not None:
            self.player.volume = conf["play_volume"] / 100.0
            self.player.mute = bool(conf["play_mute"])
        if self.recorder is not None:
            self.recorder.volume = conf["rec_volume"] / 100.0
            self.recorder.mute = bool(conf["rec_mute"])

    def send_gain(self, level):
        for link in self.links.values():
            link.send_ctrl({"t": "gain", "level": int(level)})

    def ping(self):
        for link in self.links.values():
            link.send_ctrl({"t": "ping", "ts": int(time.time() * 1000)})

    def state_text(self):
        if not self.running:
            return "未启动"
        ups = [l for l in self.links.values() if l.channel == "up"]
        if not ups:
            return "等待树莓派连接…"
        up = ups[0]
        if up.peer_online():
            return "已连接 %s%s" % (up.peer or "树莓派",
                                    "（%s）" % up.describe() if up.kind == "mqtt" else "")
        if up.handshaked:
            return "树莓派已掉线，等待重连…"
        return "等待树莓派连接…"


# ---------------- 图形界面 ----------------


class App:
    def __init__(self, root):
        self.root = root
        self.conf = dict(DEFAULT_CONF)
        self.load_conf()
        self.events = queue.Queue()
        self.levels = {"up": 0.0, "down": 0.0}
        self.level_hold = {"up": 0.0, "down": 0.0}
        self.io = AudioIO(self.log_direct)
        self.server = None
        self.audio_ok = False
        self.build_ui()
        self.root.after(100, self.pump)

    # ---- 配置 ----
    def load_conf(self):
        try:
            with open(CONF_PATH, "r", encoding="utf-8") as f:
                self.conf.update(json.load(f))
        except (OSError, ValueError):
            pass
        if not self.conf.get("token"):
            self.conf["token"] = "%06x" % random.getrandbits(24)

    def save_conf(self):
        try:
            with open(CONF_PATH, "w", encoding="utf-8") as f:
                json.dump(self.conf, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.log_direct("设置保存失败：%s" % exc)

    def get_conf(self):
        return dict(self.conf)

    # ---- 界面 ----
    def build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("880x760")
        self.root.minsize(820, 700)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        pad = dict(padx=8, pady=4)

        # ===== 连接设置 =====
        box1 = ttk.LabelFrame(self.root, text="① 连接设置（树莓派主动连过来）")
        box1.pack(fill="x", **pad)
        self.var_transport = tk.StringVar(value=self.conf["transport"])
        row = ttk.Frame(box1)
        row.pack(fill="x", padx=6, pady=3)
        ttk.Radiobutton(row, text="TCP 直连（同一局域网/有公网 IP 时首选）",
                        variable=self.var_transport, value="tcp",
                        command=self.on_transport).pack(side="left")
        ttk.Radiobutton(row, text="MQTT 中继（4G 场景，无需公网 IP）",
                        variable=self.var_transport, value="mqtt",
                        command=self.on_transport).pack(side="left", padx=12)

        self.frm_tcp = ttk.Frame(box1)
        self.frm_tcp.pack(fill="x", padx=6)
        ttk.Label(self.frm_tcp, text="监听端口").pack(side="left")
        self.var_port = tk.StringVar(value=str(self.conf["tcp_port"]))
        ttk.Entry(self.frm_tcp, textvariable=self.var_port, width=8).pack(side="left", padx=4)
        ttk.Label(self.frm_tcp, text="（树莓派侧填本机 IP：）").pack(side="left")
        self.lbl_ips = ttk.Label(self.frm_tcp, text="启动后显示", foreground="#0a58ca")
        self.lbl_ips.pack(side="left")

        self.frm_mqtt = ttk.Frame(box1)
        self.frm_mqtt.pack(fill="x", padx=6)
        ttk.Label(self.frm_mqtt, text="Broker").pack(side="left")
        self.var_mqtt_host = tk.StringVar(value=self.conf["mqtt_host"])
        ttk.Entry(self.frm_mqtt, textvariable=self.var_mqtt_host, width=22).pack(side="left", padx=4)
        ttk.Label(self.frm_mqtt, text="端口").pack(side="left")
        self.var_mqtt_port = tk.StringVar(value=str(self.conf["mqtt_port"]))
        ttk.Entry(self.frm_mqtt, textvariable=self.var_mqtt_port, width=6).pack(side="left", padx=4)
        ttk.Label(self.frm_mqtt, text="主题前缀").pack(side="left")
        self.var_prefix = tk.StringVar(value=self.conf["mqtt_prefix"])
        ttk.Entry(self.frm_mqtt, textvariable=self.var_prefix, width=10).pack(side="left", padx=4)

        frm_mqtt2 = ttk.Frame(box1)
        frm_mqtt2.pack(fill="x", padx=6)
        self.frm_mqtt2 = frm_mqtt2
        ttk.Label(frm_mqtt2, text="下行专用 Broker").pack(side="left")
        self.var_mqtt_host2 = tk.StringVar(value=self.conf["mqtt_host2"])
        ttk.Entry(frm_mqtt2, textvariable=self.var_mqtt_host2, width=22).pack(side="left", padx=4)
        ttk.Label(frm_mqtt2, text="端口").pack(side="left")
        self.var_mqtt_port2 = tk.StringVar(value=str(self.conf["mqtt_port2"]))
        ttk.Entry(frm_mqtt2, textvariable=self.var_mqtt_port2, width=6).pack(side="left", padx=4)
        ttk.Label(frm_mqtt2, text="（可填同一个；换一个能多一倍带宽）").pack(side="left")

        row = ttk.Frame(box1)
        row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="令牌（两端必须一致）").pack(side="left")
        self.var_token = tk.StringVar(value=self.conf["token"])
        ttk.Entry(row, textvariable=self.var_token, width=14).pack(side="left", padx=4)
        ttk.Button(row, text="换一个", command=self.new_token).pack(side="left")
        self.btn_run = ttk.Button(row, text="启动服务", command=self.toggle_run)
        self.btn_run.pack(side="left", padx=16)
        self.lbl_state = ttk.Label(row, text="未启动", foreground="#888")
        self.lbl_state.pack(side="left")

        # ===== 上行 =====
        box2 = ttk.LabelFrame(self.root, text="② 上行：树莓派麦克风（已降噪）→ 电脑")
        box2.pack(fill="x", **pad)
        row = ttk.Frame(box2)
        row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="播放设备").pack(side="left")
        self.var_play_dev = tk.StringVar(value=self.conf["play_device"])
        self.cmb_play = ttk.Combobox(row, textvariable=self.var_play_dev, width=42,
                                     state="readonly")
        self.cmb_play.pack(side="left", padx=4)
        self.cmb_play.bind("<<ComboboxSelected>>", lambda e: self.on_device_change())
        row = ttk.Frame(box2)
        row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="音量").pack(side="left")
        self.var_play_vol = tk.IntVar(value=self.conf["play_volume"])
        ttk.Scale(row, from_=0, to=200, variable=self.var_play_vol, length=180,
                  command=lambda v: self.on_volume("play")).pack(side="left", padx=4)
        self.lbl_play_vol = ttk.Label(row, text="%d%%" % self.conf["play_volume"], width=5)
        self.lbl_play_vol.pack(side="left")
        self.var_play_mute = tk.IntVar(value=self.conf["play_mute"])
        ttk.Checkbutton(row, text="静音", variable=self.var_play_mute,
                        command=lambda: self.on_volume("play")).pack(side="left", padx=10)
        self.var_rec_wav = tk.IntVar(value=self.conf["record_wav"])
        ttk.Checkbutton(row, text="同时录音到 WAV", variable=self.var_rec_wav,
                        command=self.on_wav_toggle).pack(side="left", padx=10)
        ttk.Button(row, text="选择保存位置…", width=13,
                   command=self.pick_wav_path).pack(side="left")
        row = ttk.Frame(box2)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="电平").pack(side="left")
        self.meter_up = tk.Canvas(row, height=14, width=260, bg="#202020",
                                  highlightthickness=0)
        self.meter_up.pack(side="left", padx=4)
        self.lbl_up = ttk.Label(row, text="峰值 -inf dBFS")
        self.lbl_up.pack(side="left")

        # ===== 下行 =====
        box3 = ttk.LabelFrame(self.root, text="③ 下行：电脑声音 → 树莓派耳机口")
        box3.pack(fill="x", **pad)
        row = ttk.Frame(box3)
        row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="采集来源").pack(side="left")
        self.var_rec_dev = tk.StringVar(value=self.conf["rec_device"])
        self.cmb_rec = ttk.Combobox(row, textvariable=self.var_rec_dev, width=42,
                                    state="readonly")
        self.cmb_rec.pack(side="left", padx=4)
        self.cmb_rec.bind("<<ComboboxSelected>>", lambda e: self.on_device_change())
        ttk.Label(row, text="「系统声音」=电脑在放什么（音乐/视频）；"
                            "「麦克风」=你说话（双向对讲用这个）").pack(side="left")
        row = ttk.Frame(box3)
        row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="音量").pack(side="left")
        self.var_rec_vol = tk.IntVar(value=self.conf["rec_volume"])
        ttk.Scale(row, from_=0, to=200, variable=self.var_rec_vol, length=180,
                  command=lambda v: self.on_volume("rec")).pack(side="left", padx=4)
        self.lbl_rec_vol = ttk.Label(row, text="%d%%" % self.conf["rec_volume"], width=5)
        self.lbl_rec_vol.pack(side="left")
        self.var_rec_mute = tk.IntVar(value=self.conf["rec_mute"])
        ttk.Checkbutton(row, text="静音", variable=self.var_rec_mute,
                        command=lambda: self.on_volume("rec")).pack(side="left", padx=10)
        ttk.Label(row, text="远端麦克风增益").pack(side="left", padx=(12, 0))
        self.var_gain = tk.IntVar(value=self.conf["remote_gain"])
        ttk.Scale(row, from_=0, to=15, variable=self.var_gain, length=140,
                  command=self.on_gain).pack(side="left", padx=4)
        self.lbl_gain = ttk.Label(row, text="%d/15" % self.conf["remote_gain"], width=6)
        self.lbl_gain.pack(side="left")
        # 闭环告警：采「系统声音」又播到同一路扬声器时，树莓派听见的是自己的回声
        row_loop = ttk.Frame(box3)
        row_loop.pack(fill="x", padx=6, pady=2)
        self.lbl_loop = ttk.Label(row_loop, text="", wraplength=560,
                                  justify="left")
        self.lbl_loop.pack(side="left")
        self.btn_use_mic = ttk.Button(row_loop, text="改用麦克风", width=12,
                                      command=self.use_microphone)
        self.btn_use_mic.pack(side="left", padx=6)
        self.btn_use_mic.state(["disabled"])
        row = ttk.Frame(box3)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="电平").pack(side="left")
        self.meter_down = tk.Canvas(row, height=14, width=260, bg="#202020",
                                    highlightthickness=0)
        self.meter_down.pack(side="left", padx=4)
        self.lbl_down = ttk.Label(row, text="峰值 -inf dBFS")
        self.lbl_down.pack(side="left")
        row = ttk.Frame(box3)
        row.pack(fill="x", padx=6, pady=3)
        ttk.Label(row, text="下行格式").pack(side="left")
        self.var_down_rate = tk.StringVar(value=str(self.conf["down_rate"]))
        ttk.Combobox(row, textvariable=self.var_down_rate, width=7, state="readonly",
                     values=["48000", "44100", "32000", "24000", "16000"]
                     ).pack(side="left", padx=4)
        ttk.Label(row, text="Hz").pack(side="left")
        self.var_down_ch = tk.StringVar(value=str(self.conf["down_channels"]))
        ttk.Combobox(row, textvariable=self.var_down_ch, width=3, state="readonly",
                     values=["2", "1"]).pack(side="left", padx=4)
        ttk.Label(row, text="声道（这里只是兜底，实际以树莓派侧 "
                            "NET_DOWNLINK_* 为准）").pack(side="left")

        # ===== 状态 =====
        box4 = ttk.LabelFrame(self.root, text="④ 状态")
        box4.pack(fill="x", **pad)
        self.lbl_status = ttk.Label(box4, text="等待启动", justify="left",
                                    font=("Consolas", 9))
        self.lbl_status.pack(fill="x", padx=6, pady=3)

        # ===== 日志 =====
        box5 = ttk.LabelFrame(self.root, text="⑤ 日志")
        box5.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(box5, height=12, wrap="word", font=("Consolas", 9))
        self.txt.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        sb = ttk.Scrollbar(box5, command=self.txt.yview)
        sb.pack(side="right", fill="y", pady=4)
        self.txt.configure(yscrollcommand=sb.set, state="disabled")

        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="音频自检", command=self.self_test).pack(side="left")
        ttk.Button(row, text="刷新设备", command=self.refresh_devices).pack(side="left", padx=6)
        ttk.Button(row, text="把连接参数复制给树莓派", command=self.copy_params).pack(side="left", padx=6)
        ttk.Button(row, text="清空日志", command=self.clear_log).pack(side="left", padx=6)
        ttk.Button(row, text="保存设置", command=self.save_all).pack(side="left", padx=6)
        self.lbl_hint = ttk.Label(row, text="", foreground="#0a58ca")
        self.lbl_hint.pack(side="left", padx=8)

        self.on_transport()
        self.refresh_devices()
        self.log_direct("提示：先点「启动服务」，再到树莓派上跑 "
                        "sudo python3 final_btmic.py --net")
        self.log_direct("本机（Windows）可用地址：%s" % self.server_ips())

    # ---- 界面回调 ----
    def server_ips(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "?"

    def on_transport(self):
        mqtt = self.var_transport.get() == "mqtt"
        # 公共 broker 实测只扛得住 ~8 条/秒、~90KB/s：切到中继时把下行降到
        # 16000Hz 立体声（62KB/s），并把帧长设成 120ms（在树莓派侧配置）
        if mqtt and getattr(self, "_last_transport", "tcp") != "mqtt":
            self.var_down_rate.set("16000")
            self.var_down_ch.set("2")
        elif not mqtt and getattr(self, "_last_transport", "tcp") == "mqtt":
            self.var_down_rate.set("48000")
            self.var_down_ch.set("2")
        self._last_transport = self.var_transport.get()
        for child in self.frm_tcp.winfo_children():
            child.configure(state="disabled" if mqtt else "normal")
        for frm in (self.frm_mqtt, self.frm_mqtt2):
            for child in frm.winfo_children():
                child.configure(state="normal" if mqtt else "disabled")
        self.lbl_ips.configure(
            text=("-" if mqtt else "%s:%s" % (self.server_ips(), self.var_port.get())))

    def new_token(self):
        self.var_token.set("%06x" % random.getrandbits(24))

    def refresh_devices(self):
        if not self.audio_ok:
            try:
                self.io.load()
                self.audio_ok = True
            except Exception as exc:  # noqa: BLE001
                self.log_direct("!! 音频库加载失败：%s" % exc)
                self.log_direct("   请先在命令行执行：pip install soundcard numpy")
                return
        self.io.refresh()
        outs = self.io.output_names()
        ins = self.io.input_names()
        self.cmb_play.configure(values=outs)
        self.cmb_rec.configure(values=ins)
        if not self.var_play_dev.get() and outs:
            # 默认用 Windows 当前默认扬声器（不是列表第一项——列表顺序可能
            # 是耳机/虚拟设备，默认设备才是用户此刻真正在用的那个）
            self.var_play_dev.set(self.io.default_output_label())
        if not self.var_rec_dev.get() and ins:
            # 默认选"默认扬声器的系统声音回环"：电脑里正在放什么就送什么
            self.var_rec_dev.set(self.io.default_input_label())
        self.log_direct("设备：%d 个播放设备，%d 个采集来源" % (len(outs), len(ins)))
        self.check_feedback_loop()

    @staticmethod
    def loopback_speaker_of(label):
        """采集来源是「系统声音 · 某扬声器（回环）」时返回那个扬声器的名字。"""
        if not label.startswith("系统声音 · "):
            return ""
        rest = label[len("系统声音 · "):]
        for sep in ("（回环）", "(回环)"):
            if rest.endswith(sep):
                rest = rest[: -len(sep)]
        return rest.strip()

    def check_feedback_loop(self):
        """采「系统声音」+ 播到同一路扬声器 = 闭环：树莓派会听见自己的回声。

        这是「树莓派端听着像回声/啸叫、听不清内容」最常见的原因：电脑喇叭把
        树莓派的声音放出来 → 又被系统回环采到 → 原样发回树莓派。要「电脑说话、
        树莓派听」，采集来源必须是「麦克风」；要「放音乐给树莓派听」，就别在本机
        同时播放树莓派的声音（把上行静音，或用耳机）。这里直接提示并给一键切换。
        """
        play = self.var_play_dev.get()
        rec = self.var_rec_dev.get()
        spk = self.loopback_speaker_of(rec)
        if spk and (not play or spk in play):
            self.lbl_loop.configure(
                text="⚠ 采集来源是「系统声音」、播放也在同一路：树莓派自己的声音会被原样"
                     "发回去（回声/啸叫、听不清内容）。要「电脑说话→树莓派听」请点右边"
                     "的「改用麦克风」；要放音乐给树莓派听，就把上行静音或用耳机",
                foreground="#b02a37")
            self.btn_use_mic.state(["!disabled"])
            return True
        self.lbl_loop.configure(text="", foreground="#198754")
        self.btn_use_mic.state(["disabled"])
        return False

    def use_microphone(self):
        """把采集来源切到第一个麦克风设备（对讲场景的正确选择）。"""
        for label in self.cmb_rec["values"]:
            if label.startswith("麦克风 · "):
                self.var_rec_dev.set(label)
                break
        else:
            self.log_direct("!! 没找到麦克风设备：先点「刷新设备」，或确认系统里接了麦克风")
            return
        self.on_device_change()
        self.check_feedback_loop()
        self.log_direct("采集来源已切到「%s」：现在电脑说话，树莓派那边能听到人声"
                        % self.var_rec_dev.get())

    def on_device_change(self):
        self.conf["play_device"] = self.var_play_dev.get()
        self.conf["rec_device"] = self.var_rec_dev.get()
        if self.server is not None and self.server.running:
            self.log_direct("设备已改：重启服务后生效（播放 %s / 采集 %s）"
                            % (self.conf["play_device"], self.conf["rec_device"]))
        self.check_feedback_loop()
        self.save_conf()

    def on_volume(self, which):
        if which == "play":
            self.conf["play_volume"] = int(self.var_play_vol.get())
            self.conf["play_mute"] = int(self.var_play_mute.get())
            self.lbl_play_vol.configure(text="%d%%" % self.conf["play_volume"])
        else:
            self.conf["rec_volume"] = int(self.var_rec_vol.get())
            self.conf["rec_mute"] = int(self.var_rec_mute.get())
            self.lbl_rec_vol.configure(text="%d%%" % self.conf["rec_volume"])
        if self.server is not None:
            self.server.apply_conf()
        self.save_conf()

    def on_gain(self, _v=None):
        level = int(self.var_gain.get())
        self.conf["remote_gain"] = level
        self.lbl_gain.configure(text="%d/15" % level)
        if self.server is not None:
            self.server.send_gain(level)

    def pick_wav_path(self):
        """选录音文件保存位置（不选就用"降噪录音_时间戳.wav"）。"""
        path = filedialog.asksaveasfilename(
            title="录音保存为", defaultextension=".wav",
            initialfile="降噪录音_%s.wav" % time.strftime("%Y%m%d_%H%M%S"),
            filetypes=[("WAV 音频", "*.wav"), ("所有文件", "*.*")])
        if path:
            self.conf["wav_path"] = path
            self.conf["record_wav"] = 1
            self.var_rec_wav.set(1)
            self.log_direct("录音将保存到：%s" % path)
            self.save_conf()

    def on_wav_toggle(self):
        self.conf["record_wav"] = int(self.var_rec_wav.get())
        if self.server is not None and self.server.running:
            self.log_direct("录音开关改了：重启服务后生效")

    def save_all(self):
        self.conf.update({
            "transport": self.var_transport.get(),
            "tcp_port": int(self.var_port.get() or 5010),
            "mqtt_host": self.var_mqtt_host.get().strip(),
            "mqtt_port": int(self.var_mqtt_port.get() or 1883),
            "mqtt_host2": self.var_mqtt_host2.get().strip(),
            "mqtt_port2": int(self.var_mqtt_port2.get() or 1883),
            "mqtt_prefix": self.var_prefix.get().strip() or "btmic",
            "token": self.var_token.get().strip(),
            "play_device": self.var_play_dev.get(),
            "rec_device": self.var_rec_dev.get(),
            "play_volume": int(self.var_play_vol.get()),
            "rec_volume": int(self.var_rec_vol.get()),
            "play_mute": int(self.var_play_mute.get()),
            "rec_mute": int(self.var_rec_mute.get()),
            "remote_gain": int(self.var_gain.get()),
            "record_wav": int(self.var_rec_wav.get()),
            "down_rate": int(self.var_down_rate.get()),
            "down_channels": int(self.var_down_ch.get()),
        })
        self.save_conf()
        self.lbl_hint.configure(text="设置已保存")

    def copy_params(self):
        conf = self.get_conf()
        if conf["transport"] == "mqtt":
            cmd = ("sudo python3 final_btmic.py --net --transport mqtt "
                   "--host %s --port %d --host-down %s --port-down %d "
                   "--prefix %s --token %s"
                   % (conf["mqtt_host"], conf["mqtt_port"],
                      conf.get("mqtt_host2") or conf["mqtt_host"],
                      conf.get("mqtt_port2") or conf["mqtt_port"],
                      conf["mqtt_prefix"], conf["token"]))
        else:
            cmd = ("sudo python3 final_btmic.py --net --transport tcp "
                   "--host %s --port %d --token %s"
                   % (self.server_ips(), conf["tcp_port"], conf["token"]))
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self.log_direct("已复制到剪贴板，粘到树莓派的 SSH 里执行：")
        self.log_direct("  " + cmd)
        self.lbl_hint.configure(text="命令已复制，去树莓派上执行即可")

    def clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def toggle_run(self):
        self.check_feedback_loop()
        if self.server is not None and self.server.running:
            self.server.stop_all()
            self.server = None
            self.btn_run.configure(text="启动服务")
            self.lbl_state.configure(text="未启动", foreground="#888")
            self.log_direct("服务已停止")
            return
        self.save_all()
        try:
            self.server = NetServer(self.io, self.get_conf, self.log, self.on_state,
                                    self.on_level, self.on_stat)
        except Exception as exc:  # noqa: BLE001
            self.log_direct("!! 启动失败：%s" % exc)
            return
        self.server.start()
        self.btn_run.configure(text="停止服务")
        self.lbl_ips.configure(text="%s:%s" % (self.server_ips(), self.var_port.get()))
        self.server.ping()

    def on_state(self, running):
        def upd():
            if running:
                self.lbl_state.configure(text="运行中", foreground="#198754")
            else:
                self.lbl_state.configure(text="未启动", foreground="#888")
        self.events.put(upd)

    def log_direct(self, msg):
        self.events.put(("log", msg))

    def log(self, msg):
        self.events.put(("log", msg))

    def on_level(self, which, peak):
        self.events.put(("level", which, peak))

    def on_stat(self):
        if self.server is not None:
            self.events.put(("stat", None))

    # ---- 主线程刷新 ----
    def pump(self):
        try:
            while True:
                item = self.events.get_nowait()
                if callable(item):
                    item()
                    continue
                kind = item[0]
                if kind == "log":
                    self.write_log(item[1])
                elif kind == "level":
                    self.levels[item[1]] = item[2]
                elif kind == "stat":
                    self.refresh_status()
        except queue.Empty:
            pass
        self.draw_meters()
        self.root.after(80, self.pump)

    def write_log(self, msg):
        self.txt.configure(state="normal")
        self.txt.insert("end", "[%s] %s\n" % (now_str(), msg))
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def draw_meters(self):
        for which, canvas, label in (("up", self.meter_up, self.lbl_up),
                                     ("down", self.meter_down, self.lbl_down)):
            peak = self.levels.get(which, 0.0)
            if peak > self.level_hold.get(which, 0):
                self.level_hold[which] = peak
            else:
                self.level_hold[which] *= 0.75
            hold = self.level_hold[which]
            canvas.delete("all")
            canvas.create_rectangle(0, 0, 260, 14, fill="#202020", outline="")
            if hold > 0:
                frac = min(1.0, (20.0 * math.log10(max(hold, 1) / 32768.0) + 60.0) / 60.0)
                frac = max(0.0, min(1.0, frac))
                w = int(260 * frac)
                color = "#198754" if frac < 0.75 else ("#fd7e14" if frac < 0.9 else "#dc3545")
                canvas.create_rectangle(0, 0, w, 14, fill=color, outline="")
            label.configure(text="峰值 %s dBFS" % log_dB(hold))

    def refresh_status(self):
        s = self.server
        if s is None:
            self.lbl_status.configure(text="等待启动")
            return
        conf = self.get_conf()
        up = s.links.get("up")
        down = s.links.get("down")
        lines = []
        lines.append("连接：%s    传输：%s    会话数：%d"
                     % (s.state_text(), conf["transport"].upper(), s.sessions))
        if up is not None:
            st = up.stat or {}
            lines.append("上行（树莓派→电脑）：%s  收 %d 帧  本地丢 %d  "
                         "树莓派发送积压 %sms  树莓派电平 %s dBFS"
                         % (st.get("state", "-"), up.recv_frames, up.dropped,
                            st.get("backlog_ms", "-"), st.get("level_db", "-")))
        if down is not None:
            st = down.stat or {}
            lines.append("下行（电脑→树莓派）：发 %d 帧 / %.1f MB（本地丢 %d）  "
                         "树莓派播放 %s 帧  抖动缓冲 %sms  补静音 %s"
                         % (down.sent_frames, down.sent_bytes / 1048576.0,
                            down.tx_dropped, st.get("played", "-"),
                            st.get("jbuf_ms", "-"), st.get("starved", "-")))
        if up is not None and up.rtt_ms is not None:
            lines.append("往返延迟（电脑↔树莓派）：%d ms" % up.rtt_ms)
        self.lbl_status.configure(text="\n".join(lines))

    # ---- 音频自检 ----
    def self_test(self):
        self.refresh_devices()
        if not self.audio_ok:
            return
        self.log_direct("=== 音频自检开始 ===")
        self.log_direct("播放设备：%s" % self.var_play_dev.get())
        self.log_direct("采集来源：%s" % self.var_rec_dev.get())
        threading.Thread(target=self._self_test_thread, daemon=True).start()

    def _self_test_thread(self):
        import numpy as np
        io = self.io
        dev = io.resolve_output(self.var_play_dev.get())
        if dev is None:
            self.log_direct("!! 没有可用的播放设备")
        else:
            try:
                player, actual = io.open_player(dev, 48000, 1)
                t = np.arange(int(actual * 0.6)) / float(actual)
                tone = (0.25 * np.sin(2 * math.pi * 1000.0 * t)).astype("float32")[:, None]
                with player:
                    player.play(tone)
                self.log_direct("已在「%s」播放 0.6 秒 1kHz 测试音（没听到就检查系统音量/静音）"
                                % dev.name)
            except Exception as exc:  # noqa: BLE001
                self.log_direct("!! 播放自检失败：%s" % exc)
        dev_in, is_loop = io.resolve_input(self.var_rec_dev.get())
        if dev_in is None:
            self.log_direct("!! 没有可用的采集设备")
            self.log_direct("=== 音频自检结束 ===")
            return
        try:
            rec, actual, ch = io.open_recorder(dev_in, 48000, 1)
            with rec:
                x = np.asarray(rec.record(numframes=int(actual * 1.0)), dtype="float32")
            peak = float(np.max(np.abs(x))) if x.size else 0.0
            self.log_direct("采集自检：%s（%s）1 秒峰值 %.1f dBFS %s"
                            % (dev_in.name, "回环" if is_loop else "麦克风",
                               20 * math.log10(max(peak, 1e-6)),
                               "（正常）" if peak > 0.001 else
                               "（几乎静音：播放点声音再试，或换一个来源）"))
        except Exception as exc:  # noqa: BLE001
            self.log_direct("!! 采集自检失败：%s" % exc)
        self.log_direct("=== 音频自检结束 ===")


def run_selftest():
    """`--selftest`：不开窗口，检查音频库/设备/采集，结果写到 exe 旁边的文件。

    打包成没有控制台的 exe 之后，"它到底能不能跑"没法用眼睛判断，这个开关就是
    给那种场合用的：跑一次，看旁边的 btmic_selftest.txt。
    """
    out = []
    def say(msg):
        out.append(str(msg))

    say("程序目录: %s" % _app_dir())
    say("配置路径: %s" % CONF_PATH)
    say("冻结打包(frozen): %s" % bool(getattr(sys, "frozen", False)))
    try:
        import numpy
        say("numpy: %s" % numpy.__version__)
    except Exception as exc:  # noqa: BLE001
        say("numpy: 缺失（%s）" % exc)
    gui_ok = True
    try:
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        root.update()
        say("界面构建: OK")
        say("播放设备: %s" % app.var_play_dev.get())
        say("采集来源: %s" % app.var_rec_dev.get())
        say("闭环检查: %s" % ("检测到闭环（采系统声音+同路播放）"
                              if app.check_feedback_loop() else "无闭环"))
        root.destroy()
    except Exception as exc:  # noqa: BLE001
        gui_ok = False
        say("界面构建: 失败 %r" % exc)
    try:
        import soundcard as sc
        say("soundcard: 播放设备 %d 个，采集来源 %d 个"
            % (len(sc.all_speakers()), len(sc.all_microphones(include_loopback=True))))
    except Exception as exc:  # noqa: BLE001
        gui_ok = False
        say("soundcard: 失败 %r" % exc)
    say("结论: %s" % ("可正常使用" if gui_ok else "有问题，看上面"))
    path = os.path.join(_app_dir(), "btmic_selftest.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError:
        pass
    return 0 if gui_ok else 1


def main():
    # 一实例一份配置：settings 面板用命令行参数覆盖
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    root = tk.Tk()
    if "--port" in sys.argv:
        i = sys.argv.index("--port")
        if i + 1 < len(sys.argv):
            DEFAULT_CONF["tcp_port"] = int(sys.argv[i + 1])
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.save_all(), root.destroy()))
    if app.conf.get("auto_start"):
        root.after(500, app.toggle_run)
    root.mainloop()


if __name__ == "__main__":
    main()
