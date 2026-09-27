#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自检：Windows 端服务（btmic_net_gui 里的 NetServer）+ net_bridge 当树莓派。

真声卡、真 socket/broker，两条方向都验音频内容：
  * 上行（树莓派 → 电脑）：发一段 1000Hz 正弦，电脑端边播边录 WAV，
    检查录到的主频是不是 1000Hz → 证明"收-播"这条路的字节是原样的；
  * 下行（电脑 → 树莓派）：在电脑扬声器上放 700Hz，采集回环，树莓派侧
    把收到的 PCM 落盘，检查主频是不是 700Hz → 证明"采-发"这条路也是通的。

用法：
    python test_e2e.py tcp       # 本机 TCP
    python test_e2e.py mqtt      # 公共 broker（需要外网）
"""
import math
import os
import queue
import subprocess
import sys
import threading
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "windows"))

from _bridge import bridge_path, gui_module, load_bridge_module  # noqa: E402

nb = load_bridge_module()
gui = gui_module()
BRIDGE = bridge_path()   # 单独再取一次路径，给子进程用

PY = sys.executable
TOKEN = "e2etest%d" % (os.getpid() & 0xFFFF)
UP_RATE, UP_CH = 16000, 1
DOWN_RATE, DOWN_CH = 48000, 2
UP_TONE, DOWN_TONE = 1000.0, 700.0
SECS = 8.0

FAIL = []


def drain(stream, label):
    """把子进程 stderr 读走并打印。

    千万不能 stderr=STDOUT：纯 PCM 是写到 stdout 的，日志文字混进去会把整条
    采样流按字节错位（实测：文件里出现 "[net] 下行接收：TCP ..." 这样的文本，
    再当 int16 读出来就是一片"均匀噪声"——排查方向会被整个带偏）。
    """
    try:
        for line in iter(stream.readline, b""):
            print("    [%s] %s" % (label, line.decode("utf-8", "replace").rstrip()))
    except (OSError, ValueError):
        pass


def check(name, cond, extra=""):
    print("  %-42s %s %s" % (name, "OK" if cond else "FAIL", extra))
    if not cond:
        FAIL.append(name)


def dominant_freq(x, rate):
    """x: 单声道 float32 → 主频（Hz）。"""
    x = np.asarray(x, dtype="float64")
    if x.size < rate // 2 or np.max(np.abs(x)) < 1e-4:
        return 0.0, float(np.max(np.abs(x)) if x.size else 0.0)
    w = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * w))
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    i = int(np.argmax(spec))
    return float(freqs[i]), float(np.max(np.abs(x)))


def make_tone(rate, ch, freq, secs):
    t = np.arange(int(rate * secs)) / float(rate)
    mono = (0.35 * np.sin(2 * math.pi * freq * t)).astype("float32")
    return np.tile(mono[:, None], (1, ch)), mono


def read_pcm_as_float(path, ch, offset_secs=1.0):
    """读裸 PCM 文件（跳过开头 offset_secs，避开握手/起播阶段）。"""
    with open(path, "rb") as f:
        raw = f.read()
    raw = raw[: (len(raw) // (2 * ch)) * 2 * ch]
    x = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    x = x.reshape(-1, ch).mean(axis=1)
    skip = min(int(offset_secs * 8000), max(0, len(x) - 8000))
    return x[skip:] if len(x) > 8000 else x


class HeadlessServer:
    """把 GUI 里的 NetServer 用无界面方式跑起来。"""

    def __init__(self, transport, port):
        self.io = gui.AudioIO(lambda m: self.events.put(("log", m)))
        self.io.load()
        self.events = queue.Queue()
        self.lines = []
        self.conf = {
            "transport": transport,
            "tcp_port": port,
            "mqtt_host": "broker.emqx.io",
            "mqtt_port": 1883,
            "mqtt_host2": "broker.emqx.io",      # 默认上下行同一个 broker
            "mqtt_port2": 1883,
            "mqtt_prefix": "btmic",
            "token": TOKEN,
            "play_device": "",          # 默认扬声器
            "play_volume": 0,           # 静音播放：测试时别吵到人（电平表仍走）
            "play_mute": 0,
            "rec_device": "",           # 默认扬声器回环
            "rec_volume": 100,
            "rec_mute": 0,
            "remote_gain": 15,
            "record_wav": 1,
            "wav_path": os.path.join(HERE, "up_recorded.wav"),
            "down_rate": DOWN_RATE,
            "down_channels": DOWN_CH,
        }
        self.server = gui.NetServer(self.io, lambda: dict(self.conf), self.on_event,
                                    self.on_state, self.on_level, self.on_stat)

    def on_event(self, msg):
        self.lines.append(msg)
        print("    [win] %s" % msg)

    def on_state(self, running):
        pass

    def on_level(self, which, peak):
        pass

    def on_stat(self):
        pass

    def start(self):
        self.server.start()

    def stop(self):
        self.server.stop_all()


def make_conf():
    """让采集默认走默认扬声器的回环（测试要抓电脑自己放的声音）。"""
    import soundcard as sc

    return sc.default_speaker().name


def run_transport(transport, port=5099):
    print("\n=== %s 端到端测试 ===" % transport.upper())
    srv = HeadlessServer(transport, port)
    if transport == "mqtt":
        # 下行用树莓派实际会选的格式：公共 broker 扛不住 48kHz 立体声（192KB/s），
        # 中继模式的默认是 16kHz 立体声（62KB/s）——这条断言本身就是那个约束
        srv.conf["down_rate"] = 16000
        srv.conf["down_channels"] = 1   # 中继默认单声道（32KB/s）
    spk_name = make_conf()
    srv.conf["rec_device"] = "系统声音 · %s（回环）" % spk_name
    srv.conf["play_device"] = "%s" % spk_name
    if transport == "tcp":
        srv.conf["play_device"] = ""   # 走"默认扬声器"
    srv.start()
    time.sleep(1.0)

    # ---- 起"树莓派"两个桥进程 ----
    up_pcm = os.path.join(HERE, "up_tone.pcm")
    down_pcm = os.path.join(HERE, "down_received.pcm")
    tone, mono = make_tone(UP_RATE, UP_CH, UP_TONE, SECS)
    open(up_pcm, "wb").write((mono * 32767).astype("<i2").tobytes())

    common = [PY, BRIDGE]
    host = "127.0.0.1" if transport == "tcp" else "broker.emqx.io"
    port_arg = str(port) if transport == "tcp" else "1883"
    tr = ["--transport", transport, "--host", host, "--port", port_arg,
          "--token", TOKEN]
    if transport == "mqtt":
        # 公共 broker 只扛得住 ~8 条/秒：中继模式用 120ms 大帧（TCP 模式用 40ms）
        tr += ["--prefix", "btmic", "--frame-ms", "120", "--jitter-ms", "300", "--queue-ms", "1500"]
    up_extra = list(tr)
    down_extra = list(tr)
    down_rate = DOWN_RATE
    down_ch = DOWN_CH
    if transport == "mqtt":
        down_rate, down_ch = 16000, 1   # 跟电脑端（也就是树莓派默认）保持一致
        # 下行走第二个 broker（和电脑端一致），上下行各占一路带宽
        down_extra = ["--transport", transport, "--host", "broker.emqx.io",
                      "--port", "1883", "--token", TOKEN, "--prefix", "btmic",
                      "--frame-ms", "120", "--jitter-ms", "300", "--queue-ms", "1500"]

    up_proc = subprocess.Popen(
        common + ["send"] + up_extra + ["--rate", str(UP_RATE), "--channels", str(UP_CH),
                                  "--name", "pi-test", "--status-file",
                                  os.path.join(HERE, "st_up.txt")],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    threading.Thread(target=drain, args=(up_proc.stderr, "up"), daemon=True).start()
    down_out = open(down_pcm, "wb")
    down_proc = subprocess.Popen(
        common + ["recv"] + down_extra + ["--rate", str(down_rate),
                                  "--channels", str(down_ch), "--name", "pi-test",
                                  "--status-file", os.path.join(HERE, "st_down.txt")],
        stdout=down_out, stderr=subprocess.PIPE)
    threading.Thread(target=drain, args=(down_proc.stderr, "down"), daemon=True).start()

    # ---- 按实时节奏喂上行（模拟 arecord）----
    def feed_realtime():
        chunk = int(UP_RATE * UP_CH * 2 * 0.04)   # 40ms
        data = (mono * 32767).astype("<i2").tobytes()
        t0 = time.time()
        pos = 0
        try:
            while pos < len(data) and up_proc.poll() is None:
                due = t0 + (pos / chunk) * 0.04
                # 必须睡满 due-now：早先写成 min(0.02, ...)，每次只睡 20ms 而
                # due 每轮推进 40ms → 实际按 2 倍速灌数据，被本地丢帧后
                # 看起来像"接收端丢了一半"，排查时白绕一圈
                slack = due - time.time()
                if slack > 0:
                    time.sleep(slack)
                up_proc.stdin.write(data[pos : pos + chunk])
                up_proc.stdin.flush()
                pos += chunk
        except (OSError, BrokenPipeError):
            pass
        try:
            up_proc.stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed_realtime, daemon=True).start()

    # ---- 电脑这边放 700Hz（给下行采集当素材）----
    import soundcard as sc

    def play_down_tone():
        try:
            spk = sc.default_speaker()
            with spk.player(samplerate=DOWN_RATE, channels=1) as p:
                t = np.arange(int(DOWN_RATE * SECS)) / float(DOWN_RATE)
                p.play((0.3 * np.sin(2 * math.pi * DOWN_TONE * t)).astype("float32")[:, None])
        except Exception as exc:  # noqa: BLE001
            print("    [test] 放测试音失败：%s" % exc)

    threading.Thread(target=play_down_tone, daemon=True).start()

    # 每秒打一次进度：收了多少帧、播了多少帧、队列里堆了多少
    t_end = time.time() + SECS + 4
    while time.time() < t_end:
        time.sleep(1.0)
        up = srv.server.links.get("up")
        dn = srv.server.links.get("down")
        pl = srv.server.player
        print("    [t] 收 %s 帧 (队列 %s 丢 %s) | 播 %s 帧 | 发 %s 帧" % (
            up.recv_frames if up else "-", len(up.audio_q) if up else "-",
            up.dropped if up else "-", pl.frames if pl else "-",
            "%s(本地丢%s)" % (dn.sent_frames, dn.tx_dropped) if dn else "-"))
    down_proc.terminate()
    time.sleep(0.5)
    up_proc.terminate()
    time.sleep(0.5)
    down_out.close()

    # ---- 检查 ----
    up_link = srv.server.links.get("up")
    down_link = srv.server.links.get("down")
    print("  --- 上行 ---")
    if up_link is None:
        check("电脑端认领 up 通道", False, "没有连接")
    else:
        check("电脑端收到上行音频帧", up_link.recv_frames + up_link.dropped > 20,
              "%d 帧（丢 %d）" % (up_link.recv_frames, up_link.dropped))
    print("  --- 下行 ---")
    if down_link is None:
        check("电脑端认领 down 通道", False, "没有连接")
    else:
        check("电脑端发出下行音频",
              down_link.sent_frames > 40 and down_link.sent_bytes > 150 * 1024,
              "%d 帧 / %.2f MB（每帧 %d 字节）"
              % (down_link.sent_frames, down_link.sent_bytes / 1048576.0,
                 down_link.sent_bytes // max(1, down_link.sent_frames)))

    srv.stop()          # 停播放线程 → 关闭 WAV，再做内容检查
    time.sleep(1.0)

    # 上行内容：录下来的 WAV 主频应为 UP_TONE
    wav_path = srv.conf["wav_path"]
    if os.path.exists(wav_path) and os.path.getsize(wav_path) > 20000:
        with wave.open(wav_path, "rb") as w:
            rate = w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        data = data[rate // 2 :]        # 去掉开头半秒（起播/垫缓冲阶段）
        freq, amp = dominant_freq(data, rate)
        check("上行音频内容 = %dHz" % UP_TONE, abs(freq - UP_TONE) < 40,
              "实测 %.0fHz（%d 帧）" % (freq, data.size))
    else:
        check("上行录音文件生成", False,
              "%s（%d 字节）" % (wav_path, os.path.getsize(wav_path)
                                if os.path.exists(wav_path) else 0))

    # 下行内容：树莓派收到的 PCM 主频应为 DOWN_TONE
    size = os.path.getsize(down_pcm) if os.path.exists(down_pcm) else 0
    if size > 20000:
        x = read_pcm_as_float(down_pcm, down_ch)
        freq, amp = dominant_freq(x, down_rate)
        check("下行音频内容 = %dHz" % DOWN_TONE, abs(freq - DOWN_TONE) < 40,
              "实测 %.0fHz（%.1f 秒）" % (freq, x.size / float(down_rate)))
    else:
        check("下行收到的 PCM 文件", False, "%s（%d 字节）" % (down_pcm, size))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "tcp"
    if which in ("tcp", "all"):
        run_transport("tcp")
    if which in ("mqtt", "all"):
        run_transport("mqtt")
    print("\n%s" % ("端到端全部通过" if not FAIL else "失败项: %s" % FAIL))
    sys.exit(1 if FAIL else 0)
