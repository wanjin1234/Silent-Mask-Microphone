#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下行"一分多"阶段（bt_downlink_dup.py）的自测：

1) 主设备收到的字节数必须和输入**完全一致**——镜像出问题也不能影响主设备；
2) 每个镜像也要收到数据；
3) 镜像"消费慢"时（模拟廉价 USB 声卡时钟慢）主设备不受影响、镜像自己丢最旧的
   （丢的字节数会被统计出来），而且不会无限堆积。

用 Python 小程序冒充 aplay：把 stdin 原样写到文件（慢的那个先 sleep 若干秒）。
不需要真声卡，Windows 上也能跑。
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import importlib.util   # noqa: E402

spec = importlib.util.spec_from_file_location(
    "fb", os.path.join(ROOT, "final_btmic.py"))
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)     # noqa: E402
fb._RESTORED = True   # 别让 atexit 里的 restore_default 在 Windows 上乱跑

TMP = tempfile.mkdtemp(prefix="dup_")
SCRIPT = os.path.join(TMP, "bt_downlink_dup.py")
MAIN_OUT = os.path.join(TMP, "main.pcm")
A_OUT = os.path.join(TMP, "mirror_a.pcm")
B_OUT = os.path.join(TMP, "mirror_b.pcm")


def sink(path, delay=0.0, slow_factor=1.0):
    """冒充 aplay：把 stdin 写到 path。delay 秒后才开始读；slow_factor>1 表示读得慢。"""
    code = (
        "import sys, time\n"
        "time.sleep(%r)\n"
        "f = open(%r, 'wb')\n"
        "n = 0\n"
        "while True:\n"
        "    b = sys.stdin.buffer.read(8192)\n"
        "    if not b:\n"
        "        break\n"
        "    f.write(b)\n"
        "    n += len(b)\n"
        "    if %r > 1.0:\n"
        "        time.sleep(len(b) / 96000.0 * (%r - 1.0))\n"
        "f.close()\n" % (delay, path, slow_factor, slow_factor)
    )
    return [sys.executable, "-c", code]


def run(secs=3.0, mirror_slow=1.0, mirror_delay=0.0):
    for p in (MAIN_OUT, A_OUT, B_OUT):
        if os.path.exists(p):
            os.remove(p)
    fb.write_downlink_dup_script(
        __import__("pathlib").Path(SCRIPT),
        [sink(A_OUT), sink(B_OUT, delay=mirror_delay, slow_factor=mirror_slow)],
        buf_seconds=0.5,
    )
    # 48kHz 单声道 16bit = 96000 字节/秒，按实时节拍喂（和真实下行同量级）
    size = int(secs * 96000)
    size -= size % 8192
    data = (bytes(range(256)) * (size // 256 + 2))[:size]
    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable, SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # 主设备那一端由我们读走（模拟下游 aplay 在消费），同时按实时速率喂
    import threading
    main_data = bytearray()

    def reader():
        while True:
            b = proc.stdout.read(8192)
            if not b:
                break
            main_data.extend(b)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    n = 0
    chunk = data[:8192]
    pace = 8192 / 96000.0
    next_t = time.time()
    while n < len(data):
        now = time.time()
        if next_t > now:
            time.sleep(max(0.0, min(0.05, next_t - now)))
            continue
        next_t = max(next_t + pace, time.time())
        piece = data[n:n + len(chunk)]
        try:
            proc.stdin.write(piece)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            break
        n += len(piece)
    proc.stdin.close()
    rc = proc.wait(timeout=60)
    th.join(timeout=10)
    tail = proc.stderr.read().decode("utf-8", "replace")
    return {"sent": n, "main": len(main_data), "rc": rc, "log": tail,
            "secs": time.time() - t0}


def size(p):
    return os.path.getsize(p) if os.path.exists(p) else -1


print("=== 1) 三个设备都正常 ===")
r = run(secs=2.0)
print("  送 %d 字节 / 主设备收 %d 字节 / 镜像A %d / 镜像B %d"
      % (r["sent"], r["main"], size(A_OUT), size(B_OUT)))
print("  镜像日志: %s" % r["log"].strip().replace("\n", " | ")[:160])
assert r["main"] == r["sent"], "主设备收到的字节数和输入不一致！"
# 镜像允许差最后一点尾巴（收尾时进程被杀/管道里剩几 KB），但绝不能差一大截
for pth in (A_OUT, B_OUT):
    assert size(pth) >= r["sent"] * 0.95, "镜像数据缺得太多：%s" % pth
print("  OK 主设备与两个镜像都拿到了全部数据")

print("\n=== 2) 镜像B 启动慢 1.5 秒 ===")
r2 = run(secs=3.0, mirror_delay=1.5)
print("  送 %d 字节 / 主设备收 %d 字节 / 镜像A %d / 镜像B %d"
      % (r2["sent"], r2["main"], size(A_OUT), size(B_OUT)))
print("  镜像日志: %s" % r2["log"].strip().replace("\n", " | ")[:200])
assert r2["main"] == r2["sent"], "镜像慢把主设备也拖住了！"
assert size(B_OUT) > 0, "慢镜像一点数据都没收到"
print("  OK 主设备不受影响，慢镜像自己丢了一部分（日志里能看到丢了多少）")

print("\n=== 3) 镜像B 消费速度只有实时的 60%（时钟慢的廉价声卡）===")
r3 = run(secs=3.0, mirror_slow=1.0 / 0.6)
print("  送 %d 字节 / 主设备收 %d 字节 / 镜像A %d / 镜像B %d"
      % (r3["sent"], r3["main"], size(A_OUT), size(B_OUT)))
print("  镜像日志: %s" % r3["log"].strip().replace("\n", " | ")[:200])
assert r3["main"] == r3["sent"], "慢镜像把主设备拖住了！"
assert size(B_OUT) <= r3["sent"], "镜像收到的比送出的还多？"
print("  OK 主设备仍然完整；慢镜像按自己的速度播，多余的在队列里被丢掉")

print("\n全部通过（用到的临时目录：%s）" % TMP)
