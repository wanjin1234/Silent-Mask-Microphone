# MLX90640 32x24 热成像显示（树莓派 4B）

这是可直接部署的版本。读取逻辑通过 `adafruit-circuitpython-mlx90640` 直接读取
MLX90640 的校准温度帧：每一帧 768 个摄氏温度值，窗口中严格显示为 32 列 x 24
行色块，没有插值，一个色块对应一个传感器像元。

本目录文件：

- `mlx90640_thermal_display.py` —— 32x24 彩色热成像显示（主程序）
- `mlx90640_temperature_detection.py` —— 高温区域检测/告警（JSON 输出）
- `test_core.py` —— 无硬件自检
- `requirements.txt` —— 树莓派依赖
- `README.md` —— 本说明

## 硬件连接

MLX90640 必须使用 3.3 V 供电，不能接树莓派的 5 V 引脚。默认 7 位 I2C 地址为
`0x33`。

| MLX90640 引脚 | 树莓派 4B 物理引脚 | BCM GPIO |
| --- | --- | --- |
| VDD | 1（3.3 V） | - |
| GND | 6（GND） | - |
| SDA | 3 | GPIO 2 / SDA1 |
| SCL | 5 | GPIO 3 / SCL1 |

使用带电压转换、稳压和 I2C 上拉电阻的 MLX90640 模块时，仍应先确认模块的 VCC
规格；树莓派 GPIO 不耐受 5 V 电平。

## 在树莓派上安装

先在 `raspi-config` 的 **Interface Options -> I2C** 启用 I2C，然后重启。

```bash
sudo apt update
sudo apt install -y python3-venv i2c-tools
mkdir -p ~/mlx90640-thermal
cd ~/mlx90640-thermal
python3.11 --version          # 确认显示 3.11.x，再把本目录文件复制到这里
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
i2cdetect -y 1
```

最后一条命令应在 `33` 位置看到设备。若没有，先断电检查 VDD/GND/SDA/SCL 接线；
权限不足时以 root 执行诊断命令即可。

## 运行

```bash
source ~/mlx90640-thermal/.venv/bin/activate
cd ~/mlx90640-thermal
python mlx90640_thermal_display.py --rate 2
```

按 `Q` 或 `Esc` 退出。启动成功后会打印传感器序列号。默认色阶取当前画面第 5 到
第 95 百分位并平滑更新，低于/高于色阶的点分别饱和为蓝色/红紫色。

固定色阶示例：

```bash
python mlx90640_thermal_display.py --rate 2 --min 20 --max 40
```

其它常用命令：

```bash
python mlx90640_thermal_display.py --flip-h            # 水平镜像
python mlx90640_thermal_display.py --flip-v            # 垂直镜像
python mlx90640_thermal_display.py --simulate          # 不读硬件，仅验证显示与配色
python mlx90640_thermal_display.py --simulate --headless --screenshot thermal_test.png
python mlx90640_thermal_display.py --rate 4 --i2c-frequency 800000
```

`--i2c-frequency` 是传给 I2C 驱动的请求频率。高帧率若出现 `too many retries`、
读帧异常或卡顿，先退回 `--rate 2`，必要时在 I2C 配置中把 `i2c_arm_baudrate`
配置为与硬件兼容的值后重启。

## 高温区域检测（可选）

`mlx90640_temperature_detection.py` 每帧输出一行 JSON，包含全视场温度统计、
最高温坐标与告警状态；相邻热像素组成的区域连续若干帧满足条件才判定 `detected`。

```bash
python mlx90640_temperature_detection.py --threshold 50 --min-pixels 4 --rate 2
python mlx90640_temperature_detection.py --threshold 40 --min-pixels 4 --consecutive-frames 3 --once
```

主要参数：`--threshold` 高温阈值 °C、`--min-pixels` 热区最小四连通像素数、
`--consecutive-frames` 连续命中帧数、`--roi x,y,width,height` 检测区域、
`--rate` 帧率、`--once` 仅读一帧。

## 树莓派 4B 读取注意（重要）

树莓派 4B 的 BCM2711 硬件 I2C 控制器对时钟拉伸容忍时间过短，读 MLX90640 的
RAM 帧时可能返回全 0（症状：EEPROM/序列号正常、状态寄存器 dataReady=1，但
RAM 全 0，`getFrame` 内部除零）。本项目默认使用 smbus2 的 `i2c_rdwr` 传输层绕开
SMBus 32 字节块读上限；若仍失败，改用软件 I2C：

在 `/boot/config.txt`（或 `/boot/firmware/config.txt`）追加：

```
dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24
```

传感器 SDA→BCM23（物理 16）、SCL→BCM24（物理 18）。重启后 `i2cdetect -y 3`
应见 `0x33`，运行：

```bash
export MLX90640_I2C_BUS=3
python mlx90640_thermal_display.py --rate 2
```

如需强制回退 Blinka 的 `busio.I2C`：`export MLX90640_USE_BLINKA=1`（不推荐）。

## 自检

不接传感器也可验证颜色映射与 32x24 帧逻辑：

```bash
python3.11 test_core.py
```

## 从开发机上传到树莓派（scp）

Windows PowerShell（开发机）侧，把 `pi@raspberrypi.local` 换成你的用户名和地址：

```powershell
cd C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\mlx90640-thermal
scp mlx90640_thermal_display.py mlx90640_temperature_detection.py test_core.py requirements.txt README.md pi@raspberrypi.local:~/mlx90640-thermal/
```

完整端到端流程：

```powershell
# 1) 树莓派侧：创建目录并装好依赖（见上一节「在树莓派上安装」）
#    ssh pi@raspberrypi.local
#    mkdir -p ~/mlx90640-thermal && cd ~/mlx90640-thermal
#    python3.11 -m venv .venv && source .venv/bin/activate
#    python -m pip install -r requirements.txt

# 2) Windows 侧：上传文件（目标目录必须已存在于树莓派上）
cd C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\mlx90640-thermal
scp mlx90640_thermal_display.py mlx90640_temperature_detection.py test_core.py requirements.txt README.md pi@raspberrypi.local:~/mlx90640-thermal/

# 3) 树莓派侧：自检 + 运行
#    cd ~/mlx90640-thermal && source .venv/bin/activate
#    python3.11 test_core.py
#    i2cdetect -y 1              # 应在 33 位置看到设备
#    python mlx90640_thermal_display.py --rate 2
```

若开发机是 Linux/macOS 或用了 WSL，可用 rsync 增量同步：

```bash
rsync -av --exclude '.venv' --exclude '__pycache__' . pi@raspberrypi.local:~/mlx90640-thermal/
```

## 重要限制

这是一台热成像显示 / 非接触式表面温度装置，并非经过计量认证的体温计。目标材质
的发射率、反射环境、距离、气流和传感器预热都会影响绝对温度；不要把显示温度用于
医疗诊断或安全临界控制。
