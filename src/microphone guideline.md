## 🤖 Assistant

# 树莓派4B + ReSpeaker 2-Mic 作为 Windows 蓝牙麦克风 — 完整方案

## 整体思路

要让树莓派通过蓝牙把 ReSpeaker 2-Mic 的音频传给 Windows 电脑，树莓派需要扮演 **蓝牙音频网关（Audio Gateway, AG）** 的角色，也就是模拟手机 / 免提设备的行为。Windows 则作为 **免提终端（Hands-Free, HF）** 主动连接到树莓派，并把它识别成一个蓝牙麦克风。

关键的技术路线是：

1. **驱动 ReSpeaker 2-Mic** —— 让树莓派能通过 ALSA 读取麦克风音频。
2. **安装 PipeWire 替代 PulseAudio** —— PipeWire 原生支持 HFP‑AG / HSP‑AG 角色，而 PulseAudio 对 AG 的支持很弱。
3. **配置 PipeWire 启用 HFP‑AG 角色** —— 让树莓派在蓝牙上呈现为“音频网关”。
4. **将树莓派设为蓝牙可发现** —— Windows 才能扫描到它。
5. **音频路由** —— 把 ReSpeaker 的 ALSA 录音设备桥接到蓝牙 AG 的输入。
6. **编写 Python 管理脚本** —— 自动完成配对、连接、音频路由和状态监控。

---

## 第一步：安装 ReSpeaker 2‑Mic 驱动

```bash
# 安装 seeed-voicecard 驱动（推荐从 GitHub 安装最新版）
sudo apt update
sudo apt install git dkms raspberrypi-kernel-headers
git clone https://github.com/respeaker/seeed-voicecard.git
cd seeed-voicecard
sudo ./install.sh
# 重启后检查 ALSA 设备
arecord -l
```

驱动安装完成后，你应该能看到类似 `seeed2micvoicec` 的录音设备，例如：

```
card 0: seeed2micvoicec [seeed-2mic-voicecard], device 0: ...
```

记下这个设备名（通常是 `hw:0,0` 或 `hw:seeed2micvoicec,0`），后面会用到。

---

## 第二步：安装 PipeWire 并替换 PulseAudio

Raspberry Pi OS (Bookworm 及更新版本) 已经默认使用 PipeWire。如果你的系统还在用 PulseAudio，请执行以下迁移步骤：

```bash
# 安装 PipeWire 及相关组件
sudo apt update
sudo apt install pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth

# 启用用户级服务
systemctl --user --now enable pipewire pipewire-pulse wireplumber

# 如果之前有 PulseAudio，禁用它
systemctl --user --now disable pulseaudio.service pulseaudio.socket 2>/dev/null
systemctl --user mask pulseaudio 2>/dev/null

# 重启或重新登录
sudo reboot
```

重启后，确认 PipeWire 正在运行：

```bash
pactl info | grep "Server Name"
# 应输出: Server Name: pulseaudio (on PipeWire ...)
```

---

## 第三步：配置 PipeWire 启用 HFP‑AG / HSP‑AG

编辑 WirePlumber 的蓝牙配置文件（如果目录不存在请创建）：

```bash
mkdir -p ~/.config/wireplumber/bluetooth.lua.d/
nano ~/.config/wireplumber/bluetooth.lua.d/50-bluez-config.lua
```

写入以下内容（**核心配置**）：

```lua
bluez_monitor.properties = {
  ["bluez5.enable-sbc-xq"] = true,
  ["bluez5.enable-msbc"] = true,
  ["bluez5.enable-hw-volume"] = true,
  ["bluez5.headset-roles"] = "[hsp_hs hsp_ag hfp_hf hfp_ag]",
  -- 关键：启用 Audio Gateway 角色
  ["bluez5.roles"] = "[a2dp_sink a2dp_source hfp_ag hsp_ag]"
}
```

**说明：**
- `hfp_ag` = HFP Audio Gateway（树莓派充当“手机”）
- `hsp_ag` = HSP Audio Gateway
- Windows 通常优先使用 HFP，因此 `hfp_ag` 最关键。

保存后重启 WirePlumber：

```bash
systemctl --user restart wireplumber
```

---

## 第四步：配置蓝牙为可发现 & 自动配对

编辑 BlueZ 主配置文件：

```bash
sudo nano /etc/bluetooth/main.conf
```

找到并修改以下行（取消注释并设置）：

```ini
DiscoverableTimeout = 0      # 始终可被发现
AlwaysPairable = true        # 始终允许配对
Class = 0x200404              # 设备类型：Audio/Video, Headset
```

然后重启蓝牙服务：

```bash
sudo systemctl restart bluetooth
```

使用 `bluetoothctl` 将树莓派设为可被发现：

```bash
bluetoothctl
power on
discoverable on
pairable on
agent NoInputNoOutput
default-agent
```

> 由于树莓派没有屏幕和键盘，使用 `NoInputNoOutput` 代理可以自动接受配对（无需 PIN 码确认）。Windows 连接时直接配对即可。

**可选：** 将以上命令写入一个启动脚本，实现开机自启。

---

## 第五步：Python 管理脚本 — 自动配对、音频路由与状态监控

下面的脚本实现以下功能：

1. 等待 Windows 电脑通过蓝牙连接。
2. 检测蓝牙 AG 音频源（source）是否出现。
3. 创建 PipeWire 环路（loopback），将 ReSpeaker 的 ALSA 录音设备桥接到蓝牙 AG 的输入。
4. 监控连接状态，断开后自动重新等待。

### 安装 Python 依赖

```bash
pip install pyaudio dbus-python
```

### 脚本 `bt_mic_bridge.py`

```python
#!/usr/bin/env python3
"""
树莓派蓝牙麦克风桥接脚本
- 将 ReSpeaker 2-Mic 的音频通过 HFP/HSP 发送给连接的 Windows 电脑
- 依赖: PipeWire, WirePlumber (已配置 HFP-AG), pyaudio, dbus-python
"""

import os
import sys
import time
import subprocess
import signal
import threading
import dbus
from dbus.mainloop.glib import DBusGMainLoop
import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib

# ===================== 配置区 =====================
# ReSpeaker ALSA 设备名（用 arecord -l 查看）
RESPEAKER_DEVICE = "hw:seeed2micvoicec,0"
# 或者用 hw:0,0 等

# 音频参数（HFP/HSP 通常为 8kHz/16kHz 单声道，16-bit）
SAMPLE_RATE = 16000          # 16 kHz 是 HFP 宽频，8 kHz 是窄频
CHANNELS = 1
FORMAT = "s16le"             # 16-bit little-endian

# 蓝牙设备名称（Windows 上看到的名称）
BT_DEVICE_NAME = "RaspberryPi-Mic"

# ===================== 蓝牙管理类 =====================
class BluetoothManager:
    """通过 D-Bus 管理蓝牙配对与连接状态"""

    def __init__(self):
        self.bus = dbus.SystemBus()
        self.adapter_path = None
        self._find_adapter()

    def _find_adapter(self):
        """找到默认蓝牙适配器"""
        manager = dbus.Interface(
            self.bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager"
        )
        objects = manager.GetManagedObjects()
        for path, ifaces in objects.items():
            if "org.bluez.Adapter1" in ifaces:
                self.adapter_path = path
                break
        if not self.adapter_path:
            raise RuntimeError("未找到蓝牙适配器")

    def set_discoverable(self, timeout=0):
        """设置可发现模式"""
        adapter = dbus.Interface(
            self.bus.get_object("org.bluez", self.adapter_path),
            "org.freedesktop.DBus.Properties"
        )
        adapter.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(timeout))
        adapter.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
        adapter.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
        print("✅ 蓝牙已设为可发现 / 可配对")

    def set_alias(self, name):
        """设置蓝牙设备名称"""
        adapter = dbus.Interface(
            self.bus.get_object("org.bluez", self.adapter_path),
            "org.freedesktop.DBus.Properties"
        )
        adapter.Set("org.bluez.Adapter1", "Alias", name)
        print(f"✅ 蓝牙名称已设为: {name}")

    def get_connected_devices(self):
        """返回已连接的设备 MAC 列表"""
        manager = dbus.Interface(
            self.bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager"
        )
        objects = manager.GetManagedObjects()
        devices = []
        for path, ifaces in objects.items():
            if "org.bluez.Device1" in ifaces:
                props = ifaces["org.bluez.Device1"]
                if props.get("Connected", False):
                    devices.append(props.get("Address", "unknown"))
        return devices

    def wait_for_connection(self, timeout=None):
        """阻塞等待直到有设备连接，返回设备 MAC"""
        print("🔍 等待 Windows 电脑连接蓝牙...")
        start = time.time()
        while True:
            devices = self.get_connected_devices()
            if devices:
                print(f"🔗 已连接设备: {devices[0]}")
                return devices[0]
            if timeout and (time.time() - start) > timeout:
                raise TimeoutError("等待连接超时")
            time.sleep(1)


# ===================== 音频桥接类 =====================
class AudioBridge:
    """将 ReSpeaker ALSA 设备桥接到 PipeWire 蓝牙 AG source"""

    def __init__(self, alsa_device, sample_rate, channels):
        self.alsa_device = alsa_device
        self.sample_rate = sample_rate
        self.channels = channels
        self.running = False
        self.process = None

    def find_bluetooth_source(self):
        """查找蓝牙 AG 的音频源（即 Windows 听到的麦克风）"""
        try:
            result = subprocess.run(
                ["pw-cli", "ls", "Node"],
                capture_output=True, text=True, timeout=5
            )
            # 简化解析：寻找包含 "bluez" 且方向为 "input" 的节点
            lines = result.stdout.splitlines()
            current_id = None
            for line in lines:
                if line.startswith("id "):
                    current_id = line.split()[1].rstrip(",")
                if "bluez" in line.lower() and "input" in line.lower():
                    return current_id
        except Exception as e:
            print(f"⚠️ 查找蓝牙 source 失败: {e}")
        return None

    def start(self):
        """启动音频桥接"""
        self.running = True
        self.process = subprocess.Popen(
            [
                "pw-loopback",
                "--capture-props", f"node.target={self.alsa_device}",
                "--playback-props", "media.class=Audio/Source",
                "--rate", str(self.sample_rate),
                "--channels", str(self.channels),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        print(f"🔄 音频桥接已启动: {self.alsa_device} → Bluetooth AG")

    def stop(self):
        """停止音频桥接"""
        self.running = False
        if self.process:
            self.process.terminate()
            self.process.wait()
            print("⏹️ 音频桥接已停止")


# ===================== 主程序 =====================
def main():
    print("=" * 50)
    print("树莓派蓝牙麦克风桥接工具")
    print("=" * 50)

    # 1. 初始化蓝牙
    bt = BluetoothManager()
    bt.set_alias(BT_DEVICE_NAME)
    bt.set_discoverable(timeout=0)  # 0 = 始终可发现

    # 2. 注册信号处理（优雅退出）
    bridge = AudioBridge(RESPEAKER_DEVICE, SAMPLE_RATE, CHANNELS)

    def cleanup(signum=None, frame=None):
        print("\n🛑 正在退出...")
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # 3. 主循环：等待连接 → 建立音频桥 → 监控断开 → 重新等待
    while True:
        try:
            # 等待 Windows 连接
            device_mac = bt.wait_for_connection()

            # 短暂延迟，确保 PipeWire 创建了蓝牙节点
            time.sleep(2)

            # 启动音频桥接
            bridge.start()

            # 监控连接状态
            while True:
                time.sleep(2)
                connected = bt.get_connected_devices()
                if device_mac not in connected:
                    print("🔌 设备已断开，停止音频桥接...")
                    bridge.stop()
                    break

        except KeyboardInterrupt:
            cleanup()
        except Exception as e:
            print(f"❌ 错误: {e}")
            bridge.stop()
            time.sleep(5)  # 出错后等待 5 秒再重试


if __name__ == "__main__":
    main()
```

---

## 第六步：开机自启（可选）

创建 systemd 用户服务：

```bash
mkdir -p ~/.config/systemd/user/
nano ~/.config/systemd/user/bt-mic-bridge.service
```

内容：

```ini
[Unit]
Description=Bluetooth Microphone Bridge
After=bluetooth.service pipewire.service wireplumber.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/pi/bt_mic_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

启用服务：

```bash
systemctl --user daemon-reload
systemctl --user enable bt-mic-bridge.service
systemctl --user start bt-mic-bridge.service
```

---

## 第七步：Windows 端操作

1. 打开 Windows **设置 → 蓝牙和其他设备**。
2. 点击 **添加蓝牙设备**。
3. 选择 **蓝牙**，Windows 会扫描到名为 `RaspberryPi-Mic` 的设备。
4. 点击配对，Windows 会自动将其识别为 **免提设备**（Hands-Free）。
5. 配对成功后，在 **声音设置 → 输入** 中，你应该能看到 `RaspberryPi-Mic` 作为麦克风选项。
6. 选择它，即可将树莓派的 ReSpeaker 2-Mic 作为 Windows 的麦克风使用。

---

## 常见问题排查

| 问题 | 可能原因 | 解决方法 |
|------|----------|----------|
| Windows 扫描不到树莓派 | 蓝牙未设为可发现 | 执行 `bluetoothctl discoverable on` |
| 配对失败 | 缺少输入代理 | `bluetoothctl agent NoInputNoOutput` |
| 连接后 Windows 未识别为麦克风 | PipeWire 未启用 HFP-AG | 检查 `~/.config/wireplumber/bluetooth.lua.d/50-bluez-config.lua` 中的 `bluez5.roles` 配置 |
| 音频不通 | 桥接未建立 | 手动测试 `pw-loopback` 命令，检查 ALSA 设备名 |
| 音质差 / 断续 | HFP 窄频限制 | HFP 窄频为 8kHz，宽频为 16kHz；尽量使用 16kHz 配置 |

---

## 备选方案：使用 bluez-alsa + ofono

如果 PipeWire 方案在树莓派上遇到兼容性问题，也可以尝试 `bluez-alsa` + `ofono` 的组合，它专门用于处理 HFP-AG 角色。不过配置更复杂，通常 PipeWire 方案已经足够。

---

这个方案利用了 PipeWire 的原生 HFP-AG 能力，避免了手动处理 CVSD 编码等底层细节，让蓝牙协议栈自动完成音频的编码和传输。如果你在实际操作中遇到具体报错，可以把错误信息贴出来，我帮你进一步排查。
[1] [树莓派可以使用蓝牙耳机作为mic输入设备吗？ - CSDN文库](https://wenku.csdn.net/answer/1ahgotyo2z)

[2] [如何在pulseaudio >=6和bluez >= 5.24上使用HSP/HFP配置文件与蓝牙设备配对。 raspberry-pi raspbian  - Dev59](https://dev59.com/unix/_49hRYkBhS1WtX2rg69t)

[3] [蓝牙耳机 - ArchWiki - Arch Linux 教程](https://wiki.archlinux.org.cn/title/Bluetooth_headset)

[4] [树莓派上连接上蓝牙音响了播放音频怎么使用音响来播放？ - CSDN文库](https://wenku.csdn.net/answer/4dqz0rjwk0)

[5] [how to use bluetooth device with HSP/HFP profile ...](https://unix.stackexchange.com/questions/341776/how-to-use-bluetooth-device-with-hsp-hfp-profile-using-pulseaudio-6-and-bluez)

[1] [树莓派可以使用蓝牙耳机作为mic输入设备吗？ - CSDN文库](https://wenku.csdn.net/answer/1ahgotyo2z)

[2] [如何在pulseaudio >=6和bluez >= 5.24上使用HSP/HFP配置文件与蓝牙设备配对。 raspberry-pi raspbian  - Dev59](https://dev59.com/unix/_49hRYkBhS1WtX2rg69t)

[3] [蓝牙耳机 - ArchWiki - Arch Linux 教程](https://wiki.archlinux.org.cn/title/Bluetooth_headset)

[4] [树莓派上连接上蓝牙音响了播放音频怎么使用音响来播放？ - CSDN文库](https://wenku.csdn.net/answer/4dqz0rjwk0)

[5] [how to use bluetooth device with HSP/HFP profile ...](https://unix.stackexchange.com/questions/341776/how-to-use-bluetooth-device-with-hsp-hfp-profile-using-pulseaudio-6-and-bluez)

[1] [树莓派可以使用蓝牙耳机作为mic输入设备吗？ - CSDN文库](https://wenku.csdn.net/answer/1ahgotyo2z)

[2] [如何在pulseaudio >=6和bluez >= 5.24上使用HSP/HFP配置文件与蓝牙设备配对。 raspberry-pi raspbian  - Dev59](https://dev59.com/unix/_49hRYkBhS1WtX2rg69t)

[3] [蓝牙耳机 - ArchWiki - Arch Linux 教程](https://wiki.archlinux.org.cn/title/Bluetooth_headset)

[4] [树莓派上连接上蓝牙音响了播放音频怎么使用音响来播放？ - CSDN文库](https://wenku.csdn.net/answer/4dqz0rjwk0)

[5] [how to use bluetooth device with HSP/HFP profile ...](https://unix.stackexchange.com/questions/341776/how-to-use-bluetooth-device-with-hsp-hfp-profile-using-pulseaudio-6-and-bluez)

[1] [树莓派可以使用蓝牙耳机作为mic输入设备吗？ - CSDN文库](https://wenku.csdn.net/answer/1ahgotyo2z)

[2] [如何在pulseaudio >=6和bluez >= 5.24上使用HSP/HFP配置文件与蓝牙设备配对。 raspberry-pi raspbian  - Dev59](https://dev59.com/unix/_49hRYkBhS1WtX2rg69t)

[3] [蓝牙耳机 - ArchWiki - Arch Linux 教程](https://wiki.archlinux.org.cn/title/Bluetooth_headset)

[4] [树莓派上连接上蓝牙音响了播放音频怎么使用音响来播放？ - CSDN文库](https://wenku.csdn.net/answer/4dqz0rjwk0)

[5] [how to use bluetooth device with HSP/HFP profile ...](https://unix.stackexchange.com/questions/341776/how-to-use-bluetooth-device-with-hsp-hfp-profile-using-pulseaudio-6-and-bluez)

[1] [树莓派可以使用蓝牙耳机作为mic输入设备吗？ - CSDN文库](https://wenku.csdn.net/answer/1ahgotyo2z)

[2] [如何在pulseaudio >=6和bluez >= 5.24上使用HSP/HFP配置文件与蓝牙设备配对。 raspberry-pi raspbian  - Dev59](https://dev59.com/unix/_49hRYkBhS1WtX2rg69t)

[3] [蓝牙耳机 - ArchWiki - Arch Linux 教程](https://wiki.archlinux.org.cn/title/Bluetooth_headset)

[4] [树莓派上连接上蓝牙音响了播放音频怎么使用音响来播放？ - CSDN文库](https://wenku.csdn.net/answer/4dqz0rjwk0)

[5] [how to use bluetooth device with HSP/HFP profile ...](https://unix.stackexchange.com/questions/341776/how-to-use-bluetooth-device-with-hsp-hfp-profile-using-pulseaudio-6-and-bluez)