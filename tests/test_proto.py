#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""net_bridge 的协议自检：帧解析、MQTT 报文解析、真实 broker 收发。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bridge import load_bridge_module  # noqa: E402

nb = load_bridge_module()  # 树莓派上跑的那份网络桥（从 final_btmic.py 里取出）

FAIL = []


def check(name, cond, extra=""):
    print("%-46s %s %s" % (name, "OK" if cond else "FAIL", extra))
    if not cond:
        FAIL.append(name)


def test_frame_parser():
    p = nb.FrameParser()
    a = nb.pack_audio(1, b"\x01\x02" * 100)
    b = nb.pack_json({"t": "stat", "level": -20.5})
    stream = a + b
    # 一次性喂进去
    got = p.feed(stream)
    check("帧解析：整包", len(got) == 2 and got[0][0] == nb.T_AUDIO and got[0][2] == b"\x01\x02" * 100)
    check("帧解析：JSON 负载", got[1][0] == nb.T_JSON and b"stat" in got[1][2])
    # 逐字节喂（模拟 TCP 半包）
    p2 = nb.FrameParser()
    out = []
    for i in range(len(stream)):
        out += p2.feed(stream[i : i + 1])
    check("帧解析：逐字节喂（半包）", len(out) == 2)
    # 混入垃圾字节后还能重新对齐
    p3 = nb.FrameParser()
    out3 = p3.feed(b"garbage" + a)
    check("帧解析：垃圾前缀后重新对齐", len(out3) == 1)
    # 超大长度字段不会吃满内存
    bad = nb.pack_frame(nb.T_AUDIO, 0, b"")[0:8] + b"\xff\xff\xff\xff"
    check("帧解析：非法长度被丢弃", p3.feed(bad) == [])


def test_mqtt_parser():
    # 手工构造一个 PUBLISH 报文，并按各种切分方式喂进去
    topic = b"btmic/t/up"
    payload = b"\x00\x01" * 64
    var = len(topic).to_bytes(2, "big") + topic + payload
    pkt = b"\x30" + nb._mqtt_varint(len(var)) + var
    p = nb.MqttParser()
    got = p.feed(pkt)
    check("MQTT 解析：整包", len(got) == 1 and got[0][0] >> 4 == 3)
    p2 = nb.MqttParser()
    out = []
    for i in range(len(pkt)):
        out += p2.feed(pkt[i : i + 1])
    check("MQTT 解析：逐字节喂（半包）", len(out) == 1)
    # 两个报文粘在一起
    p3 = nb.MqttParser()
    check("MQTT 解析：粘包", len(p3.feed(pkt + pkt)) == 2)
    # 长报文（超过 127 字节 → 2 字节 varint）
    big = nb.MqttParser()
    payload2 = b"x" * 5000
    var2 = len(topic).to_bytes(2, "big") + topic + payload2
    pkt2 = b"\x30" + nb._mqtt_varint(len(var2)) + var2
    got2 = big.feed(pkt2)
    check("MQTT 解析：>127 字节 varint", len(got2) == 1 and len(got2[0][1]) == len(var2))


def test_broker(host="broker.emqx.io", port=1883):
    """真实公共 broker 上跑一轮：A 订阅、B 发布，A 收到即算过。"""
    tok = "selftest%d" % int(time.time())
    sub = nb.MiniMQTT(host, port, "btmic-test-sub-%04x" % (os.getpid() & 0xFFFF))
    pub = nb.MiniMQTT(host, port, "btmic-test-pub-%04x" % (os.getpid() & 0xFFFF))
    try:
        sub.connect(10)
        pub.connect(10)
    except OSError as exc:
        check("MQTT 真实 broker 连接", False, str(exc))
        return
    check("MQTT 真实 broker 连接", True, "%s:%d" % (host, port))
    sub.subscribe("btmic/%s/up" % tok)
    time.sleep(1.0)
    for i in range(3):
        pub.publish("btmic/%s/up" % tok, b"frame-%d" % i + b"\x00" * 1000)
    deadline = time.time() + 8
    got = []
    while time.time() < deadline and len(got) < 3:
        sub.maybe_ping()
        if not sub.pump(timeout=0.5):
            break
        while sub.messages:
            got.append(sub.messages.popleft())
    check("MQTT 真实 broker 收发 3 帧", len(got) == 3,
          "收到 %d 帧 %s" % (len(got), [len(p) for _t, p in got]))
    pub.close()
    sub.close()


def test_mqtt_link(host="broker.emqx.io", port=1883):
    """MqttLink 的 hello/welcome 握手与音频收发（用另一个客户端扮演 Windows 端）。"""
    tok = "link%d" % int(time.time())
    peer = nb.MiniMQTT(host, port, "btmic-peer-%04x" % (os.getpid() & 0xFFFF))
    peer.connect(10)
    peer.subscribe("btmic/%s/up" % tok)
    peer.subscribe("btmic/%s/p2c" % tok)
    time.sleep(0.8)
    # Windows 端在线时每秒发心跳（真实程序也是这么做的）——握手靠它判定"电脑在"
    hb_stop = [False]

    def hb_loop():
        while not hb_stop[0]:
            try:
                peer.publish("btmic/%s/c2p" % tok,
                             b'{"t":"hb","peer":"win-test","ts":%d}' % int(time.time() * 1000))
            except OSError:
                return
            time.sleep(0.5)

    threading.Thread(target=hb_loop, daemon=True).start()

    up = nb.MqttLink(host, port, token=tok, channel="up", name="pi-test", peer_timeout=15)
    up.connect(10)
    up.handshake({"rate": 16000, "channels": 1, "frame_ms": 40}, wait=6)
    check("MqttLink 握手（靠电脑心跳）", True, "peer=%s" % up.peer)

    for i in range(5):
        up.send_audio(b"\x11\x22" * 640)
        up.send_ctrl({"t": "stat", "sent": i})
    time.sleep(1.5)
    audio = json_ctrl = 0
    deadline = time.time() + 6
    while time.time() < deadline and (audio < 5 or json_ctrl < 4):
        if not peer.pump(timeout=0.5):
            break
        while peer.messages:
            topic, payload = peer.messages.popleft()
            if topic.endswith("/up"):
                audio += 1
                check("MqttLink 音频负载完整", payload == b"\x11\x22" * 640)
            else:
                json_ctrl += 1
    check("MqttLink 发出 5 帧音频", audio == 5, "收到 %d" % audio)
    check("MqttLink 发出控制帧", json_ctrl >= 4, "收到 %d" % json_ctrl)
    hb_stop[0] = True
    up.close()
    peer.close()


if __name__ == "__main__":
    test_frame_parser()
    test_mqtt_parser()
    if "--no-net" not in sys.argv:
        test_broker()
        test_mqtt_link()
    print("\n%s" % ("全部通过" if not FAIL else "失败项: %s" % FAIL))
    sys.exit(1 if FAIL else 0)
