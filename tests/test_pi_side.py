#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 Windows 上验证 final_btmic.py 的网络模式代码：命令行解析 + 生成的桥命令。

树莓派上真正跑的 arecord/aplay 在 Windows 上没有，但"网络"这一半可以完整验证：
  * apply_net_argv() 解析 --net 的那些参数；
  * net_bridge_cmd() 生成的命令行，直接拿去当子进程跑（就是树莓派会跑的那条）；
  * write_net_bridge_script() 写出来的脚本能被跑起来。
配合 btmic_net_gui 的 NetServer（无界面）做真实音频收发。
"""
import os
import subprocess
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "windows"))
import test_e2e as e2e  # noqa: E402  （复用排水/报告等工具）
from _bridge import load_btmic  # noqa: E402

FAIL = []
TOKEN = e2e.TOKEN  # 必须与服务端（HeadlessServer）用的令牌一致


def check(name, cond, extra=""):
    print("  %-44s %s %s" % (name, "OK" if cond else "FAIL", extra))
    if not cond:
        FAIL.append(name)


def load_module():
    return load_btmic()


def test_argv(m):
    print("--- 命令行参数解析 ---")
    sys.argv = ["final_btmic.py", "--net", "--transport", "tcp", "--host", "1.2.3.4",
                "--port", "5099", "--token", TOKEN, "--down-rate", "44100"]
    s = m.apply_net_argv()
    check("--transport/--host/--port", s["transport"] == "tcp" and s["host"] == "1.2.3.4"
          and s["port"] == 5099, str((s["transport"], s["host"], s["port"])))
    check("--token", s["token"] == TOKEN)
    check("--down-rate 覆盖", s["down_rate"] == 44100)
    check("TCP 默认 40ms 帧 / 48k 下行", s["frame_ms"] == 40 and s["down_rate"] == 44100)
    sys.argv = ["final_btmic.py", "--net", "--transport", "mqtt",
                "--host", "broker.emqx.io", "--port", "1883",
                "--host-down", "broker2.example", "--port-down", "1884"]
    s = m.apply_net_argv()
    check("MQTT 默认 120ms 帧 / 16k 下行",
          s["frame_ms"] == 120 and s["down_rate"] == 16000, str(s["frame_ms"]))
    check("下行 broker 独立", s["host_down"] == "broker2.example" and s["port_down"] == 1884)
    check("net_bridge_cmd 下行用第二个 broker",
          "broker2.example" in m.net_bridge_cmd(s, "recv", "down", "", "python"))
    check("net_bridge_cmd 上行用第一个 broker",
          "broker.emqx.io" in m.net_bridge_cmd(s, "send", "up", "", "python"))
    return s


def test_bridge_script(m):
    print("--- 桥脚本写出 ---")
    path = m.write_net_bridge_script()
    check("写出 %s" % path, path.exists() and path.stat().st_size > 10000,
          "%d 字节" % (path.stat().st_size if path.exists() else 0))
    chk = subprocess.run([sys.executable, str(path), "--help"],
                         capture_output=True, text=True)
    check("脚本可执行（--help）", chk.returncode == 0 and "send" in chk.stdout)


def test_generated_commands(m, settings):
    """用 net_bridge_cmd 生成的命令行做一次真实收发（对上 Windows 端服务）。"""
    print("--- 用生成的命令跑一次真实收发 ---")
    s = dict(settings)
    s.update({"transport": "tcp", "host": "127.0.0.1", "port": 5099,
              "token": TOKEN, "frame_ms": 40, "up_rate": 16000, "up_channels": 1,
              "down_rate": 48000, "down_channels": 2})
    srv = e2e.HeadlessServer("tcp", 5099)
    srv.conf["rec_device"] = "系统声音 · %s（回环）" % e2e.make_conf()
    srv.conf["play_device"] = ""
    srv.conf["wav_path"] = os.path.join(HERE, "pi_side_up.wav")
    srv.start()
    time.sleep(1.0)

    up_cmd = m.net_bridge_cmd(s, "send", "up", os.path.join(HERE, "pi_st_up.txt"),
                              sys.executable)
    down_cmd = m.net_bridge_cmd(s, "recv", "down", os.path.join(HERE, "pi_st_down.txt"),
                                sys.executable)
    print("    上行命令: %s" % " ".join(up_cmd[2:8]))
    check("生成的命令行含 send/--transport tcp", up_cmd[2] == "send" and "tcp" in up_cmd)

    up_pcm = os.path.join(HERE, "pi_up_tone.pcm")
    down_pcm = os.path.join(HERE, "pi_down_recv.pcm")
    tone, mono = e2e.make_tone(16000, 1, 1000.0, 8.0)
    open(up_pcm, "wb").write((mono * 32767).astype("<i2").tobytes())

    up = subprocess.Popen(up_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE)
    threading.Thread(target=e2e.drain, args=(up.stderr, "up"), daemon=True).start()
    down_out = open(down_pcm, "wb")
    down = subprocess.Popen(down_cmd, stdout=down_out, stderr=subprocess.PIPE)
    threading.Thread(target=e2e.drain, args=(down.stderr, "down"), daemon=True).start()

    def feed():
        chunk = 1280
        data = (mono * 32767).astype("<i2").tobytes()
        t0 = time.time()
        pos = 0
        try:
            while pos < len(data) and up.poll() is None:
                slack = t0 + (pos / chunk) * 0.04 - time.time()
                if slack > 0:
                    time.sleep(slack)
                up.stdin.write(data[pos:pos + chunk])
                up.stdin.flush()
                pos += chunk
        except OSError:
            pass
        try:
            up.stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(1.0), e2e_play_tone()), daemon=True).start()
    time.sleep(13)
    down.terminate()
    up.terminate()
    time.sleep(0.5)
    down_out.close()
    uplink = srv.server.links.get("up")
    downlink = srv.server.links.get("down")
    check("生成的命令连上并收到上行音频", uplink is not None and uplink.recv_frames > 100,
          "%d 帧" % (uplink.recv_frames if uplink else 0))
    check("下行音频发给树莓派", downlink is not None and downlink.sent_frames > 50,
          "%d 帧" % (downlink.sent_frames if downlink else 0))
    srv.stop()
    time.sleep(1.0)
    wav = srv.conf["wav_path"]
    if os.path.exists(wav) and os.path.getsize(wav) > 20000:
        import wave
        with wave.open(wav, "rb") as w:
            rate = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        freq, _amp = e2e.dominant_freq(x[rate // 2:], rate)
        check("上行内容 = 1000Hz（电脑端解出来的）", abs(freq - 1000) < 40,
              "实测 %.0fHz" % freq)
    else:
        check("上行录音文件", False, wav)
    size = os.path.getsize(down_pcm) if os.path.exists(down_pcm) else 0
    if size > 20000:
        x = e2e.read_pcm_as_float(down_pcm, 2)
        freq, _amp = e2e.dominant_freq(x, 48000)
        check("下行内容 = 700Hz（树莓派侧收到的）", abs(freq - 700) < 40,
              "实测 %.0fHz（%.1f 秒）" % (freq, x.size / 48000.0))
    else:
        check("下行收到的 PCM", False, "%d 字节" % size)


def e2e_play_tone():
    import math

    import soundcard as sc
    try:
        with sc.default_speaker().player(samplerate=48000, channels=1) as p:
            t = np.arange(48000 * 8) / 48000.0
            p.play((0.3 * np.sin(2 * math.pi * 700 * t)).astype("float32")[:, None])
    except Exception as exc:  # noqa: BLE001
        print("    放测试音失败：%s" % exc)


if __name__ == "__main__":
    m = load_module()
    s = test_argv(m)
    test_bridge_script(m)
    if "--no-audio" not in sys.argv:
        test_generated_commands(m, s)
    print("\n%s" % ("全部通过" if not FAIL else "失败项: %s" % FAIL))
    sys.exit(1 if FAIL else 0)
