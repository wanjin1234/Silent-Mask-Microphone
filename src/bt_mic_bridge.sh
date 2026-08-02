#!/bin/bash

# ========== 配置 ==========
BT_ALIAS="RaspberryPi-Mic"      # 蓝牙显示名称
# =========================

# -------------------- 清理函数 --------------------
cleanup() {
    echo "正在退出..."
    [ -n "$AGENT_PID" ] && kill $AGENT_PID 2>/dev/null
    exit 0
}
trap cleanup INT TERM

# -------------------- 1. 初始化蓝牙 --------------------
echo "[初始化] 打开蓝牙并设置可发现..."

# 启动 bt-agent 作为配对代理（后台持续运行，自动接受配对）
killall bt-agent 2>/dev/null
bt-agent -c NoInputNoOutput &
AGENT_PID=$!
sleep 1

# 设置蓝牙名称、上电、可发现、可配对（一次性命令，状态会保持）
bluetoothctl -- system-alias "$BT_ALIAS"
bluetoothctl -- power on
bluetoothctl -- discoverable on
bluetoothctl -- pairable on

echo "[蓝牙] 已启动，设备名称：$BT_ALIAS"

# -------------------- 2. 循环等待连接与桥接 --------------------
LAST_ADDR=""     # 上一次已连接设备的 MAC 地址

while true; do
    # 获取当前连接的蓝牙设备（取第一个）
    CURRENT_ADDR=$(bluetoothctl devices Connected | awk '{print $2}' | head -1)

    if [ -z "$CURRENT_ADDR" ]; then
        # 没有设备连接
        if [ -n "$LAST_ADDR" ]; then
            echo "[断开] 设备 $LAST_ADDR 已断开"
            LAST_ADDR=""
        fi
        sleep 3
        continue
    fi

    # 设备相同且已经桥接过，继续等待
    if [ "$CURRENT_ADDR" == "$LAST_ADDR" ]; then
        sleep 3
        continue
    fi

    # ---------- 新设备连接 ----------
    echo "[连接] 检测到设备: $CURRENT_ADDR"
    LAST_ADDR="$CURRENT_ADDR"

    # 等待 bluez_output 节点出现（最多等15秒）
    BLUEZ_OUTPUT=""
    for i in $(seq 1 15); do
        BLUEZ_OUTPUT=$(pw-cli ls Node | grep "bluez_output" | awk -F\" '{print $2}' | head -1)
        [ -n "$BLUEZ_OUTPUT" ] && break
        sleep 1
    done

    if [ -z "$BLUEZ_OUTPUT" ]; then
        echo "[错误] 未找到 bluez_output 节点，等待设备重新连接"
        LAST_ADDR=""
        continue
    fi
    echo "[节点] 蓝牙输出: $BLUEZ_OUTPUT"

    # 查找 ReSpeaker 录音节点
    RESPEAKER=$(pw-cli ls Node | grep -iE "seeed.*input|alsa_input.*seeed" | awk -F\" '{print $2}' | head -1)
    if [ -z "$RESPEAKER" ]; then
        echo "[错误] 未找到 ReSpeaker 节点"
        LAST_ADDR=""
        continue
    fi
    echo "[节点] ReSpeaker: $RESPEAKER"

    # 建立音频桥接（pw-link：源端口 -> 目标端口）
    # 端口通常为 capture_1 和 playback_1，如不同请用 pw-link -l 查看后修正
    pw-link "${RESPEAKER}:capture_1" "${BLUEZ_OUTPUT}:playback_1"
    if [ $? -eq 0 ]; then
        echo "[成功] 音频桥接已建立！Windows 现在可以收到麦克风声音。"
    else
        echo "[失败] 桥接建立失败，将重试"
        LAST_ADDR=""
        continue
    fi

    sleep 3
done