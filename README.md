# 开源项目参考与使用说明

**作品名称**：多感智能降噪消防面具

---

## 一、总体声明

**本项目参考并使用了开源项目。** 经完整核查本作品全部源码、部署脚本与依赖清单，共使用开源项目 **19 项**（涉及 21 个上游软件包，其中 3 项各含两个可互换的包，见第 4、9、10 项），逐项列明如下。

关于使用方式：

1. **本项目未修改任何开源项目的源代码。** 所有第三方库均以依赖方式调用（`import` 或编译安装），不存在内联、复制或改造的第三方代码。
2. **本项目未将任何第三方依赖捆绑进自己的发布产物。** 作品自研的树莓派上位机 `radarpi` 所生成的 `.deb` 安装包声明 `Depends: python3 (>= 3.7)`，第三方库以 `Recommends:` 形式交由系统包管理器安装（见 `src/view/radarpi/packaging/build_deb.py:161-162`）。
3. 开源项目的使用分为两类：**使用其代码库/运行时**，与**使用其预训练模型权重**（DeepFilterNet 的降噪权重、Ultralytics YOLO11n 的检测权重）。二者均为上游公开发布的成果。

除下表所列 19 项之外，本项目未参考或使用其他开源项目的代码。另有少量非开源的第三方引用（传感器厂商随附的演示程序、雷达厂商的私有上位机），不属于开源项目，为完整起见在第四节单独说明。

---

## 二、开源项目使用总览

| 序号 | 开源项目 | 项目链接 | 许可证 | 使用方式 | 参考/使用的具体内容 |
| --- | --- | --- | --- | --- | --- |
| 1 | DeepFilterNet | https://github.com/Rikorose/DeepFilterNet | MIT OR Apache-2.0 | 使用其库 + 预训练权重 | 语音降噪模型 `df.init_df()` / `df.enhance()`，DeepFilterNet2/3 预训练权重 |
| 2 | PyTorch | https://github.com/pytorch/pytorch | BSD-3-Clause | 使用其库 | 作为 DeepFilterNet 的推理运行时，并限制线程数 |
| 3 | TensorFlow / Keras | https://github.com/tensorflow/tensorflow | Apache-2.0 | 使用其库 | `tf.keras` 搭建并训练 CNN、雷达时序模型；`TFLiteConverter` 导出模型 |
| 4 | ai-edge-litert / tflite-runtime | https://github.com/google-ai-edge/LiteRT<br>https://pypi.org/project/tflite-runtime/ | Apache-2.0 | 使用其库 | 树莓派端 TFLite 轻量推理运行时（`Interpreter`） |
| 5 | NumPy | https://github.com/numpy/numpy | BSD-3-Clause | 使用其库 | 全作品数值计算基座：温度矩阵、信号重采样、点云矩阵、合成数据 |
| 6 | pygame | https://github.com/pygame/pygame | LGPL-2.1 | 使用其库 | AR HUD 全部界面渲染、热像显示窗口 |
| 7 | matplotlib | https://github.com/matplotlib/matplotlib | matplotlib License（BSD 兼容） | 使用其库 | `plt.imshow(interpolation="bicubic")` 双三次插值渲染温度场图像 |
| 8 | pyserial | https://github.com/pyserial/pyserial | BSD-3-Clause | 使用其库 | 三路 C4002 毫米波雷达串口通信、radarpi 串口链路 |
| 9 | smbus2<br>python-smbus（`smbus`） | https://github.com/kplindegaard/smbus2<br>https://pypi.org/project/smbus/ | MIT<br>**GPL-2.0** | 使用其库 | MLX90640 热像传感器 I²C 读帧、UPS 电量计读取（两者为可互换后端） |
| 10 | Adafruit Blinka<br>adafruit-circuitpython-mlx90640 | https://github.com/adafruit/Adafruit_Blinka<br>https://github.com/adafruit/Adafruit_CircuitPython_MLX90640 | MIT | 使用其库 | 热像传感器对象构造、刷新率设置与温度标定计算 |
| 11 | pigpio | https://github.com/joan2937/pigpio | Unlicense（公有领域） | 使用其库 + 源码编译安装 | 三路超声波测距 GPIO、物理按键输入 |
| 12 | pyftdi | https://github.com/eblot/pyftdi | BSD-3-Clause | 使用其库 | FT232H 的 MPSSE 异步 bitbang，一块转接板驱动三只超声波探头 |
| 13 | RPi.GPIO | https://sourceforge.net/projects/raspberry-gpio-python/ | MIT | 使用其库 | 独立的 GPIO 引脚自检脚本 |
| 14 | OpenCV | https://github.com/opencv/opencv | Apache-2.0 | 使用其库 | 温度帧→伪彩色映射（`applyColorMap`）、色彩空间转换与缩放 |
| 15 | Ultralytics YOLO11n | https://github.com/ultralytics/ultralytics | **AGPL-3.0** | **使用其预训练模型权重** | COCO 预训练的 YOLO11n（TFLite int8）person 类人体检测 |
| 16 | reportlab | https://www.reportlab.com/opensource/ | BSD-3-Clause | 使用其库 | 生成树莓派上位机使用说明 PDF |
| 17 | CPython | https://github.com/python/cpython | PSF License | 源码编译安装 | 在树莓派上从源码编译 Python 3.11.4 解释器 |
| 18 | WCH CH343 内核驱动 | https://github.com/WCHSoftGroup/ch343ser_linux | GPL-2.0 | 编译安装（可选） | CH343/CH344 串口小板的内核驱动 |

> 表中许可证以各上游项目仓库的 LICENSE 文件为准。

---

## 三、逐项说明（项目名称、链接、具体参考或使用的内容）

### （一）音频降噪与语音通信
#### 0. Seeed-Studio 声卡驱动
- **项目链接**：https://github.com/respeaker/seeed-voicecard
- **许可证**：GNU General Public License v3.0
- **使用方式**：使用其声卡产品驱动

#### 1. DeepFilterNet

- **项目链接**：https://github.com/Rikorose/DeepFilterNet
- **许可证**：MIT OR Apache-2.0（双许可）
- **使用方式**：使用其开源实现与上游发布的预训练模型权重，属本作品"降噪通信"功能的核心

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| PyPI 包 `deepfilternet`（导入名 `df`，0.5.x 版本） | `src/microphone/auto_btmic.py:5374` |
| 调用其 `init_df()` 加载预训练降噪模型 | `auto_btmic.py:5460-5462` |
| 调用其 `df.enhance(model, state, tensor)` 逐块推理降噪 | `auto_btmic.py:5601` |
| 使用其**预训练模型权重** DeepFilterNet2 / DeepFilterNet3（默认 DeepFilterNet2） | `auto_btmic.py:106-108`、`5455-5464` |
| 权重缓存目录 `~/.cache/DeepFilterNet/<模型名>/`，首次使用时联网下载 | `auto_btmic.py:5466-5475` |

**需特别说明的自主实现部分**（以下内容不属于 DeepFilterNet，为本作品自行实现，使用开源模型之外另有大量自研工作）：

- 上游 0.5.x 模型仅支持 48 kHz 输入，而蓝牙耳机 HFP/mSBC 上行链路为 16 kHz，因此自研了纯 NumPy 的 **16 kHz↔48 kHz 多相 FIR 重采样**：`_resample_h`（`auto_btmic.py:5404`）、`resample_up`（`:5416`）、`resample_down`（`:5425`）
- 自研**块边界爆音防护**：推理状态预热前缀（`PREFIX_MS`）、块尾边缘丢弃（`EDGE_MS`）、相邻窗口交叉淡化（`XFADE_MS`）。问题说明见 `auto_btmic.py:121`，参数常量见 `:5384-5386`，实现见 `:5585-5729`
- 自研**后置谱减法门**（可选增益下限 `SPEC_POST_FLOOR`）：`auto_btmic.py:5534-5569`
- 自研**纯 NumPy 谱减法兜底方案**（模型不可用时零模型加载运行）：`auto_btmic.py:5934` 起，模式选择见 `:141-142`、`:4391`
- 自研**积压截断机制**（积压超限时丢弃最旧音频，保证延迟封顶而非无限增长）：设计说明见 `auto_btmic.py:5351-5361`，实现见 `:5778`、`:6043`、`:6732`

#### 2. PyTorch

- **项目链接**：https://github.com/pytorch/pytorch
- **许可证**：BSD-3-Clause
- **使用方式**：作为上述 DeepFilterNet 模型的推理运行时，本项目未直接构建神经网络

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| 限制推理线程数（树莓派 4B 四核，实测 2 线程最优） | `src/microphone/auto_btmic.py:5436-5439` |
| NumPy 数组与 Torch 张量互转，送入模型推理 | `auto_btmic.py:5599-5600` |
| 缺失时的安装引导提示 | `auto_btmic.py:7045` |

---

### （二）传感器驱动与硬件接口

#### 3. pyserial

- **项目链接**：https://github.com/pyserial/pyserial
- **许可证**：BSD-3-Clause
- **使用方式**：使用其库，实现三路 DFRobot C4002 毫米波雷达的串口数据采集

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| 三路雷达串口打开、读取与解析（人体存在判定主链路） | `src/view/src/c4002_parser.py:5` |
| 传感器集线器与测试脚本 | `src/view/src/real_sensors.py:1`、`radar_4d.py:400`、`test_radar.py:3` |
| radarpi 上位机的**双后端串口访问层**：有 pyserial 时使用 pyserial，无 pyserial 时自动回退到 Python 标准库 `termios` | `src/view/radarpi/src/radarpi/serialport.py:355`、`:431` |

#### 4. smbus2 / python-smbus（`smbus`）

- **项目链接**：https://github.com/kplindegaard/smbus2 （`smbus2`，MIT）<br>https://pypi.org/project/smbus/ （`smbus`，属 i2c-tools 项目，GPL-2.0）
- **许可证**：`smbus2` 为 MIT；`smbus` 为 **GPL-2.0**
- **使用方式**：使用其库，实现 I²C 总线设备（热像传感器、电池电量计）的读写。二者在代码中作为**可互换的双后端**使用

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| MLX90640 热像传感器 I²C 帧读取（主读取路径，使用 `i2c_rdwr` 组合读写） | `src/view/thermal_ai/thermal_camera.py:49` |
| 同上（热像显示与高温检测程序） | `src/view/mlx90640-thermal/mlx90640_thermal_display.py:169`<br>`src/view/mlx90640-thermal/mlx90640_temperature_detection.py:232` |
| 热像传感器诊断与读帧测试脚本 | `src/view/mlx90640-thermal/read_frame_test.py:25,36,60`<br>`src/view/mlx90640-thermal/diagnose_mlx90640.py:23,34,42` |
| UPS HAT 电池电量计读取：`_load_smbus()` 中优先尝试 `import smbus`（系统包 `python3-smbus`），失败后回退 `import smbus2 as smbus` | `src/view/src/ups_battery.py:46-57`（`smbus` 在 `:49`，`smbus2` 在 `:54`） |

**工程说明**：树莓派 4B 的 BCM2711 硬件 I²C 对 MLX90640 时钟拉伸的容忍时间不足，直接调用 Adafruit 库的 `getFrame()` 会出现 `dataReady` 死循环。本作品因此改为以 `smbus2.i2c_rdwr` 直读传感器 RAM 并自行做超时保护（2 秒），Adafruit 库作为回退路径保留。该问题与解决方式记录于 `src/view/thermal_ai/README.md`。

**合规说明**：`smbus`（python-smbus）为 GPL-2.0，在本作品中仅作为**可选回退导入**使用 —— 树莓派系统若已装 `python3-smbus` 则优先使用，未装则由 MIT 许可的 `smbus2` 承担全部功能。本作品未修改其源码，也未将其捆绑进任何发布产物。

#### 5. Adafruit Blinka + adafruit-circuitpython-mlx90640

- **项目链接**：https://github.com/adafruit/Adafruit_Blinka（提供 `board` / `busio`）<br>https://github.com/adafruit/Adafruit_CircuitPython_MLX90640
- **许可证**：MIT（两个包均为）
- **使用方式**：使用其库，作为热像传感器 I²C 访问与温度标定计算的实现路径

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `adafruit_mlx90640.MLX90640(i2c, address=0x33)` 构造传感器对象 | `src/view/thermal_ai/thermal_camera.py:139`<br>`src/view/mlx90640-thermal/mlx90640_thermal_display.py:246`<br>`src/view/mlx90640-thermal/mlx90640_temperature_detection.py:299` |
| `adafruit_mlx90640.RefreshRate.REFRESH_x_HZ` 设置传感器刷新率 | `thermal_camera.py:140`、`mlx90640_temperature_detection.py:209-215` |
| 复用其 `_GetTa` / `_CalculateTo` 温度标定计算方法 | `thermal_camera.py:44` |
| `board.I2C()` / `busio.I2C()` 建立 I²C 总线 | `thermal_camera.py:158-159`、`mlx90640_temperature_detection.py:292-293` |

#### 6. pigpio

- **项目链接**：https://github.com/joan2937/pigpio
- **许可证**：Unlicense（公有领域）
- **使用方式**：使用其库提供 GPIO 中断与精准计时能力；因新版 Raspberry Pi OS 已无 apt 包，从源码编译安装并建立 systemd 服务

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| 三路超声波测距的 GPIO 触发与回波计时 | `src/view/src/ultrasonic_rpigpio.py:29` |
| 物理按键输入检测 | `src/view/src/gpio_button.py:26` |
| 超声波 GPIO 诊断脚本 | `src/view/src/diagnose_ultrasonic_rpi.py:27` |
| 源码编译安装并注册 `pigpiod` 开机自启服务：源码获取优先 Gitee 镜像 `gitee.com/mirrors/pigpio`、失败回退 GitHub 上游 | `src/view/deploy/setup_pi.sh:96-97`；编译、安装与 systemd 服务配置 `:92-121` |

#### 7. pyftdi

- **项目链接**：https://github.com/eblot/pyftdi
- **许可证**：BSD-3-Clause
- **使用方式**：使用其库驱动 FT232H 的 MPSSE 异步 bitbang 模式

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `from pyftdi.gpio import GpioController`；利用异步 bitbang 一次性读回全部 8 个引脚，以实现一块 USB 转接板同时驱动三只超声波探头 | `src/view/src/ultrasonic_mpsse.py:5-17`、`:211` |
| 传感器集线器中的超声波后端 | `src/view/src/real_sensors.py:5` |
| 超声波与硬件集线器诊断脚本 | `src/view/src/diagnose_ultrasonic.py:2`、`test_ultrasonic.py:2`、`diagnose_hub.py:17-20` |

#### 8. RPi.GPIO

- **项目链接**：https://sourceforge.net/projects/raspberry-gpio-python/
- **许可证**：MIT
- **使用方式**：使用其库，作为独立于 pigpio 的 GPIO 引脚自检通道（用于交叉验证接线，不参与主运行链路）

**具体使用的内容**：`src/view/src/gpio_verify.py:19`（脚本说明见 `:8`）

#### 9. WCH CH343 内核驱动（WCHSoftGroup/ch343ser_linux）

- **项目链接**：https://github.com/WCHSoftGroup/ch343ser_linux
- **许可证**：GPL-2.0
- **使用方式**：可选辅助脚本，在特定串口小板无法被系统识别时从上游仓库获取并编译内核驱动；本项目**仅编译安装，未修改其源码**

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `git clone` 上游仓库并以 DKMS 方式编译安装驱动 | `src/view/radarpi/packaging/install-ch343-driver.sh:18` |
| 何时需要该驱动的说明（多数模块内核自带 `cdc_acm` 已可识别，无需安装） | 同文件 `:6-13` |

---

### （三）显示与人机交互

#### 10. pygame

- **项目链接**：https://github.com/pygame/pygame
- **许可证**：LGPL-2.1
- **使用方式**：使用其库，作为本作品 AR HUD 界面的**唯一 UI 引擎**（本项目以动态链接方式调用，未修改其源码）

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `pygame.init()`、`display.set_mode((w, h), SCALED \| FULLSCREEN)` 建立全屏 HUD 画面 | `src/view/src/stereo_ar_display.py:50`、`:76-80` |
| `pygame.gfxdraw` 绘制抗锯齿多边形（障碍物填充块） | `stereo_ar_display.py:2`、`:401-402` |
| 绘制十字准星、视图分隔线、传感器框：`draw.line` / `draw.polygon` / `draw.lines` | `stereo_ar_display.py:301-447` |
| `pygame.font.Font` 文字渲染 | `stereo_ar_display.py:159` |
| HUD 图层与半透明叠加层 `pygame.Surface(..., pygame.SRCALPHA)` | `stereo_ar_display.py:144-150` |
| `pygame.transform.smoothscale` 将 32×24 热像平滑放大显示 | `stereo_ar_display.py:804-838` |
| 主程序显示循环 | `src/view/src/main_stereo.py:2`、`main_4d.py`、`main_integrated.py`、`radar_view.py` |
| radarpi pygame 桌面版上位机 | `src/view/radarpi/src/radarpi/viewer_pygame.py:685` |
| 热像连续显示窗口 | `src/view/mlx90640-thermal/show_graph.py:56-158` |

**部署说明**：`src/view/deploy/setup_pi.sh:83` 中特意使用 `pip install --no-binary pygame pygame` 从源码编译 pygame。原因是 PyPI 预编译 wheel 内置的 SDL2 不支持 `kmsdrm` 后端，只有从源码编译才会链接树莓派系统自带的 SDL2，HDMI 全屏显示才能正常工作。

#### 11. matplotlib

- **项目链接**：https://github.com/matplotlib/matplotlib
- **许可证**：matplotlib License（PSF 风格，BSD 兼容）
- **使用方式**：使用其库，用于将温度矩阵以双三次插值渲染为图像

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| **双三次插值渲染**：`plt.imshow(arr, cmap="jet", vmin, vmax, interpolation="bicubic")` | `src/view/mlx90640-thermal/continuos_graphic.py:92` |
| `plt.imsave(...)` 输出温度场 PNG（采用临时文件 + 原子替换写盘） | `continuos_graphic.py:106`、原子写实现 `:100-107` |
| import | `continuos_graphic.py:21` |

**说明**：该链路为"温度矩阵 → 插值渲染 PNG → 由显示程序读取并展示"，是本作品红外热成像显示方案中的一条渲染链路；另一条链路直接由 pygame 逐像元绘制（对应 `mlx90640_thermal_display.py`，不做插值）。

#### 12. reportlab

- **项目链接**：https://www.reportlab.com/opensource/
- **许可证**：BSD-3-Clause
- **使用方式**：使用其库生成树莓派上位机的使用说明 PDF 文档

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `reportlab.lib` 的 colors / enums / pagesizes / styles / units、`reportlab.pdfbase`、`reportlab.pdfbase.cidfonts.UnicodeCIDFont`（系统中文字体缺失时回退使用内置 CID 字体） | `src/view/radarpi/docs/build_manual_pdf.py:21-27` |

---

### （四）数值计算与人工智能

#### 13. NumPy

- **项目链接**：https://github.com/numpy/numpy
- **许可证**：BSD-3-Clause
- **使用方式**：使用其库，作为全作品的数值计算基座

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| 温度帧归一化、热区统计、按片段切分 | `src/view/thermal_ai/preprocess.py` |
| 纯 NumPy 逻辑回归后端（TensorFlow 不可用时的降级推理方案） | `src/view/thermal_ai/thermal_numpy.py` |
| 热像合成数据集生成 | `src/view/thermal_ai/synth.py`、`src/view/src/test_ai/test_ai/synth.py` |
| 雷达点云 → BEV / 体素 / 处理器链矩阵接口 | `src/view/radarpi/src/radarpi/ndarray_iface.py` |
| 音频多相 FIR 重采样（`_resample_h`:5404、`resample_up`:5416、`resample_down`:5425）与谱减法门（`:5534-5569`），均为纯 NumPy 实现 | `src/microphone/auto_btmic.py` |
| 距离中位数/IQR 离群剔除、多传感器数据融合、目标跟踪与滤波 | `src/view/src/data_fusion.py`、`radar_tracker.py`、`radar_filter.py`、`breath_detector.py` |

（作品 `src/` 目录下共 6 个子目录、40 余个文件引用 NumPy）

#### 14. TensorFlow / Keras

- **项目链接**：https://github.com/tensorflow/tensorflow
- **许可证**：Apache-2.0
- **使用方式**：使用其库，训练本作品的人体存在检测模型并导出为树莓派可部署格式

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `tf.keras` 搭建卷积神经网络（Conv / Pool / Dense 层）、`optimizers.Adam` 优化器、`metrics.AUC` 指标 | `src/view/thermal_ai/train.py:42-73` |
| 训练回调 `EarlyStopping` / `ReduceLROnPlateau` / `ModelCheckpoint` | `src/view/thermal_ai/train.py:196-202` |
| `tf.lite.TFLiteConverter.from_keras_model` + `tf.lite.Optimize.DEFAULT` 量化导出 TFLite | `src/view/thermal_ai/train.py:227-228` |
| 雷达时序模型的训练链路 | `src/view/src/test_ai/test_ai/train.py` |
| 推理侧解释器加载（回退顺序：tensorflow → ai_edge_litert → tflite_runtime） | `src/view/thermal_ai/model_io.py:83-101` |
| 全链路自检脚本 | `src/view/thermal_ai/smoke_test.py` |

**版本与部署约束**：因 PyPI 未提供 aarch64 架构的 TensorFlow 官方 wheel，本作品采用"开发机训练、树莓派推理"的分工，依赖声明为 `tensorflow>=2.16; platform_machine != "aarch64"`，树莓派端仅安装轻量推理运行时。

#### 15. ai-edge-litert / tflite-runtime

- **项目链接**：https://github.com/google-ai-edge/LiteRT （ai-edge-litert）<br>https://pypi.org/project/tflite-runtime/ （tflite-runtime，旧包名）
- **许可证**：Apache-2.0
- **使用方式**：使用其库，作为树莓派端的 TFLite 推理运行时

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `from ai_edge_litert.interpreter import Interpreter` / `from tflite_runtime.interpreter import Interpreter` | `src/view/thermal_ai/model_io.py:93-98`、`src/view/src/test_ai/test_ai/model_io.py` |
| 部署脚本按需安装（`INSTALL_AI=1` 时） | `src/view/deploy/setup_pi.sh:85` |

#### 16. OpenCV

- **项目链接**：https://github.com/opencv/opencv
- **许可证**：Apache-2.0（4.5.0 版本起）
- **使用方式**：使用其库，将温度帧转换为伪彩色图像以适配预训练检测模型的输入

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| `cv2.applyColorMap(gray, cv2.COLORMAP_JET / TURBO / INFERNO)` 温度场伪彩色映射 | `src/view/thermal_ai/pretrained_detect.py:70-78` |
| `cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)` 色彩空间转换 | 同上 |
| `cv2.resize(rgb, (256, 256), interpolation=cv2.INTER_LINEAR)` 缩放至模型输入尺寸 | `pretrained_detect.py:78` |
| import | `pretrained_detect.py:62` |

（色带选择与显示端保持一致的说明见 `pretrained_detect.py:36-41`）

#### 17. Ultralytics YOLO11n（预训练模型权重）

- **项目链接**：https://github.com/ultralytics/ultralytics
- **许可证**：**AGPL-3.0**
- **使用方式**：**使用其公开发布的预训练模型权重**（COCO 数据集训练的 YOLO11n，int8 量化的 TFLite 格式），用于热像伪彩图上的人体检测，作为本作品自训练模型链路之外的独立技术验证旁路

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| 预训练权重文件 `pretrained_models/yolo11n_full_integer_quant.tflite`（从 Ultralytics 官方渠道导出，未随源码提交） | `src/view/thermal_ai/pretrained_detect.py:3`、`:15` |
| `PERSON_CLASS = 0`：使用 COCO 数据集的 80 类中 person 类的检测结果 | `pretrained_detect.py:30` |
| `DEFAULT_INPUT = 256`：与导出时的 `imgsz` 对齐 | `pretrained_detect.py:31` |

**需说明的事项**

1. 本作品**未训练、未微调、未修改**该模型，仅调用其推理输出；模型的概率平滑与双阈值滞回由本作品自行实现的 `PresenceEngine` 完成（`detect_live.py`）。
2. 该功能为**独立旁路**，不修改也不影响本作品的主检测链路（两条链路并行运行、结果可再融合），说明见 `pretrained_detect.py:4-6`。
3. Ultralytics YOLO 系列采用 **AGPL-3.0** 许可证，这是本作品所用开源项目中唯一具有强传染性的许可证。本作品仅将其用于技术可行性验证，未将其作为作品对外发布版本的组成部分。

#### 18. CPython（Python 解释器）

- **项目链接**：https://github.com/python/cpython
- **许可证**：PSF License
- **使用方式**：在树莓派上从源码编译安装 Python 3.11.4 解释器

**具体使用的内容**

| 使用内容 | 代码位置 |
| --- | --- |
| 从 python.org 下载源码，以 `--enable-shared --with-system-ffi`（可选 `--enable-optimizations`）配置编译，`make altinstall` 安装至 `/usr/local` 而不覆盖系统 Python，并设置 rpath 指向 `/usr/local/lib` | `src/view/deploy/setup_pi.sh:45-65` |

---

## 四、非开源项目的参考与引用说明

以下内容**不属于开源项目**，为完整披露计单独说明：

| 项 | 性质 | 说明 |
| --- | --- | --- |
| MLX90640 温度—颜色映射断点 | 随传感器提供的厂商演示程序 | `src/view/mlx90640-thermal/mlx90640_thermal_display.py:57` 注明"色阶断点复制自随附的 STM32 演示程序"（蓝→青→绿→黄→红→品红）。该演示程序由传感器厂商随硬件提供，非开源项目，本作品仅参考其色阶取值 |
| Calterah 4D 成像雷达原厂上位机（Radar Data Application Management Tool） | 雷达厂商私有 Windows 软件 | 本作品 `src/view/radarpi/` 是为在树莓派上替代该原厂上位机而**自行编写**的上位机程序，其代码全部为本作品实现（字符串协议解析使用标准库 `struct`，Web 服务使用标准库 `http.server`，录制使用 csv/json 标准库），**未使用或复制原厂软件的任何代码**。设备的通信协议字段定义依据雷达厂商公开的技术手册与设备行为整理 |
| SolidWorks 结构件模型（`src/3d_model/component_1.SLDPRT`、`component_2.SLDPRT`） | 商业软件文件格式 | 面具结构件为本作品自主设计，使用商业软件 SolidWorks 建模，非开源内容 |
| JS 前端**未使用**任何第三方库 | —— | `src/view/radarpi/src/radarpi/static/` 下的 Web 界面（3D 点云渲染、投影视图、数据表格）为本作品手写实现，未引入 three.js 等任何第三方前端库 |

---

## 五、自主实现内容说明（与开源使用无关的本作品工作）

为避免混淆开源使用与本作品自研边界，此处列出主要自主实现内容：

- **音频链路**：16 kHz↔48 kHz 多相 FIR 重采样、推理块边界爆音防护（状态预热 / 边缘丢弃 / 交叉淡化）、后置谱减法门、纯 NumPy 谱减法降级方案、积压截断低延迟机制（`src/microphone/auto_btmic.py`）
- **蓝牙免驱自动配置**：BlueALSA 链路自动配置、蓝牙音频设备自动发现与重连、A2DP/HFP 模式管理（`src/microphone/auto_btmic.py`）
- **热成像**：温度帧读取（含绕过 Adafruit `dataReady` 死循环的超时保护）、色阶自适应（5%~95% 百分位平滑更新）、高温区域检测与告警（`src/view/mlx90640-thermal/`、`src/view/thermal_ai/`）
- **雷达算法**：能量选优、滞回与死区、众数滤波、EMA 平滑、突变剔除（`src/view/src/c4002_parser.py`）；距离门位掩码解析与时序特征构造（`src/view/src/test_ai/test_ai/`）
- **多传感器融合与 HUD**：三路雷达 + 三路超声波的障碍物融合、AR HUD 全部绘制逻辑（`src/view/src/`）
- **树莓派上位机 radarpi**：4D 成像雷达上位机的完整树莓派实现（协议解析、Web SSE 服务、pygame 界面、ndarray 算法接口、CSV/JSONL/npz 录制回放、.deb 打包、systemd/udev 集成）（`src/view/radarpi/`）
- **部署自动化**：树莓派环境一键配置脚本、跨平台同步脚本（`src/view/deploy/`）
