# Silent-Mask-Microphone
A project for Tsinghua University Hardware Design Competition,aiming at reduce the volume when you speak

树莓派作为**降噪麦克风 + 网络音频桥**：把 reSpeaker 麦克风的声音经 DeepFilterNet
降噪后送给 Windows 电脑，同时把电脑的声音收回来从耳机口放出来。支持三种链路：
蓝牙 HFP/A2DP、**网络直连（TCP）**、**MQTT 中继**、以及 **4G 模块串口 PPP 拨号**。

## 目录

| 路径 | 说明 |
| --- | --- |
| `final_btmic.py` | 树莓派端主程序。蓝牙模式（默认）与网络模式（`--net`）都在这里面；网络桥脚本内嵌其中，运行时写到 `/tmp/btmic_net_bridge.py` |
| `windows/` | Windows 端图形界面（`btmic_net_gui.py` 源码 + `树莓派网络麦克风.exe` 单文件程序 + `启动.bat`） |
| `4G模块安装与网络音频使用说明.md` | 4G 模块接线、固件、管网卡/PPP 拨号、配置项、排查手册（含实测踩到的坑） |
| `接线图/` | 模块接口位置与接线示意图（从厂商手册整理） |
| `tests/` | 自检脚本：帧协议、真声卡端到端、Pi 侧命令行（在 Windows 上跑） |

> 4G 模块的厂商资料（Luatools、固件包、AT 手册）体积较大（单个 139MB，超过 GitHub
> 单文件上限），未纳入版本库；需要请到合宙官方下载：<https://docs.openluat.com/air780eg/>

## 快速开始

树莓派（先装好 DeepFilterNet，见说明文档）：

```bash
# 蓝牙模式（默认）
sudo python3 final_btmic.py
# 开机自启：sudo python3 final_btmic.py --install

# 网络模式：连 Windows 电脑（电脑侧先点「启动服务」）
sudo systemctl stop bt-mic                     # 两种模式都要独占声卡
sudo python3 final_btmic.py --net --transport tcp --host <电脑IP> --port 5010 --token <令牌>

# 走 4G：先拨号，再用 MQTT 中继（两端都在 NAT 后面也能通）
sudo python3 final_btmic.py --net-4g-ppp-up
sudo python3 final_btmic.py --net --transport mqtt --host broker.emqx.io \
     --token <令牌> --down-rate 12000 --down-channels 1
```

Windows：双击 `windows\树莓派网络麦克风.exe`（免装 Python、无控制台窗口）→
选传输方式 → 「启动服务」→ 点「把连接参数复制给树莓派」粘到树莓派执行。

## 常用排查命令

```bash
sudo python3 final_btmic.py --net-4g            # 模块/串口/PPP/路由 一屏诊断
sudo python3 final_btmic.py --net-probe         # 只测能不能连上电脑
sudo python3 final_btmic.py --net-status        # 上行/下行帧数、丢帧、抖动缓冲、电平
sudo python3 final_btmic.py --net-4g-at 'AT'    # 给模块发 AT（自动找口、自动试波特率）
```

自检（Windows 上，需 `soundcard`/`numpy`）：

```bash
python tests/test_proto.py     # 帧解析 + MQTT 报文 + 真 broker 收发
python tests/test_e2e.py tcp   # 真声卡端到端（两个方向都验音频内容）
python tests/test_pi_side.py   # 用 final_btmic.py 真正生成的命令行跑一遍
```
