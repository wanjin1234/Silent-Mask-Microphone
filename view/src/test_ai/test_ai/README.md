# test_ai —— 用 TensorFlow 实现「提示存在人体」的可行性与实现

本文档回答一个问题：**在现有硬件（3 × DFRobot C4002 毫米波雷达）不变的前提下，
能否用基于 TensorFlow 的模型实现「提示存在人体」？**

结论：**能，但有一个前提，且必须诚实界定边界。**

---

## 一、结论

**可以。** 现有硬件不缺信息，缺的是「把已有信息读出来」和「把多个判据组合起来」。
本项目只用了雷达每帧的一个「被选中的目标点」（距离 + 速度 + 能量），
而 C4002 的帧里还有一整套被解析后**直接丢弃**的字段，其中最关键的是一张
**距离门位掩码**。把这些字段用起来，再配一个小的时序模型，就能显著优于
现在「速度超阈值即报警」的规则。

**同时要说清楚两条硬约束**（做不到的部分，换任何模型也做不到）：

1. **C4002 是"成品存在雷达模块"，不是原始雷达。** 它内部有 MCU 完成 FMCW 处理，
   输出的是已经判定过的结果（目标状态、距离、速度、能量、门掩码）。
   拿不到**原始 IQ / 中频信号 / Range-Doppler 图 / 点云**。
   因此做不了微多普勒频谱、相位级生命体征提取、多人分辨、二维成像。
   硬件天花板在这里，与用不用 TensorFlow 无关。
2. **完全静止（连呼吸微动都测不到）的人，仍然无法可靠识别。**
   呼吸引起的胸腔起伏在 C4002 上表现为低速多普勒微动（实测量级 1~8 cm/s），
   与「吊扇 / 空调出风 / 桌面振动」造出的低速周期信号在**单一速度通道上不可分**。
   模型能利用门掩码、多路一致性、能量结构把其中一部分拆开
   （见 `synth.py` 里显式建模的这个场景），但不能保证 100%。

所以本方案的定位是：**在"移动 / 挥手 / 呼吸微动"范围内把人体存在检测做得比
现有阈值规则更准、更少误报**，而不是「超越硬件物理极限」。

---

## 二、为什么现在「只能得到三个点」

`src/c4002_parser.py` 的 `read_data()` 是为**阈值规则**写的：
它做能量选优、滞回、死区、众数滤波、EMA 平滑、突变剔除，
最后只返回 `distance / signal / presence` 一类结果。因此上层看到的就是
「每个方向一个点」。

而同一帧里还有这些字段，代码解析了却**从未返回**（见 `c4002_parser.py:423` 附近）：

| 字段 | 含义 | 为什么重要 |
| --- | --- | --- |
| `exist_gate_index` | 存在距离门**位掩码**（80cm 分辨率 16 位 / 20cm 分辨率 26 位） | **唯一的一维距离剖面**：把「每方向 1 个点」变成「每方向 16~26 格」 |
| `exist_count_down` | 存在倒计时 | 目标持续性的直接证据，可区分「瞬态杂波」与「持续存在的人」 |
| `move_direction` | 运动方向（靠近/远离） | 走动与随机振动/噪声的区分依据 |
| `light` | 环境光强 | 辅助上下文（明暗场景的杂波特性不同） |
| `target_status` | 固件状态 0~5 | 作为**特征**而非**判决**使用，让模型自己决定信多少 |

另外，父项目为保证规则稳定所做的**死区（<2cm/s 归零）与 EMA 平滑**，
恰恰抹掉了呼吸级微动。这些处理对规则有利，对模型是信息损失。
`test_ai/c4002_ext.py` 因此只做「字节 → 物理量」的忠实还原，不做任何滤波。

---

## 三、方案与代码结构

```
test_ai/
├── c4002_ext.py       扩展解析器：输出全部原始物理量（含距离门位掩码），不滤波
├── features.py        原始帧 → 模型张量；多传感器时间对齐；窗口统计量；特征消融
├── metrics.py         纯 numpy 指标（AUC/精确率/召回率/F1/阈值搜索），不依赖 sklearn
├── synth.py           合成场景数据集（含吊扇干扰、幻影目标），无硬件也能跑通全流程
├── collect.py         真机标注采集（--simulate 可在无雷达时试跑）
├── train.py           TensorFlow/Keras 训练：CNN/GRU/MLP + 类别加权 + TFLite 导出
├── baseline_numpy.py  纯 numpy 逻辑回归（TF 装不上时的备选，同一套特征与评估口径）
├── model_io.py        模型加载，自动识别 tf / tflite / numpy 后端
├── rule_baseline.py   复刻现行规则判定，作为可证伪的比较对象
├── evaluate.py        同一批数据上「模型 vs 现行规则」并排对比 + 消融表
├── detect_live.py     实时推理：滑窗 + 概率平滑 + 双阈值滞回，可 JSON 发布给主程序
├── probe_gates.py     上机探针：验证距离门字段的真实语义（**上机第一件事**）
├── smoke_test.py      一条命令跑完全部入口的自检（无需硬件，含行为断言）
└── requirements.txt   依赖说明（含树莓派 aarch64 的注意事项）
```

本目录**不附带预训练模型**：现有的模型只可能是在合成数据上训出来的，
拿它上机是有害的（会让人误以为可以直接用）。请按下面流程用真机数据自己训。
`smoke_test.py` 负责证明代码本身是通的。

### 模型输入

每个窗口 = **2 秒 @ 10Hz**（20 帧，因为呼吸在 0.1~0.6Hz，需要足够长的窗）
× **3 路雷达**，两路输入：

* `seq (20, 81)`：逐帧原始物理量。每路雷达 11 个标量（含 `fresh` 标记）
  + 16 位距离门掩码 → 27 维 × 3 路 = 81 维。
* `agg (63,)`：窗口级统计量。每路 21 维（速度 RMS / 均值 / 峰值 / 过零率 /
  速度非零占比 / 运动与存在能量的均值·标准差·峰值·饱和占比 / 距离标准差·极差 /
  门掩码均值·变化率·并集·最长稳定游程 / 倒计时均值 / 有效帧占比）。

`agg` 里那些量正是 `breath_detector.py` 里人手写死的判据（自相关峰值、
RMS 幅值、过零次数）的等价物。**把它们显式喂给模型**，在几百个样本的量级上
比让网络从原始序列里自己悟要稳得多，这也是小样本下能训得动的原因。

模型本身很小（两个 Conv1D + 全局池化 + 一个 Dense 支路拼接），
参数量约 3 万，TFLite 导出后 34KB，树莓派 CPU 上单窗推理亚毫秒级。
**规模是刻意压小的**：数据量小的时候，大模型只会更快过拟合。

---

## 四、怎么用（完整流程）

### 0. 安装依赖

```bash
pip install -r requirements.txt          # 开发机：tensorflow + numpy
# 树莓派（aarch64 常常装不上完整 TF）：
#   pip install tensorflow-aarch64       # 社区轮子
#   pip install ai-edge-litert           # 或只装 TFLite 解释器（推荐）
#   都装不上就退到纯 numpy 后端：python baseline_numpy.py ...
```

`test_ai` 的所有脚本都只用 numpy 也能跑（除 `train.py` 需要 TF）。
`baseline_numpy.py` 完全不依赖 TF，是保底方案。

### 1. 先跑自检（不需要硬件，约 1 分钟）

```bash
cd src/test_ai
python smoke_test.py          # 需要 TensorFlow
python smoke_test.py --no-tf  # 只测数据链路，秒级，只需要 numpy
```

自检覆盖：字节级字段偏移、窗口对齐与去重、合成数据维度、
训练+评估+TFLite 导出、纯 numpy 后端、实时推理行为、CSV 回放。
其中「实时推理行为」是关键：它断言空场概率必须低、有人场景概率必须高。

**为什么需要行为断言而不是「跑通就行」**：这条链路上有一类静默失败——
时间戳语义、多传感器对齐、窗口缓冲去重、CSV 回放基准时间。
出错时不抛异常，只让模型输出退化成常数或恒为 0，日志看着完全正常。
开发过程中实际踩到三处，都是自检的这类断言抓出来的：

1. 窗口缓冲未按时间戳去重 → 主循环把同一帧塞满缓冲，概率恒为 1.0；
2. `frame.get('timestamp') or time.time()` 把合法的 `0.0` 当成缺失 →
   后续帧全被判为乱序丢弃，窗口退化成空档；
3. 推理/采集在缓冲「每路只有 1 帧」时就建窗 → 窗口 95% 是空档，
   与训练分布完全不同（训练总是跳过前 `window_frames` 帧），
   实测空场被稳定判成「有人」而离线指标却是满分。现在统一用
   `WindowBuilder.covered()`（时间跨度必须覆盖完整窗口）来出窗。

改任何代码后请重跑自检，再上机采集数据——否则采回来的数据可能不可用。

### 2. 上机先验证距离门字段（**不要跳过**）

```bash
cd src
export RADAR_PORTS=/dev/serial/by-path/...,/dev/serial/by-path/...,/dev/serial/by-path/...
export RADAR_ANGLES=-45,0,45
python test_ai/probe_gates.py --seconds 30 --csv gates_probe.csv
```

按报告里的判读提示确认：是否多个相邻位置位、距离变化时位是否平移、
有没有 `bit>=16`（有则 `export C4002_GATE_BITS=26`）。
**如果字段语义与假设不符，模型的输入就是错的，后面全白做。**

### 3. 采集带标签的真机数据

```bash
# 空场（建议开着风扇/空调，制造"像人"的干扰）
python test_ai/collect.py --label empty --seconds 30 --session empty_fan_01 --out data/real.npz
# 有人（走动 / 挥手 / 静止呼吸，各采几段）
python test_ai/collect.py --label human --seconds 30 --session still_breath_01 --out data/real.npz
python test_ai/collect.py --label human --seconds 30 --session wave_01      --out data/real.npz
```

采集要求（**决定模型上限**）：

* 每个类别 ≥ 10 段、每段 20~60s，**分段独立**（脚本按段记录 group，训练按段切分）；
* 负样本要包含「像人」的干扰：开风扇、有人在隔壁走、雷达前有摆动的物体；
* 正样本要包含难例：静止只呼吸、缓慢挥手、坐着轻微活动；
* **换房间/换摆位要重新采**——C4002 的输出强烈依赖场景与安装位置。

### 4. 训练与评估

```bash
# 训练（默认按片段切分验证集，避免相邻窗口泄漏）
python test_ai/train.py --data data/real.npz --out-dir models --tflite

# 关键一步：在同一批数据上比较「模型」与「现行规则」
python test_ai/evaluate.py --data data/real.npz --model-dir models

# 想知道「新挖出来的字段到底有没有用」：各消融模式各训一次再对比
for m in none legacy gates agg; do
  python test_ai/train.py --data data/real.npz --out-dir models/abl_$m --ablate $m
done
python test_ai/evaluate.py --data data/real.npz --model-dir models --compare-ablation
```

`legacy` 消融 = 只保留旧解析器暴露的 5 个标量（屏蔽距离门掩码、倒计时、
运动方向、固件状态、光强）。**「模型 vs legacy」的差距，就是这个方案
相对于"不改解析器、只换模型"的净收益。**

### 5. 实时运行

```bash
# 真机运行，并把状态发布成 JSON（零侵入，不改 main_stereo.py）
python test_ai/detect_live.py --source real --model-dir models --publish /tmp/presence.json

# 无硬件演示 / 调显示逻辑
python test_ai/detect_live.py --source sim --sim-mode mixed --publish /tmp/presence.json

# 把现行规则接到同一条流水线上做 A/B 对比（同一段输入，同一套滞回）
python test_ai/detect_live.py --source real --backend rule --rule-mode motion

# 回放录制的 CSV
python test_ai/detect_live.py --source csv --csv gates_probe.csv
```

### 6. 与主程序集成

推荐**独立进程 + JSON 文件**（不改动比赛代码，风险最低）：`detect_live.py`
原子写入 `/tmp/presence.json`（`present / prob / target_angle / target_distance`），
`main_stereo.py` 在主循环里读该文件即可把人体图标画出来。
若想同进程集成，直接 `from test_ai.detect_live import PresenceEngine`，
把 `engine.update(frames)` 的返回值接到显示层。

---

## 五、关于评测数据的诚实说明

* `synth.py` 生成的数据**只能证明流水线通畅**（维度、对齐、去重、归一化、
  训练、阈值选择、TFLite 导出、实时推理都跑通），**不能作为方案有效性的依据**。
  在合成数据上模型容易拿到满分（AUC 1.0），因为手写的噪声模型无法忠实复现
  真实雷达的杂波结构。
* **一个具体的证据**：在合成数据上跑特征消融，`none / legacy / gates / agg`
  四种配置的 AUC 全都是 1.0000——也就是说「只用旧字段」和「用上全部新字段」
  在合成数据上**分不出差别**。这恰恰说明合成数据太容易，
  无法用来验证「新字段到底有没有用」这个核心命题。
  消融实验的价值只有在真机数据上才体现得出来，工具已备好
  （`evaluate.py --compare-ablation`）。
* **唯一可信的比较**是在 `collect.py` 采集的**真机、分场次、带标签**数据上，
  用 `evaluate.py` 得到的「模型 vs 规则（含调优阈值）」对照表。
* 开发中实测到一个值得警惕的现象：`train.py` 报告验证集 AUC 满分，
  **并不代表实时表现好**。曾出现「离线指标 1.0，实时空场却被判成有人」，
  根因是推理侧窗口覆盖不足（第四节第 1 步里列的第 3 条）。
  因此判断效果要看 `detect_live.py` 的实时输出，不能只看训练日志。
* `evaluate.py` 默认也会打印「规则在本数据上重选阈值后」的成绩——
  规则同样可以调参，只跟未调参的规则比是不公平的。
* 验证集必须**按录制片段切分**（`train.py` 默认如此）。
  按窗口随机切分会让相邻窗口同时出现在训练和验证里，指标虚高一大截。

---

## 六、如果 TensorFlow 无法部署（备选方案）

按代价从低到高排列：

1. **TFLite + `ai-edge-litert`**（推荐）。
   训练在开发机用 TF 完成，导出 `presence_model.tflite`（本仓库导出为 34KB），
   树莓派上只需轻量解释器，不装完整 TF。`model_io.py` 会自动识别该后端。
2. **纯 numpy 逻辑回归**（`baseline_numpy.py`）。
   零额外依赖，同一套特征、同一套评估口径、同一个 `detect_live.py`。
   代价是看不到时序形态，上限低于 CNN。若真机上两者差距不大，就用这个。
3. **不换模型，只改规则**。
   即使一个模型都不训，只把 `exist_gate_index` / `exist_count_down` /
   `move_direction` 接进现有规则，也能立刻改善误报：
   例如「距离门掩码连续 N 帧落在同一门 + 存在倒计时持续增长 + 多路一致」
   才判有人，比「单帧速度 > 5cm/s」稳健得多。
   **这是收益/工作量比最高的一步，建议无论如何都做。**
4. **换硬件**（若确实需要探测完全静止的人）：
   需要能输出原始相位/IQ 或点云的雷达，例如 TI IWR6843 / AWR1843、
   Acconeer A121 一类，才能做相位级呼吸与心跳提取。
   C4002 的固件输出接口决定了它做不到这件事。

---

## 七、已知局限

* 模型的效果**强依赖训练场景**。C4002 的输出与安装位置、朝向、房间反射
  密切相关，换场地后建议至少补采少量数据微调（`--data` 拼接新旧数据重训）。
* 「呼吸级静止人体」的召回率会明显低于「移动人体」，这是物理限制而非模型问题。
  建议在 UI 上对两者区分置信度（模型输出的概率本身就是很好的指示）。
* 数据量小的时候（每类 < 100 个窗口），优先用 `--arch mlp`，
  并先看 `evaluate.py` 给出的规则基线成绩——如果规则已经够用，不必强行上模型。
* `exist_gate_index` 的位宽与语义依赖固件版本，**必须先用 `probe_gates.py` 确认**。
