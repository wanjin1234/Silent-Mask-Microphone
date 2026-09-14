# Silent-Mask-Microphone
A project for Tsinghua University Hardware Design Competition,aiming at reduce the volume when you speak

## 项目简介

这是一个**穿戴式生命探测与避障 AR 系统**，面向火灾救援等弱光、浓烟、陌生环境场景。
设备佩戴在救援人员头部，通过三个方向的毫米波雷达 + 超声波传感器感知周围环境，
在双目分屏显示器上以 **AR 立体 HUD**（或俯视图）叠加显示：

- **人体（幸存者）方向与距离** —— 黄色警示图标 + 距离数值；
- **普通障碍物方向与距离** —— 红/黄距离条。

救援人员**移动到位置 → 停下 → 按一下按钮**触发一次约 2 秒的「人体存在扫描」（检测移动的人），
扫描结果固定在视野中，帮助定位幸存者方位；同时超声波持续提供近距离避障信息。

## 系统组成

### 硬件

| 部件 | 数量 | 作用 |
| --- | --- | --- |
| DFRobot C4002 毫米波雷达 | 3 | 检测人体（运动），覆盖左 -45° / 中 0° / 右 +45° |
| JSN-SR04T 超声波传感器 | 3 | 检测障碍物距离（不测人体） |
| 树莓派 + ReSpeaker 2-Mic Pi HAT | 1 | 主控；音频；板载 GPIO 驱动超声波 |
| 双目分屏显示器 | 1 | AR 立体 HUD（1920×1080，左右各一） |
| 轻触按钮 | 1 | 接 GPIO，按一次触发一次扫描 |
| 微雪 UPS HAT (E) + 锂电池 | 1 | 顶针给树莓派（Pi 4/5）供电；I2C 上报电量 |

- 超声波优先使用**树莓派板载 GPIO + pigpio**（微秒级硬件定时，近距离也准）；
  备选 **FT232H + pyftdi**（`ultrasonic_mpsse.py`）。
- 雷达通过 USB 转串口接入（三个 `ttyUSB*`），用 `/dev/serial/by-path/` 稳定路径固定端口↔角度映射。

### 软件架构（`src/`）

| 文件 | 职责 |
| --- | --- |
| `main_stereo.py` | 主程序：传感器采集 → 扫描判定 → 融合 → 显示 |
| `c4002_parser.py` | C4002 串口帧解析 + `RealSensorHub`（雷达 + 超声波 hub） |
| `ultrasonic_rpigpio.py` | 树莓派 pigpio 超声波驱动（推荐） |
| `ultrasonic_mpsse.py` | FT232H 超声波驱动（备选） |
| `data_fusion.py` | 雷达/超声波距离融合与时间滤波 |
| `stereo_ar_display.py` | 立体 AR HUD / 俯视图渲染（`V` 键切换） |
| `gpio_button.py` | GPIO 按钮（边沿中断 + 去抖）触发扫描 |
| `simulated_sensors.py` | 模拟传感器（无硬件/Windows 下调试用） |
| `real_sensors.py` | 旧版 FT232H 传感器 hub（历史实现） |
| `probe_usb_ports.py` | USB 串口插拔监控，标定端口↔角度 |
| `probe_self_motion.py` | 佩戴者自呼吸干扰量化实验 |
| `diagnose_hub.py` / `diagnose_ultrasonic.py` | 数据链路诊断 |
| `test_radar.py` / `test_ultrasonic.py` | 单传感器调试 |
| `gpio_probe.py` / `gpio_verify.py` | GPIO 占用探测 / 引脚验证 |

## 检测原理

### 人体检测（C4002 毫米波雷达）

- 触发方式：GPIO 按钮（或空格键）触发一次**扫描窗口**（`C4002_SCAN_DURATION`，默认 2s）。
- 判定：**只检测移动的人**（走动 / 挥手），基于雷达的多普勒速度 `move_speed`。
  **不使用**固件学习出的 presence / target_status，零校准、零环境学习，即插即用。
- 极低速杂波滤除：|move_speed| 低于 `C4002_SPEED_DEADZONE`（默认 2 cm/s）一律按 0 处理，
  滤除传感器噪声 / 桌面微振动；超过 `C4002_MOTION_SPEED_MIN`（默认 20 cm/s）才算运动。
- 扫描窗口内某方向运动证据命中帧数达到 `C4002_SCAN_MOTION_MIN` 即判「有人」。

> 说明：完全静止（只呼吸、无任何动作）的人，C4002 无法在不做环境校准的情况下
> 与静态背景区分，故本项目明确只覆盖「移动 / 挥手可被探测」的幸存者场景。

### 障碍物检测（JSN-SR04T 超声波）

- 三个方向超声波持续测距，经 `data_fusion.py` 做中位数去噪 + 时间滤波，
  显示为红（<1.2m）/黄（<2.5m）距离条。

## 快速开始

```bash
# 1. 启动 pigpio（超声波与按钮依赖）
sudo pigpiod

# 2. 启动（run.sh 已固化雷达端口/角度映射）
bash run.sh
```

> 无硬件时（如 Windows）自动回退到 `SimulatedSensorHub` 模拟数据，便于调试显示层。

## GPIO 按钮触发人体扫描

用一个树莓派 GPIO 接普通按钮，按一次触发一次"人体存在扫描"（等同按空格键）。

接线：按钮一端接 GPIO（BCM 编号，默认 `GPIO4`），另一端接 `GND`，使用内部上拉，无需外接电阻。复用超声波已在用的 pigpio（`sudo pigpiod`）。

环境变量：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BUTTON_GPIO` | 按钮引脚 BCM 编号 | `4` |
| `BUTTON_DEBOUNCE_S` | 两次触发最小间隔秒数（扫描自身还有冷却时间兜底） | `0.2` |
| `BUTTON_DEBUG` | 设为 `1` 打印按键事件，便于在树莓派上验证 | `0` |

## UPS 电池监测与低电压关机（UPS HAT (E)）

用微雪 **UPS HAT (E)**（弹簧顶针给树莓派 Pi 4/5 供电）作不间断电源，板载 IP2368
（充电）+ BQ4050（电量计）经 I2C 上报电池电压/电流/电量，在 AR 画面左上角显示电池图标：
**外框 + 内部 5 格矩形**，电量 >50% 绿 / 20%~50% 黄 / ≤20% 红；充电时外框变青，
低电量（≤ `UPS_LOW_PERCENT`）时图标闪烁。

电量过低（默认 ≤ `UPS_SHUTDOWN_PERCENT` 且连续 `UPS_SHUTDOWN_CONSECUTIVE` 次命中）时，
自动把运行状态保存到 `logs/last_state.json`，向 UPS 写 `0x55` 切断输出（保护电池不过放），
并执行系统关机作为兜底。

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UPS_I2C_BUS` | `1` | I2C 总线号 |
| `UPS_I2C_ADDR` | `0x2D` | UPS HAT (E) 从站地址（官方默认） |
| `UPS_POLL_SECONDS` | `1.0` | 轮询间隔 s |
| `UPS_LOW_PERCENT` | `20` | 低于该百分比判为低电量并闪烁 |
| `UPS_SHUTDOWN_PERCENT` | `10` | 低于该百分比自动保存并关机 |
| `UPS_SHUTDOWN_VOLTAGE` | `0` | 关机电压阈值 V（0=禁用，仅用百分比判定） |
| `UPS_SHUTDOWN_CONSECUTIVE` | `3` | 连续多少次低电量才关机（防瞬时抖动） |
| `UPS_SHUTDOWN_ENABLED` | `1` | `0` 关闭实际关机（仅告警，调试用） |
| `UPS_SAVE_DIR` | `logs` | 关机前保存运行状态的目录 |
| `UPS_SIMULATE` | `0` | `1` 用模拟电量（Windows 调试图标/闪烁用） |

依赖与启用：
- 树莓派 venv 里装 `smbus2`（`pip install smbus2`），或系统装 `python3-smbus`。
- I2C 需在 `raspi-config` 中开启（官方：Interfacing Options → I2C）。
- 数据读取照搬官方寄存器文档（`UPS HAT (E) Register`，详见 `src/ups_battery.py` 注释）：
  电量百分比直接由 BQ4050 电量计给出（寄存器 `0x24/0x25`），无需电压换算；
  电池电压 `0x20/0x21`(mV)、电流 `0x22/0x23`(有符号 mA，正=充电/负=输出)、充电状态 `0x02` bit7。

## 雷达端口与角度映射

`/dev/ttyUSB*` 编号由内核按枚举顺序临时分配，每次开机/拔插可能变化，导致「端口 → 角度」对应关系漂移。请改用**稳定设备路径**：

```bash
ls -l /dev/serial/by-path/    # 按物理 USB 插口固定（推荐）
ls -l /dev/serial/by-id/      # 按设备序列号固定
```

环境变量：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `RADAR_PORTS` | 三个雷达串口路径（逗号分隔），建议用 `/dev/serial/by-path/...` | `/dev/ttyUSB0,1,2` |
| `RADAR_ANGLES` | 每个端口对应的角度（逗号分隔，与 `RADAR_PORTS` 顺序一致） | `-45,0,45` |
| `C4002_DEBUG` | 设为 `1` 打印雷达原始帧/解析字段，`0` 关闭 | `1` |

示例：

```bash
export RADAR_PORTS=/dev/serial/by-path/pci-0000:00:14.0-usb-0:1:1.0-port0,/dev/serial/by-path/pci-0000:00:14.0-usb-0:1:2.0-port0,/dev/serial/by-path/pci-0000:00:14.0-usb-0:1:3.0-port0
export RADAR_ANGLES=-45,0,45
```

> 已把本项目实际映射固化到 `run.sh`，直接 `bash run.sh` 启动即可（端口 = by-path，角度 = 左 -45° / 中 0° / 右 +45°）。

## 运动判定阈值

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `C4002_SCAN_DURATION` | 单次扫描时长 s | `3.0` |
| `C4002_SCAN_MOTION_MIN` | 判「有人」所需运动命中帧数 | `1` |
| `C4002_SPEED_DEADZONE` | 极低速杂波死区 cm/s（低于此值按 0） | `2` |
| `C4002_MOTION_SPEED_MIN` | 运动速度下限 cm/s（挥手慢速段 3~7） | `5` |
| `C4002_DISTANCE_VARIANCE` | 运动判定的距离变化阈值 m | `0.15` |
| `C4002_CONFIG_REPORT_PERIOD` | 上报周期（0.1s 单位） | `1`（10Hz） |
