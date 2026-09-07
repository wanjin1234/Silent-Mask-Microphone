# Silent-Mask-Microphone
A project for Tsinghua University Hardware Design Competition,aiming at reduce the volume when you speak

## GPIO 按钮触发人体扫描

用一个树莓派 GPIO 接普通按钮，按一次触发一次"人体存在扫描"（等同按空格键）。

接线：按钮一端接 GPIO（BCM 编号，默认 `GPIO4`），另一端接 `GND`，使用内部上拉，无需外接电阻。复用超声波已在用的 pigpio（`sudo pigpiod`）。

环境变量：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `BUTTON_GPIO` | 按钮引脚 BCM 编号 | `4` |
| `BUTTON_DEBOUNCE_S` | 两次触发最小间隔秒数（扫描自身还有冷却时间兜底） | `0.2` |
| `BUTTON_DEBUG` | 设为 `1` 打印按键事件，便于在树莓派上验证 | `0` |

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

## 呼吸 / 运动判定阈值

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `C4002_BREATH_SPEED_MIN` | 呼吸微动速度下限 cm/s（过滤微小抖动） | `5` |
| `C4002_BREATH_SPEED_MAX` | 呼吸微动速度上限 cm/s | `20` |
| `C4002_BREATH_PERIOD_LEN` | 周期性检测窗口帧数 | `20` |
| `C4002_BREATH_CROSS_MIN` | 窗口内最少速度符号翻转次数 | `1` |
| `C4002_MOTION_SPEED_MIN` | 运动速度下限 cm/s | `20` |
| `C4002_DISTANCE_VARIANCE` | 运动判定的距离变化幅度 m | `0.15` |
| `C4002_SCAN_DURATION` | 单次扫描时长 s | `2.0` |
| `C4002_SCAN_BREATH_MIN` | 判「有人」所需呼吸命中帧数 | `2` |
| `C4002_SCAN_MOTION_MIN` | 判「有人」所需运动命中帧数 | `2` |
