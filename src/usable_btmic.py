#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Raspberry Pi 作为蓝牙麦克风（BlueALSA + DeepFilterNet）— 完成版

- 适配 Seeed Studio reSpeaker 2-Mics HAT v2：plughw 自动转换声道
- 不主动连接 Windows，改为等待 Windows 发起 HFP/HSP 连接
- 修掉 HFP UUID 冲突（停止 pipewire/pulseaudio/ModemManager 等）
- 只等待 BlueALSA 真正发布的 SCO PCM，不再盲目测试不存在的设备

新增能力：
1. 免 PIN 配对：常驻 bt-agent -c NoInputNoOutput 代理（Just Works 配对，
   旧版 transient bluetoothctl agent 会在命令退出后立即失效，导致配对要求 PIN）
2. 开机自启动：`sudo python3 final_btmic.py --install` 安装 systemd 服务
   （--uninstall 卸载）；开机后自动等待 Windows 连接
3. Windows 输入音量真实控制采集音量：HFP 的 +VGM（麦克风增益 0~15）由
   BlueALSA 记录到 SCO 上行 PCM 的 volume 属性；本脚本监控该属性并把
   增益实时施加到音频管道（gain 阶段），从而真正改变树莓派采集音量
   （BlueALSA 原生模式不缩放样本、softvol 模式又忽略 +VGM，故自行桥接）
4. 连接成功后自动 discoverable off；断开后回到等待状态重新可发现
5. 蓝牙断开时停止音频管道但程序保持运行，等待 Windows 再次连接
6. 降噪监控：/tmp/bt_denoise_status 记录当前降噪模式与实测 RTF，
   `python3 final_btmic.py --status` 随时查看；日志每 5 秒输出输入/输出
   电平（dBFS）与静音段衰减（dB），用于实时检测降噪效果
7. DeepFilterNet 安装检查：`python3 final_btmic.py --df-info` 逐个探测
   候选 Python 解释器，打印 df 包的文件路径/版本/API 类型、torch 是否
   可用、模型缓存目录是否已有模型，判断能否启用 DeepFilterNet

开机可靠性（修复"电脑搜不到树莓派"）：
- 等待蓝牙适配器就绪（Powered: yes）后才设置 discoverable，期间反复
  rfkill unblock；设置后立即校验，失败自动重试
- 适配器卡死自愈：hci0 长时间未注册（bluetoothd 重启/链路异常后
  树莓派 UART 蓝牙固件可能再也不加载，rfkill/power on 均无效）时，
  每 30 秒节流执行主动恢复——重启 hciuart 重挂固件、拉起 hci0、
  power on，每 3 次附加重启 bluetooth 服务；前台等待与后台轮询都会触发
- 等待连接期间每 15 秒重新断言 discoverable/pairable
- 麦克风未就绪时不再退出（USB 枚举慢），循环等待并保持蓝牙可发现
- systemd Restart=always + StartLimitIntervalSec=0，服务不会因失败放弃
- 每次会话开始先清理上一实例残留的转发进程（旧 aplay 独占 SCO 会让
  新实例"找不到 PCM 却仍有声音"）；SCO PCM 探测为多来源
  （bluealsa-aplay -L / bluealsa-cli list-pcms / aplay -L）+ 标准命名
  构造兜底，列表工具失效时自动打印原始诊断信息
- 检测到连接后主动请求链路上的 HFP profile（ConnectProfile，轮换
  0x111E Handsfree/全部 profile/0x111F）：HFP 的 RFCOMM 只能由 Windows
  （AG）主动连接 HF 侧，ConnectProfile 主要用于把 BlueZ 反馈打进日志；
  等待期间通过 BlueALSA D-Bus（Manager1.GetPCMs/GetDevices）实时判定
  Windows 是否已发起 HFP 服务级连接（SLC），约 25 秒仍无 SLC 时主动
  断开 ACL 触发 Windows 自动重连并重试 Hands-Free 服务（实测旧版正是
  重连后才拉起 HFP 并成功传送），并在日志给出"Windows 缓存的服务里没
  有 HFP → 删除设备重新配对"的判定指引；等待期间一旦蓝牙断开立即返回
  并恢复可发现，缩短 Windows 需要反复尝试才能连上的窗口
- 开机时验证本机 SDP 里确实存在 Handsfree 服务记录（sdptool，缺失则
  重启 bluealsa 重新注册）：Windows 配对时只缓存当时查得到的服务，
  记录缺失会让 Windows 永远不发起 HFP（症状=已连接但无法选为麦克风）；
  同时全局 mask 用户会话的 pipewire/pulseaudio 单元防止 socket 重拉抢
  注 HFP UUID，卸载/恢复时 unmask
- PCM 等待期间预启动降噪子进程并一次性灌入约 0.8 秒静音：模型加载
  （Pi4 上 5~7 秒）与 PCM 等待并行进行，转发真正启动时降噪进程已就绪，
  消除 arecord 爆缓冲（实测 overrun 5.1s）/aplay underrun（实测 402ms）/
  积压丢弃（740ms）这一整套启动爆音；预热输出由 stdout 排水线程丢弃、
  不进 SCO，接入真实音频时 stdin 里没有残留静音垫底（旧实现按实时
  节拍喂 1.5 秒且不排 stdout：进程被输出管道反压停住，接入后首句
  人声前最多有 ~1.2 秒静音——本次 3 秒延迟的主要成分之一）

低延迟（修复 2~3 秒延迟，根因是旧降噪脚本按 10ms 小块调用模型推理、
每次调用固定开销大于块时长，加上 BufferedReader.read 要读满 64KB 才返回）：
- 降噪大块处理，摊薄每次模型推理（df.enhance）调用的固定开销
- 0.5.x 的 DeepFilterNet 模型只支持 48kHz：脚本内置纯 numpy 多相 FIR
  重采样（16k→48k→16k，无新增依赖），加上模型的 STFT 算法延迟
  10ms，整链附加延迟约 13ms
- 默认直接启用 DeepFilterNet（DENOISE_MODE=df，启动不测速）；
  DENOISE_MODE=auto 才在启动时一次加载模型、实测多个块大小
  （DF_CHUNK_MS 的 1/2、1、2、4 倍，DF_MIN/MAX_CHUNK_MS 限定范围）的 RTF，
  选“最小达标块”转发：块越小处理延迟越低，小块不达标才放大块；
  全部不达标才改用纯 numpy 轻量谱减法降噪；DENOISE_MODE=spec/off
  显式指定。运行中持续 RTF>1 也会自动降级（退出码 10）
- 模型按块推理会在块边界留下伪影（听感为周期性低频爆音），根因是
  0.5.x 的 enhance 每次调用重置 GRU 状态、各卷积层在调用边界补零、
  DF2 需 2 帧真实前瞻且库在块尾追加零样本，损坏上下文合计 40~60ms：
  改为"预热窗口"推理——窗口 = 块头 DF_PREFIX_MS（默认 50ms）真实历史
  音频预热 + 本块 + 块尾 DF_EDGE_MS（默认 40ms）丢弃边缘，本块输出
  两侧上下文完整；相邻窗口接缝再做 DF_XFADE_MS（默认 15ms）线性交叉
  淡化，即使两侧 GRU 预热有微小掩码差也听感完全连续；DF_POST_FILTER
  （默认开）启用模型后置滤波 PF；DF_SPEC_POST（默认 0.4）在模型后
  追加轻量谱减法门（帧 32ms/跳 8ms，+24ms 延迟），与 DF 级联进一步
  压低噪声地板、加大静音衰减（0=关闭）；块长默认 320ms（窗口 420ms，
  Pi4 实测 RTF 约 0.87），整链附加延迟 ≈ 320+50+40+15+24 ≈ 450ms
- Windows 调音量（+VGM 变化）经 gain 阶段按样本小步渐变（15→8 约
  20ms），消除音量台阶突变引起的"咔哒"爆音；gain 阶段非阻塞排空
  （select+os.read，读多少发多少），去掉旧实现 read(4096) 的 128ms
  块延迟；320ms 突发原样透传，由 aplay 的 128ms FIFO + 60ms 硬件
  缓冲（合计 188ms）平滑，块间约 42ms 空档不会造成欠载。积压上限
  800ms——必须大于单块突发 320ms，否则每个突发都会被误删一段
  （曾设 240ms，导致 aplay 每轮 underrun 约 300ms：周期性爆音 +
  SCO 频繁掉线）；只有 SCO 真卡死才丢最旧数据、保留最近 100ms
- 音量提升（上行链没有任何放大，+VGM=15 时 gain=1.0 直通）：gain 阶段
  默认叠加 GAIN_BOOST_DB=6dB 数字增益（约 2 倍幅度），BOOST>1 时
  接近满幅按软限幅平滑压缩、绝不硬削波；编解码器模拟 PGA（amixer
  'PGA'，ADC 之前）启动时探测并报告当前值，设 MIC_PGA_GAIN 环境
  变量（如 "20dB"）即可提升模拟增益，比数字放大信噪比更好
- --uninstall 除卸载 systemd 服务外，还恢复 install 阶段写入的蓝牙
  override/main.conf 备份、重启 bluetooth/bluealsa，然后断开并关闭
  适配器电源（bluetoothctl power off / hci0 down）：disconnect 只能
  断开当前链路，Windows 对已配对设备会自动重连，关闭电源后重连才会
  真正失败；配对信息两侧都保留，下次运行本程序时适配器自动 power on
  并恢复可发现，Windows 无需删除设备即可自动重连
- DF_MODEL（默认 deepfilternet2）显式选用轻量版模型：比 deepfilternet3
  快 2~3 倍，是 Pi4 上 RTF 达标的关键；空串=库默认模型
- 模型按需下载无超时，缓存缺失时降噪脚本快速失败并打印下载指引，
  避免服务启动卡在 GitHub 下载上十几分钟
- torch 线程数 DF_NUM_THREADS（默认 2，Pi4 四核可按 env 实测调节），
  且 set_num_interop_threads(1) 减少线程池调度开销
- 启动时把 CPU 调速器设为 performance，避免 ondemand 升频滞后拉高 RTF
- 非阻塞排空管道并做积压截断（DF_MAX_BACKLOG_MS，默认 1200ms）：
  RTF>1 时丢弃最旧音频（跳音）保低延迟，而不是延迟无限增长；
  谱减法降噪脚本同样做非阻塞排空 + 积压截断
- 降噪/gain 脚本把输出管道缩到 8KB（约 256ms）：下游 SCO 卡顿恢复后
  不需要先播完几秒积压旧音频，恢复延迟有硬上界
- DeepFilterNet 基准失败时把降噪子进程的日志与 df 包信息（文件路径/
  版本/API）打印到 journal，便于定位"基准总是失败"的根因
- 每 5 秒输出 rtf/backlog 日志（journalctl -u bt-mic -f 可见）
- arecord/aplay 显式 ALSA period/buffer 时间（默认 20ms/60ms）
"""

import atexit
import glob
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# 可被环境变量覆盖
MIC_DEVICE = os.environ.get("MIC_DEVICE", "")  # 空 = 自动探测
SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", "16000"))
CHANNELS = int(os.environ.get("CHANNELS", "1"))
FORMAT = "S16_LE"
CONNECTION_TIMEOUT = int(os.environ.get("CONNECTION_TIMEOUT", "0"))  # 0 = 无限等待
MAX_RESTARTS = int(os.environ.get("MAX_RESTARTS", "5"))
PCM_WAIT_TIMEOUT = int(os.environ.get("PCM_WAIT_TIMEOUT", "60"))

# 低延迟调优（可在 systemd 单元或环境变量中覆盖）
# 用时间值而非帧数：plughw 会按设备实际采样率换算，避免 48kHz 麦克风
# 拿到过小 period 导致 xrun
PERIOD_TIME_US = int(os.environ.get("PERIOD_TIME", "20000"))  # 20ms
BUFFER_TIME_US = int(os.environ.get("BUFFER_TIME", "60000"))  # 60ms
DF_NUM_THREADS = int(
    os.environ.get("DF_NUM_THREADS", "2")
)  # torch 线程数（Pi4 四核，1~4 可按 env 实测调节）
DF_MODEL = os.environ.get(
    "DF_MODEL", "deepfilternet2"
)  # 模型选择：deepfilternet2 比 deepfilternet3 轻量数倍；空串=库默认（DF3）
DF_CHUNK_MS = int(
    os.environ.get("DF_CHUNK_MS", "320")
)  # 降噪每次处理的时长（毫秒），auto 模式的起始块大小
DF_MAX_BACKLOG_MS = int(
    os.environ.get("DF_MAX_BACKLOG_MS", "1200")
)  # 允许的最大积压（超了跳音保低延迟）
DF_MIN_CHUNK_MS = int(
    os.environ.get("DF_MIN_CHUNK_MS", "40")
)  # auto 模式尝试的最小块大小（越小处理延迟越低）
DF_MAX_CHUNK_MS = int(
    os.environ.get("DF_MAX_CHUNK_MS", "640")
)  # auto 模式尝试的最大块大小
# 块边界爆音防护（deepfilternet 0.5.x 每次 enhance 都重置 GRU/滤波器状态，
# 且模型各卷积层在调用边界补零、库在块尾追加 n_fft 零样本、DF2 需要 2 帧
# 真实前瞻，损坏的上下文合计约 40~60ms，之前的 10ms 边缘盖不住）：
#   DF_PREFIX_MS = 块头预热前缀：把真实历史音频连同本块一起喂模型，吸收
#     头部全部冷启动/零填充伪影（前缀输出丢弃）；
#   DF_EDGE_MS   = 块尾丢弃边缘：覆盖 lookahead 与库补零产生的尾部垃圾；
#   DF_XFADE_MS  = 相邻窗口接缝交叉淡化宽度：两侧预热仍可能有微小掩码差，
#     线性混合后接缝听感完全连续（GRU 预热残差的兜底）。
DF_PREFIX_MS = int(os.environ.get("DF_PREFIX_MS", "50"))  # 块头预热前缀时长（毫秒）
DF_EDGE_MS = int(os.environ.get("DF_EDGE_MS", "40"))  # 块尾丢弃边缘时长（毫秒）
DF_XFADE_MS = int(
    os.environ.get("DF_XFADE_MS", "15")
)  # 相邻窗口接缝交叉淡化宽度（毫秒，0=关闭）
DF_POST_FILTER = int(
    os.environ.get("DF_POST_FILTER", "1")
)  # 后置滤波器 PF：额外降噪、加大静音衰减
DF_SPEC_POST = float(
    os.environ.get("DF_SPEC_POST", "0.4")
)  # 模型后追加的轻量谱减法门增益下限（>0 启用；0=关闭）
# 降噪模式: df=DeepFilterNet（默认）；其他模式需显式指定：
#   auto=启动时实测 DeepFilterNet 速度，跟不上实时则自动用轻量谱减法；
#   spec=纯 numpy 谱减法；off=不降噪
DENOISE_MODE = os.environ.get("DENOISE_MODE", "df").lower()
DF_BENCH_LIMIT = float(os.environ.get("DF_BENCH_LIMIT", "0.9"))  # 基准 RTF 阈值
DF_RTF_EXIT_SECS = int(
    os.environ.get("DF_RTF_EXIT_SECS", "30")
)  # 运行中持续超限多久自动降级
SPEC_FLOOR = float(os.environ.get("SPEC_FLOOR", "0.15"))  # 轻量降噪增益下限

# 音量提升：上行链路没有任何放大（+VGM=15 时 gain=1.0 纯直通），麦克风整体偏轻。
#   GAIN_BOOST_DB = gain 阶段的数字增益（dB）。+6dB ≈ 2 倍幅度；接近满幅时
#     软限幅平滑压缩，防止增强后削波爆音。0 = 纯直通
#   MIC_PGA_GAIN   = 编解码器模拟 PGA 增益（amixer cset 值，如 "20dB"、"50%"）。
#     tlv320aic3x 的 'PGA' 位于 ADC 之前，比数字放大更干净（提升输入信噪比）；
#     空 = 不改动，只报告当前值
GAIN_BOOST_DB = float(os.environ.get("GAIN_BOOST_DB", "6"))
MIC_PGA_GAIN = os.environ.get("MIC_PGA_GAIN", "")

BACKUP_DIR = "/tmp/bt_mic_backup"
BT_OVERRIDE = "/etc/systemd/system/bluetooth.service.d/override.conf"
BLUEALSA_OVERRIDE = "/etc/systemd/system/bluealsa.service.d/override.conf"
BLUETOOTH_CONF = "/etc/bluetooth/main.conf"
DENOISE_SCRIPT = Path("/tmp/df_denoise.py")
SPEC_SCRIPT = Path("/tmp/df_spec_denoise.py")
DOWNMIX_SCRIPT = Path("/tmp/df_downmix.py")
GAIN_SCRIPT = Path("/tmp/bt_gain.py")
GAIN_LEVEL_FILE = "/tmp/bt_gain_level"  # 0~15，15=满增益
DENOISE_STATUS_FILE = "/tmp/bt_denoise_status"  # 当前降噪模式/实测 RTF（--status 可查）
SERVICE_UNIT_PATH = "/etc/systemd/system/bt-mic.service"

SERVICE_UNIT_TEMPLATE = """[Unit]
Description=Silent Mask - Bluetooth Microphone (BlueALSA HFP + DeepFilterNet)
After=bluetooth.service bluealsa.service
Wants=bluetooth.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={python} {script}
Restart=always
RestartSec=10
Nice=-5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""


def run(cmd, check=False, timeout=60, verbose=True):
    if verbose:
        print(f"$ {cmd}")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode != 0:
            print(result.stdout)
            print(result.stderr)
            raise RuntimeError(f"命令失败(returncode={result.returncode}): {cmd}")
        return result
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"命令超时: {cmd}")
        return None


def print_status(msg):
    print(f"\n=== {msg} ===")


def ensure_root():
    if os.geteuid() != 0:
        print("当前不是 root，尝试自动用 sudo 重新启动...")
        os.execvp("sudo", ["sudo", "python3", *sys.argv])


# ---------------- 系统状态备份 / 恢复 ----------------


def backup_system_state():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for path in [BLUETOOTH_CONF, BT_OVERRIDE, BLUEALSA_OVERRIDE]:
        if os.path.exists(path):
            name = os.path.basename(path)
            if name == "override.conf":
                # 蓝牙与 bluealsa 的 override 同名，用所属 service 目录区分
                name = path.split("/")[-2] + ".override.conf"
            shutil.copy2(path, os.path.join(BACKUP_DIR, name + ".bak"))


_RESTORED = False


def disconnect_bluetooth_devices():
    """退出时断开所有已连接的蓝牙设备，避免音频通道残留。"""
    result = run(
        "bluetoothctl devices Connected", check=False, timeout=8, verbose=False
    )
    if not result or result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            mac = parts[1]
            print(f"  -> 断开蓝牙设备 {mac}")
            run(f"bluetoothctl disconnect {mac}", check=False, timeout=8, verbose=False)


def remove_paired_devices():
    """移除全部已配对设备，阻止对端（Windows）在断开后自动重连。

    disconnect 只能断开当前链路：Windows 对已配对设备会在几秒内自动
    重连，看起来像"没断开"。把配对从树莓派侧删掉后，Windows 的重连
    会因配对不存在而失败，连接才算真正了断（下次使用需重新配对）。
    """
    result = run("bluetoothctl devices", check=False, timeout=8, verbose=False)
    if not result or result.returncode != 0:
        print("  !! 无法读取配对列表（适配器未就绪？），重启系统后链路即断开")
        return
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            mac = parts[1]
            run(f"bluetoothctl remove {mac}", check=False, timeout=8, verbose=False)
            print(f"  -> 已移除配对设备 {mac}（Windows 自动重连将失败）")


def _restore_bluetooth_overrides():
    """移除 install 写入的 override 配置；存在安装前备份则还原。"""
    for d in [
        "/etc/systemd/system/bluetooth.service.d",
        "/etc/systemd/system/bluealsa.service.d",
    ]:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    for service in ["bluetooth", "bluealsa"]:
        bak = os.path.join(BACKUP_DIR, f"{service}.service.d.override.conf.bak")
        if os.path.exists(bak):
            dst_dir = f"/etc/systemd/system/{service}.service.d"
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(bak, os.path.join(dst_dir, "override.conf"))
    main_conf_bak = os.path.join(BACKUP_DIR, "main.conf.bak")
    if os.path.exists(main_conf_bak):
        shutil.copy2(main_conf_bak, BLUETOOTH_CONF)


def restore_default():
    global _RESTORED
    if _RESTORED:
        return
    _RESTORED = True
    print_status("恢复系统默认设置并断开蓝牙")

    disconnect_bluetooth_devices()

    _restore_bluetooth_overrides()

    # 恢复 install 时全局屏蔽的用户会话音频单元（pipewire/pulseaudio 等）
    run(
        "systemctl --global unmask pipewire.socket pipewire-pulse.socket "
        "pulseaudio.socket pipewire pipewire-pulse wireplumber pulseaudio "
        "2>/dev/null || true",
        check=False,
        verbose=False,
    )

    for service in ["pulseaudio", "pipewire", "pipewire-pulse", "ofono"]:
        run(
            f"systemctl enable {service} 2>/dev/null || true",
            check=False,
            verbose=False,
        )
        run(
            f"systemctl start {service} 2>/dev/null || true", check=False, verbose=False
        )

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl restart bluetooth 2>/dev/null || true", check=False, verbose=False)
    run("pkill -f 'arecord.*aplay' || true", check=False, verbose=False)
    run("pkill -f deepfilter_denoise || true", check=False, verbose=False)

    if os.path.exists("/tmp/asound.conf.removed"):
        shutil.move("/tmp/asound.conf.removed", "/etc/asound.conf")


atexit.register(restore_default)


# ---------------- 依赖 / 服务清理 ----------------


def setup_packages():
    required = ["bluetoothctl", "bluealsa", "bluealsa-cli", "arecord"]
    missing = [b for b in required if shutil.which(b) is None]
    if not missing:
        print_status("依赖检查通过（bluez/bluealsa 已安装，跳过 apt）")
        return
    print_status(f"检查并安装依赖（缺少: {', '.join(missing)}）")
    run("apt-get update -y --allow-releaseinfo-change", check=False, verbose=False)
    run(
        "apt-get install -y bluez bluez-tools bluez-alsa bluez-alsa-utils bluetooth "
        "libasound2-dev libasound2-plugins",
        check=False,
        verbose=False,
    )


def disable_audio_servers():
    print_status("禁用 PulseAudio / PipeWire / oFono / ModemManager / hsphfpd")

    # 系统服务
    for service in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "ofono",
        "hsphfpd",
        "ModemManager",
    ]:
        run(f"systemctl stop {service} 2>/dev/null || true", check=False, verbose=False)
        run(
            f"systemctl disable {service} 2>/dev/null || true",
            check=False,
            verbose=False,
        )

    # 用户会话服务（例如 wanjin1234 的 pipewire）
    run(
        "systemctl --user stop pipewire.socket pipewire-pulse.socket "
        "pipewire pipewire-pulse wireplumber pulseaudio.socket pulseaudio "
        "2>/dev/null || true",
        check=False,
        verbose=False,
    )

    # 全局屏蔽用户会话单元：仅 stop 会被 socket/登录重新拉起，mask 之后
    # 任何用户会话都不会再启动（--uninstall/restore 流程里会 unmask）
    run(
        "systemctl --global mask pipewire.socket pipewire-pulse.socket "
        "pulseaudio.socket pipewire pipewire-pulse wireplumber pulseaudio "
        "2>/dev/null || true",
        check=False,
        verbose=False,
    )

    # 进程级清理（最关键，覆盖各用户会话里已在运行的实例）
    kill_bt_profile_conflicts()

    time.sleep(1)
    leftover = run(
        "ps -ef | grep -iE 'pipewire|pulseaudio|ofono|ModemManager|hsphfpd' | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if leftover and (leftover.stdout or "").strip():
        print("  !! 仍有冲突进程残留，请手动检查：")
        print((leftover.stdout or "").strip())
    else:
        print("  -> 未发现残留的音频/电话服务进程")


def kill_bt_profile_conflicts():
    """杀死可能抢占 HFP/HSP UUID 的音频/电话服务进程，返回是否杀到了进程。

    pipewire/pulseaudio 等在用户会话里会被 socket 重新拉起：若它们先于
    bluealsa 向 BlueZ 注册 HFP/HSP，bluealsa 的注册会静默失败（SCO 永远
    不出现）。杀掉后 BlueZ 移除其注册，重启 bluealsa 即可重新占有 UUID。
    """
    killed = False
    for proc in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "hsphfpd",
        "ofono",
        "ModemManager",
        "modemmanager",
    ]:
        res = run(f"pkill -x {proc} 2>/dev/null", check=False, verbose=False)
        if res and res.returncode == 0:
            killed = True
    return killed


# ---------------- BlueZ / BlueALSA ----------------


def find_bluetoothd():
    for p in [
        "/usr/libexec/bluetooth/bluetoothd",
        "/usr/lib/bluetooth/bluetoothd",
        "/usr/local/libexec/bluetooth/bluetoothd",
    ]:
        if os.path.exists(p):
            return p
    found = shutil.which("bluetoothd")
    if found:
        return found
    raise FileNotFoundError("找不到 bluetoothd")


def get_hci_name():
    candidates = sorted(glob.glob("/sys/class/bluetooth/hci*"))
    if candidates:
        return os.path.basename(candidates[0])
    for name in ["hci0", "hci1"]:
        if os.path.exists(f"/sys/class/bluetooth/{name}"):
            return name
    return "hci0"


def has_bluealsa_dbus_name():
    result = run(
        "dbus-send --system --print-reply --dest=org.freedesktop.DBus "
        "/org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner string:org.bluealsa",
        check=False,
        timeout=8,
        verbose=False,
    )
    if not result:
        return False
    return "boolean true" in (result.stdout or "")


def ensure_bluealsa_dbus(hci):
    if has_bluealsa_dbus_name():
        return True
    print("  !! 未检测到 org.bluealsa，尝试重启 bluealsa 服务")
    run("systemctl restart bluealsa", check=False, verbose=False)
    time.sleep(2)
    if has_bluealsa_dbus_name():
        print("  -> org.bluealsa 已恢复")
        return True
    ba_path = shutil.which("bluealsa")
    if not ba_path:
        return False
    print("  !! 继续尝试前台参数直启 bluealsa 守护进程")
    run("pkill -x bluealsa 2>/dev/null || true", check=False, verbose=False)
    run(
        f"{ba_path} --dbus=org.bluealsa -i {hci} -p hsp-hs -p hfp-hf "
        ">/tmp/bluealsa.log 2>&1 &",
        check=False,
        verbose=False,
    )
    time.sleep(2)
    return has_bluealsa_dbus_name()


def diagnose_uuid_conflicts():
    print("  -- 可能占用 HFP/HSP UUID 的进程 --")
    result = run(
        "ps -ef | grep -iE 'pipewire|pulseaudio|ofono|ModemManager|hsphfpd|bluealsa|bluetoothd' "
        "| grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if result and (result.stdout or "").strip():
        print((result.stdout or "").strip())
    else:
        print("    （未发现相关进程）")

    result = run(
        "ps -ef | grep bluetoothd | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if result and (result.stdout or "").strip():
        if "--noplugin=audio,headset" in (result.stdout or ""):
            print("  -> bluetoothd 已禁用内置 audio/headset 插件")
        else:
            print(
                "  !! bluetoothd 未带 --noplugin=audio,headset，请检查 override.conf 是否生效"
            )

    names = run(
        "dbus-send --system --print-reply --dest=org.freedesktop.DBus "
        "/org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>/dev/null "
        "| grep -iE 'blue|ofono|modem|pulse|pipewire|phone'",
        check=False,
        timeout=8,
        verbose=False,
    )
    if names and (names.stdout or "").strip():
        print("  -- D-Bus 上相关服务名 --")
        print((names.stdout or "").strip())


def resolve_uuid_conflict(hci):
    print_status("尝试解决 UUID 冲突: 停止冲突服务并重启 bluetooth/bluealsa")
    run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl stop bluetooth 2>/dev/null || true", check=False, verbose=False)

    for svc in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "ofono",
        "hsphfpd",
        "ModemManager",
    ]:
        run(f"systemctl stop {svc} 2>/dev/null || true", check=False, verbose=False)
        run(
            f"systemctl --user stop {svc} 2>/dev/null || true",
            check=False,
            verbose=False,
        )

    for proc in [
        "pulseaudio",
        "pipewire",
        "pipewire-pulse",
        "wireplumber",
        "hsphfpd",
        "ofono",
        "ModemManager",
        "modemmanager",
    ]:
        run(f"pkill -x {proc} 2>/dev/null || true", check=False, verbose=False)

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl start bluetooth", check=False, verbose=False)
    time.sleep(2)

    check = run(
        "ps -ef | grep bluetoothd | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if not (check and "--noplugin=audio,headset" in (check.stdout or "")):
        print("  !! 注意：bluetoothd 未带 --noplugin=audio,headset，可能无法释放 UUID")

    run("systemctl restart bluealsa", check=False, verbose=False)
    time.sleep(3)

    journal = run(
        "journalctl -u bluealsa -n 120 --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    if (
        journal
        and "Couldn't register hands-free profile" not in (journal.stdout or "")
        and "UUID already registered" not in (journal.stdout or "")
    ):
        print("  -> UUID 冲突已清除，bluealsa 可注册 HFP/HSP")
        return True

    print("  !! UUID 冲突仍然存在，可能占用 UUID 的进程/服务如下：")
    diagnose_uuid_conflicts()
    if journal and (journal.stdout or "").strip():
        print("  -- bluealsa 最近日志 --")
        print((journal.stdout or "").strip())
    return False


def configure_main_conf():
    """持久化蓝牙参数：headset 设备类型 + 永远可发现/可配对 + Just Works 重配对。"""
    print_status("配置 /etc/bluetooth/main.conf（设备类型/可发现性/免 PIN 重配对）")
    if not os.path.exists(BLUETOOTH_CONF):
        print("  !! 未找到 main.conf，跳过（运行时 bluetoothctl 设置仍然生效）")
        return

    text = Path(BLUETOOTH_CONF).read_text(encoding="utf-8", errors="replace")

    def set_general_key(key, value):
        nonlocal text
        new_line = f"{key} = {value}"
        pat = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=.*$", re.M)
        if pat.search(text):
            text = pat.sub(new_line, text, count=1)
            return
        m = re.search(r"^\s*\[General\]\s*$", text, re.M)
        if m:
            text = text[: m.end()] + "\n" + new_line + text[m.end() :]
        else:
            text += "\n[General]\n" + new_line + "\n"

    # Class 0x240404 = Audio/Video, Hands-Free（让 Windows 按头戴设备对待，
    # 配合 NoInputNoOutput 代理使用 Just Works 免 PIN 配对）
    set_general_key("Class", "0x240404")
    set_general_key("DiscoverableTimeout", "0")
    set_general_key("PairableTimeout", "0")
    # 允许已配对设备用 Just Works 方式重新配对（默认 never 会导致重配对失败）
    set_general_key("JustWorksRepairing", "always")

    Path(BLUETOOTH_CONF).write_text(text, encoding="utf-8")


def configure_bluetoothd():
    print_status("配置 BlueZ，禁用内置 audio/headset 插件")
    bt_bin = find_bluetoothd()
    new_exec = f"{bt_bin} --experimental --noplugin=audio,headset"
    os.makedirs(os.path.dirname(BT_OVERRIDE), exist_ok=True)
    with open(BT_OVERRIDE, "w", encoding="utf-8") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl restart bluetooth", check=False, verbose=False)
    time.sleep(2)

    check = run(
        "ps -ef | grep bluetoothd | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    if check and "--noplugin=audio,headset" in (check.stdout or ""):
        print("  -> bluetoothd 已禁用 audio/headset 插件")
    else:
        print("  !! 警告：未检测到 --noplugin=audio,headset，可能仍存在插件冲突")


def verify_hfp_sdp_record():
    """检查本机 SDP 里是否真的存在 Handsfree 服务记录，返回 True/False/None。

    Windows 配对时只缓存当时查得到的服务：若那一刻 HFP 记录缺失，Windows
    会把设备记为"无 HFP"，之后永不发起服务级连接（症状 = 已连接但无法选
    为麦克风），必须在 Windows 侧删除设备重新配对才能修复。None 表示无法
    验证（缺 sdptool 或查询失败），调用方跳过该检查。
    """
    if not shutil.which("sdptool"):
        return None
    res = run("sdptool browse local", check=False, timeout=15, verbose=False)
    if not res:
        return None
    text = (res.stdout or "") + (res.stderr or "")
    if "Failed to connect" in text:
        return None
    return "Handsfree" in text or "Hands-Free" in text or "Hands free" in text


def configure_bluealsa():
    print_status("配置 BlueALSA")
    ba_path = shutil.which("bluealsa")
    if not ba_path:
        raise FileNotFoundError("找不到 bluealsa，请确认已安装 bluez-alsa")

    hci = get_hci_name()
    new_exec = f"{ba_path} -i {hci} -p hsp-hs -p hfp-hf"
    print(f"  -> 使用参数: {new_exec}")
    os.makedirs(os.path.dirname(BLUEALSA_OVERRIDE), exist_ok=True)
    with open(BLUEALSA_OVERRIDE, "w", encoding="utf-8") as f:
        f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")

    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl enable bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("systemctl start bluealsa", check=False, verbose=False)
    time.sleep(2)

    result = run("systemctl is-active bluealsa", check=False, verbose=False)
    if result and result.stdout.strip() == "active":
        print("  -> BlueALSA 服务状态: active")
    else:
        print("  !! BlueALSA 服务未 active，尝试直接启动蓝牙 HF/HS 模式")
        run(
            f"{ba_path} -i {hci} -p hsp-hs -p hfp-hf >/tmp/bluealsa.log 2>&1 &",
            check=False,
            verbose=False,
        )
        time.sleep(2)

    if ensure_bluealsa_dbus(hci):
        print("  -> BlueALSA D-Bus 就绪（org.bluealsa）")
    else:
        print(
            "  !! BlueALSA D-Bus 仍未就绪，请查看 /tmp/bluealsa.log 与 systemctl status bluealsa"
        )

    journal = run(
        "journalctl -u bluealsa -n 120 --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    if journal and "UUID already registered" in (journal.stdout or ""):
        # 区分 A2DP UUID 冲突和 HFP/HSP UUID 冲突
        if "Couldn't register hands-free profile" in (journal.stdout or ""):
            print("  !! 检测到 HFP/HSP UUID 冲突，尝试清理...")
            resolve_uuid_conflict(hci)
        else:
            # 只有 A2DP UUID 冲突，不影响 HFP/HSP 麦克风功能
            print("  -> 仅 A2DP UUID 冲突，不影响 HFP/HSP 麦克风通道，可继续")
    else:
        print("  -> 未检测到 HFP/HSP 注册冲突")

    # 关键验证：Windows 配对时只缓存当时能查到的服务。若 HFP 记录缺失，
    # Windows 永远不发起服务级连接（"已连接但无法选为麦克风"的根因）
    sdp_ok = verify_hfp_sdp_record()
    if sdp_ok is False:
        print("  !! 本机 SDP 缺 Handsfree 记录，重启 bluealsa 重新注册...")
        run("systemctl restart bluealsa", check=False, verbose=False)
        time.sleep(3)
        sdp_ok = verify_hfp_sdp_record()
    if sdp_ok is True:
        print("  -> 已确认本机 SDP 含 Handsfree 服务记录（Windows 可发现 HFP）")
    elif sdp_ok is False:
        print("  !! 重启后仍缺 Handsfree SDP 记录：Windows 配对时看不到 HFP，")
        print("     树莓派将无法被选为麦克风。请查看 journalctl -u bluealsa。")


# ---------------- 免 PIN 配对代理 ----------------


_AGENT_PROC = None


def _spawn_pairing_agent():
    """启动一个常驻的 NoInputNoOutput 配对代理。

    Windows 看到 NoInputNoOutput IO 能力后会走 Just Works 配对，不弹 PIN。
    代理必须保持存活：bluetoothctl 的 agent 注册会随进程退出而失效。
    """
    bt_agent = shutil.which("bt-agent")
    if bt_agent:
        proc = subprocess.Popen(
            [bt_agent, "-c", "NoInputNoOutput"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        # 备选：保持 bluetoothctl 进程存活并注册 agent
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.stdin.write(b"agent NoInputNoOutput\ndefault-agent\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
    time.sleep(1)
    if proc.poll() is None:
        return proc
    return None


def start_pairing_agent():
    global _AGENT_PROC
    _AGENT_PROC = _spawn_pairing_agent()
    if _AGENT_PROC is not None:
        # bt-agent 注册在默认 Agent 路径上，显式设为默认代理
        run("bluetoothctl default-agent", check=False, verbose=False)
        print("  -> 配对代理常驻运行（NoInputNoOutput），Windows 免 PIN 配对就绪")
    else:
        print("  !! 配对代理启动失败，稍后会自动重试")


def ensure_pairing_agent():
    global _AGENT_PROC
    if _AGENT_PROC is not None and _AGENT_PROC.poll() is None:
        return
    print("  -> 配对代理已退出，正在重启...")
    _AGENT_PROC = _spawn_pairing_agent()
    if _AGENT_PROC is not None:
        print("  -> 配对代理已重启")


# ---------------- 蓝牙可见性控制 ----------------
#
# 开机自启动时最大的坑：bluetoothctl 的 discoverable on 命令发出时，
# 蓝牙适配器（brcmfmac UART）往往还没注册完成，命令静默失败，
# 于是服务"正常运行"但电脑永远搜不到树莓派。因此这里：
#   1. 先等适配器就绪（Powered: yes），期间反复 rfkill unblock
#   2. discoverable/pairable 设置后立即校验，失败重试
#   3. 等待连接的循环里周期性重新断言，防止中途状态丢失


def bt_controller_present():
    res = run("bluetoothctl show", check=False, timeout=8, verbose=False)
    if not res or res.returncode != 0:
        return False
    return "Controller" in (res.stdout or "") and "Powered: yes" in (res.stdout or "")


_LAST_BT_RECOVERY = [0.0]
_BT_RECOVERY_COUNT = [0]


def recover_bt_adapter():
    """hci0 未注册时主动恢复：重启 hciuart 重挂固件、拉起 hci0、power on。

    树莓派 4B 的蓝牙芯片挂在 UART 上，固件由 hciuart（hciattach）加载：
    bluetoothd 重启或链路异常后 hci0 可能再也不注册，此时 bluetoothctl
    连控制器都看不到，rfkill unblock / bluetoothctl power on 全部无效
    （本次卡死即属此类）。重启 hciuart 会重新执行 hciattach 加载固件，
    是实测有效的恢复手段；每隔几次同时重启 bluetooth 服务，覆盖
    bluetoothd 自身卡死的情况。内置 30 秒节流，后台等待循环可反复调用。
    """
    now = time.time()
    if now - _LAST_BT_RECOVERY[0] < 30:
        return
    _LAST_BT_RECOVERY[0] = now
    _BT_RECOVERY_COUNT[0] += 1
    print("  !! 蓝牙控制器缺失，尝试恢复（重启 hciuart / 拉起 hci0 / power on）...")
    run(
        "systemctl restart hciuart 2>/dev/null || true",
        check=False,
        timeout=20,
        verbose=False,
    )
    if _BT_RECOVERY_COUNT[0] % 3 == 0:
        run(
            "systemctl restart bluetooth 2>/dev/null || true",
            check=False,
            timeout=20,
            verbose=False,
        )
    run("rfkill unblock bluetooth 2>/dev/null || true", check=False, verbose=False)
    if shutil.which("hciconfig"):
        run(
            "hciconfig hci0 up 2>/dev/null || true",
            check=False,
            timeout=8,
            verbose=False,
        )
    run("bluetoothctl power on", check=False, timeout=8, verbose=False)
    time.sleep(3)


def wait_for_bt_adapter(timeout=90):
    print_status("等待蓝牙适配器就绪（开机时可能需要数秒）")
    start = time.time()
    while time.time() - start < timeout:
        run("rfkill unblock bluetooth 2>/dev/null || true", check=False, verbose=False)
        if bt_controller_present():
            print("  -> 蓝牙适配器已就绪（Powered: yes）")
            return True
        run("bluetoothctl power on", check=False, verbose=False)
        # 控制器不存在（hci0 未注册）时仅 power on 无效：节流式主动恢复，
        # 实际约每 30 秒执行一次（重启 hciuart 重挂固件等）
        recover_bt_adapter()
        time.sleep(3)
    print(f"  !! 蓝牙适配器 {timeout} 秒内未就绪，继续运行（后台会持续重试）")
    return False


def discoverable_state():
    """返回 (discoverable, pairable)；控制器不存在时返回 None。"""
    res = run("bluetoothctl show", check=False, timeout=8, verbose=False)
    if not res or res.returncode != 0:
        return None
    disc = pairable = False
    for line in res.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Discoverable:"):
            disc = "yes" in stripped.lower()
        elif stripped.startswith("Pairable:"):
            pairable = "yes" in stripped.lower()
    return (disc, pairable)


def set_discoverable(on):
    if on:
        run("rfkill unblock bluetooth 2>/dev/null || true", check=False, verbose=False)
        run("bluetoothctl power on", check=False, verbose=False)
        run("bluetoothctl pairable on", check=False, verbose=False)
        run("bluetoothctl pairable-timeout 0", check=False, verbose=False)
        run("bluetoothctl discoverable on", check=False, verbose=False)
        run("bluetoothctl discoverable-timeout 0", check=False, verbose=False)
        # 校验结果：命令可能因适配器未就绪而静默失败，重试几次
        for attempt in range(3):
            state = discoverable_state()
            if state == (True, True):
                print("  -> 蓝牙已设为可发现/可配对，等待 Windows 连接")
                return True
            time.sleep(1)
            run("bluetoothctl discoverable on", check=False, verbose=False)
            run("bluetoothctl pairable on", check=False, verbose=False)
        state = discoverable_state()
        if state is None:
            print("  !! 蓝牙控制器不存在（适配器未就绪？），稍后会自动重试")
            recover_bt_adapter()
        else:
            print(
                f"  !! 可发现性设置未生效（Discoverable={state[0]}, Pairable={state[1]}），"
                "稍后会自动重试"
            )
        return False
    else:
        run("bluetoothctl discoverable off", check=False, verbose=False)
        # pairable 保持开启：已配对设备仍然可以重连/重配对
        print("  -> 设备已连接，蓝牙 discoverable 已关闭")


def ensure_discoverable():
    """确认蓝牙仍可发现；状态丢失或控制器未就绪时重新设置。"""
    state = discoverable_state()
    if state == (True, True):
        return True
    if state is None:
        # 适配器可能尚未注册（开机竞态），先等一小会儿再试
        wait_for_bt_adapter(timeout=15)
    return set_discoverable(True)


def prepare_bt_state():
    print_status("设置蓝牙设备类型与电源")
    run("rfkill unblock bluetooth 2>/dev/null || true", check=False, verbose=False)
    wait_for_bt_adapter()
    run("bluetoothctl power on", check=False, verbose=False)
    hci = get_hci_name()
    run(
        f"hciconfig {hci} class 0x240404 2>/dev/null || true",
        check=False,
        verbose=False,
    )
    start_pairing_agent()
    set_discoverable(True)
    time.sleep(2)


def is_device_connected(device):
    result = run(f"bluetoothctl info {device}", check=False, timeout=8, verbose=False)
    if not result or result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if "Connected:" in line:
            return "yes" in line.lower()
    return False


def get_connected_devices():
    result = run(
        "bluetoothctl devices Connected", check=False, timeout=8, verbose=False
    )
    devices = []
    if result and result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("Device "):
                parts = line.split()
                if len(parts) >= 2:
                    devices.append(parts[1])
    return devices


# ---------------- 等待 Windows 主动连接 ----------------


def wait_for_connection(timeout=CONNECTION_TIMEOUT):
    if timeout > 0:
        print_status(f"等待 Windows 主动连接（最长 {timeout} 秒）")
    else:
        print_status("等待 Windows 主动连接（持续等待，Ctrl+C 停止）")
    print("  免 PIN 配对：在 Windows 蓝牙设置中添加设备并选择树莓派，无需输入配对码")
    print(
        "  若 Windows 仍弹出 PIN 提示，请先在 Windows 蓝牙设置中『删除设备』再重新添加"
    )
    print("  连接后，请到 Windows『声音设置 → 输入』中选择 Hands-Free/Headset 设备，")
    print("  此时 BlueALSA 才会建立 HFP/SCO 麦克风通道。")
    start = time.time()
    last_notice = 0
    last_ensure = 0
    while True:
        if 0 < timeout < time.time() - start:
            return None
        ensure_pairing_agent()
        # 周期性重新断言可发现状态（开机竞态 / 状态丢失时自动恢复）
        if time.time() - last_ensure >= 15:
            ensure_discoverable()
            last_ensure = time.time()
        devices = get_connected_devices()
        if devices:
            print(f"  -> 已检测到设备连接: {devices[0]}")
            return devices[0]
        if time.time() - last_notice >= 30:
            print("  仍在等待 Windows 连接...（程序保持运行）")
            last_notice = time.time()
        time.sleep(3)


def get_device_info(device):
    run(f"bluetoothctl info {device}", check=False, verbose=False)


def trust_device(device):
    run(f"bluetoothctl trust {device}", check=False, verbose=False)


# ---------------- BlueALSA PCM 发现 ----------------


def _mac_variants(device):
    return [
        device.lower(),
        device.upper(),
        device.lower().replace(":", ""),
        device.upper().replace(":", ""),
    ]


def _collect_bluealsa_lines(cmd):
    """运行 PCM 列表命令，返回 stdout 中以 bluealsa: 开头的行（失败返回空）。"""
    tool = cmd.split()[0]
    if not shutil.which(tool):
        return []
    res = run(cmd, check=False, timeout=8, verbose=False)
    if not res or res.returncode != 0:
        return []
    return [
        s.strip()
        for s in res.stdout.splitlines()
        if s.strip().lower().startswith("bluealsa:")
    ]


def list_bluealsa_pcms(device):
    """
    从 bluealsa-aplay -L / bluealsa-cli list-pcms / aplay -L 三个来源
    收集含目标 MAC 的 BlueALSA PCM 名。HFP/SCO 通道存在时会出现形如
    bluealsa:DEV=...,PROFILE=sco 的条目；多来源互相兜底，避免单个
    列表工具缺失或输出格式变化导致误报"找不到 PCM"。
    """
    macs = _mac_variants(device)
    found = []
    for cmd in ("bluealsa-aplay -L", "bluealsa-cli list-pcms", "aplay -L"):
        for s in _collect_bluealsa_lines(cmd):
            if any(m in s.lower() for m in macs):
                found.append(s)
    seen, unique = set(), []
    for c in found:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def fallback_pcm_names(device):
    """按 BlueALSA 标准命名规则直接构造 SCO PCM 名（列表工具失效时兜底）。"""
    names = []
    for d in (device.upper(), device):
        for profile in ("sco", "hfp"):
            names.append(f"bluealsa:DEV={d},PROFILE={profile}")
    return names


def get_bluealsa_pcm_candidates(device):
    """列表工具报出的 PCM 名 + 构造兜底名，由 test_pcm 实测过滤。"""
    seen, unique = set(), []
    for c in list_bluealsa_pcms(device) + fallback_pcm_names(device):
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def hfp_slc_state(device):
    """通过 BlueALSA D-Bus 判断 HFP 服务级连接（SLC）是否建立。

    Windows（AG）先对 BlueALSA 注册的 RFCOMM 通道发起 HFP 服务级连接，
    BlueALSA 才会创建 dev_XX/hfphf/... PCM。返回 (SLC是否建立, 诊断文本)。
    """
    lines = []
    has_transport = False
    for method in ("GetPCMs", "GetDevices"):
        res = run(
            f"dbus-send --system --print-reply --dest=org.bluealsa / "
            f"org.bluealsa.Manager1.{method}",
            check=False,
            timeout=8,
            verbose=False,
        )
        text = ""
        if res:
            text = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
        if text:
            head = text.splitlines()[0].strip() if text.splitlines() else ""
            lines.append(f"$ org.bluealsa.Manager1.{method}  ->  {head}")
            # 返回体里出现 object path 说明 BlueALSA 已拿到 HFP 传输通道
            if "object path" in text:
                has_transport = True
    return has_transport, "\n".join(lines)


def dump_pcm_diagnostics():
    """找不到 SCO PCM 时输出原始诊断信息，便于直接定位 BlueALSA 侧原因。"""
    print("  -- PCM 发现诊断（BlueALSA 原始输出）--")
    for cmd in (
        "bluealsa-aplay -L",
        "bluealsa-cli list-pcms",
        "bluealsa-cli list-devices",
        "aplay -L",
    ):
        if not shutil.which(cmd.split()[0]):
            print(f"  $ {cmd}  ->  (命令不存在)")
            continue
        res = run(cmd, check=False, timeout=8, verbose=False)
        print(f"  $ {cmd}  ->  rc={res.returncode if res is not None else '(超时)'}")
        if res:
            for line in ((res.stdout or "") + "\n" + (res.stderr or "")).splitlines():
                if line.strip():
                    print(f"      {line.strip()}")
    res = run(
        "systemctl is-active bluealsa bluetooth", check=False, timeout=8, verbose=False
    )
    if res:
        for line in (res.stdout or "").splitlines():
            if line.strip():
                print(f"  $ systemctl is-active  ->  {line.strip()}")


def test_pcm(pcm):
    """实测 PCM 是否可打开，返回 (可用?, 失败原因)。"""
    # -t raw 显式声明原始流：/dev/zero 不是 WAV 文件，个别 aplay 版本
    # 解析头部失败会误报设备不可用；SCO 上行恒为单声道
    cmd = (
        f"timeout 2 aplay -t raw -D '{pcm}' -f {FORMAT} -r {SAMPLE_RATE} "
        f"-c 1 /dev/zero 2>&1"
    )
    result = run(cmd, check=False, timeout=5, verbose=False)
    if result is None:
        return False, "aplay 超时"
    if result.returncode in (0, 124):
        return True, ""
    err = " ".join(((result.stdout or "") + " " + (result.stderr or "")).split())
    return False, err[-180:]


def request_hfp_profile(device, uuid="0000111e-0000-1000-8000-00805f9b34fb"):
    """在已有 ACL 链路上请求连接 HFP profile（best-effort，结果打印）。

    BlueALSA 以 -p hfp-hf 运行时向 BlueZ 注册的是 0x111E（Handsfree，
    HF 侧服务），不是 0x111F（0x111F 是 AG 侧服务，Windows 才会注册）。
    uuid 传空字符串 = 请求连接设备全部已发现 profile。

    注意方向性：HFP 的 RFCOMM 只能由 AG（Windows）主动连接 HF（树莓派），
    HF 侧无法真正"拉"起服务级连接；这里 ConnectProfile 主要把 BlueZ 的
    反馈打出来用于诊断，真正建立 SLC 的通常是 Windows 自己在（重）连接
    时的行为。返回 True 仅表示 D-Bus 调用本身被接受，不等于 SLC 已建立。
    """
    dev_path = f"/org/bluez/{get_hci_name()}/dev_{device.replace(':', '_').upper()}"
    arg = f"string:{uuid}" if uuid else ""
    res = run(
        f"dbus-send --system --print-reply --dest=org.bluez {dev_path} "
        f"org.bluez.Device1.ConnectProfile {arg}".strip(),
        check=False,
        timeout=10,
        verbose=False,
    )
    if res is None:
        print("    ConnectProfile: 调用超时/失败（无输出）")
        return False
    out = ((res.stdout or "") + " " + (res.stderr or "")).strip()
    tag = "全部 profile" if uuid == "" else f"UUID {uuid}"
    print(f"    ConnectProfile({tag})  ->  rc={res.returncode} {out[:160]}")
    return res.returncode == 0


def find_working_pcm(device, timeout=90):
    print_status(f"等待 BlueALSA SCO PCM 出现（本轮最长 {timeout} 秒）")
    # 连接后先尝试一次 ConnectProfile。注意方向性（见 request_hfp_profile）：
    # HFP 的 RFCOMM 只能由 Windows（AG）主动连接，HF 侧请求主要用于诊断，
    # 把 BlueZ 的反馈打进日志，判断 Windows 到底有没有能力建立 SLC。
    request_hfp_profile(device)
    start = time.time()
    round_no = 0
    diag_done = False
    forced_disconnect = False
    hfp_variant = 0
    while time.time() - start < timeout:
        round_no += 1
        # 等待期间蓝牙断开：立即返回，主循环会恢复可发现状态，
        # 缩小 Windows 重连时需要反复尝试的窗口
        if round_no > 1 and not is_device_connected(device):
            print("  -> 等待 PCM 期间检测到蓝牙断开")
            return None
        listed = list_bluealsa_pcms(device)
        candidates = listed + [c for c in fallback_pcm_names(device) if c not in listed]
        detailed = round_no == 1 or round_no % 5 == 0
        for pcm in candidates:
            ok, err = test_pcm(pcm)
            if ok:
                print(f"  ** 找到可用 PCM: {pcm}")
                return pcm
            if detailed:
                print(f"  测试: {pcm}  ->  不可用{('：' + err) if err else ''}")

        # BlueALSA D-Bus 直接反映 Windows 是否已发起 HFP 服务级连接（SLC），
        # 这是 SCO PCM 出现的前提，也是"已连接但无法选为麦克风"的直接判据
        slc_up, slc_text = hfp_slc_state(device)
        if detailed:
            print(
                f"  （第 {round_no} 轮）HFP 服务级连接: {'已建立' if slc_up else '未建立'}"
            )
            if slc_text:
                print(f"    {slc_text}")
            if slc_up:
                print("    SLC 已建立但仍打不开 PCM：通道可能被残留 aplay 独占，")
                print("    或 Windows 尚未启用录音（声音设置→输入 选中后才建 SCO）。")
            else:
                print("    Windows 尚未发起 HFP 服务级连接。它只对配对时缓存到的")
                print("    服务发起：若缓存里没有 HFP，将永远不发起（见下方指引）。")

        if not listed:
            print(
                f"  （第 {round_no} 轮）列表工具未报出含本机 MAC 的 PCM，已用"
                "构造名实测。"
            )
            if not diag_done and round_no >= 2:
                diag_done = True
                dump_pcm_diagnostics()
        elif detailed:
            print(
                f"  （第 {round_no} 轮）{len(candidates)} 个 PCM 候选均不可用："
                "SCO 尚未建立，或通道被残留的 aplay 占用"
            )

        # 每 5 轮（约 15 秒）轮换 UUID 重试 ConnectProfile，并顺手清掉被
        # socket 重新拉起的冲突进程（它们抢注 HFP UUID 会让 bluealsa 注册
        # 失败、SCO 永远不出现）；杀掉后重启 bluealsa 重新占有 UUID
        if round_no % 5 == 0:
            uuids = (
                "0000111e-0000-1000-8000-00805f9b34fb",
                "",
                "0000111f-0000-1000-8000-00805f9b34fb",
            )
            request_hfp_profile(device, uuid=uuids[hfp_variant % len(uuids)])
            hfp_variant += 1
            if kill_bt_profile_conflicts():
                print("    -> 清理了重新出现的冲突进程，重启 bluealsa 重新注册 profile")
                run("systemctl restart bluealsa", check=False, verbose=False)
                time.sleep(3)

        # 约 25 秒仍无 SLC：强制断开 ACL 触发 Windows 自动重连。实测旧版
        # 就是 Windows 重连之后才拉起 HFP 并成功传送的——干等会等到超时
        if not slc_up and not forced_disconnect and time.time() - start >= 25:
            forced_disconnect = True
            print("  -> 25 秒仍无 HFP 服务级连接：主动断开蓝牙，触发 Windows")
            print("     自动重连并重试 Hands-Free 服务（Windows 数秒内重连）")
            run(
                f"bluetoothctl disconnect {device}",
                check=False,
                timeout=15,
                verbose=False,
            )
            print("  -> 若重连后依旧如此，说明 Windows 缓存的设备服务里没有 HFP：")
            print("     请在 Windows 蓝牙设置中删除该设备，等本程序显示可发现后")
            print("     重新添加配对（配对那一刻树莓派必须已注册好 HFP）。")

        time.sleep(3)

    # 额外诊断
    print("  !! 本轮未找到可用 PCM，再次请求链路上的 HFP profile")
    request_hfp_profile(device)
    time.sleep(5)

    candidates = get_bluealsa_pcm_candidates(device)
    for pcm in candidates:
        if test_pcm(pcm)[0]:
            return pcm

    print("  !! 仍未找到 PCM。请尝试：")
    print("     1. 在 Windows 蓝牙设置中删除该设备，重新配对；")
    print("     2. 配对后点开『声音设置』，把输入设备选为 Hands-Free/Headset；")
    print("     3. 打开任意录音软件，让 Windows 真正启用麦克风通道；")
    print("     4. 检查 Windows 是否把树莓派识别为“耳机”而不是“音箱”。")
    if not diag_done:
        dump_pcm_diagnostics()
    return None


def kill_stale_audio_pipelines():
    """
    清理上一实例残留的转发子进程。旧 aplay 独占 SCO 时，新实例会测不
    通任何 PCM（EBUSY）却仍能"听到声音"（旧管道继续推流）——先杀干净
    再探测。模式按命令行结尾精确匹配，不会误杀带 --benchmark 等参数
    的降噪基准进程。
    """
    if not shutil.which("pkill"):
        return
    for pat in (
        "'/tmp/df_denoise.py$'",
        "'/tmp/df_spec_denoise.py$'",
        "'/tmp/df_downmix.py$'",
        "'/tmp/bt_gain.py$'",
        "'aplay .*bluealsa:.*--period-time'",
        "'arecord .*--period-time'",
    ):
        run(f"pkill -f {pat} 2>/dev/null", check=False, timeout=8, verbose=False)


# ---------------- 麦克风自动探测 ----------------


def probe_input_devices():
    devices = []
    result = run("arecord -l", check=False, timeout=8, verbose=False)
    if not result or result.returncode != 0:
        return devices

    for line in result.stdout.splitlines():
        line = line.strip()
        m_card = re.match(r"^card\s+(\d+):\s*(.*)$", line)
        if not m_card:
            continue
        try:
            card = int(m_card.group(1))
        except ValueError:
            continue
        rest = m_card.group(2)
        m_dev = re.search(r",\s*device\s+(\d+):\s*(.*?)(?:\s*\[[^\]]*\])?\s*$", rest)
        if not m_dev:
            continue
        try:
            dev = int(m_dev.group(1))
        except ValueError:
            continue
        card_part = rest[: m_dev.start()].strip()
        card_name = re.sub(r"\s*\[.*?\]\s*$", "", card_part).strip()
        dev_name = m_dev.group(2).strip()
        label = f"{card_name} / {dev_name}".strip(" /")
        devices.append((card, dev, label))
    return devices


def verify_mic(dev, channels=CHANNELS):
    print(f"  测试录音设备 {dev}（{channels} 通道）...")
    cmd = (
        f"arecord -t raw -D '{dev}' -f {FORMAT} -r {SAMPLE_RATE} "
        f"-c {channels} -d 1 /dev/null 2>&1"
    )
    result = run(cmd, check=False, timeout=10, verbose=False)
    if result and result.returncode == 0:
        print(f"  -> 麦克风 {dev} 可用（{channels} 通道）")
        return True
    if result:
        err = (result.stdout or "").strip().splitlines()
        tail = err[-1] if err else "未知错误"
        print(f"  !! {dev}（{channels} 通道）不可用: {tail}")
    else:
        print(f"  !! {dev} 测试超时")
    return False


def select_and_verify_mic():
    print_status("探测麦克风输入设备")
    devices = probe_input_devices()
    candidates = []

    env_dev = os.environ.get("MIC_DEVICE")
    if env_dev:
        candidates.append((env_dev, CHANNELS))
        print(f"  -> 使用环境变量 MIC_DEVICE={env_dev}")

    usb = [d for d in devices if "usb" in d[2].lower() or "microphone" in d[2].lower()]
    other = [d for d in devices if d not in usb]
    for card, dev, name in usb + other:
        d = f"hw:{card},{dev}"
        print(f"  -> 发现录音设备: {d} ({name})")
        candidates.append((f"plughw:{card},{dev}", 1))
        candidates.append((d, 1))
        candidates.append((f"plughw:{card},{dev}", 2))
        candidates.append((d, 2))

    candidates.append(("plughw:0,0", 1))
    candidates.append(("hw:0,0", 1))

    seen = set()
    for dev, ch in candidates:
        key = (dev, ch)
        if key in seen:
            continue
        seen.add(key)
        if verify_mic(dev, ch):
            return dev, ch
    return None, None


# ---------------- DeepFilterNet 降噪 ----------------

# 降噪脚本要点（针对 2~3 秒延迟的根因）：
# 旧版按 10ms 小块调用模型推理，每次调用的 Python/torch 固定开销就超过
# 10ms，处理速度 < 实时（RTF>1），管道积压持续增长直至撑满 64KB 管道
# （约 2 秒音频）——这就是 2~3 秒延迟的来源。
# 新版：
#   1. 每次处理 DF_CHUNK_MS（默认 160ms）的大块，摊薄每调用的固定开销
#   2. 限制 torch 线程数（Pi4 上 2 线程最优，减少线程同步开销）
#   3. 0.5.x 模型只吃 48kHz：内置纯 numpy 多相 FIR 重采样（16k→48k→16k）
#   4. 积压截断：积压超过上限时丢弃最旧的音频（跳音保低延迟），
#      延迟封顶而不是无限增长
#   5. 每 5 秒向 stderr 打印 RTF/积压，便于 journalctl 观察
DENOISE_SCRIPT_CONTENT = r"""
import os

# 必须在导入 torch/df 之前设置 OpenMP 线程数
os.environ.setdefault("OMP_NUM_THREADS", str(__DF_THREADS__))

import sys
import math
import time

import numpy as np

import df

SR = __SR__
CHANNELS = __CHANNELS__   # 采集声道数：1 或 2
CHUNK_MS = __CHUNK_MS__
MAX_BACKLOG_MS = __MAX_BACKLOG_MS__
DF_THREADS = __DF_THREADS__
DF_MODEL = __DF_MODEL__   # 模型选择：deepfilternet2 等库内名/本地路径/空串=库默认（DF3）
BENCH_LIMIT = __BENCH_LIMIT__
RTF_EXIT_SECS = __RTF_EXIT_SECS__
PREFIX_MS = __PREFIX_MS__  # 块头预热前缀：喂真实历史音频吸收 GRU/卷积冷启动与零填充
EDGE_MS = __EDGE_MS__       # 块尾丢弃边缘：覆盖 lookahead=2 与库补零产生的尾部垃圾
XFADE_MS = __XFADE_MS__     # 相邻窗口接缝交叉淡化宽度
POST_FILTER = __POST_FILTER__  # 后置滤波器 PF（额外降噪，静音衰减更大）
SPEC_POST_FLOOR = __SPEC_POST_FLOOR__  # >0：模型后追加轻量谱减法门（增益下限）


def log(msg):
    print("[denoise] %s" % msg, file=sys.stderr, flush=True)


# ---------------- 16k <-> 48k 多相 FIR 重采样（纯 numpy） ----------------
# 0.5.x 的 init_df/enhance 只支持 48kHz 全频带模型，HFP/mSBC 上行是 16kHz，
# 因此在模型前后各接一级整数比（×3/÷3）多相 FIR 重采样。窗函数 sinc 低通
# 原型滤波器 144 抽头（每相位 48 抽头），截止 7.5kHz（mSBC 有效频带上限约
# 7kHz，带外镜像由阻带抑制），滤波器状态跨块保持，无块间边界伪影。
_RES_K = 48  # 每相位抽头数
_RES_N = 3 * _RES_K


def _resample_h():
    cutoff = 7500.0 / 48000.0
    n = np.arange(_RES_N, dtype=np.float64)
    h = np.sinc(2.0 * cutoff * (n - (_RES_N - 1) / 2.0)) * np.hamming(_RES_N)
    up = (h * (3.0 / h.sum())).astype(np.float32)    # 上采样通带增益 = ×3
    down = (h * (1.0 / h.sum())).astype(np.float32)  # 下采样通带增益 = 1
    return up, down


_UP_H, _DOWN_H = _resample_h()


def resample_up(x, state):
    # 16kHz -> 48kHz（×3）；state: 最近 _RES_K-1 个 16k 输入样本
    xall = np.concatenate([state, x])
    y = np.empty(3 * len(x), dtype=np.float32)
    for p in range(3):
        y[p::3] = np.convolve(xall, _UP_H[p::3][::-1], mode="valid")
    return y, xall[-(_RES_K - 1):].copy()


def resample_down(y, state):
    # 48kHz -> 16kHz（÷3）；state: 最近 _RES_N-1 个 48k 输入样本。
    # 144 抽头全核 + _RES_N-1 状态使 'valid' 卷积长度恒等于 len(y)，
    # 无尾部补零，跨块输出与一次性处理逐样本一致
    yall = np.concatenate([state, y])
    conv = np.convolve(yall, _DOWN_H[::-1], mode="valid")
    return conv[::3][:len(y) // 3].astype(np.float32), yall[-(_RES_N - 1):].copy()


def main():
    try:
        import torch
        torch.set_num_threads(DF_THREADS)
        try:
            torch.set_num_interop_threads(1)  # 减少线程池调度开销
        except Exception:
            pass
    except Exception:
        torch = None

    # 打印 df 包信息，便于排查基准失败原因（不同 df 包的 API 完全不同）
    log("df API: %s" % ", ".join(sorted(a for a in dir(df) if not a.startswith("_"))))
    if not (hasattr(df, "init_df") and hasattr(df, "enhance")):
        log("该 df 包没有 init_df/enhance（需要 PyPI deepfilternet 0.5.x），"
            "无法使用 DeepFilterNet，改用轻量谱减法")
        return 2

    # 0.5.x 模型只支持 48kHz：16kHz 麦克风输入经内置多相重采样（16k→48k→16k）。
    # DF_MODEL 用库内预训练模型名（deepfilternet2 等）映射到 init_df 的
    # model_base_dir；其他值按本地模型目录路径处理，空串用库默认（DF3）。
    PRETRAINED = ("DeepFilterNet", "DeepFilterNet2", "DeepFilterNet3")
    model_dir = None
    if DF_MODEL:
        model_dir = {
            "deepfilternet2": "DeepFilterNet2",
            "deepfilternet3": "DeepFilterNet3",
            "deepfilternet": "DeepFilterNet",
        }.get(DF_MODEL.lower(), DF_MODEL)
        # 预训练模型按需联网下载且无超时，缓存缺失时快速失败并给出指引，
        # 避免服务启动卡在 GitHub 下载上十几分钟
        if model_dir in PRETRAINED:
            cache_root = os.path.join(os.path.expanduser("~"), ".cache",
                                      "DeepFilterNet")
            if not (os.path.isfile(os.path.join(cache_root, model_dir, "config.ini"))
                    or os.path.isdir(os.path.join(cache_root, model_dir,
                                                  "checkpoints"))):
                log("模型未下载：%s/%s 缺少 config.ini/checkpoints。请联网时先"
                    "运行一次：sudo %s -c \"from df import init_df; "
                    "init_df('%s')\"，或手动解压模型 zip 到 %s/"
                    % (cache_root, model_dir, sys.executable, model_dir,
                       cache_root))
                return 2
        log("loading DeepFilterNet (model=%s -> %s) ..." % (DF_MODEL, model_dir))
    else:
        log("loading DeepFilterNet（库默认模型）...")
    try:
        model, df_state, suffix = df.init_df(model_base_dir=model_dir,
                                             log_level="ERROR",
                                             post_filter=bool(POST_FILTER))
    except Exception as exc:
        log("init_df failed: %s" % exc)
        return 2
    log("model loaded: %s, sr=%d Hz（输入 %d Hz，内置多相重采样，PF 后置滤波=%s）"
        % (suffix, df_state.sr(), SR, "开" if POST_FILTER else "关"))

    # 流式降噪管线：16k -> 48k 重采样 -> 模型 -> 16k 重采样。重采样状态跨块
    # 保持，块边界连续；enhance 内部 pad 对齐，输出长度恒等于输入长度。
    # 0.5.x 的 df.enhance 每次调用都重置 GRU/滤波器状态：GRU 从零起步、
    # 编码器 conv 与通路卷积（核 5）在调用头补零、多帧滤波器头尾各补零、
    # conv_lookahead=2 在调用尾追加 2 帧零特征、库还会在输入尾追加 n_fft
    # 零样本——损坏的上下文合计约 40~60ms。直接拼接各块输出会在每个块
    # 边界产生周期性低频爆音（160ms 块即 6.25Hz 的"噗噗"声）。
    # 改为"预热窗口"推理：窗口 = PREFIX（真实历史音频，供 GRU/卷积预热，
    # 其输出丢弃）+ 本块 + EDGE（前瞻/尾部，lookahead 与库补零产生的
    # 不可信输出丢弃）。本块输出两侧都有完整真实上下文。相邻窗口即使
    # 预热深度不同仍可能有微小掩码差，接缝处再做 XFADE 宽度线性交叉
    # 淡化（前一块输出延伸 XFADE 与新块头部混合），接缝听感完全连续。
    # 代价：每个窗口重复计算 PREFIX+EDGE+XFADE（约 100ms 开销），输出
    # 比输入滞后 PREFIX（默认 50ms）。第一块预热不足时无输出，由主循环
    # 补零。可选 SPEC_POST_FLOOR>0 时在模型后追加轻量谱减法门。
    prefix = max(0, int(PREFIX_MS * SR * 3 // 1000))
    edge = max(0, int(EDGE_MS * SR * 3 // 1000))
    xf_cell = [max(0, int(XFADE_MS * SR * 3 // 1000))]
    up_state = [np.zeros(_RES_K - 1, dtype=np.float32)]
    down_state = [np.zeros(_RES_N - 1, dtype=np.float32)]
    edge_buf = [np.zeros(0, dtype=np.float32)]   # 48k 输入侧（历史+本块+前瞻+淡化余量）
    out_buf = [np.zeros(0, dtype=np.float32)]    # 48k 输出侧（待下采样）
    wins = []                                    # 最近两个窗口的可信段（win_unit+xf）
    win_unit = [0]                               # 首块确定的标准窗口块长（48k）

    # ---- 可选后置谱减法门（SPEC_POST_FLOOR>0 时启用） ----
    # 帧 32ms / 跳 8ms，Wiener 增益 + 噪声 PSD 慢速跟踪，增益下限为
    # SPEC_POST_FLOOR（如 0.4 ≈ 最多 -8dB 附加抑制），语音帧增益≈1。
    # 与 DeepFilterNet 级联进一步压低宽带噪声地板（静音衰减更大）。帧
    # 提取按全局跳距对齐（每块保留最后 384 样本供下一块帧对齐），OLA
    # 固定延迟 24ms，输出长度与输入一致，无块边界伪影。
    _SG_FFT = 512
    _SG_HOP = _SG_FFT // 4
    _SG_WIN = np.hanning(_SG_FFT).astype(np.float32)
    _sg_fifo = [np.zeros(0, dtype=np.float32)]
    _sg_tail = [np.zeros(_SG_FFT, dtype=np.float32)]
    _sg_base = [0]
    _sg_emitted = [0]
    _sg_psd = [None]
    _sg_n = [0]

    def spec_gate(x):
        fifo = np.concatenate([_sg_fifo[0], x])
        total = len(fifo)
        m = (total - _SG_FFT) // _SG_HOP + 1 if total >= _SG_FFT else 0
        frames = []
        for k in range(m):
            frame = fifo[k * _SG_HOP:k * _SG_HOP + _SG_FFT]
            spec = np.fft.rfft(frame * _SG_WIN)
            pxx = np.abs(spec) ** 2
            if _sg_psd[0] is None or _sg_n[0] < 6:
                _sg_psd[0] = pxx if _sg_psd[0] is None else 0.85 * _sg_psd[0] + 0.15 * pxx
            else:
                noise = pxx < _sg_psd[0] * 2.0
                _sg_psd[0] = np.where(noise, 0.98 * _sg_psd[0] + 0.02 * pxx,
                                      _sg_psd[0] * 1.0005)
            gain = np.maximum(1.0 - _sg_psd[0] / np.maximum(pxx, 1e-10),
                              SPEC_POST_FLOOR)
            # 分析+合成都乘了 hanning（双窗），1/4 跳距下 OLA 和为 1.5，
            # 乘 2/3 归一，否则直通时整体被放大 1.5 倍（+1.8 dB）
            frames.append(np.fft.irfft(spec * gain) * _SG_WIN * (2.0 / 3.0))
            _sg_n[0] += 1
        _sg_fifo[0] = fifo[m * _SG_HOP:]
        if m == 0:
            return np.zeros(0, dtype=np.float32)
        ola = np.zeros(m * _SG_HOP + _SG_FFT, dtype=np.float32)
        for i, fr in enumerate(frames):
            ola[i * _SG_HOP:i * _SG_HOP + _SG_FFT] += fr
        ola[:_SG_FFT] += _sg_tail[0]
        _sg_tail[0] = ola[m * _SG_HOP:]
        start = _sg_emitted[0] - _sg_base[0]
        _sg_base[0] += m * _SG_HOP
        take = _sg_base[0] - _sg_emitted[0]
        out = ola[start:start + take] if take > 0 else np.zeros(0, dtype=np.float32)
        _sg_emitted[0] += len(out)
        return out

    def reset_stream():
        # 基准测试切换块大小时调用：清空全部流状态，避免跨大小串扰
        up_state[0] = np.zeros(_RES_K - 1, dtype=np.float32)
        down_state[0] = np.zeros(_RES_N - 1, dtype=np.float32)
        edge_buf[0] = np.zeros(0, dtype=np.float32)
        out_buf[0] = np.zeros(0, dtype=np.float32)
        del wins[:]
        win_unit[0] = 0
        _sg_fifo[0] = np.zeros(0, dtype=np.float32)
        _sg_tail[0] = np.zeros(_SG_FFT, dtype=np.float32)
        _sg_base[0] = 0
        _sg_emitted[0] = 0
        _sg_psd[0] = None
        _sg_n[0] = 0

    def denoise(audio):
        # audio: float32 [1, T] @16kHz -> float32 [1, T] @16kHz
        # （稳态后输出长度恒等于 T；最初一两块可能不足 T，由主循环补零）
        y, up_state[0] = resample_up(audio.reshape(-1), up_state[0])
        unit = len(y)
        if win_unit[0] == 0:
            win_unit[0] = unit
            if xf_cell[0] > win_unit[0] // 4:
                xf_cell[0] = win_unit[0] // 4
        xf = xf_cell[0]
        edge_buf[0] = np.concatenate([edge_buf[0], y])
        while len(edge_buf[0]) >= prefix + win_unit[0] + edge + xf:
            win = edge_buf[0][:prefix + win_unit[0] + edge + xf]
            inp = win.reshape(1, -1)
            if torch is not None:
                inp = torch.from_numpy(inp)
            e = np.asarray(df.enhance(model, df_state, inp)).reshape(-1)
            seg = e[prefix:prefix + win_unit[0] + xf]  # 本块 + 向下一窗口延伸 xf
            wins.append(seg)
            if xf <= 0 or len(wins) == 1:
                out = seg[:win_unit[0]]
            else:
                w = (np.arange(xf, dtype=np.float32) + 0.5) / xf
                a = wins[-2]
                out = np.concatenate([
                    (1.0 - w) * a[win_unit[0]:win_unit[0] + xf] + w * wins[-1][:xf],
                    a[xf:win_unit[0] - xf],
                    a[win_unit[0] - xf:win_unit[0]],
                ])
            if len(wins) > 2:
                wins.pop(0)
            out_buf[0] = np.concatenate([out_buf[0], out])
            edge_buf[0] = edge_buf[0][win_unit[0]:]
        if len(out_buf[0]) >= unit:
            z48, out_buf[0] = out_buf[0][:unit], out_buf[0][unit:]
        else:
            z48, out_buf[0] = out_buf[0], out_buf[0][0:0]
        z, down_state[0] = resample_down(z48, down_state[0])
        if SPEC_POST_FLOOR > 0:
            z = spec_gate(z)
        return z.reshape(1, -1)

    if "--preload-only" in sys.argv:
        log("preload ok")
        return 0

    chunk = max(1, SR * CHUNK_MS // 1000)

    if "--benchmark" in sys.argv:
        # 一次进程内实测多个块大小的 RTF（只加载一次模型），供启动逻辑
        # 挑选最小的达标块：块越小处理延迟越低；每次模型推理调用的
        # 固定开销在小块上占比高，所以不达标的候选会显示 RTF 明显偏大。
        # 每个大小处理 2 秒噪声计时，最后输出 "benchmark done: ms:rtf,..."
        sizes = []
        for arg in sys.argv[sys.argv.index("--benchmark") + 1:]:
            if arg.isdigit():
                sizes.append(int(arg))
        if not sizes:
            sizes = [CHUNK_MS]
        results = []
        for cs in sizes:
            ch = max(1, SR * cs // 1000)
            rng = np.random.RandomState(0)
            noise = (rng.randn(ch).astype(np.float32) * 0.05).reshape(1, -1)
            log("benchmark chunk=%dms warmup ..." % cs)
            reset_stream()  # 切换块大小时清空流状态，保证各大小独立实测
            try:
                denoise(noise)
            except Exception as exc:
                log("benchmark chunk=%dms warmup failed: %s" % (cs, exc))
                continue
            total = int(2.0 * SR)
            done = 0
            t0 = time.time()
            while done < total:
                denoise(noise)
                done += ch
            elapsed = time.time() - t0
            rtf = elapsed / (done / SR)
            results.append((cs, rtf))
            log("benchmark chunk=%dms rtf=%.2f (阈值 %.2f)"
                % (cs, rtf, BENCH_LIMIT))
        log("benchmark done: %s"
            % ",".join("%d:%.3f" % (cs, r) for cs, r in results))
        return 0

    # 每次处理的采样数（输出长度与输入一致：重采样与模型内部 STFT
    # 跳距都不改变块长）
    max_backlog = max(chunk, SR * MAX_BACKLOG_MS // 1000)
    step = CHANNELS * 2  # 一个采样帧的字节数
    log("model loaded, chunk=%d samples (%d ms), threads=%d"
        % (chunk, chunk * 1000 // SR, DF_THREADS))

    in_fd = sys.stdin.buffer.fileno()
    # 非阻塞排空：把内核管道缓冲里的积压全部读到自己手里，
    # 否则积压会滞留在 64KB 管道里（约 2 秒音频），截断逻辑看不到它
    try:
        import fcntl
        import select as _select

        fcntl.fcntl(in_fd, fcntl.F_SETFL, os.O_NONBLOCK)

        def wait_and_drain(timeout=0.05):
            r, _, _ = _select.select([in_fd], [], [], timeout)
            if not r:
                return 0
            got = 0
            while True:
                try:
                    raw = os.read(in_fd, 1 << 16)
                except BlockingIOError:
                    break
                if not raw:
                    raise EOFError
                pending.append(raw)
                got += len(raw)
            return got
    except (ImportError, OSError, ValueError):
        # 无 fcntl 的环境（如 Windows 测试）：阻塞 os.read 同样立即返回
        # 管道里当前已有的数据（注意不能用 BufferedReader.read(n)，
        # 它会阻塞直到读满 n 字节，凭空引入数秒延迟）
        def wait_and_drain(timeout=0.05):
            try:
                raw = os.read(in_fd, 1 << 16)
            except OSError:
                raise EOFError
            if not raw:
                raise EOFError
            pending.append(raw)
            return len(raw)

    # 缩小输出管道：下游（aplay/SCO）卡顿时旧音频最多积压约 256ms，
    # 而不是默认 64KB 的约 2 秒
    try:
        import fcntl as _fcntl
        _fcntl.fcntl(sys.stdout.buffer.fileno(), _fcntl.F_SETPIPE_SZ, 8192)
    except Exception:
        pass

    pending = []  # 字节块列表，避免大 bytes 反复拼接
    pending_bytes = 0
    t_start = time.time()
    proc_sec = 0.0
    audio_sec = 0.0
    cum_proc = 0.0
    cum_audio = 0.0
    session_start = time.time()
    eof = False
    # 降噪效果监测：噪声地板（静音判定）+ 窗口内输入/输出功率
    noise_floor = 0.0
    win_in_pow = 0.0
    win_out_pow = 0.0
    win_n = 0
    quiet_in_pow = 0.0
    quiet_out_pow = 0.0
    quiet_n = 0
    while True:
        if not eof:
            try:
                pending_bytes += wait_and_drain()
            except EOFError:
                eof = True

        # 积压截断：处理跟不上采集时（RTF>1）丢弃最旧音频，
        # 使延迟封顶在 MAX_BACKLOG_MS，而不是无限增长
        if pending_bytes > max_backlog * step:
            drop = pending_bytes - max_backlog * step
            drop -= drop % step
            log("backlog 超限，丢弃 %.0f ms（跳音保低延迟）"
                % (drop / step * 1000.0 / SR))
            while pending and drop >= len(pending[0]):
                drop -= len(pending[0])
                pending.pop(0)
            if drop and pending:
                pending[0] = pending[0][drop:]
            pending_bytes = sum(len(b) for b in pending)

        n = pending_bytes // step
        if n < chunk:
            if eof and n > 0:
                take = n  # 输入已结束：不足一块的尾块也处理掉
            elif eof and n == 0:
                break
            else:
                continue
        else:
            take = min(n, chunk)
        need = take * step
        parts = []
        while pending and need >= len(pending[0]):
            parts.append(pending.pop(0))
            need -= len(parts[-1])
        if need and pending:
            parts.append(pending[0][:need])
            pending[0] = pending[0][need:]
        pending_bytes -= take * step
        data = b"".join(parts)

        raw_int = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if CHANNELS == 2:
            audio = raw_int.reshape(-1, 2).mean(axis=1)
        else:
            audio = raw_int
        audio = audio.reshape(1, -1)

        t0 = time.time()
        try:
            processed = denoise(audio)
        except Exception as exc:
            log("process failed: %s" % exc)
            return 3
        proc_sec += time.time() - t0
        audio_sec += take / SR

        out = np.asarray(processed).reshape(-1)
        padded = False
        if out.shape[0] < take:
            out = np.pad(out, (0, take - out.shape[0]))
            padded = True
        elif out.shape[0] > take:
            out = out[:take]
        # 降噪效果监测：静音块（无语音）用于估计噪声衰减量。预热窗口
        # 启动期（PREFIX_MS>0）前几块返回的是补零输出，不是真实降噪结果，
        # 计入监测会算出假的衰减值（输入有能量、输出为零）
        if not padded:
            p_in = float(np.mean(audio ** 2))
            p_out = float(np.mean(out ** 2))
            win_in_pow += p_in * take
            win_out_pow += p_out * take
            win_n += take
            # 噪声地板最小跟踪：低于地板时快速下压，否则极缓慢上浮。
            # 只下压不上升会卡在启动时的低值（阈值 4 倍地板始终低于真实
            # 噪声功率），导致"静音衰减"永远显示 0.0
            if noise_floor <= 0.0 or p_in < noise_floor * 2.0:
                noise_floor = p_in if noise_floor <= 0.0 else 0.95 * noise_floor + 0.05 * p_in
            else:
                noise_floor *= 1.0002
            if p_in < noise_floor * 4.0:
                quiet_in_pow += p_in * take
                quiet_out_pow += p_out * take
                quiet_n += take
        out = (out * 32768.0).astype(np.int16)
        try:
            sys.stdout.buffer.write(out.tobytes())
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            break

        if time.time() - t_start >= 5.0:
            rtf = proc_sec / audio_sec if audio_sec > 0 else 0.0
            bl_ms = pending_bytes // step * 1000.0 / SR
            cum_proc += proc_sec
            cum_audio += audio_sec
            in_db = 10.0 * math.log10(win_in_pow / win_n) if win_n and win_in_pow > 0 else -120.0
            out_db = 10.0 * math.log10(win_out_pow / win_n) if win_n and win_out_pow > 0 else -120.0
            q_db = 0.0
            if quiet_n and quiet_in_pow > 0 and quiet_out_pow > 0:
                q_db = 10.0 * math.log10(quiet_in_pow / quiet_out_pow)
            log("rtf=%.2f backlog=%.0f ms in=%.1f out=%.1f dBFS 静音衰减=%.1f dB%s"
                % (rtf, bl_ms, in_db, out_db, q_db,
                   "" if rtf < 1.0 else "（RTF>1：处理跟不上采集）"))
            # 持续处理不过来（真实 RTF 长期 >1）→ 退出并让启动器换成轻量降噪
            if (
                cum_audio > 0
                and time.time() - session_start > RTF_EXIT_SECS
                and cum_proc / cum_audio > 1.15
            ):
                log("持续 RTF=%.2f > 1.15，模型在本机处理速度不足，"
                    "退出(10)建议切换到轻量降噪" % (cum_proc / cum_audio))
                return 10
            proc_sec = 0.0
            audio_sec = 0.0
            t_start = time.time()
            win_in_pow = 0.0
            win_out_pow = 0.0
            win_n = 0
            quiet_in_pow = 0.0
            quiet_out_pow = 0.0
            quiet_n = 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def write_denoise_script(path, channels, chunk_ms=None):
    content = (
        DENOISE_SCRIPT_CONTENT.replace("__SR__", str(SAMPLE_RATE))
        .replace("__CHANNELS__", str(channels))
        .replace("__CHUNK_MS__", str(chunk_ms if chunk_ms else DF_CHUNK_MS))
        .replace("__MAX_BACKLOG_MS__", str(DF_MAX_BACKLOG_MS))
        .replace("__DF_THREADS__", str(DF_NUM_THREADS))
        .replace("__DF_MODEL__", repr(str(DF_MODEL)))
        .replace("__BENCH_LIMIT__", str(DF_BENCH_LIMIT))
        .replace("__RTF_EXIT_SECS__", str(DF_RTF_EXIT_SECS))
        .replace("__PREFIX_MS__", str(DF_PREFIX_MS))
        .replace("__EDGE_MS__", str(DF_EDGE_MS))
        .replace("__XFADE_MS__", str(DF_XFADE_MS))
        .replace("__POST_FILTER__", str(DF_POST_FILTER))
        .replace("__SPEC_POST_FLOOR__", repr(float(DF_SPEC_POST)))
    )
    path.write_text(content)


# ---------------- 轻量降噪（纯 numpy 谱减法，零模型加载） ----------------
#
# 当 DeepFilterNet 在树莓派上实测处理速度达不到实时（RTF>1）时自动切换到这里。
# 帧 32ms / 跳 8ms，Wiener 增益 + 噪声 PSD 指数平滑估计，CPU 开销可忽略、
# 加载时间为零，但降噪强度弱于 DeepFilterNet（适合救急/低延迟优先）。
SPEC_SCRIPT_CONTENT = r"""
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")

import sys
import math
import time

import numpy as np

SR = __SR__
CHANNELS = __CHANNELS__   # 采集声道数：1 或 2
FFT = __SPEC_FFT__        # 帧长（采样）
HOP = FFT // 4            # 跳距
OLA_GAIN = FFT / (2.0 * HOP)  # hanning 窗 1/4 跳距的 OLA 幅度和（归一用）
FLOOR = __SPEC_FLOOR__    # 增益下限
MAX_BACKLOG_MS = __MAX_BACKLOG_MS__  # 允许的最大输入积压（超了丢最旧音频保低延迟）
NOISE_SMOOTH = 0.5        # 噪声 PSD 更新系数
GAIN_SMOOTH = 0.35        # 增益平滑系数


def log(msg):
    print("[spec] %s" % msg, file=sys.stderr, flush=True)


def main():
    window = np.hanning(FFT).astype(np.float32)
    noise_psd = np.full(FFT // 2 + 1, 1e-6, dtype=np.float32)
    gain_smooth = np.ones(FFT // 2 + 1, dtype=np.float32)
    noise_level = 0.0  # 时域噪声能量估计（用于 VAD）
    acc = np.zeros(FFT, dtype=np.float32)  # 重叠相加（OLA）累加缓冲
    zeros_hop = np.zeros(HOP, dtype=np.float32)
    carry = np.zeros(0, dtype=np.float32)  # 跨 chunk 的剩余样本（保证不丢样本）
    learn_until = time.time() + 0.5  # 前 0.5 秒强制学习噪声模型
    win_in_pow = 0.0
    win_out_pow = 0.0
    win_n = 0
    quiet_in_pow = 0.0
    quiet_out_pow = 0.0
    quiet_n = 0
    step = CHANNELS * 2
    max_backlog = max(FFT, SR * MAX_BACKLOG_MS // 1000)  # 积压上限（采样数）
    in_fd = sys.stdin.buffer.fileno()

    # 缩小输出管道：下游（aplay/SCO）卡顿时旧音频最多在管道里积 256ms，
    # 而不是默认 64KB 的约 2 秒（恢复后要先把这 2 秒旧音频播完才到新音频）
    try:
        import fcntl as _fcntl
        _fcntl.fcntl(sys.stdout.buffer.fileno(), _fcntl.F_SETPIPE_SZ, 8192)
    except Exception:
        pass

    # 非阻塞排空 + 积压截断：把管道里的数据全部读到自己手里，超过上限
    # 就丢弃最旧部分（跳音保低延迟），延迟封顶在 MAX_BACKLOG_MS 附近
    try:
        import fcntl
        import select as _select

        fcntl.fcntl(in_fd, fcntl.F_SETFL, os.O_NONBLOCK)

        def wait_and_drain(timeout=0.05):
            r, _, _ = _select.select([in_fd], [], [], timeout)
            if not r:
                return 0
            got = 0
            while True:
                try:
                    raw = os.read(in_fd, 1 << 16)
                except BlockingIOError:
                    break
                if not raw:
                    raise EOFError
                pending.append(raw)
                got += len(raw)
            return got
    except (ImportError, OSError, ValueError):
        # 无 fcntl 的环境（如 Windows 测试）：阻塞 os.read 同样立即返回
        # 管道里当前已有的数据
        def wait_and_drain(timeout=0.05):
            try:
                raw = os.read(in_fd, 1 << 16)
            except OSError:
                raise EOFError
            if not raw:
                raise EOFError
            pending.append(raw)
            return len(raw)

    pending = []  # 字节块列表，避免大 bytes 反复拼接
    pending_bytes = 0
    log("lightweight spectral denoiser ready, fft=%d hop=%d, backlog 上限 %d ms"
        % (FFT, HOP, MAX_BACKLOG_MS))
    t_start = time.time()
    proc_sec = 0.0
    audio_sec = 0.0
    eof = False
    while True:
        if not eof:
            try:
                pending_bytes += wait_and_drain()
            except EOFError:
                eof = True

        # 积压截断：丢弃最旧音频（按采样帧对齐），使延迟封顶
        if pending_bytes > max_backlog * step:
            dropped = pending_bytes - max_backlog * step
            dropped -= dropped % step
            drop = dropped
            while drop and pending:
                if len(pending[0]) <= drop:
                    drop -= len(pending[0])
                    pending.pop(0)
                else:
                    pending[0] = pending[0][drop:]
                    drop = 0
            pending_bytes = max_backlog * step
            log("backlog 超限，丢弃 %.0f ms（跳音保低延迟）"
                % (dropped * 1000.0 / (SR * step)))

        take = pending_bytes // step
        if take == 0:
            if eof:
                break
            continue
        need = take * step
        parts = []
        while pending and need >= len(pending[0]):
            parts.append(pending.pop(0))
            need -= len(parts[-1])
        if need and pending:
            parts.append(pending[0][:need])
            pending[0] = pending[0][need:]
        pending_bytes -= take * step
        data = b"".join(parts)

        t0 = time.time()
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if CHANNELS == 2:
            x = x.reshape(-1, 2).mean(axis=1)
        if carry.size:
            x = np.concatenate((carry, x))
            carry = np.zeros(0, dtype=np.float32)
        out_parts = []
        pos = 0
        while pos + FFT <= len(x):
            seg_in = x[pos:pos + HOP]
            frame = x[pos:pos + FFT] * window
            frame_pow = float(np.mean(frame ** 2))  # 时域帧功率（VAD 用）
            spec = np.fft.rfft(frame)
            power = np.abs(spec) ** 2
            quiet = frame_pow < max(noise_level, 1e-7) * 4.0
            if quiet or (time.time() < learn_until and frame_pow < 0.003):
                noise_psd = NOISE_SMOOTH * noise_psd + (1.0 - NOISE_SMOOTH) * power
                noise_level = 0.98 * noise_level + 0.02 * frame_pow
                quiet = True
            gain = np.maximum(FLOOR, 1.0 - noise_psd / (power + 1e-9))
            gain_smooth = GAIN_SMOOTH * gain_smooth + (1.0 - GAIN_SMOOTH) * gain
            y = np.fft.irfft(spec * gain_smooth)
            acc += y
            seg = acc[:HOP] / OLA_GAIN  # OLA 幅度归一
            acc = np.concatenate((acc[HOP:], zeros_hop))
            out_parts.append(seg)
            # 降噪效果监测：静音帧用于估计噪声衰减量
            p_in = float(np.mean(seg_in ** 2))
            p_out = float(np.mean(seg ** 2))
            win_in_pow += p_in * HOP
            win_out_pow += p_out * HOP
            win_n += HOP
            if quiet:
                quiet_in_pow += p_in * HOP
                quiet_out_pow += p_out * HOP
                quiet_n += HOP
            pos += HOP
        if pos < len(x):
            carry = x[pos:]
        if not out_parts:
            continue
        out = np.concatenate(out_parts)
        np.clip(out, -1.0, 1.0, out=out)
        proc_sec += time.time() - t0
        audio_sec += len(out) / SR
        try:
            sys.stdout.buffer.write((out * 32768.0).astype(np.int16).tobytes())
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            break
        if time.time() - t_start >= 5.0:
            rtf = proc_sec / audio_sec if audio_sec > 0 else 0.0
            in_db = 10.0 * math.log10(win_in_pow / win_n) if win_n and win_in_pow > 0 else -120.0
            out_db = 10.0 * math.log10(win_out_pow / win_n) if win_n and win_out_pow > 0 else -120.0
            q_db = 0.0
            if quiet_n and quiet_in_pow > 0 and quiet_out_pow > 0:
                q_db = 10.0 * math.log10(quiet_in_pow / quiet_out_pow)
            bl_ms = pending_bytes // step * 1000.0 / SR
            log("rtf=%.2f backlog=%.0f ms in=%.1f out=%.1f dBFS 静音衰减=%.1f dB"
                % (rtf, bl_ms, in_db, out_db, q_db))
            proc_sec = 0.0
            audio_sec = 0.0
            t_start = time.time()
            win_in_pow = 0.0
            win_out_pow = 0.0
            win_n = 0
            quiet_in_pow = 0.0
            quiet_out_pow = 0.0
            quiet_n = 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def write_spec_script(path, channels):
    content = (
        SPEC_SCRIPT_CONTENT.replace("__SR__", str(SAMPLE_RATE))
        .replace("__CHANNELS__", str(channels))
        .replace("__SPEC_FFT__", str(max(256, SAMPLE_RATE * 32 // 1000)))
        .replace("__SPEC_FLOOR__", str(SPEC_FLOOR))
        .replace("__MAX_BACKLOG_MS__", str(DF_MAX_BACKLOG_MS))
    )
    path.write_text(content)


def denoise_python_candidates():
    """所有可能装有 df 包的 Python 解释器（按优先级），供运行与诊断共用。"""
    return [
        os.environ.get("DENOISE_PYTHON"),
        "/home/wanjin1234/denoise_mic/venv/bin/python",
        os.path.expanduser("~/denoise_mic/venv/bin/python"),
        os.path.expanduser("~/venv/bin/python"),
        "/usr/bin/python3",
        sys.executable,
    ]


def get_denoise_python():
    for p in denoise_python_candidates():
        if p and os.path.isfile(p):
            return p
    return sys.executable


# ---------------- 音频管道 ----------------

DOWNMIX_SCRIPT_CONTENT = r"""
import array
import sys


def main():
    while True:
        data = sys.stdin.buffer.read(8192)
        if not data:
            break
        n = len(data) // 4
        data = data[:n * 4]
        samples = array.array("h")
        samples.frombytes(data)
        out = array.array("h")
        for i in range(0, len(samples), 2):
            out.append((samples[i] + samples[i + 1]) // 2)
        sys.stdout.buffer.write(out.tobytes())
        sys.stdout.flush()


if __name__ == "__main__":
    main()
"""


def drain_stream(stream, label, sink):
    try:
        for raw_line in iter(stream.readline, b""):
            if not raw_line:
                break
            line = raw_line.decode(errors="replace").rstrip()
            sink.append(line)
            print(f"    [{label}] {line}", flush=True)
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def build_pipeline(mic_device, bluealsa_pcm, venv_python, mode, mic_channels=1):
    """mode: "df"=DeepFilterNet / "spec"=纯 numpy 谱减法 / None=不降噪"""
    out_channels = 1
    # 显式指定 ALSA period/buffer，避免默认大缓冲（数百毫秒）增加端到端延迟
    rec_cmd = [
        "arecord",
        "-t",
        "raw",
        "-D",
        mic_device,
        "-f",
        FORMAT,
        "-r",
        str(SAMPLE_RATE),
        "-c",
        str(mic_channels),
        "--period-time",
        str(PERIOD_TIME_US),
        "--buffer-time",
        str(BUFFER_TIME_US),
    ]
    play_cmd = [
        "aplay",
        "-t",
        "raw",
        "-D",
        bluealsa_pcm,
        "-f",
        FORMAT,
        "-r",
        str(SAMPLE_RATE),
        "-c",
        str(out_channels),
        "--period-time",
        str(PERIOD_TIME_US),
        "--buffer-time",
        str(BUFFER_TIME_US),
    ]
    stages = [("arecord", rec_cmd)]
    if mode == "df":
        stages.append(("denoise", [venv_python, str(DENOISE_SCRIPT)]))
    elif mode == "spec":
        write_spec_script(SPEC_SCRIPT, mic_channels)
        stages.append(("specdenoise", [venv_python, str(SPEC_SCRIPT)]))
    elif mic_channels == 2:
        DOWNMIX_SCRIPT.write_text(DOWNMIX_SCRIPT_CONTENT)
        stages.append(("downmix", [venv_python, str(DOWNMIX_SCRIPT)]))
    # 采集增益阶段：读取电平文件，实时响应 Windows 输入音量（+VGM）
    write_gain_script(GAIN_SCRIPT)
    stages.append(("gain", [venv_python, str(GAIN_SCRIPT)]))
    stages.append(("aplay", play_cmd))
    return stages


def launch_pipeline(pipeline, prestarted=None):
    """启动管道。prestarted=(label, proc, stderr行, stop_event) 是 PCM
    等待期间预启动的降噪进程（模型已加载完毕）；本函数把它接到管道
    对应位置：上游阶段（arecord）的 stdout 直接写入它的 stdin，下游
    阶段从它的 stdout 接续。预启动进程已退出时回退为正常启动。"""
    procs = []
    logs = {label: [] for label in set(l for l, _ in pipeline)}
    pre_label = pre_proc = pre_lines = pre_stop = pre_drain_stop = None
    used_pre = False
    if prestarted:
        pre_label, pre_proc, pre_lines, pre_stop = prestarted[:4]
        if pre_label in logs:
            logs[pre_label] = pre_lines
        # 第 5 项：预热进程 stdout 排水线程的停止事件。在启动任何管道
        # 阶段之前就置位：排水线程若继续读到 arecord 接入后的真实音频
        # 会把它丢掉（丢首段人声），所以必须抢在 arecord spawn 前停住。
        # 预热静音在接入时早已被消费/排空（灌入即消费，RTF≈0.87），
        # 停排后残留在输出管道里的静音最多约 250ms，交给 aplay 播放
        pre_drain_stop = prestarted[4] if len(prestarted) > 4 else None
        if pre_drain_stop is not None:
            pre_drain_stop.set()

    prev_stdout = None
    for i, (label, cmd) in enumerate(pipeline):
        if (
            pre_proc is not None
            and label == pre_label
            and not used_pre
            and pre_proc.poll() is None
        ):
            # 接管预启动进程：模型已加载，喂静音线程随即停止
            used_pre = True
            if pre_stop is not None:
                pre_stop.set()
            if pre_drain_stop is not None:
                pre_drain_stop.set()
            procs.append((label, pre_proc))
            prev_stdout = pre_proc.stdout
            continue
        stdin = prev_stdout
        # 下一阶段是预启动进程时，本阶段的 stdout 直接接它的 stdin，
        # 避免上游写进无人读取的中间管道后被 64KB 缓冲卡死
        next_is_pre = (
            pre_proc is not None
            and not used_pre
            and i + 1 < len(pipeline)
            and pipeline[i + 1][0] == pre_label
        )
        stdout = pre_proc.stdin if next_is_pre else subprocess.PIPE
        p = subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
        if stdin is not None:
            stdin.close()
        if next_is_pre:
            pre_proc.stdin.close()  # 父进程释放写端，由上游进程持有
        procs.append((label, p))
        threading.Thread(
            target=drain_stream, args=(p.stderr, label, logs[label]), daemon=True
        ).start()
        prev_stdout = p.stdout
    return procs, logs


def terminate_all(procs):
    for label, p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
    time.sleep(1)
    for label, p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass


def print_stderr_tails(logs, n=12):
    for label, lines in logs.items():
        if lines:
            print(f"  -- {label} stderr tail --")
            for line in lines[-n:]:
                print(f"    {line}")


def check_pipeline_startup(procs, logs):
    reasons = []
    time.sleep(3)
    for label, p in procs:
        if p.poll() is not None:
            reasons.append(f"{label} 启动后立即退出（退出码 {p.returncode}）")
    if not reasons:
        time.sleep(5)
        for label, p in procs:
            if p.poll() is not None:
                reasons.append(f"{label} 启动后退出（退出码 {p.returncode}）")
    if reasons:
        print_stderr_tails(logs)
        return False, reasons
    print("  -> 管道健康检查通过：所有子进程均在运行")
    return True, []


def drain_discard(stream, stop):
    """持续读空预热进程的 stdout 并丢弃（预热输出是静音，不能进 SCO）。"""
    fd = stream.fileno()
    try:
        import select as _sel

        while not stop.is_set():
            r, _, _ = _sel.select([fd], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(fd, 1 << 16)
            except OSError:
                return
            if not data:
                return
    except (ImportError, OSError, ValueError):
        # select 不可用（Windows 测试环境）：阻塞读，stop 置位后线程退出
        while not stop.is_set():
            try:
                data = os.read(fd, 1 << 16)
            except OSError:
                return
            if not data:
                return


def start_denoise_warmup(venv_python, mic_channels):
    """PCM 等待期间预启动降噪子进程并灌入约 0.8 秒静音。

    DeepFilterNet 在树莓派上加载需 5~7 秒：若等转发管道启动时才加载，
    arecord 的 60ms 缓冲会爆掉（实测 overrun 5.1s），下游 aplay 也因
    无数据而 underrun（实测 402ms），SCO 开头出现爆音/断流。预启动让
    加载与 PCM 等待并行进行。

    静音一次性灌入（不是按实时节拍）：进程以快于实时的速度消费
    （RTF≈0.87），消化完即阻塞等真实输入，预热窗口上下文与谱门噪声
    学习先行就绪；预热输出的静音由 stdout 排水线程丢弃。旧实现按
    20ms/块实时喂 1.5 秒且不排 stdout：进程被输出管道（8KB）反压后
    停住，stdin 里最多积压 ~1.2 秒未消费的静音，接入后这些静音作为
    输出垫在真实音频前头，首句人声被推迟 1 秒以上（听感=启动延迟大）。
    返回 (label, proc, stderr行列表, 停止事件, stdout排水停止事件)；
    非降噪模式（off/无依赖）返回 None。
    """
    mode = _select_denoise_mode(venv_python, mic_channels)
    if mode == "df":
        script, label = DENOISE_SCRIPT, "denoise"
    elif mode == "spec":
        write_spec_script(SPEC_SCRIPT, mic_channels)
        script, label = SPEC_SCRIPT, "specdenoise"
    else:
        return None
    lines = []
    proc = subprocess.Popen(
        [venv_python, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    threading.Thread(
        target=drain_stream, args=(proc.stderr, label, lines), daemon=True
    ).start()
    stop = threading.Event()
    drain_stop = threading.Event()
    threading.Thread(
        target=drain_discard, args=(proc.stdout, drain_stop), daemon=True
    ).start()

    def feed_zeros():
        # 一次性灌入约 0.8 秒静音：模型尚未加载完时会暂存在 stdin 管道
        # 里（64KB 容量足够），加载完立刻被消费；预热窗口需要 prefix+
        # chunk+edge 约 450ms 历史，再多喂一块让交叉淡化窗口成对
        total = 2 * max(1, SAMPLE_RATE * 800 // 1000)
        chunk_bytes = 2 * max(1, SAMPLE_RATE * 20 // 1000)
        try:
            for _ in range(0, total, chunk_bytes):
                if stop.is_set():
                    return
                proc.stdin.write(b"\x00" * chunk_bytes)
                proc.stdin.flush()
        except (OSError, ValueError):
            return

    threading.Thread(target=feed_zeros, daemon=True).start()
    return (label, proc, lines, stop, drain_stop)


def stop_denoise_warmup(warmup):
    """会话未走到转发就结束时，停掉预启动的降噪进程。"""
    if not warmup:
        return
    _, proc, _lines, stop = warmup[:4]
    drain_stop = warmup[4] if len(warmup) > 4 else None
    stop.set()
    if drain_stop is not None:
        drain_stop.set()
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        time.sleep(0.5)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


# ---------------- Windows 输入音量 -> 采集增益 ----------------
#
# HFP 音量模型：AG（Windows）通过 +VGS（扬声器）/ +VGM（麦克风增益，0~15）
# 控制 HF（树莓派）。BlueALSA 内置 HFP-HF 已声明 VOLUME 能力位并处理
# +VGM：把增益写入 SCO 上行 PCM（D-Bus 路径 .../hfphf/sink，即本脚本
# aplay 写入的那条流）的 volume 属性。
#
# 但 BlueALSA 官方文档明确：原生音量模式下它只更新 volume 属性、
# 不缩放样本（期望耳机硬件自己施加增益）；softvol 模式下虽然缩放
# 样本，+VGM 却会被忽略。因此这里自己做桥接：
#   1. 保持原生模式（SoftVolume=false），让 +VGM 如实反映到 volume 属性
#   2. 音量监控线程轮询 bluealsa-cli volume，把 0~15 增益写入电平文件
#   3. 音频管道中的 gain 阶段读取电平文件，对样本施加 sqrt(level/15)
#      的幅度缩放（与 BlueALSA 自身的 loudness 曲线一致），并以每样本
#      小步渐变逼近目标因子，消除音量台阶突变引起的"咔哒"声
#   4. 上行链路整体没有任何放大（+VGM=15 时 gain=1.0 直通），麦克风
#      偏轻：gain 阶段默认再叠加 GAIN_BOOST_DB=6dB 数字增益，BOOST>1
#      时软限幅防削波；编解码器模拟 PGA（tlv320aic3x 的 amixer 'PGA'
#      控制，位于 ADC 之前、信噪比更好）由 MIC_PGA_GAIN 环境变量驱动，
#      启动时探测并报告当前值（空 = 不动它）


GAIN_SCRIPT_CONTENT = r"""
import array
import math
import os
import sys
import time

LEVEL_FILE = "__LEVEL_FILE__"
SAMPLE_RATE = __SAMPLE_RATE__

# 缩小输出管道（约 256ms）：SCO 卡顿时旧音频最多积压这些，
# 而不是默认 64KB 的约 2 秒
try:
    import fcntl
    fcntl.fcntl(sys.stdout.buffer.fileno(), fcntl.F_SETPIPE_SZ, 8192)
except Exception:
    pass


def read_level():
    try:
        with open(LEVEL_FILE, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 15


# 音量变化不再瞬时跳变：因子按样本小步逼近目标（15->8 约 20ms 渐变、
# 满幅度变化约 80ms），消除 Windows 调音量时 +VGM 台阶突变引起的"咔哒"。
STEP = 0.0008

# 数字增益（dB）：上行链路没有硬件放大（+VGM=15 时 gain=1.0），麦克风
# 整体偏轻，因此默认额外 +6dB（约 2 倍幅度）；0 = 纯直通。
# BOOST>1 时启用软限幅：超过拐点后平滑压缩到封顶值，增强后的强信号
# 不会被硬削波成方波（爆音），只是轻微压缩。
BOOST = 10 ** (__GAIN_BOOST_DB__ / 20.0)
KNEE = 20000   # 软限幅拐点（样本值）
CEIL = 32000   # 软限幅封顶（样本值）


def main():
    in_fd = sys.stdin.buffer.fileno()
    # 积压上限必须大于降噪阶段单块突发（默认 320ms）：上一版取 240ms，
    # 比突发还小，结果每个 320ms 突发一到就被"截断"删掉 200ms，剩下
    # 120ms 播完后又空等 200ms 才有下一块——aplay 每轮 underrun 约
    # 300ms，周期性爆音 + SCO 频繁掉线。800ms 上限下正常峰值（一个
    # 突发 320ms + 启动残留静音 ≤250ms）不会触发截断，只有 SCO 真
    # 卡死（aplay 不再消费、积压持续增长）才会触发。
    CAP = 2 * max(1, SAMPLE_RATE * 800 // 1000)
    # 真卡顿恢复时丢最旧数据、只保留最近 100ms，不播陈旧缓冲
    KEEP = 2 * max(1, SAMPLE_RATE * 100 // 1000)
    pending = []
    pending_bytes = 0

    # 排空方式：有 select 时非阻塞读多少算多少（Linux 生产环境）；
    # 否则阻塞 os.read（Windows 测试环境）——注意绝不能用
    # BufferedReader.read(n)：它会阻塞到读满 n 字节才返回，旧实现
    # read(4096) 等于每块 128ms 延迟。读到的数据立即整体透传给
    # aplay：320ms 突发由 aplay 的 128ms FIFO + 60ms 硬件缓冲
    # （合计 188ms）平滑，块间约 42ms 空档不会造成欠载（此前按
    # 10ms 节拍发射的版本在积压上限配置错误时反而每块误删数据，
    # 已废弃；透传下 gain 阶段自身不再引入节拍延迟）。
    try:
        import select as _sel
        # 探测一次：Linux 上对管道 select 正常；Windows 上会抛 OSError，
        # 此时退回阻塞 os.read（测试环境，不影响树莓派生产路径）
        _sel.select([sys.stdin.buffer.fileno()], [], [], 0)
        HAS_SELECT = True
    except Exception:
        _sel = None
        HAS_SELECT = False

    lvl0 = read_level()
    gain = math.sqrt(lvl0 / 15.0) * BOOST if lvl0 > 0 else 0.0
    eof = False
    while True:
        # 1) 排空输入
        if not eof:
            if HAS_SELECT:
                while True:
                    r, _, _ = _sel.select([in_fd], [], [], 0)
                    if not r:
                        break
                    try:
                        raw = os.read(in_fd, 1 << 16)
                    except (BlockingIOError, OSError):
                        eof = True
                        break
                    if not raw:
                        eof = True
                        break
                    pending.append(raw)
                    pending_bytes += len(raw)
            else:
                # 无 select（Windows 测试环境）：每次循环最多读一次，
                # 读到多少算多少；读不到就阻塞等数据，不影响后面发射
                try:
                    raw = os.read(in_fd, 1 << 16)
                except (BlockingIOError, OSError):
                    raw = b""
                    eof = True
                if not raw:
                    eof = True
                else:
                    pending.append(raw)
                    pending_bytes += len(raw)

        # 2) 积压截断：仅当积压超过 CAP（SCO 真卡死）时，丢最旧数据、
        #    保留最近 KEEP（100ms），恢复后接上的是新音频而不是陈旧缓冲
        if pending_bytes > CAP:
            dropped = pending_bytes - KEEP
            trim = dropped
            while pending and trim >= len(pending[0]):
                trim -= len(pending[0])
                pending.pop(0)
            if trim and pending:
                pending[0] = pending[0][trim:]
            pending_bytes = KEEP
            print("[gain] 积压超限，丢弃 %.0f ms（SCO 卡顿恢复）"
                  % (dropped * 1000.0 / (2 * SAMPLE_RATE)),
                  file=sys.stderr, flush=True)

        # 3) 透传全部积压：应用音量渐变后整体写出。突发形态交给 aplay
        #    的 FIFO/硬件缓冲（合计 188ms）平滑，覆盖块间约 42ms 空档，
        #    gain 阶段自身不引入节拍延迟
        if pending_bytes > 0:
            data = b"".join(pending)
            pending = []
            pending_bytes = 0
            level = read_level()
            target = math.sqrt(level / 15.0) * BOOST if level > 0 else 0.0
            n = len(data) // 2
            samples = array.array("h")
            samples.frombytes(data[: n * 2])
            out = array.array("h")
            for s in samples:
                if gain < target:
                    gain = min(target, gain + STEP)
                elif gain > target:
                    gain = max(target, gain - STEP)
                v = s * gain
                if BOOST > 1.0:
                    if v > KNEE:
                        v = KNEE + (CEIL - KNEE) * (
                            1.0 - math.exp(-(v - KNEE) / (CEIL - KNEE)))
                    elif v < -KNEE:
                        # 负向对称：v+KNEE 恒为负，exp 指数负值 → 压缩到 -CEIL
                        v = -(KNEE + (CEIL - KNEE) * (
                            1.0 - math.exp((v + KNEE) / (CEIL - KNEE))))
                out.append(int(min(32767, max(-32768, v))))
            sys.stdout.buffer.write(out.tobytes())
            sys.stdout.buffer.flush()
            continue

        if eof:
            break
        time.sleep(0.004)


if __name__ == "__main__":
    main()
"""


def write_gain_script(path):
    path.write_text(
        GAIN_SCRIPT_CONTENT.replace("__LEVEL_FILE__", GAIN_LEVEL_FILE)
        .replace("__SAMPLE_RATE__", str(SAMPLE_RATE))
        .replace("__GAIN_BOOST_DB__", repr(float(GAIN_BOOST_DB)))
    )


def set_gain_level_file(level):
    """原子写入增益电平，供 gain 阶段读取。"""
    tmp = f"{GAIN_LEVEL_FILE}.tmp"
    with open(tmp, "w") as f:
        f.write(str(int(level)))
    os.replace(tmp, GAIN_LEVEL_FILE)


def apply_capture_pga(mic_device, gain_spec):
    """探测并（可选）提升麦克风所在声卡的模拟 PGA 增益。

    tlv320aic3x 等编解码器在 ADC 之前有模拟可编程增益（amixer 控制
    'PGA'，0~59.5dB），比数字放大干净得多：在量化之前提升输入信号，
    信噪比更好。gain_spec 为空时只报告当前值；否则用 amixer cset
    设置（支持百分比或 dB 值，如 "50%"、"20dB"）。设置前会确保
    PGA 前的 Mic2 输入开关打开。找不到 PGA 控制时返回 None。
    """
    if shutil.which("amixer") is None:
        return None
    card = "0"
    m = re.search(r"hw:(\d+)", mic_device or "")
    if m:
        card = m.group(1)
    cget = run(
        f"amixer -c {card} cget name='PGA'", check=False, timeout=8, verbose=False
    )
    if not cget or cget.returncode != 0:
        return None
    cur = None
    for line in (cget.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(": values="):
            cur = line.split("=", 1)[1].strip()
    print(
        f"  -> 模拟 PGA 增益（声卡 {card}）：{cur if cur else '未知'}"
        f"{'（可通过 MIC_PGA_GAIN 提升，如 20dB）' if not gain_spec else ''}"
    )
    if not gain_spec:
        return cur
    # PGA 前的输入开关：麦克风一般接在 Mic2L/Mic2R，确保通路打开
    for sw in ("Left PGA Mixer Mic2L", "Right PGA Mixer Mic2R"):
        run(
            f"amixer -c {card} cset name='{sw}' on",
            check=False,
            timeout=8,
            verbose=False,
        )
    res = run(
        f"amixer -c {card} cset name='PGA' {gain_spec}",
        check=False,
        timeout=8,
        verbose=False,
    )
    if res and res.returncode == 0:
        last = (res.stdout or "").strip().splitlines()
        print(f"  -> 已设置 PGA = {gain_spec}：{last[-1] if last else ''}")
    return cur


def get_sco_sink_pcm_path(device):
    """返回当前设备 SCO 上行 PCM 的 D-Bus 路径（hfphf/sink 或 hsphs/sink）。"""
    if not shutil.which("bluealsa-cli"):
        return None
    res = run("bluealsa-cli list-pcms", check=False, timeout=8, verbose=False)
    if not res or res.returncode != 0:
        return None
    mac_part = device.replace(":", "_").upper()
    for line in res.stdout.splitlines():
        line = line.strip()
        if f"dev_{mac_part}" in line and line.endswith("/sink"):
            return line
    return None


def ensure_soft_volume_disabled(pcm_path):
    """确保 PCM 未启用 SoftVolume，否则 HFP 的 +VGM 音量命令会被忽略。"""
    res = run(
        f"dbus-send --system --print-reply --dest=org.bluealsa {pcm_path} "
        "org.freedesktop.DBus.Properties.Get "
        "string:org.bluealsa.PCM1 string:SoftVolume",
        check=False,
        timeout=8,
        verbose=False,
    )
    if res and "boolean true" in (res.stdout or ""):
        print("  !! 检测到 SoftVolume=true，+VGM 音量命令会被忽略，正在关闭...")
        run(
            f"bluealsa-cli soft-volume {pcm_path} false",
            check=False,
            verbose=False,
        )
        print("  -> SoftVolume 已关闭，Windows 输入音量将真实作用到采集增益")


def volume_monitor(device, stop_event, lost_event=None):
    """
    轮询 BlueALSA 上行 PCM 的 volume（0~15，即 Windows +VGM 的结果），
    写入电平文件供管道 gain 阶段实时施加采集增益。
    lost_event: 连续多次读不到 PCM（SCO 断开）时置位，供转发循环快速响应。
    """
    pcm_path = None
    last_level = None
    warned_no_change = False
    fail_count = 0
    start = time.time()
    while not stop_event.is_set():
        if pcm_path is None:
            pcm_path = get_sco_sink_pcm_path(device)
            if pcm_path:
                ensure_soft_volume_disabled(pcm_path)
                fail_count = 0
        if pcm_path:
            res = run(
                f"bluealsa-cli volume {pcm_path}",
                check=False,
                timeout=8,
                verbose=False,
            )
            m = None
            if res and res.returncode == 0:
                m = re.match(r"Volume:\s*(\d+)", (res.stdout or "").strip())
            if m:
                fail_count = 0
                level = int(m.group(1))
                pct = level * 100 // 15
                if last_level is None:
                    print(f"  [音量] 当前 SCO 麦克风增益: {level}/15（{pct}%）")
                    set_gain_level_file(level)
                elif level != last_level:
                    db = (
                        20 * math.log10(math.sqrt(level / 15.0))
                        if level > 0
                        else float("-inf")
                    )
                    print(
                        f"  [音量] Windows 输入音量变化 -> "
                        f"树莓派采集增益 {last_level}/15 -> {level}/15"
                        f"（{pct}%，{db:.1f} dB）"
                    )
                    set_gain_level_file(level)
                last_level = level
            else:
                fail_count += 1
                # 连续失败 3 次视为 SCO 断开，通知转发循环尽快确认
                if lost_event is not None and fail_count >= 3:
                    lost_event.set()
                # PCM 可能已消失（SCO 断开），下一轮重新解析
                pcm_path = None
        if last_level is not None and not warned_no_change and time.time() - start > 20:
            print(
                "  [音量] 提示：若调整 Windows 输入音量后此处没有变化，"
                "说明该 Windows 版本未发送 HFP +VGM 命令"
                "（此时 Windows 仅在本地做软件增益）"
            )
            warned_no_change = True
        stop_event.wait(1.0)


# ---------------- 音频转发（连接期间持续运行） ----------------


def _write_denoise_status(mode, **extra):
    """把当前降噪模式/实测信息写入状态文件，供 --status 与手动查看。"""
    lines = [
        "mode=%s" % (mode if mode else "off"),
        "model=%s"
        % {"df": "DeepFilterNet", "spec": "lightweight-spectral(numpy)", None: "none"}[
            mode
        ],
        "updated=%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
    ]
    for key, val in extra.items():
        if val is not None:
            lines.append("%s=%s" % (key, val))
    tmp = DENOISE_STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, DENOISE_STATUS_FILE)
    print("  -> 降噪状态已写入 %s" % DENOISE_STATUS_FILE)


def print_denoise_status():
    """--status: 打印当前降噪模式与增益状态（只读，不启动服务）。"""
    print("=== 降噪状态 ===")
    if os.path.exists(DENOISE_STATUS_FILE):
        try:
            print(Path(DENOISE_STATUS_FILE).read_text().strip())
        except OSError as exc:
            print("  读取失败: %s" % exc)
    else:
        print("  尚未记录：bt-mic 服务未运行，或降噪模式尚未决策")
    if os.path.exists(GAIN_LEVEL_FILE):
        try:
            print("gain_level=%s/15" % Path(GAIN_LEVEL_FILE).read_text().strip())
        except OSError:
            pass


DF_INFO_CODE = (
    "import df, os, sys; "
    "print('df 文件:', df.__file__); "
    "print('df 版本:', getattr(df, '__version__', '?')); "
    "print('API 全列表:', sorted(a for a in dir(df) if not a.startswith('_'))); "
    "print('0.5.x API (init_df/enhance, 48kHz):', "
    "hasattr(df, 'init_df') and hasattr(df, 'enhance')); "
    "print('libdf v2 (init_model/process, 16kHz):', "
    "hasattr(df, 'init_model') and hasattr(df, 'process')); "
    "try:\n"
    " import torch; tv = torch.__version__\n"
    "except Exception as e:\n"
    " tv = '缺失(%s)' % e\n"
    "print('torch:', tv); "
    "cache = os.path.join(os.path.expanduser('~'), '.cache', 'DeepFilterNet'); "
    "print('模型缓存目录:', cache, '存在' if os.path.isdir(cache) else '不存在'); "
    "found = ([d for d in sorted(os.listdir(cache)) "
    "if os.path.isfile(os.path.join(cache, d, 'config.ini'))] "
    "if os.path.isdir(cache) else []); "
    "print('已下载模型:', found if found else '(无)'); "
    "print('python 解释器:', sys.executable)"
)


def run_df_info(python_path, timeout=120):
    """在指定解释器里探测 df 包详情，返回输出行列表（失败返回 None）。"""
    res = run(
        f"{shlex.quote(python_path)} -c {shlex.quote(DF_INFO_CODE)}",
        check=False,
        timeout=timeout,
        verbose=False,
    )
    if res is None:
        return None
    return [
        line.strip()
        for line in ((res.stdout or "") + "\n" + (res.stderr or "")).splitlines()
        if line.strip()
    ]


def print_df_info():
    """--df-info: 检查 DeepFilterNet 在所有候选 Python 里的安装情况（只读）。"""
    print("=== DeepFilterNet 安装检查 ===")
    seen = set()
    for py in denoise_python_candidates():
        if not py or py in seen or not os.path.isfile(py):
            continue
        seen.add(py)
        print(f"\n--- {py} ---")
        lines = run_df_info(py)
        if lines is None:
            print("  (执行失败或超时)")
            continue
        for line in lines:
            print(f"  {line}")
    print("\n说明：")
    print("  - '0.5.x API (init_df/enhance): True' 且 torch 可用、模型已下载")
    print("    = 脚本可启用 DeepFilterNet（48kHz 模型，内置 16k↔48k 重采样）")
    print("  - 有 API 但 torch 缺失或模型未下载 = 需补装/补下载：")
    print("    pip install torch；联网执行一次 init_df 或手动放置模型 zip")
    print("  - init_df/enhance 与 init_model/process 都为 False = 未检测到")
    print("    可用的 df 包（脚本回退轻量谱减法，仍可用）")


_DF_MODE = None  # 缓存降噪模式决策: "df" | "spec" | None
_DF_DECIDE_LOCK = threading.Lock()


def _select_denoise_mode(venv_python, mic_channels):
    """
    决定降噪模式，返回 "df" | "spec" | None。
    - DENOISE_MODE=auto（默认）：用 --benchmark 实测 DeepFilterNet 的 RTF，
      达标用 df；否则用纯 numpy 谱减法（零模型加载、RTF≈0.02）
    - DENOISE_MODE=df/spec/off 强制指定（依赖缺失时自动降级并提示）
    - 后台预加载线程与转发线程可能并发调用，用锁保证只实测一次
    """
    global _DF_MODE
    if _DF_MODE is not None:
        return _DF_MODE
    with _DF_DECIDE_LOCK:
        if _DF_MODE is not None:
            return _DF_MODE

        def has_module(module):
            check = run(
                f"{venv_python} -c 'import {module}'",
                check=False,
                timeout=120,
                verbose=False,
            )
            return bool(check and check.returncode == 0)

        def benchmark_df(sizes):
            """一次进程内实测多个块大小的 RTF，返回 {块ms: rtf}（失败返回 None）。"""
            write_denoise_script(DENOISE_SCRIPT, mic_channels)
            res = run(
                f"{venv_python} {DENOISE_SCRIPT} --benchmark "
                + " ".join(str(s) for s in sizes),
                check=False,
                timeout=900,
                verbose=False,
            )
            rtf_map = {}
            if res is not None:
                lines = ((res.stderr or "") + "\n" + (res.stdout or "")).splitlines()
                # 把降噪子进程的日志全部透出，基准失败时可直接看到原因
                for line in lines:
                    if "[denoise]" in line and line.strip():
                        print(f"    {line.strip()}")
                for line in lines:
                    if "benchmark done:" in line:
                        for part in line.split("benchmark done:", 1)[1].split(","):
                            part = part.strip()
                            m = re.match(r"(\d+):([\d.]+)", part)
                            if m:
                                rtf_map[int(m.group(1))] = float(m.group(2))
                if not rtf_map:
                    print(
                        "  !! 基准进程退出码 %s，df 包信息（供诊断）：" % res.returncode
                    )
                    info_lines = run_df_info(venv_python)
                    if info_lines:
                        for line in info_lines:
                            print(f"    {line}")
                    if res.returncode != 0:
                        print("  !! 基准进程 stderr 末尾：")
                        for line in lines[-25:]:
                            if line.strip():
                                print(f"      {line}")
            return rtf_map or None

        if DENOISE_MODE == "df":
            if has_module("df"):
                write_denoise_script(DENOISE_SCRIPT, mic_channels)
                _DF_MODE = "df"
            else:
                print("  !! DENOISE_MODE=df 但未检测到 DeepFilterNet，降级")
                _DF_MODE = "spec" if has_module("numpy") else None
            _write_denoise_status(_DF_MODE)
            return _DF_MODE
        if DENOISE_MODE == "spec":
            if has_module("numpy"):
                _DF_MODE = "spec"
            else:
                print("  !! 缺少 numpy，无法使用轻量降噪，改为原样转发")
                _DF_MODE = None
            _write_denoise_status(_DF_MODE)
            return _DF_MODE
        if DENOISE_MODE == "off":
            _DF_MODE = None
            _write_denoise_status(None)
            return _DF_MODE

        # auto：实测 DF 的 RTF 再决定。
        # 一次进程内测多个候选块大小（只加载一次模型），选“最小达标块”：
        # 块越小处理延迟越低；小块不达标（每次调用的固定开销占比高）时
        # 自动放大块重试，全部不达标才放弃 DeepFilterNet。
        if has_module("df"):
            print(
                "  -> 正在实测 DeepFilterNet 处理速度（首次运行会加载模型，请稍候）..."
            )
            sizes = []
            for ms in (DF_CHUNK_MS // 2, DF_CHUNK_MS, DF_CHUNK_MS * 2, DF_CHUNK_MS * 4):
                ms = min(max(ms, DF_MIN_CHUNK_MS), DF_MAX_CHUNK_MS)
                if ms not in sizes:
                    sizes.append(ms)
            sizes.sort()
            rtf_map = benchmark_df(sizes)
            if rtf_map:
                chosen = None
                for ms in sizes:
                    rtf = rtf_map.get(ms)
                    if rtf is not None and rtf <= DF_BENCH_LIMIT:
                        chosen = ms
                        break
                if chosen is not None:
                    # 用选中的块大小重写降噪脚本，保证转发时与实测一致
                    write_denoise_script(DENOISE_SCRIPT, mic_channels, chunk_ms=chosen)
                    print(
                        "  -> DeepFilterNet 达标（chunk=%d ms, rtf=%.2f），启用 DF 降噪转发"
                        % (chosen, rtf_map[chosen])
                    )
                    _DF_MODE = "df"
                    _write_denoise_status(
                        "df", bench_rtf=rtf_map[chosen], chunk_ms=chosen
                    )
                    return _DF_MODE
                print("  !! DeepFilterNet 各块大小 RTF 均超限，自动改用轻量谱减法降噪")
            else:
                print("  !! DeepFilterNet 基准测试失败，自动改用轻量谱减法降噪")
        elif has_module("numpy"):
            print("  -> 未检测到 DeepFilterNet，使用轻量谱减法降噪")
        else:
            print("  -> 未检测到降噪依赖，使用原样转发（无降噪）")
            _DF_MODE = None
            _write_denoise_status(None)
            return _DF_MODE
        _DF_MODE = "spec"
        _write_denoise_status("spec")
        return _DF_MODE


def _degrade_denoise_mode(venv_python):
    """DF 运行中实时性不足（退出码 10）时降级：spec → 原样转发。"""
    global _DF_MODE
    check = run(
        f"{venv_python} -c 'import numpy'",
        check=False,
        timeout=60,
        verbose=False,
    )
    _DF_MODE = "spec" if (check and check.returncode == 0) else None
    _write_denoise_status(_DF_MODE, degraded_from="df")
    print("  -> 降噪模式已降级为: %s" % (_DF_MODE or "无降噪（原样转发）"))


def run_audio_forwarding(bluealsa_pcm, mic_device, mic_channels, device, warmup=None):
    """
    在蓝牙连接保持期间持续转发音频。
    返回结束原因: "disconnected" | "failed" | "interrupted"
    warmup: PCM 等待期间预启动的降噪进程（见 start_denoise_warmup），
    首次启动管道时接管它，避免启动时重新加载模型造成爆音。
    """
    print_status("启动音频转发（蓝牙保持连接期间持续运行）")
    venv_python = get_denoise_python()
    mode = _select_denoise_mode(venv_python, mic_channels)

    # 每次会话开始时重置增益为满档，随后由音量监控线程按 +VGM 更新
    set_gain_level_file(15)

    def make_pipeline():
        return build_pipeline(mic_device, bluealsa_pcm, venv_python, mode, mic_channels)

    pipeline = make_pipeline()
    print("  管道命令:")
    for label, cmd in pipeline:
        print(f"    {label}: {' '.join(cmd)}")

    stop_vol = threading.Event()
    lost_event = threading.Event()
    vol_thread = threading.Thread(
        target=volume_monitor, args=(device, stop_vol, lost_event), daemon=True
    )
    vol_thread.start()

    restart_count = 0
    procs = []
    logs = {}
    try:
        while True:
            if not is_device_connected(device):
                print("  !! 检测到蓝牙连接已断开")
                return "disconnected"

            procs, logs = launch_pipeline(pipeline, prestarted=warmup)
            print("  -> 音频转发已启动")

            healthy, reasons = check_pipeline_startup(procs, logs)
            if not healthy:
                restart_count += 1
                print(f"  !! 启动失败: {'; '.join(reasons)}")
                if restart_count > MAX_RESTARTS:
                    print("  !! 连续多次启动失败，返回等待状态")
                    terminate_all(procs)
                    return "failed"
                terminate_all(procs)
                print(f"  -> 将在 3 秒后重试（第 {restart_count}/{MAX_RESTARTS} 次）")
                time.sleep(3)
                continue
            restart_count = 0
            healthy_since = time.time()
            last_bt_check = time.time()
            lost_event.clear()

            while True:
                time.sleep(1)
                # 断开检测：SCO PCM 消失时音量监控线程会置位 lost_event；
                # bluetoothctl 派生进程较重，只做 5 秒一次的兜底确认，
                # 减少与 torch 的 CPU 争用
                if lost_event.is_set() or time.time() - last_bt_check >= 5:
                    last_bt_check = time.time()
                    if not is_device_connected(device):
                        print("  !! 检测到蓝牙断开，停止音频转发，回到等待状态")
                        terminate_all(procs)
                        return "disconnected"
                    lost_event.clear()  # 蓝牙仍连接：蓝音服务抖动，继续观察

                dead = [(label, p) for label, p in procs if p.poll() is not None]
                if not dead:
                    continue
                print("  !! 检测到转发子进程退出")
                for label, p in dead:
                    print(f"    -> {label} 退出码: {p.returncode}")
                print_stderr_tails(logs)
                # 降噪进程报告 DF 处理速度不足（退出码 10）：自动降级为
                # 纯 numpy 谱减法，重搭管道继续转发
                if mode == "df" and any(
                    label == "denoise" and p.returncode == 10 for label, p in dead
                ):
                    print("  -> DeepFilterNet 实时性不足，自动切换到轻量谱减法降噪")
                    terminate_all(procs)
                    _degrade_denoise_mode(venv_python)
                    mode = _DF_MODE
                    pipeline = make_pipeline()
                    print("  新管道命令:")
                    for label, cmd in pipeline:
                        print(f"    {label}: {' '.join(cmd)}")
                    restart_count = 0
                    time.sleep(1)
                    break
                if time.time() - healthy_since >= 30:
                    restart_count = 0  # 已稳定运行过，重置失败计数
                restart_count += 1
                if restart_count > MAX_RESTARTS:
                    print("  !! 超过最大重启次数，返回等待状态")
                    terminate_all(procs)
                    return "failed"
                terminate_all(procs)
                print(
                    f"  -> 将在 2 秒后重启转发管道（第 {restart_count}/{MAX_RESTARTS} 次）"
                )
                time.sleep(2)
                break
    except KeyboardInterrupt:
        return "interrupted"
    finally:
        stop_vol.set()
        terminate_all(procs)
        # 预启动进程未被接管（连接在管道启动前就断开）时单独清理
        if warmup is not None and not any(p is warmup[1] for _, p in procs):
            stop_denoise_warmup(warmup)


# ---------------- systemd 开机自启动 ----------------


def install_systemd_unit():
    print_status("安装 systemd 开机自启动服务 bt-mic.service")
    python = sys.executable or "/usr/bin/python3"
    script = os.path.abspath(__file__)
    unit = SERVICE_UNIT_TEMPLATE.format(python=python, script=script)
    with open(SERVICE_UNIT_PATH, "w", encoding="utf-8") as f:
        f.write(unit)
    print(f"  -> 已写入 {SERVICE_UNIT_PATH}")
    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl enable bt-mic.service", check=False, verbose=False)
    run("systemctl start bt-mic.service", check=False, verbose=False)
    time.sleep(2)
    status = run(
        "systemctl is-active bt-mic.service", check=False, timeout=10, verbose=False
    )
    if status and status.stdout.strip() == "active":
        print("  -> bt-mic.service 已安装并启动（active），开机将自动运行")
        print("  查看日志: journalctl -u bt-mic -f")
    else:
        print("  !! 服务可能未成功启动，请检查:")
        print("     systemctl status bt-mic.service")
        print("     journalctl -u bt-mic -n 100")


def uninstall_systemd_unit():
    print_status("卸载 systemd 服务 bt-mic.service")
    run(
        "systemctl disable bt-mic.service 2>/dev/null || true",
        check=False,
        verbose=False,
    )
    run("systemctl stop bt-mic.service 2>/dev/null || true", check=False, verbose=False)
    if os.path.exists(SERVICE_UNIT_PATH):
        os.remove(SERVICE_UNIT_PATH)
    run("systemctl daemon-reload", check=False, verbose=False)
    print("  -> 已卸载（脚本文件本身保留，可随时重新 --install）")
    # 蓝牙连接的所有权在系统服务（bluetoothd/bluealsa）而非 bt-mic 进程，
    # 停掉服务并不会断开已建立的 HFP 连接。卸载时一并恢复 install 阶段
    # 写入的 override/main.conf 备份并重启蓝牙栈，彻底断开连接。
    print_status("恢复蓝牙 override 配置并断开连接")
    disconnect_bluetooth_devices()
    _restore_bluetooth_overrides()
    # 恢复 install 时全局屏蔽的用户会话音频单元（否则桌面音频长期被禁）
    run(
        "systemctl --global unmask pipewire.socket pipewire-pulse.socket "
        "pulseaudio.socket pipewire pipewire-pulse wireplumber pulseaudio "
        "2>/dev/null || true",
        check=False,
        verbose=False,
    )
    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl restart bluetooth 2>/dev/null || true", check=False, verbose=False)
    time.sleep(3)
    # 保留配对、关闭适配器：Windows 对已配对设备会自动重连，disconnect
    # 之后几秒链路又会回来，看起来像"没断开"；但删除配对会迫使下次在
    # Windows 删设备重新配对。改为断开后直接关闭蓝牙电源（power off /
    # hci0 down）：链路立即断开且 Windows 无法重连；下次运行本程序时
    # wait_for_bt_adapter 会自动重新 power on 并恢复可发现，Windows 无需
    # 删除设备即可自动重连（配对信息两侧都保留着）。
    run("systemctl restart bluealsa 2>/dev/null || true", check=False, verbose=False)
    run("bluetoothctl power off 2>/dev/null || true", check=False, verbose=False)
    run("hciconfig hci0 down 2>/dev/null || true", check=False, verbose=False)
    print("  -> 蓝牙已断开并关闭电源（配对保留，下次运行本程序时 Windows 可直接重连）")


# ---------------- 主流程 ----------------


def main():
    global _RESTORED
    if "--status" in sys.argv:
        _RESTORED = True  # 只读查询，退出时不触发恢复流程
        print_denoise_status()
        return
    if "--df-info" in sys.argv:
        _RESTORED = True  # 只读查询，退出时不触发恢复流程
        print_df_info()
        return
    ensure_root()
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    def _sigterm(signum, frame):
        # systemd 停止/重启服务时会先发 SIGTERM 并等待 TimeoutStopSec。
        # 退出前不做 restore_default（它要 stop bluealsa / restart bluetooth，
        # 可能超过 30 秒导致被 SIGKILL），只做子进程清理（由 main 的 finally 完成）
        global _RESTORED
        _RESTORED = True
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm)

    set_performance_governor()

    if "--install" in sys.argv:
        _RESTORED = True  # 安装进程退出时不要触发恢复流程，避免清掉刚写好的配置
        install_systemd_unit()
        return
    if "--uninstall" in sys.argv:
        _RESTORED = True
        uninstall_systemd_unit()
        return

    print("=== Raspberry Pi 蓝牙麦克风（BlueALSA + DeepFilterNet）完成版 ===")
    print("等待 Windows 主动连接；免 PIN 配对；断开后保持运行等待重连")

    backup_system_state()
    try:
        setup_packages()
        disable_audio_servers()

        if os.path.exists("/etc/asound.conf"):
            shutil.move("/etc/asound.conf", "/tmp/asound.conf.removed")
            print("  -> 已移走 /etc/asound.conf")

        configure_main_conf()
        configure_bluetoothd()
        # configure_bluetoothd 会重启 bluetooth，适配器需要数秒重新注册；
        # 必须先等它就绪，否则 bluealsa 和 discoverable 设置都会静默失败
        wait_for_bt_adapter()
        configure_bluealsa()
        prepare_bt_state()

        # 开机时 USB 麦克风可能还没枚举完成：等待而不是退出，
        # 否则服务退出会让电脑彻底搜不到蓝牙
        mic_device = None
        mic_channels = 1
        while True:
            mic_result = select_and_verify_mic()
            if mic_result[0]:
                mic_device, mic_channels = mic_result
                break
            print("  !! 未找到录音设备，5 秒后重试（服务保持运行，蓝牙仍可被发现）")
            ensure_discoverable()
            time.sleep(5)
        print(f"  -> 最终使用麦克风: {mic_device}（{mic_channels} 通道）")
        if mic_device:
            apply_capture_pga(mic_device, MIC_PGA_GAIN)

        # 后台预选降噪模式：利用等待连接的时间加载模型并实测 RTF，
        # 连接建立后转发管道可直接启动，无需再等模型加载
        venv_python = get_denoise_python()
        threading.Thread(
            target=_select_denoise_mode,
            args=(venv_python, mic_channels),
            daemon=True,
        ).start()

        session = 0
        while True:
            session += 1
            if get_connected_devices():
                # 上次会话因转发失败结束、蓝牙仍连着：保持 discoverable off
                print_status(f"会话 {session}：设备仍处于连接状态，直接尝试恢复转发")
            else:
                print_status(f"会话 {session}：等待 Windows 连接")
                ensure_discoverable()
            ensure_pairing_agent()

            device = wait_for_connection()
            if not device:
                # 仅在设置了有限超时时才会走到这里；继续等待即可
                continue

            # 连接成功：关闭 discoverable（要求 4）
            set_discoverable(False)
            get_device_info(device)
            trust_device(device)

            # 清理上一会话残留的转发进程：旧 aplay 独占 SCO 时，新实例
            # 测不通任何 PCM 却仍能收到旧管道的声音，先杀干净再探测
            kill_stale_audio_pipelines()

            # PCM 等待期间预启动降噪进程：模型加载（5~7 秒）与等待并行，
            # 转发真正启动时无需再加载，避免 arecord 爆缓冲/aplay underrun
            warmup = start_denoise_warmup(venv_python, mic_channels)

            # 等待 Windows 启用 Hands-Free 输入（SCO PCM 出现）
            pcm = None
            while is_device_connected(device):
                pcm = find_working_pcm(device, timeout=PCM_WAIT_TIMEOUT)
                if pcm:
                    break
                print("  -> 连接仍在，但尚无 SCO PCM，稍后重试...")
                time.sleep(2)
            if not pcm:
                print("  -> 蓝牙在等待 PCM 期间断开，回到等待状态")
                stop_denoise_warmup(warmup)
                continue

            print(f"  -> 最终使用 PCM: {pcm}")
            reason = run_audio_forwarding(
                pcm, mic_device, mic_channels, device, warmup=warmup
            )
            print_status(f"会话 {session} 结束（原因: {reason}）")
            if reason == "interrupted":
                raise KeyboardInterrupt
            print("  -> 程序保持运行，蓝牙重新可发现，等待下一次连接")
            time.sleep(3)

    except KeyboardInterrupt:
        print("\n  用户中断程序")
    except (RuntimeError, FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        print(f"\n[错误] {e}")
        sys.exit(1)
    finally:
        restore_default()


def set_performance_governor():
    """把 CPU 调速器设为 performance。

    实时音频对 CPU 频率抖动敏感：默认 ondemand 在负载上升时才升频，
    启动基准测试和转发期间会测到偏高的 RTF。开机即锁定高频可稳定处理速度。
    （权限不足或内核不支持时静默跳过，不影响主流程）
    """
    governors = glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_governor")
    if not governors:
        return
    changed = 0
    for path in governors:
        try:
            with open(path, "w") as f:
                f.write("performance")
            changed += 1
        except OSError:
            pass
    if changed:
        print("  -> CPU 调速器已设为 performance（实时降噪速度更稳）")


if __name__ == "__main__":
    main()
