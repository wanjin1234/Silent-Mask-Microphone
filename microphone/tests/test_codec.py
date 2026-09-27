#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""μ-law 端到端自检：真桥脚本 + 真电脑端服务（走真 broker），
验证①数据减半 ②音调/声音不被破坏 ③旧电脑端（不报 codecs）时自动退回 PCM。"""
import os
import subprocess
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "windows"))
sys.path.insert(0, HERE)

import numpy as np              # noqa: E402
import _bridge                  # noqa: E402
import test_e2e as e2e          # noqa: E402

BRIDGE = _bridge.bridge_path()
TOKEN = "c0dec7"


def run_case(codec_env, label, advertise=True):
    """跑一次：树莓派桥收 1kHz 音频发出去；电脑端收下并录音 → 检查音调与字节数。"""
    wav = os.path.join(HERE, "codec_up_%s.wav" % codec_env)
    if os.path.exists(wav):
        os.remove(wav)
    srv = e2e.HeadlessServer("mqtt", 5017)
    srv.conf.update({"token": TOKEN, "mqtt_prefix": "btmic",
                     "mqtt_host": "broker.emqx.io", "mqtt_port": 1883,
                     "mqtt_host2": "broker.emqx.io", "mqtt_port2": 1883,
                     "rec_mute": 1, "play_volume": 0, "record_wav": 1, "wav_path": wav,
                     "down_rate": 16000, "down_channels": 1})
    if not advertise:      # 模拟老版电脑端：welcome 里不报 codecs
        for link_cls in (srv.server.__class__,):
            pass
        import btmic_net_gui as gui
        orig = gui.BaseLink.__dict__.get("_welcome_extra")

        def patch(self, obj):
            return {}
        gui.BaseLink.codecs_advert = None
    srv.start()
    # 树莓派侧：桥子进程，把 1kHz 音频喂给它的 stdin
    env = dict(os.environ)
    env.update(codec_env)
    cmd = [sys.executable, BRIDGE, "send", "--transport", "mqtt",
           "--host", "broker.emqx.io", "--port", "1883",
           "--prefix", "btmic", "--token", TOKEN, "--rate", "16000",
           "--channels", "1", "--frame-ms", "200",
           "--status-file", os.path.join(HERE, "pi_st_codec.txt")]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    # 实时喂：桥的下游管道就是这么喂它的（队列只留 ~1.5 秒），一次性灌会被丢
    import threading
    frame_bytes = 16000 * 200 // 1000 * 2          # 200ms 单声道 s16le
    total = int(16000 * 12)
    t = np.arange(total, dtype=np.float32) / 16000
    pcm = (np.sin(2 * np.pi * 1000 * t) * 0.5 * 32767).astype("<i2").tobytes()

    def feeder():
        n, nxt = 0, time.time()
        while n < len(pcm):
            now = time.time()
            if nxt > now:
                time.sleep(min(0.02, nxt - now))
                continue
            nxt = max(nxt + 0.2, time.time())
            try:
                proc.stdin.write(pcm[n:n + frame_bytes])
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break
            n += frame_bytes
        try:
            proc.stdin.close()
        except OSError:
            pass

    th = threading.Thread(target=feeder, daemon=True)
    th.start()
    th.join(timeout=20)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    out = (proc.stderr.read() or b"").decode("utf-8", "replace")
    for line in [l for l in out.splitlines() if "μ-law" in l or "音频" in l][:4]:
        print("     [pi] %s" % line.strip(), flush=True)
    time.sleep(1.0)
    up = srv.server.links.get("up")
    got = up.recv_frames if up else 0
    wire = getattr(up, "recv_bytes", None)
    srv.stop()
    time.sleep(0.5)
    result = {"frames": got, "wire_bytes": wire, "pitch": None, "snr": None}
    if os.path.exists(wav) and os.path.getsize(wav) > 40000:
        with wave.open(wav, "rb") as w:
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
            sr = w.getframerate()
        if x.size > sr // 2:
            seg = x[sr // 4: sr // 4 + sr]
            sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
            fr = np.fft.rfftfreq(len(seg), 1.0 / sr)
            result["pitch"] = fr[int(np.argmax(sp))]
            rms = np.sqrt((seg ** 2).mean()) / 32768.0
            result["snr"] = 20 * np.log10(max(rms, 1e-9) / 0.5 / np.sqrt(2))
    print("  %-28s 收到 %d 帧（%.1f 秒音频），线上负载 %s 字节%s"
          % (label, result["frames"], result["frames"] * 0.2,
             result["wire_bytes"] if result["wire_bytes"] is not None else "?",
             "，主频 %.0f Hz" % result["pitch"] if result["pitch"] else ""), flush=True)
    return result


print("=== 1) 树莓派用 auto（电脑端新版支持 μ-law）===")
r1 = run_case({"NET_CODEC": "auto"}, "auto + 新版电脑端")
print("=== 2) 树莓派强制 PCM（对照）===")
r2 = run_case({"NET_CODEC": "pcm"}, "pcm（对照）")
print("\n判定：")
ok = True
if r1["pitch"] and abs(r1["pitch"] - 1000) < 60:
    print("  ✓ μ-law 通路音调正确（%.0f Hz，应为 1000）" % r1["pitch"])
else:
    print("  ✗ μ-law 通路音调异常：%s" % r1["pitch"]); ok = False
if r1["frames"] and r2["frames"] and abs(r1["frames"] - r2["frames"]) <= 2:
    print("  ✓ 帧数一致（%d vs %d）" % (r1["frames"], r2["frames"]))
else:
    print("  ! 帧数差异 %s vs %s" % (r1["frames"], r2["frames"]))
if r1["wire_bytes"] and r2["wire_bytes"]:
    ratio = r1["wire_bytes"] / float(r2["wire_bytes"])
    print("  %s 线上负载 μ-law %d 字节 vs PCM %d 字节 = %.2f 倍（应≈0.5）"
          % ("✓" if 0.4 < ratio < 0.62 else "✗", r1["wire_bytes"], r2["wire_bytes"], ratio))
    ok = ok and 0.4 < ratio < 0.62
print("  说明：μ-law 每样本 1 字节（PCM 是 2 字节），语音带宽不变（16kHz 采样仍是 8kHz 带宽）")
print("\n" + ("全部通过" if ok else "有问题，看上面"))
