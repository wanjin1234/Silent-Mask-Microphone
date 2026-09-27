# thermal_ai —— 用 TF-Lite 在 32x24 热像上做「人类存在」视觉检测

把 MLX90640 的 **32x24 温度帧当成图像**，训练一个小型 CNN 判「有人/无人」，
导出 TFLite 在树莓派 4B 上用轻量解释器推理。与 `src/test_ai/test_ai/`（基于
C4002 毫米波雷达的时序模型）是两条**相互独立**的通道，可以各自运行、结果融合。

---

## 一、它能做什么、不能做什么（先读）

**能**：在热像视野内检测「温度约 30~37°C、且形态像人（身体+头部的一块连续斑块）」
的目标，并给出 `present / prob / region`。

**不能**（物理/场景限制，换任何模型都一样）：

1. MLX90640 只有 768 个低分辨率像素、没有颜色、没有深度。远距离或半个身位
   的人可能只有几个像素，容易漏检。
2. 它会「看见」所有热的东西。**高温物体（热饮/电暖器/火苗/阳光热斑）都比人更热**，
   模型靠「人体温区 + 人形斑块」把它们区分开，但训练数据若没覆盖足够多的这类
   干扰，就会误报。**这是本任务最主要的难点**。
3. 穿厚外套/隔热层的人，体表温度会显著低于 30°C，可能漏检；靠近发热源的背景
   会被加热，可能误报。
4. 换房间、换朝向、换传感器摆位后，背景温度与反射环境变了，需要补采少量数据重训。

---

## 二、目录结构

```
thermal_ai/
├── thermal_camera.py   读取 MLX90640（smbus2 直连，复用已验证的读帧方式）
├── preprocess.py       温度帧 → 模型张量 + 热区统计 + 按片段切分
├── synth.py            合成热像数据集（打通流程/冒烟用，不替代真机数据）
├── model_io.py         模型加载（自动识别 tf / tflite / numpy 后端）
├── thermal_numpy.py    纯 numpy 逻辑回归后端（装不上 TF 时的保底）
├── collect.py          真机标注采集
├── train.py            训练 + 评估 + 导出 TFLite / numpy
├── evaluate.py         独立场次验证（AUC/混淆矩阵/逐段诊断）
├── detect_live.py      实时推理（平滑 + 滞回，可发布 JSON）
├── smoke_test.py       一条命令自检整条链路
├── requirements.txt    依赖
└── README.md           本说明
```

---

## 三、环境配置步骤

### 3.1 开发机（Windows/Linux/macOS，训练用）

```powershell
# 建议 Python 3.10/3.11
python -m venv .venv
.venv\Scripts\activate           # Windows；Linux/macOS 用 source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

TensorFlow 只装在有 x86 官方 wheel 的开发机上。**训练必须在开发机完成**。

### 3.2 树莓派 4B（部署/推理用）

```bash
# 1) 启用 I2C（raspi-config -> Interface Options -> I2C -> Yes），然后重启
sudo apt update
sudo apt install -y python3-venv i2c-tools

# 2) 建虚拟环境（用系统自带 python3）
mkdir -p ~/thermal_ai && cd ~/thermal_ai
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# 3) 只装「推理 + 读传感器」所需，不装完整 TF
python -m pip install numpy smbus2 adafruit-blinka adafruit-circuitpython-mlx90640
python -m pip install ai-edge-litert        # TFLite 轻量解释器（推荐）

# 4) 确认 I2C 能读到传感器（0x33）
i2cdetect -y 1
```

> 若 `ai-edge-litert` 装不上，可依次尝试 `pip install tflite-runtime` 或
> `pip install tensorflow-aarch64`；都失败就退到纯 numpy 后端
> （`train.py --export-numpy-only`，推理无需任何 TF 系依赖）。

### 3.3 树莓派 4B 硬件 I2C 注意事项（重要）

BCM2711 硬件 I2C 对 MLX90640 的时钟拉伸容忍时间过短，读 RAM 帧可能返回全 0。
本项目所有读取脚本默认走 `smbus2.i2c_rdwr`，并**绕过 Adafruit `getFrame` 的
dataReady 死循环**（已带 2s 超时）。若仍读不到，改用软件 I2C：

在 `/boot/firmware/config.txt`（旧系统 `/boot/config.txt`）追加：

```
dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24
```

传感器 SDA→BCM23（物理 16）、SCL→BCM24（物理 18），重启后：

```bash
i2cdetect -y 3            # 应见 0x33
export MLX90640_I2C_BUS=3
```

---

## 四、操作步骤（完整流程）

### 0. 开发机自检（无需硬件，先跑通）

```powershell
cd thermal_ai
python smoke_test.py            # 需要 TensorFlow
python smoke_test.py --no-tf    # 秒级，只测数据链路
```

自检会验证：归一化维度、合成数据、numpy 后端、训练 + TFLite 导出 + 解释器推理、
以及「空场概率低 / 有人概率高」的实时行为断言。**改任何代码后都先重跑**。

### 1. 树莓派上先确认传感器能读

```bash
cd ~/thermal_ai && source .venv/bin/activate
python - <<'PY'
from thermal_camera import ThermalCamera
cam = ThermalCamera(rate_hz=2.0)
f = cam.read_frame()
print("serial:", cam.serial_number, "n=", len(f), "min/max=", min(f), max(f))
cam.close()
PY
```

打印出序列号且 `max` 明显高于室温（>28°C）说明读取正常。

### 2. 采集带标签的真机数据

```bash
# 无人（空房间；务必包含：热饮、电暖器、阳光热斑、显示器热源等「像人不是人」干扰）
python collect.py --label empty --seconds 30 --session empty_hotcup_01 --out data/real.npz

# 有人（站不同距离/朝向/姿态，各采几段）
python collect.py --label human --seconds 30 --session person_near_01 --out data/real.npz
python collect.py --label human --seconds 30 --session person_far_01  --out data/real.npz
```

要求：每类 **≥10 段、每段 20~60s**，分段独立（脚本按 `--session` 分组）。
没有硬件时可用 `--simulate` 试跑采集流程。

### 3. 训练（在开发机）

```bash
python train.py --data data/real.npz --out-dir models --tflite
```

可选：单独验证集、不同结构、纯 numpy 后端：

```bash
python train.py --data data/real.npz --val-data data/real_val.npz --arch mlp --tflite
python train.py --data data/real.npz --out-dir models --export-numpy-only   # 无 TF 保底
```

训练输出：`models/presence_model.keras`、`models/presence_model.tflite`、
`models/presence_model_meta.json`（含归一化窗、阈值、AUC）。

### 4. 独立场次验证（离线评估）

用**从未参与训练**的场次数据验证模型，输出 AUC、精确率/召回率/F1、混淆矩阵与
逐片段错误分布：

```bash
python evaluate.py --data data/heldout.npz --model-dir models
python evaluate.py --data data/heldout.npz --model-dir models --sweep   # 附阈值扫描
```

判读：召回率低=漏报（人太远/穿厚外套/姿态没覆盖）；精确率低=误报（负样本没覆盖
热饮/电暖器/阳光热斑等干扰）；逐片段有 `!!` = 某场景系统性失效，优先补采。

### 5. 部署到树莓派并实时推理

把 `models/` 和 `thermal_ai/*.py` 上传到树莓派后：

```bash
cd ~/thermal_ai && source .venv/bin/activate
python detect_live.py --publish /tmp/thermal_presence.json
```

每帧打印一行 JSON，并原子写入 `/tmp/thermal_presence.json`：

```json
{"present": true, "prob": 0.87, "prob_raw": 0.91, "region": {"center_x": 16.2, "center_y": 11.0, "pixels": 42, "peak_c": 36.1}, ...}
```

无硬件演示：

```bash
python detect_live.py --source sim --sim-mode mixed
```

### 6. 与主程序 `main_stereo.py` 集成

推荐**独立进程 + JSON 文件**（零侵入）：`detect_live.py` 写
`/tmp/thermal_presence.json`，主循环读该文件即可把人体图标画出来。
同进程则 `from detect_live import PresenceEngine`，每帧 `engine.update(frame)`。

---

## 五、调参与阈值

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--on-threshold` | 取模型 meta 的阈值 | 升到该概率才置「有人」 |
| `--off-threshold` | 开阈值的 0.7 倍 | 降到该概率才复位（滞回带） |
| `--smooth` | `0.4` | 概率 EMA 平滑系数 |
| `--min-on` | `2` | 连续多少帧确认「有人」 |
| `--min-off` | `3` | 连续多少帧确认「无人」 |

热像 1~2 Hz 帧率下，`min_on=2` 意味着「有人」要持续约 1~2 秒才确认，
能抑制单帧闪烁与读帧偶发异常。

---

## 六、关于评测数据的诚实说明

- `synth.py` 生成的数据**只能证明流水线通畅**，不能作为方案有效性依据。
  合成数据里模型容易拿满分，因为手写的噪声模型无法忠实复现真实热像的
  反射/背景结构。**唯一可信的结论来自 `collect.py` 采集的真机、分场次、
  带标签数据**。
- 验证集必须**按录制片段切分**（`train.py` 默认如此，`group_split` 处理）。
  按帧随机切分会让相邻帧同时出现在训练和验证里，指标虚高。
- 模型效果强依赖训练场景。换房间/摆位后至少补采少量数据、拼接重训。

---

## 七、如果 TF-Lite 无法部署（备选）

按代价从低到高：

1. **TFLite + `ai-edge-litert`**（推荐）。训练在开发机完成，树莓派只跑解释器。
2. **纯 numpy 逻辑回归**（`train.py --export-numpy-only`）。零 TF 依赖，把每个
   像素当独立特征，上限低于 CNN，但常能拿到可用基线。
3. **不换模型，只改规则**：`preprocess.human_region()` 已经是「30°C 以上热区
   质心/像素数/峰值」的简单规则，`detect_live.py` 在无模型时会自动退回它。
   它等价于「有个不太热也不太烫的斑块 = 有人」，能滤掉部分高温物体误报。
4. **换硬件**：若需可靠分辨「人 vs 任何热源」或远距离小目标，32x24 热像本身
   信息量不足，需更高分辨率热像或与毫米波雷达（人体微动）融合。
