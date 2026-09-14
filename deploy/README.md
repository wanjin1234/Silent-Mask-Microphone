# 部署说明（Raspberry Pi OS Lite）

本文档记录如何把 **Silent-Mask-Microphone** 部署到树莓派并配置开机自启动。

目标环境：**Raspberry Pi OS Lite（无桌面）+ HDMI 直连屏幕**。

## 〇、全新系统一键部署（推荐）

从一个**刚烧录完的树莓派**到能跑代码，只需两步。

### 第 1 步：Windows 开发机同步全部代码（scp）

在 PowerShell 里执行仓库内的同步脚本（已内置 scp 全部代码 + 建目录 + 清 `__pycache__`）：

```powershell
cd C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone
.\deploy\sync_to_pi.ps1
```

> 脚本顶部可改 `PI_USER / PI_HOST / PI_DIR`（默认 `wanghy25@192.168.124.25`，目录 `~/arDisplay`）。
> 已同步：`src/`（含 `test_ai`）、`run.sh`、`run_ai.sh`、`deploy/`、`README.md`。

### 第 2 步：树莓派一键装环境（Python 3.11.4 + 依赖）

```bash
ssh wanghy25@192.168.124.25
cd ~/arDisplay
bash deploy/setup_pi.sh
```

脚本会：源码编译安装 **Python 3.11.4**（`make altinstall` 到 `/usr/local`，不覆盖系统 python）
→ 建 `~/arDisplay/venv` → 装 `pygame/pyserial/pigpio/smbus2/numpy` → 开启 I2C → 从源码编译 pigpio 并启用 `pigpiod`。

> 编译耗时 15~40 分钟（视树莓派型号）。需要 AI 推理运行时加 `INSTALL_AI=1 bash deploy/setup_pi.sh`。

装完后直接运行：

```bash
cd ~/arDisplay
bash run.sh
```

## 一、目录结构与依赖

```
~/arDisplay/
├── run.sh                 # 启动脚本（固定雷达端口、选 venv python）
├── src/                   # 所有 .py 源码
├── venv/                  # Python 虚拟环境（pygame/serial/pigpio 都装在这里）
└── deploy/
    ├── ar-display.service # systemd 系统服务（Lite 用这个）
    └── README.md          # 本文档
```

**关键依赖**：
- `pygame`、`pyserial`、`pigpio` 都装在 `~/arDisplay/venv` 里，**系统 `python3` 没有**。
- UPS 电池监测需 `smbus2`（`venv/bin/pip install smbus2`）或系统 `python3-smbus`。
- 运行程序必须用 `venv/bin/python3`，见 `run.sh` 末尾。

## 二、文件同步（从 Windows 开发机，scp 全部代码）

推荐直接用脚本 `deploy\sync_to_pi.ps1`（已封装下面这些 scp，并自动建目录、清 `__pycache__`）：

```powershell
cd C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone
.\deploy\sync_to_pi.ps1
```

等价的手动命令如下（如需单独同步某个文件可参考）：

```powershell
$DST = "wanghy25@192.168.124.25:/home/wanghy25/arDisplay/"

# 先清本地 __pycache__，避免 .pyc 跨版本错乱
Get-ChildItem -Recurse -Directory -Filter __pycache__ "C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\src" | Remove-Item -Recurse -Force

# 全部代码
scp -r "C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\src" $DST
scp "C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\run.sh" $DST
scp "C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\run_ai.sh" $DST
scp -r "C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\deploy" $DST
scp "C:\Users\Ashes\Desktop\project\Silent-Mask-Microphone\README.md" $DST
```

> 注意：`src`、`deploy` 不带尾斜杠，避免变成 `src/src` 嵌套。
> PowerShell 用分号 `;` 连接命令，不用 `&&`。

## 三、部署到 systemd（开机自启动）

```bash
# 1. 确保 pigpiod 开机自启（超声波 + GPIO 按钮依赖）
sudo systemctl enable --now pigpiod

# 2. 启动脚本可执行
chmod +x ~/arDisplay/run.sh

# 3. 安装服务
sudo cp ~/arDisplay/ar-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ar-display.service
sudo systemctl start ar-display.service

# 4. 查看状态 / 日志
systemctl status ar-display.service
sudo journalctl -u ar-display.service -f
```

重启验证：`sudo reboot`，上电后自动运行到 HDMI。

## 四、常见问题排查

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'pygame'` | 用了系统 python3 | 确认 `run.sh` 末尾用 `venv/bin/python3` |
| 服务疯狂重启（counter 飙升） | 启动即崩 | `journalctl -u ar-display.service -f` 看报错 |
| SDL 视频初始化失败 | SDL 无 kmsdrm 支持 | 删掉 service 里 `SDL_VIDEODRIVER=kmsdrm` |
| pigpiod 启动失败 | 手动实例占用 | `sudo killall pigpiod && sudo systemctl restart pigpiod` |
| `RuntimeWarning: neon capable...` | pygame 无 NEON 加速 | 无害，忽略 |

## 五、雷达端口映射

`run.sh` 中已固化（by-path 稳定路径，避免 `/dev/ttyUSB*` 漂移）：

```
左 -45°  → /dev/serial/by-path/...usb-0:1.1:1.0-port0
中  0°   → /dev/serial/by-path/...usb-0:1.2:1.0-port0
右 +45°  → /dev/serial/by-path/...usb-0:1.4:1.0-port0
```

如果硬件插口变了，先在树莓派 `ls -l /dev/serial/by-path/` 确认后改 `run.sh`。

## 六、人体检测（只检测移动的人）

检测逻辑：**只信多普勒速度 `move_speed`**，零校准、零环境学习、即插即用。

- 极低速杂波死区：`C4002_SPEED_DEADZONE=2`（cm/s），低于此值按 0。
- 运动阈值：`C4002_MOTION_SPEED_MIN=5`（cm/s），超过即判定运动。
- 扫描窗口：`C4002_SCAN_DURATION=3.0`（s），命中 `C4002_SCAN_MOTION_MIN=1` 帧即判有人。

> 已确认：完全静止（只呼吸）的人无法用 C4002 在不校准的情况下与背景区分，故本项目只覆盖"移动 / 挥手可被探测"的场景。

关键参数（环境变量，默认值已写入 `run.sh` / 代码）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `C4002_SCAN_DURATION` | 3.0 | 单次扫描时长 s |
| `C4002_SCAN_MOTION_MIN` | 1 | 判有人所需运动命中帧数 |
| `C4002_SPEED_DEADZONE` | 2 | 极低速杂波死区 cm/s |
| `C4002_MOTION_SPEED_MIN` | 5 | 运动速度下限 cm/s |
| `C4002_DEBUG` | 0 | 自启动时 0；调试时改 1 |
| `C4002_CONFIG_REPORT_PERIOD` | 1 | 上报周期（0.1s），改后需重启生效 |
| `C4002_CONFIG_RESTART` | 1 | 配置后重启雷达 |

## 七、手动运行（调试用）

```bash
cd ~/arDisplay
bash run.sh            # 自动选 venv python，勿用 python3 src/main_stereo.py
```

调试雷达时临时开打印：`export C4002_DEBUG=1` 后再 `bash run.sh`。

## 八、AI 人体存在检测（可选）

`src/test_ai/test_ai/` 是一套基于 TensorFlow 的人体存在检测（滑动窗口 + 概率平滑 + 滞回），
已通过 `src/ai_presence.py` 接入 `main_stereo.py`。启用后，主程序用 `RawRadarHub`
读取距离门掩码等扩展字段，AI 引擎与"移动扫描/障碍物融合"**共用同一份雷达串口**；
画面右上角会显示 `AI人体: 有人/无人 P=… 目标…`。

### 前置：在开发机训练并导出模型

```bash
# 开发机（有 TensorFlow），按 test_ai/README.md 采集真机数据后：
cd src/test_ai/test_ai
python train.py --data data/real.npz --out-dir models --tflite
# 产出：models/presence_model.tflite 与 models/presence_model_meta.json
```

### 上传模型到树莓派

```powershell
$DST = "wanghy25@192.168.124.25:/home/wanghy25/arDisplay/"

scp "C:\...\src\test_ai\test_ai\models\presence_model.tflite"  "$DST/models/"
scp "C:\...\src\test_ai\test_ai\models\presence_model_meta.json" "$DST/models/"
```

### 运行（AI 模式）

```bash
cd ~/arDisplay
bash run_ai.sh          # 等价于 run.sh + C4002_AI=1
```

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `C4002_AI` | 0 | 1 启用 AI 人体检测 |
| `C4002_AI_MODEL_DIR` | models | 模型目录（相对项目根目录） |
| `C4002_AI_NAME` | presence_model | 模型名（不含扩展名） |
| `C4002_AI_ON_THRESHOLD` | 取 meta | 判「有人」概率阈值 |
| `C4002_AI_RULE_MODE` | motion_or_breath | 无模型时规则后端模式 |

> 找不到模型时自动退回规则后端（motion_or_breath），不会崩溃；
> 但该规则后端只是现行移动判定的连续版，误报特性与现行逻辑一致。
> 上真机前请务必按 `test_ai/README.md` 先跑 `probe_gates.py` 验证距离门字段语义。
