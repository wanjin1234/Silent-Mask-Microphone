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
8. 蓝牙耳机/扬声器：电脑把音频传到树莓派，由 reSpeaker HAT 的 3.5mm 耳机口
   放出来——BlueALSA 额外注册 A2DP Sink（44.1kHz 立体声），并兼容 HFP/HSP
   下行（16kHz 单声道），用 `arecord <下行 PCM> | aplay -D <耳机口>` 管道把
   下行音频送到声卡播放设备（不用 bluealsa-aplay：它的选项跨版本差异极大，
   实测的版本没有指定输出设备的选项，传 -d 直接 `invalid option` 退出）；
   `python3 final_btmic.py --play-test` 可放 1kHz 测试音验证耳机口是否出声；
   `python3 final_btmic.py --find-output` 逐张声卡放测试音，**确认耳机插在哪张卡的
   孔上**（本机有两个 3.5mm 输出：HAT 的孔和树莓派板载的孔）并写进配置文件；
   `python3 final_btmic.py --hp-test` 分步自检（左右声道/各采样率/边录边放）；
   `python3 final_btmic.py --audio-info` 诊断"电脑里为什么没有音频输出项"；
   `python3 final_btmic.py --forget` 删掉树莓派侧配对，逼电脑重新配对并
   重新枚举服务（修 Windows 缓存旧服务列表导致"只能当麦克风"的问题）；
   改配置不用编辑 systemd 单元：往 /etc/default/bt-mic 写 KEY=VALUE 即可；
   详见下方"蓝牙耳机（A2DP Sink / HFP 下行 → 3.5mm 耳机口）"一节

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
- 修复交叉淡化接缝拼接取错窗口的 bug：本块主体曾误取上一窗口
  （wins[-2] 的 [xf:unit] 段），实际输出里每个 320ms 块的头部 15ms
  与上一块主体顺序错乱、块边界硬切（听感即周期性爆破音）；改为取
  当前窗口 wins[-1]，仿真验证输出与输入仅差 PREFIX 延迟、接缝零跳变
- Windows 调音量（+VGM 变化）经 gain 阶段按样本小步渐变（15→8 约
  20ms），消除音量台阶突变引起的"咔哒"爆音；gain 阶段非阻塞排空
  （select+os.read，读多少发多少），去掉旧实现 read(4096) 的 128ms
  块延迟；320ms 突发原样透传，由 aplay 的 128ms FIFO + 60ms 硬件
  缓冲（合计 188ms）平滑，块间约 42ms 空档不会造成欠载。积压上限
  800ms——必须大于单块突发 320ms，否则每个突发都会被误删一段
  （曾设 240ms，导致 aplay 每轮 underrun 约 300ms：周期性爆音 +
  SCO 频繁掉线）；只有 SCO 真卡死才丢最旧数据、保留最近 100ms。
  SCO 长停顿（Windows/bluealsa 停止消费，实测 1~17s）恢复时：靠积压
  上限 800ms 一次性丢掉停顿期间积压的最旧数据、只留最近 100ms，不做
  "停顿检测+恢复窗口裁剪+静音补空"——实测恢复窗口把实时到达的每个
  320ms 突发裁到 40ms，噪声↔静音边界是硬切，在环境噪声里爆破声反而
  成倍增多。所有裁剪接缝（gain 与降噪阶段）一律 20ms 交叉淡化——
  有环境噪声时硬切拼接点就是爆破声，淡化后听感连续
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
- SCO 停顿恢复（降噪脚本）：写输出被下游阻塞 ≥0.5s 说明 aplay/SCO
  停止消费，恢复后一次性把停顿期间攒下的陈旧积压裁到只留最近一块
  （交叉淡化接缝，只裁一次），避免把停顿前/中说的话延迟 1~2s 重放
  造成卡顿失真；实时到达的新音频完整透传
- DeepFilterNet 基准失败时把降噪子进程的日志与 df 包信息（文件路径/
  版本/API）打印到 journal，便于定位"基准总是失败"的根因
- 每 5 秒输出 rtf/backlog 日志（journalctl -u bt-mic -f 可见）
- arecord/aplay 显式 ALSA period/buffer 时间（默认 20ms/60ms）

蓝牙耳机（A2DP Sink / HFP 下行 → 3.5mm 耳机口）：
- BlueALSA 默认只注册 HFP/HSP，电脑里只能看到"免手持设备"；本脚本额外用
  -p a2dp-sink 注册 A2DP Sink，Windows 才会出现 "<设备名> Stereo" 输出项，
  树莓派因此可以当普通蓝牙耳机/扬声器用（44.1kHz 立体声）
- A2DP profile 名在不同 bluez-alsa 版本里写法不同（a2dp-sink / a2dp-sink-sbc
  等）：按 --help 探测并逐个尝试，全部失败就回退成纯 HFP/HSP——耳机功能绝不
  拖累麦克风；注册结果用 sdptool 校验本机 SDP 里确实有 Audio Sink 记录
- 同样有"Windows 只在配对那一刻缓存服务"的坑：若那时没有 A2DP Sink 记录，
  Windows 永远不会出现立体声输出项，必须在 Windows 删除设备重新配对；
  症状就是"能连上、能被当麦克风，但声音设置里不能设为输出设备"。
  本程序把这条做成可诊断可修复：
    * `--audio-info` 只读打印适配器 Class、bluealsa 实际参数、本机 SDP 里的
      服务记录（电脑配对时看到的就是这些）、耳机口与混音状态、当前下行 PCM，
      并给出结论与处理步骤
    * `--forget` 删掉树莓派侧配对：电脑会重新配对（免 PIN）并重新做服务发现，
      不用在 Windows 里手动删设备（Windows 侧记录保留）
    * 运行中检测：连上 30 秒仍没有 A2DP 流时直接在日志里提示这件事
- Windows 声音设置里的两个本设备条目都用得上：
  『耳机 (设备名 Stereo)』= A2DP（44.1/48kHz 立体声，音质好）；
  『耳机 (设备名 Hands-Free AG Audio)』= HFP 下行（16kHz 单声道，但无需 A2DP）
- 下行播放用 `arecord <BlueALSA 下行 PCM> → [电平表] → aplay -D <耳机口>`
  管道（本程序内置，与麦克风上行链路同源；电平表是纯 Python 小脚本，原样转发
  音频并每 5 秒报一次峰值）：A2DP 接收流是 source 方向，必须用 arecord 读；
  采样率由 bluealsa-cli info 取、取不到就实测 44.1k/48k；有下行 PCM 才启动
  播放器，PCM 消失或进程退出自动重建。**不用 bluealsa-aplay**：它的命令行
  选项跨版本差异极大——实测某个版本没有指定输出设备的选项，照老写法传 -d 会
  `invalid option -- 'd'` 立即退出（耳机因此一直没声音，日志里刷屏的就是它）
- 电平表专治"先能出声、后来没声"这类问题：日志里出现
  `[downlink] 44100 Hz 2 ch 峰值 -xx.x dBFS（有音频信号）` 说明链路里确实有
  音频；出现 `全静音：链路里没有音频数据，或被缩放到 0` 就说明问题在蓝牙/
  BlueALSA 侧（音量被缩放到 0、电脑停止推流），而不是耳机口或混音
- A2DP 音量检查（每 30 秒一次）只做只读检查 + 一种明确修复：音量能读到且为 0
  时抬到 A2DP_MIN_VOLUME；**读不到时不再去动 SoftVolume**——实测"流正在播放
  时改 PCM 的音量/软音量属性"会把已经正常的 A2DP 流变成静音（进程还在、就是
  没声），要手工试的开关都列在 --audio-info 的输出里
- 耳机口 = reSpeaker 声卡的播放设备，自动选（HAT 声卡 → 其它声卡 → 树莓派
  板载 3.5mm → HDMI）：aplay -l 里没有 seeed/wm8960 声卡（HAT 驱动未加载）
  时会回退到板载口并明确告警——"耳机插在 HAT 孔上、声音却送给板载口"正是
  最常见的"插了耳机没声音"；可用 PLAYBACK_DEVICE 指定其它设备
- 混音按声卡类型分别处理：wm8960（HAT）用 Playback/Headphone/Speaker 与左右
  输出混音器 PCM；其它声卡（板载 bcm2835 Headphones、tlv320aic3x 等 HAT、
  USB 声卡）按通用名 PCM/Master/HP/... 处理——音量类设音量并取消静音，开关类
  直接打开。两种 amixer 写法（sget/sset 与 cget/cset name=）都试：部分 amixer
  版本不接受 name= 形式，旧实现因此在这些声卡上静默什么都没做
- A2DP 静音缩放修复（本机测试音正常、电脑音频无声的根因）：bluealsa(8) 明确
  A2DP 默认开 SoftVolume，此时样本按 PCM 自身音量缩放；AVRCP 协商异常时该音量
  可能是 0 或读不到，整条流就被缩放到静音（`--audio-info` 里表现为
  `SoftVolume=true 音量=读不到`）。本程序检测到就自动把音量抬到
  A2DP_MIN_VOLUME（默认 127=满幅，此后音量由本机混音控制），连设置都无效时
  关闭 SoftVolume 让样本直通；A2DP_SOFTVOL_FIX=0 可关闭该处理
- `--audio-info` 会打印 aplay -l / arecord -l 的**全部声卡原始输出**、选中的
  播放设备与它的混音当前值、每个下行 PCM 的音量与 SoftVolume、**手工复现命令**
  （arecord 读下行 PCM 管给本机 aplay，用于判断"BlueALSA→耳机口"这段是否通）
  与 soft-volume/volume 修复命令，另外还有 bluealsa-aplay 是否在运行、
  bt-mic 服务日志里与耳机相关的最近记录
- 检测到 A2DP 正在播放时，"等不到麦克风 SCO PCM"的强制断开逻辑会跳过：
  电脑只放音乐时不能为了逼 HFP 重连而把正在播放的音乐掐断
- 带宽提示：A2DP（约 345kbps）与 SCO 同时占用蓝牙链路，Pi4 的 BT/WiFi 是
  二合一芯片，2.4GHz WiFi 忙时容易卡顿/断音，建议 5GHz 或有线网络
- **同卡共存的取舍（第 3 轮实测后改回"放音优先"）**：麦克风与耳机口同一张声卡时，
  这张卡的收发共用 codec 的一路 I2S 时钟，两个方向只能同一个采样率。上一版把**下行
  降到 16kHz 单声道**来迁就麦克风，实测结果是**彻底没声**（打开成功、aplay 不报错、
  数据也在流，就是听不到）——而 44.1kHz 立体声放音一直是好的。所以现在的默认是
  **放音保持原生采样率**（可闻优先），只做加法（HP 交叉路由 + 混音看门狗），并把
  共存写成两个可实测的开关，由 --hp-test 的数据来定：
    * `HEADPHONE_CAPTURE_RATE=44100`（推荐先试这个）——反过来让**麦克风**按放音的
      采样率采集，上行链路内置重采样降到 16kHz 送 SCO。这样放音保住 44.1kHz 立体声、
      两个方向也同率。采集是否可用由 verify_mic 的电平检查把关（能采到非静音才行）
    * `HEADPHONE_HAT_MODE=1`——上一版的做法（下行统一 16kHz 单声道）。本卡 16kHz
      放音实测不可靠，除非 --hp-test 证明 16kHz 能听到，否则别用
- **逐声道混音读写（"耳机只有一边有声音"的排查盲区）**：原来读混音只取第一行，
  于是"右声道被静音/归零"在日志里完全看不见；现在读回**所有声道**、写的时候按
  `80%,80%` 逐声道设置，看门狗也按声道判断（任一声道变静音就重设）。`--hp-test`
  还会把该声卡**全部**混音控制连同各声道值打出来（`dump_all_mixer_controls`）
- **电平表心跳 + 字节计数**：电平表每 HP_METER_HEARTBEAT_SECS 秒（默认 15 秒）报一次
  `峰值 … 已转发 N 字节`，看门狗改成看**字节数有没有增长**来判断通路是否卡死。
  上一轮排查卡在这里：没有心跳时"通路一直有数据但耳机没声"和"通路卡死了"在日志里
  长得一模一样
- **`--hp-test` 改成"客观测量 + 需要耳朵的部分分开"**：
    1. 左/右/双声道各放一遍 1kHz（唯一需要耳朵的一步）
    2. 各格式放音的**客观指标**：退出码、underrun 次数、实际耗时（耗时明显偏离 =
       硬件没按声明的采样率跑，这就是同卡时钟冲突的直接证据）
    3. 麦克风在 16k/44.1k/48k 下的**采集电平**（能采到非静音才可用）
    4. 边录边放：录音用 `-d 30` 自限时、结束时按进程杀（上一版只杀到 shell，
       残留的 arecord 一直占着采集设备，让后面每一轮都误报 EBUSY，结论全错）
- 开麦克风后放音被压掉（"一开录音就不能放音"）的应对：下行有**两条**通路，
  电脑只往它选中的那条输出送音频，所以程序实测到下行近乎静音/根本没数据时
  按阶梯处理：① 摆正 A2DP 音量模型（soft-volume true + volume 127，排除半
  状态按 AVRCP 0 缩放）；② 探活另一条通路——只有"能读到字节"才切过去，避免
  在两条死通路之间来回跳——A2DP ↔ Hands-Free 下行(SCO) 互顶（最多 6 次）；
  ③ 都不行就打印电脑侧排查指引（通信音量压制/输出音量滑块）。
  可用 HEADPHONE_PROFILE=a2dp|sco 固定优先通路（sco 适合"电脑输出选
  『Hands-Free AG Audio』、要和麦克风同时工作"的场景，16kHz 单声道但最稳：
  两个方向都走同一条 SCO 链路，不必让蓝牙控制器同时背 A2DP+SCO）
- 下行"没有数据/不再消费"看门狗（症状=开麦克风后耳机没声音、日志里什么都没有）：
  通路**存在**不等于通路**在传音频**——电脑把输出切到另一条通路后，这条 PCM 仍然
  列在 BlueALSA 里、arecord 也打得开，就是永远读不到样本，而播放器的 arecord/aplay
  进程都活着，旧实现光看进程状态完全正常，于是既不报日志也不做任何处理（能哑一整场
  会话）。现在每 10 秒看一次电平表心跳里的**累计字节数有没有增长**：不再增长（无论
  是上游没数据，还是下游声卡不再消费）就说明这条通路哑了，记录日志并探活另一条通路
  切过去；电脑切回原通路后同样会自动切回来，无需重连
- 耳机输出播放器不再"放弃本轮"：连续启动失败改为换通路 + 退避重试（旧实现
  失败 MAX_RESTARTS 次后线程直接退出，之后本场会话再也没人重启播放器，
  表现就是"开过一次麦克风之后耳机再也起不来，只能重连"）
- **耳机口可能有两个孔，声音送错卡就没有声音（本次修复"换插孔后没声音"）**：
  本机板上有**两个** 3.5mm 输出——reSpeaker HAT 的孔（seeed/respeaker/voicecard
  声卡）和树莓派自己的孔（bcm2835 Headphones），耳机插哪个孔，声音就得送给哪张卡。
  旧实现固定优先 HAT，所以"耳机改插树莓派板载孔"之后完全听不到。现在：
    * `select_playback_device(mic_device)` **优先选与麦克风不同卡的声卡**：跨声卡时
      两个方向不共享 I2S 时钟，既没有"开麦克风就没声音"的冲突，也没有音质取舍；
      同卡的（麦克风的卡）只作为兜底，并打印两个对策开关
    * **配置文件 `/etc/default/bt-mic`**（KEY=VALUE，就是那些环境变量）：改配置不用
      编辑 systemd 单元，启动日志会打印实际生效的配置项；`BT_MIC_CONF` 可换路径
    * **`--find-output`**：逐张播放声卡放 2.5 秒 1kHz，听到哪张就把它写进配置的
      `PLAYBACK_DEVICE`（一步到位，不用猜、不用手工改单元）；`--hp-test` 第 0 步
      也会列出所有声卡并标出"与麦克风同卡"，可选进入同样的试听流程
    * 一张播放设备都找不到时提示"板载音频可能被关掉：确认 /boot/firmware/config.txt
      里有 dtparam=audio=on"
- 开关：HEADPHONE_ENABLE=0 只当麦克风；A2DP_SINK_ENABLE=0 不注册 A2DP（电脑只能
  用 Hands-Free 通路放音）；HEADPHONE_HAT_MODE=auto|1 是否把下行统一到采集采样率；
  HEADPHONE_CAPTURE_RATE=44100/48000 让麦克风按该采样率采集（上行内置重采样到
  16kHz，与放音同率）；HEADPHONE_CROSS_ROUTE=0 关掉 HP 交叉路由；HEADPHONE_VOLUME
  设耳机口音量；PLAYBACK_DEVICE 指定耳机所在声卡；HP_METER_HEARTBEAT_SECS 调电平表
  心跳间隔（0 = 只在档位变化时报）
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
import wave
from pathlib import Path

# ---------------- 配置文件（改配置不用编辑 systemd 单元） ----------------
# /etc/default/bt-mic 里写 KEY=VALUE 即可（就是下面那些环境变量），启动时读入作为
# 默认值（真实环境变量优先）。换耳机插孔、改音量这类事只需要：
#   echo 'PLAYBACK_DEVICE=plughw:1,0' | sudo tee -a /etc/default/bt-mic
#   sudo systemctl restart bt-mic
# 也可用 BT_MIC_CONF 指定别的路径；`--find-output` 会自动帮你写进去。
CONFIG_FILE = os.environ.get("BT_MIC_CONF", "/etc/default/bt-mic")


def load_config_file(path=None):
    """读取 KEY=VALUE 配置文件写入 os.environ（已存在的环境变量不覆盖）。

    返回真正生效的 [(键, 值)]，启动时打印——"我改了配置怎么没生效"这种问题看一眼
    启动日志就知道配置到底被读到了什么。
    """
    applied = []
    try:
        text = Path(path or CONFIG_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return applied
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        applied.append((key, value))
    return applied


def save_config_key(key, value):
    """把 KEY=VALUE 写进配置文件（同键覆盖、其它行保留），返回 (成功?, 说明)。"""
    path = Path(CONFIG_FILE)
    kept = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                kept.append(line)
                continue
            if stripped.split("=", 1)[0].strip() == key:
                continue          # 旧的同名配置丢掉，下面写新的
            kept.append(line)
    except OSError:
        pass
    if kept and kept[-1].strip():
        kept.append("")
    kept.append("# 由 %s --find-output 写入（%s）"
                % (os.path.basename(__file__), time.strftime("%Y-%m-%d %H:%M:%S")))
    kept.append("%s=%s" % (key, value))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    return True, str(path)


_CONFIG_APPLIED = load_config_file()


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
PERIOD_TIME_US = int(os.environ.get("PERIOD_TIME", "20000"))   # 20ms
BUFFER_TIME_US = int(os.environ.get("BUFFER_TIME", "60000"))   # 60ms
DF_NUM_THREADS = int(os.environ.get("DF_NUM_THREADS", "2"))  # torch 线程数（Pi4 四核，1~4 可按 env 实测调节）
DF_MODEL = os.environ.get("DF_MODEL", "deepfilternet2")  # 模型选择：deepfilternet2 比 deepfilternet3 轻量数倍；空串=库默认（DF3）
DF_CHUNK_MS = int(os.environ.get("DF_CHUNK_MS", "320"))      # 降噪每次处理的时长（毫秒），auto 模式的起始块大小
DF_MAX_BACKLOG_MS = int(os.environ.get("DF_MAX_BACKLOG_MS", "1200"))   # 允许的最大积压（超了跳音保低延迟）
DF_MIN_CHUNK_MS = int(os.environ.get("DF_MIN_CHUNK_MS", "40"))    # auto 模式尝试的最小块大小（越小处理延迟越低）
DF_MAX_CHUNK_MS = int(os.environ.get("DF_MAX_CHUNK_MS", "640"))   # auto 模式尝试的最大块大小
# 块边界爆音防护（deepfilternet 0.5.x 每次 enhance 都重置 GRU/滤波器状态，
# 且模型各卷积层在调用边界补零、库在块尾追加 n_fft 零样本、DF2 需要 2 帧
# 真实前瞻，损坏的上下文合计约 40~60ms，之前的 10ms 边缘盖不住）：
#   DF_PREFIX_MS = 块头预热前缀：把真实历史音频连同本块一起喂模型，吸收
#     头部全部冷启动/零填充伪影（前缀输出丢弃）；
#   DF_EDGE_MS   = 块尾丢弃边缘：覆盖 lookahead 与库补零产生的尾部垃圾；
#   DF_XFADE_MS  = 相邻窗口接缝交叉淡化宽度：两侧预热仍可能有微小掩码差，
#     线性混合后接缝听感完全连续（GRU 预热残差的兜底）。
DF_PREFIX_MS = int(os.environ.get("DF_PREFIX_MS", "50"))   # 块头预热前缀时长（毫秒）
DF_EDGE_MS = int(os.environ.get("DF_EDGE_MS", "40"))       # 块尾丢弃边缘时长（毫秒）
DF_XFADE_MS = int(os.environ.get("DF_XFADE_MS", "15"))     # 相邻窗口接缝交叉淡化宽度（毫秒，0=关闭）
DF_POST_FILTER = int(os.environ.get("DF_POST_FILTER", "1"))  # 后置滤波器 PF：额外降噪、加大静音衰减
DF_SPEC_POST = float(os.environ.get("DF_SPEC_POST", "0.4"))  # 模型后追加的轻量谱减法门增益下限（>0 启用；0=关闭）
# 降噪模式: df=DeepFilterNet（默认）；其他模式需显式指定：
#   auto=启动时实测 DeepFilterNet 速度，跟不上实时则自动用轻量谱减法；
#   spec=纯 numpy 谱减法；off=不降噪
DENOISE_MODE = os.environ.get("DENOISE_MODE", "df").lower()
DF_BENCH_LIMIT = float(os.environ.get("DF_BENCH_LIMIT", "0.9"))  # 基准 RTF 阈值
DF_RTF_EXIT_SECS = int(os.environ.get("DF_RTF_EXIT_SECS", "30"))  # 运行中持续超限多久自动降级
SPEC_FLOOR = float(os.environ.get("SPEC_FLOOR", "0.15"))  # 轻量降噪增益下限

# 音量提升：上行链路没有任何放大（+VGM=15 时 gain=1.0 纯直通），麦克风整体偏轻。
#   GAIN_BOOST_DB = gain 阶段的数字增益（dB）。+6dB ≈ 2 倍幅度；接近满幅时
#     软限幅平滑压缩，防止增强后削波爆音。0 = 纯直通
#   MIC_PGA_GAIN   = 编解码器模拟 PGA 增益（amixer cset 值，如 "20dB"、"50%"）。
#     tlv320aic3x 的 'PGA' 位于 ADC 之前，比数字放大更干净（提升输入信噪比）；
#     空 = 不改动，只报告当前值
GAIN_BOOST_DB = float(os.environ.get("GAIN_BOOST_DB", "6"))
MIC_PGA_GAIN = os.environ.get("MIC_PGA_GAIN", "")

# 蓝牙耳机（扬声器）：电脑把音频传到树莓派，由 reSpeaker HAT 的 3.5mm 耳机口放出。
#   HEADPHONE_ENABLE = 1/0 是否启用耳机输出通路（0 = 只当麦克风）
#   A2DP_SINK_ENABLE = 1/0 是否让 BlueALSA 额外注册 A2DP Sink。1 = 电脑可选
#     "Stereo/耳机"（44.1kHz 立体声）；0 = 只能用 HFP/HSP 下行（16kHz 单声道）
#   PLAYBACK_DEVICE  = 耳机口所在的 ALSA 播放设备（如 plughw:1,0）；空 = 自动探测
#   HEADPHONE_VOLUME = 启动时给 wm8960 播放/耳机音量设置的 amixer 值
HEADPHONE_ENABLE = os.environ.get("HEADPHONE_ENABLE", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)
A2DP_SINK_ENABLE = os.environ.get("A2DP_SINK_ENABLE", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)
PLAYBACK_DEVICE = os.environ.get("PLAYBACK_DEVICE", "")
HEADPHONE_VOLUME = os.environ.get("HEADPHONE_VOLUME", "80%")
# A2DP 下行静音缩放修复：BlueALSA 对 A2DP 默认开 SoftVolume，样本按 PCM 自身
# 音量缩放；AVRCP 协商异常时该音量可能是 0 或读不到，整条流就成了静音
# （症状：链路全绿、本机测试音正常、电脑音频无声）。默认自动把音量抬到
# A2DP_MIN_VOLUME（127=满幅，音量改由本机混音控制）；连设置都无效就关闭
# SoftVolume 让样本直通。A2DP_SOFTVOL_FIX=0 可关闭这个自动处理
A2DP_SOFTVOL_FIX = os.environ.get("A2DP_SOFTVOL_FIX", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)
A2DP_MIN_VOLUME = int(os.environ.get("A2DP_MIN_VOLUME", "127"))
# WiFi 省电：Pi4 的 WiFi 与蓝牙是同一颗二合一芯片，WiFi 省电会在蓝牙音频
# 传输期间周期性抢占/休眠，表现为卡顿甚至链路掉线（连上后突然断开、反复重连
# 常是它）。默认启动时关掉（iw set power_save off，仅本次运行有效、不改配置），
# WIFI_POWERSAVE=keep 可保留系统设置
WIFI_POWERSAVE = os.environ.get("WIFI_POWERSAVE", "off").strip().lower()
# 额外追加给 bluealsa 的命令行参数（空格分隔，会写在 override 的 ExecStart 末尾）。
# 用途：A2DP 音量的两种模型需要靠 bluealsa 启动参数切换，而 override 是本程序
# 自己写的（直接手改会被下次 --install 覆盖），所以留这个环境变量做 A/B 试验：
#   BLUEALSA_EXTRA_ARGS=--a2dp-volume   → 改用原生 AVRCP 音量（不做样本缩放，
#     电脑侧音量滑块重新生效；但 BlueZ <5.65 有 AVRCP 音量丢失的老问题）
BLUEALSA_EXTRA_ARGS = os.environ.get("BLUEALSA_EXTRA_ARGS", "").strip()
# 耳机下行优先用哪条通路：auto（默认：先 A2DP，实测静音就自动换另一条）| a2dp | sco。
# 开麦克风时（HFP 在用）Windows 往往只往它选中的那条输出送音频，auto 会自己找
# 有声音的那条——这正是"一开录音就不能放音"的对策之一
HEADPHONE_PROFILE = os.environ.get("HEADPHONE_PROFILE", "auto").strip().lower()
# 同卡（HAT）兼容模式——修"开麦克风后耳机没声音"和"耳机只有一边有声音"：
#
# 这台机器的麦克风和耳机口在**同一张声卡**上（plughw:3,0，seeed2micvoicec /
# tlv320aic3x，reSpeaker HAT）。而 seeed-voicecard 驱动把 **codec 设成 I2S 的
# bit/frame clock master**（设备树里 bitclock-master/frame-master = codec_dai），
# 也就是 LRCLK/BCLK 全由 codec 的 PLL 产生、收发共用同一路时钟：**采集和放音
# 只能是同一个采样率**。麦克风走 HFP，固定 16kHz；A2DP 音乐是 44.1kHz——
# 一开麦克风，两个方向就在抢同一个时钟，结果就是"放音没了"（本地声卡冲突，
# 不是蓝牙协议冲突；Windows 侧怎么设置都绕不过去，问题在树莓派声卡这一层）。
#
# 兼容模式做两件事，让两个方向在同一张卡上真正共存：
#   1. 把耳机下行统一成 **16kHz 单声道**（A2DP 44.1/48kHz 立体声 → 抗混叠低通
#      + 线性插值重采样，见 DOWNLINK_MIX_SCRIPT_CONTENT），与麦克风同率；
#   2. 混音上把两路耳机输出都接到有信号的那路 DAC（tlv320aic3x 的
#      Left/Right HP Mixer DACL1/DACR1 交叉开关）——该卡放音是单声道通路，
#      只开直连那一侧时另一个耳机会完全没声。
#
# auto（默认）= 当麦克风与耳机口在同一张声卡上、且该卡支持 16kHz 播放时启用；
# 1 = 强制启用；0 = 关闭（此时 A2DP 保持 44.1kHz，但与麦克风同时使用时该卡
# 无法同时跑两个采样率——放音大概率没声）
# 同卡（HAT）共存策略——麦克风与耳机口在同一张声卡上时怎么让两个方向都不哑。
#
# 这台机器：麦克风 plughw:3,0 与耳机口 plughw:3,0 是同一张卡
# （seeed2micvoicec / tlv320aic3x，reSpeaker HAT），而 seeed-voicecard 驱动把
# **codec 设成 I2S 的 bit/frame clock master**（设备树 bitclock-master /
# frame-master = codec_dai）：LRCLK/BCLK 由 codec 的 PLL 产生、收发共用一路时钟，
# 所以采集与放音**只能有一个采样率**同时在跑（麦克风走 HFP 固定 16kHz，A2DP 音乐
# 44.1kHz）。这是树莓派本地声卡的时钟冲突，不是蓝牙协议冲突。
#
# 但"哪边让路"有讲究，而且实测告诉我们：**这张卡的 16kHz 放音不可信**
# （打开成功、却听不到声音；44.1kHz 立体声放音一直是好的）。所以默认策略是
# "放音保持原生采样率"，把选择权留给下面两个开关，配合 --hp-test 的实测数据来定：
#   auto（默认）= 放音用 A2DP 原生采样率（44.1/48kHz 立体声，音质最好、实测可闻）
#        + 打开 HP 交叉路由 + 混音看门狗。麦克风并行时若被时钟冲突挤掉，日志会
#        明确提示，用下面两个开关试：
#   1 / on      = 把耳机下行**统一到采集采样率**（16kHz 单声道，重采样）：
#        两个方向同率，理论最稳；但本卡 16kHz 放音实测可能无声，慎用
#   HEADPHONE_CAPTURE_RATE=44100/48000 = 反过来：让**麦克风**按放音的采样率采集，
#        再由上行链路的重采样阶段降到 16kHz 送 SCO。这样放音保持 44.1kHz 立体声、
#        两个方向也同率——本卡 44.1kHz 采集可用时这是最推荐的方案
#   HEADPHONE_CROSS_ROUTE=0 = 不打开 HP 交叉路由（默认打开：该卡放音通路数据只在
#        一路 DAC 上，只开直连那一侧会让另一个耳塞完全没声）
HEADPHONE_HAT_MODE = os.environ.get("HEADPHONE_HAT_MODE", "auto").strip().lower()
HEADPHONE_CROSS_ROUTE = os.environ.get("HEADPHONE_CROSS_ROUTE", "1").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)
# 麦克风在声卡上的采集采样率：默认与 SCO 一致（16kHz）。设成 44100/48000 时上行
# 链路会插一个"重采样到 16kHz"的阶段（HAT 同卡共存用，见上面说明）
MIC_CAPTURE_RATE = int(os.environ.get("HEADPHONE_CAPTURE_RATE", str(SAMPLE_RATE)))
HP_MIXER_CHECK_SECS = int(os.environ.get("HP_MIXER_CHECK_SECS", "15"))  # 混音看门狗间隔
# 下行电平表心跳：每隔这么久打印一次"已转发字节数+峰值"。没有心跳时无法区分
# "通路正常但一直没声音"和"通路卡住了"——上一轮排查就卡在这里
HP_METER_HEARTBEAT_SECS = float(os.environ.get("HP_METER_HEARTBEAT_SECS", "15"))

BACKUP_DIR = "/tmp/bt_mic_backup"
BT_OVERRIDE = "/etc/systemd/system/bluetooth.service.d/override.conf"
BLUEALSA_OVERRIDE = "/etc/systemd/system/bluealsa.service.d/override.conf"
BLUETOOTH_CONF = "/etc/bluetooth/main.conf"
DENOISE_SCRIPT = Path("/tmp/df_denoise.py")
SPEC_SCRIPT = Path("/tmp/df_spec_denoise.py")
DOWNMIX_SCRIPT = Path("/tmp/df_downmix.py")
GAIN_SCRIPT = Path("/tmp/bt_gain.py")
DOWNLINK_METER_SCRIPT = Path("/tmp/bt_downlink_meter.py")
DOWNLINK_MIX_SCRIPT = Path("/tmp/bt_downlink_mix.py")
UPLINK_MIX_SCRIPT = Path("/tmp/bt_uplink_mix.py")  # HEADPHONE_CAPTURE_RATE != 16k 时用
GAIN_LEVEL_FILE = "/tmp/bt_gain_level"  # 0~15，15=满增益
DENOISE_STATUS_FILE = "/tmp/bt_denoise_status"  # 当前降噪模式/实测 RTF（--status 可查）
HEADPHONE_STATUS_FILE = "/tmp/bt_headphone_status"  # 耳机（扬声器）输出状态（--status 可查）
_A2DP_REGISTERED = [None]  # 实际注册成功的 A2DP profile 名（None = 未注册）
# 麦克风上行管道是否正在跑（耳机输出线程据此确认"开麦克风前后耳机口混音
# 有没有被声卡驱动改回默认"，见 headphone_monitor 里的混音看门狗）
_UPLINK_ACTIVE = threading.Event()
# 是否处于 HAT 兼容模式（同卡收发共用 I2S 时钟，下行统一到采集采样率）
_HAT_MODE = [False]
SERVICE_UNIT_PATH = "/etc/systemd/system/bt-mic.service"

# 构建标记：每次改动都要更新，启动时和 --audio-info 都会打印。
# 用途很实在——已经多次出现"复制了新脚本但服务还在跑旧进程"（systemctl start
# 对已运行的服务是空操作），日志里能一眼看出跑的到底是哪一版
BUILD_ID = ("2026-09-11-14 播放设备选择优先『与麦克风不同卡』+ /etc/default/bt-mic "
            "配置文件 + --find-output 逐卡试听（换耳机插孔后没声音）")
_SCRIPT_MTIME = [None]
_UPDATE_WARNED = [False]
_LAST_DOWNLINK_ERR = [""]  # 下行 PCM 打不开的原因（只在变化时打日志，避免刷屏）


def check_script_updated():
    """磁盘上的脚本是否在本次进程启动后被更新过（更新了要重启服务才生效）。

    发现更新就提示一次：否则会出现"改了代码、日志里却还是旧行为"的迷惑现场。
    """
    try:
        mtime = os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return
    if _SCRIPT_MTIME[0] is None:
        _SCRIPT_MTIME[0] = mtime
        return
    if mtime > _SCRIPT_MTIME[0] + 1 and not _UPDATE_WARNED[0]:
        _UPDATE_WARNED[0] = True
        _SCRIPT_MTIME[0] = mtime
        print("  !! 磁盘上的脚本已更新，但当前进程仍在跑旧代码：")
        print("     请执行 sudo systemctl restart bt-mic（前台运行则重启本程序）")

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


def forget_paired_devices():
    """--forget: 删除树莓派侧的配对记录，迫使电脑重新配对并重新枚举服务。

    为什么需要这一步：Windows 只在**配对那一刻**缓存对方的服务列表，之后
    不会因为对方新增服务而重新枚举。如果那次配对时树莓派还没注册 A2DP
    Sink（升级本脚本之前配的对就是如此），Windows 只会把它当"只有麦克风
    的语音设备"：声音设置→输出里永远不出现它，也无法设为音频输出。

    删掉树莓派侧的配对后，Windows 会重新配对（本程序 NoInputNoOutput 代理
    免 PIN 自动接受），配对时重新做服务发现，A2DP 音乐通道就会出现。
    电脑上的设备记录不用手动删，重新连一下即可。
    """
    print("=== 删除树莓派侧配对，迫使电脑重新配对并重新发现服务 ===")
    print("  电脑侧不用删设备：它会重新配对（免 PIN），配对时会重新枚举服务")
    remove_paired_devices()
    set_discoverable(True)
    print("  -> 现在请在电脑的蓝牙设置里点一下该设备重新连接")
    print("  -> 重新配对后『声音设置→输出』会出现『耳机 (设备名 Stereo)』，")
    print("     那就是 A2DP 音乐通道（把电脑音频送到 3.5mm 耳机口）")


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
    run("pkill -f bluealsa-aplay || true", check=False, verbose=False)

    if os.path.exists("/tmp/asound.conf.removed"):
        shutil.move("/tmp/asound.conf.removed", "/etc/asound.conf")


atexit.register(restore_default)


# ---------------- 依赖 / 服务清理 ----------------


def setup_packages():
    required = ["bluetoothctl", "bluealsa", "bluealsa-cli", "arecord", "aplay"]
    missing = [b for b in required if shutil.which(b) is None]
    if not missing:
        print_status("依赖检查通过（bluez/bluealsa 已安装，跳过 apt）")
        if shutil.which("bluealsa-cli") is None:
            print("  !! 缺少 bluealsa-cli：诊断与音量控制不可用")
            print("     请执行 sudo apt install bluez-alsa-utils（麦克风不受影响）")
        return
    print_status(f"检查并安装依赖（缺少: {', '.join(missing)}）")
    run("apt-get update -y --allow-releaseinfo-change", check=False, verbose=False)
    run(
        "apt-get install -y bluez bluez-tools bluez-alsa bluez-alsa-utils bluetooth "
        "alsa-utils libasound2-dev libasound2-plugins",
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
            text = text[: m.end()] + "\n" + new_line + text[m.end():]
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


def verify_a2dp_sdp_record():
    """检查本机 SDP 里是否有 A2DP Sink（Audio Sink）服务记录。

    与 HFP 同理：Windows 只对配对时缓存到的服务发起连接。若配对那一刻
    A2DP Sink 记录缺失，Windows 声音设置里永远不会出现立体声输出项
    （只能走 Hands-Free 下行）。返回 True/False/None（None = 无法验证）。
    """
    if not shutil.which("sdptool"):
        return None
    res = run("sdptool browse local", check=False, timeout=15, verbose=False)
    if not res:
        return None
    text = (res.stdout or "") + (res.stderr or "")
    if "Failed to connect" in text:
        return None
    return "Audio Sink" in text or "AudioSink" in text or "A2DP" in text


def sdp_local_service_names():
    """本机 SDP 已注册的服务名列表（Windows 配对时看到的就是这些）。

    返回 None 表示无法验证（缺 sdptool 或查询失败）。
    """
    if not shutil.which("sdptool"):
        return None
    res = run("sdptool browse local", check=False, timeout=15, verbose=False)
    if not res:
        return None
    text = (res.stdout or "") + (res.stderr or "")
    if "Failed to connect" in text:
        return None
    names = []
    for line in text.splitlines():
        m = re.match(r"\s*Service Name:\s*(.+?)\s*$", line)
        if m:
            names.append(m.group(1))
    return names


def sdp_has_a2dp_sink(names):
    """服务名列表里是否含 A2DP Sink（Audio Sink）。names=None 时返回 None。"""
    if names is None:
        return None
    for n in names:
        low = n.lower()
        if "audio sink" in low or "audiosink" in low or "a2dp" in low:
            return True
    return False


def sdp_has_handsfree(names):
    """服务名列表里是否含 Handsfree/Headset（麦克风需要的 HFP/HSP）。"""
    if names is None:
        return None
    for n in names:
        low = n.lower()
        if "handsfree" in low or "hands-free" in low or "hands free" in low:
            return True
        if "headset" in low and "gateway" not in low:
            return True
    return False


def bluealsa_journal_lines(n=120):
    """bluealsa 最近日志行（配置阶段的诊断输出都从这里取）。"""
    res = run(
        f"journalctl -u bluealsa -n {n} --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    if not res:
        return []
    return [
        line.strip()
        for line in ((res.stdout or "") + (res.stderr or "")).splitlines()
        if line.strip()
    ]


def a2dp_uuid_conflict(journal_text):
    """日志里是否出现 A2DP UUID 注册冲突（被别的进程/服务抢先注册）。"""
    low = (journal_text or "").lower()
    if "a2dp" not in low:
        return False
    return (
        "uuid already registered" in low
        or "couldn't register" in low
        or "failed to register" in low
    )


def log_bt_disconnect_reason():
    """蓝牙断开时打印 BlueZ 日志里的断开原因（链路超时 / 对端主动断开 / 被本机断开）。

    只有断开时才调用，用于区分"Windows 自己断的"、"链路超时（信号/带宽问题）"
    和"被本程序断的"，避免只看到"已断开"却不知道原因。
    """
    res = run(
        "journalctl -u bluetooth -n 60 --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    lines = [
        line.strip()
        for line in ((res.stdout or "") if res else "").splitlines()
        if line.strip()
    ]
    keys = ("disconnect", "reason", "timeout", "link key", "connection", "supervision")
    hits = [line for line in lines if any(k in line.lower() for k in keys)]
    if hits:
        print("  -- BlueZ 日志（断开相关，用于判断是谁断的）--")
        for line in hits[-6:]:
            print(f"    {line}")


def bluealsa_a2dp_profile_name(ba_path):
    """bluealsa 支持的 A2DP Sink profile 名候选（按优先级排序）。

    bluez-alsa 各版本命名不一致（a2dp-sink / a2dp-sink-sbc / a2dp-sink-aac）：
    名字写错会让 bluealsa 直接启动失败，所以先看 --help 里列出的名字；
    列不出来（很多版本不打印 profile 列表）就按最常见命名依次尝试，调用方
    逐个试启动，全部失败就退回纯 HFP/HSP——耳机功能绝不拖累麦克风。
    """
    names = []
    res = run(f"{ba_path} --help 2>&1", check=False, timeout=8, verbose=False)
    text = ((res.stdout or "") + (res.stderr or "")) if res else ""
    low = text.lower()
    if low.strip() and "a2dp" in low:
        for name in (
            "a2dp-sink",
            "a2dp-sink-sbc",
            "a2dp-sink-aac",
            "a2dp-sink-aptx",
            "a2dp-sink-aptx-hd",
            "a2dp_sink",
        ):
            if name in low:
                names.append(name.replace("_", "-"))
    for name in ("a2dp-sink", "a2dp-sink-sbc"):
        if name not in names:
            names.append(name)
    return names


def configure_bluealsa():
    print_status("配置 BlueALSA")
    ba_path = shutil.which("bluealsa")
    if not ba_path:
        raise FileNotFoundError("找不到 bluealsa，请确认已安装 bluez-alsa")

    hci = get_hci_name()
    # 麦克风（HF 侧）固定需要的 profile；A2DP Sink 让电脑能把树莓派当耳机
    base_profiles = ["-p", "hsp-hs", "-p", "hfp-hf"]
    if BLUEALSA_EXTRA_ARGS:
        base_profiles += shlex.split(BLUEALSA_EXTRA_ARGS)
        print("  -> 追加 bluealsa 参数（BLUEALSA_EXTRA_ARGS）: %s" % BLUEALSA_EXTRA_ARGS)
    a2dp_candidates = bluealsa_a2dp_profile_name(ba_path) if A2DP_SINK_ENABLE else []
    if not A2DP_SINK_ENABLE:
        print("  -> A2DP_SINK_ENABLE=0：只注册 HFP/HSP（电脑只能用 Hands-Free 通路放音）")

    def write_override(profile_args):
        new_exec = f"{ba_path} -i {hci} " + " ".join(profile_args)
        os.makedirs(os.path.dirname(BLUEALSA_OVERRIDE), exist_ok=True)
        with open(BLUEALSA_OVERRIDE, "w", encoding="utf-8") as f:
            f.write("[Service]\nExecStart=\nExecStart=" + new_exec + "\n")
        run("systemctl daemon-reload", check=False, verbose=False)
        return new_exec

    def restart_bluealsa():
        run("systemctl stop bluealsa 2>/dev/null || true", check=False, verbose=False)
        run("systemctl enable bluealsa 2>/dev/null || true", check=False, verbose=False)
        run("systemctl start bluealsa", check=False, verbose=False)
        time.sleep(2)
        res = run("systemctl is-active bluealsa", check=False, timeout=8, verbose=False)
        return bool(res and res.stdout.strip() == "active")

    # profile 名是命令行参数，在取得 D-Bus 名之前解析：不支持的名字会让
    # bluealsa 直接启动失败（is-active 不为 active）。逐个候选试启动，任何
    # 失败都退回纯 HFP/HSP，保证耳机功能绝不拖累麦克风。
    a2dp_profile = None
    a2dp_sdp_ok = None
    new_exec = ""
    for name in a2dp_candidates:
        new_exec = write_override(base_profiles + ["-p", name])
        if not restart_bluealsa():
            print(f"  !! 带 A2DP profile '{name}' 启动 bluealsa 失败，尝试其他命名")
            for line in bluealsa_journal_lines(40)[-6:]:
                print(f"     {line}")
            continue
        # 服务起来还不够：SDP 里得真有 Audio Sink 记录，电脑才看得到耳机输出。
        # 个别版本要写成编解码器专名（a2dp-sink-sbc）才会注册，所以这里校验
        a2dp_sdp_ok = verify_a2dp_sdp_record()
        if a2dp_sdp_ok is False:
            print(f"  !! profile '{name}' 启动成功，但 SDP 里没有 Audio Sink 记录，")
            print("     换用其他 profile 命名重试")
            continue
        a2dp_profile = name
        break
    if a2dp_profile is None:
        new_exec = write_override(base_profiles)
        restart_bluealsa()
        if a2dp_candidates:
            print("  !! 未能注册 A2DP Sink，已回退为仅 HFP/HSP（麦克风优先）")
    _A2DP_REGISTERED[0] = a2dp_profile
    print(f"  -> 使用参数: {new_exec}")
    if a2dp_profile:
        print(f"  -> 已注册 A2DP Sink（{a2dp_profile}）：电脑可把树莓派当耳机/扬声器")

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

    journal_text = "\n".join(bluealsa_journal_lines(120))
    if "UUID already registered" in journal_text:
        # 区分 A2DP UUID 冲突和 HFP/HSP UUID 冲突
        if "Couldn't register hands-free profile" in journal_text:
            print("  !! 检测到 HFP/HSP UUID 冲突，尝试清理...")
            resolve_uuid_conflict(hci)
        elif a2dp_profile and a2dp_uuid_conflict(journal_text):
            # 耳机功能要求 BlueALSA 独占 A2DP Sink UUID：被别的进程抢注时，
            # Windows 只把它当免手持设备，声音里听不到立体声
            print("  !! 检测到 A2DP UUID 冲突（耳机功能需要它），尝试清理...")
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

    # A2DP Sink 记录同理：缺失时 Windows 只把它当免手持设备，声音设置里
    # 没有立体声输出项（耳机口只能靠 HFP 下行出声，16kHz 单声道）
    if a2dp_profile:
        if a2dp_sdp_ok is True:
            print("  -> 已确认本机 SDP 含 Audio Sink 记录（电脑可把树莓派当耳机/扬声器）")
        else:
            # 上面校验收到了 None（缺 sdptool 或查询失败），这里再补一次
            a2dp_ok = verify_a2dp_sdp_record()
            if a2dp_ok is False:
                print("  !! 本机 SDP 缺 Audio Sink（A2DP）记录，重启 bluealsa 重新注册...")
                run("systemctl restart bluealsa", check=False, verbose=False)
                time.sleep(3)
                a2dp_ok = verify_a2dp_sdp_record()
            if a2dp_ok is True:
                print("  -> 已确认本机 SDP 含 Audio Sink 记录（电脑可把树莓派当耳机/扬声器）")
            elif a2dp_ok is False:
                print("  !! 仍缺 Audio Sink SDP 记录：Windows 声音设置里不会有立体声输出项，")
                print("     耳机口只能走 Hands-Free 下行。请查看 journalctl -u bluealsa。")


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
        run(
            "rfkill unblock bluetooth 2>/dev/null || true", check=False, verbose=False
        )
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
    print("  若 Windows 仍弹出 PIN 提示，请先在 Windows 蓝牙设置中『删除设备』再重新添加")
    print("  连接后，请到 Windows『声音设置 → 输入』中选择 Hands-Free/Headset 设备，")
    print("  此时 BlueALSA 才会建立 HFP/SCO 麦克风通道。")
    print("  要让 reSpeaker 的 3.5mm 耳机口放音：到 Windows『声音设置 → 输出』")
    print("  选择本设备（『… Stereo』走 A2DP 立体声，『… Hands-Free』走 16kHz 通路），")
    print("  两者都能出声，声音设置里看不到立体声项时说明需要删除设备重新配对。")
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
            check_script_updated()
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
    for cmd in ("bluealsa-aplay -L", "bluealsa-cli list-pcms",
                "bluealsa-cli list-devices", "aplay -L"):
        if not shutil.which(cmd.split()[0]):
            print(f"  $ {cmd}  ->  (命令不存在)")
            continue
        res = run(cmd, check=False, timeout=8, verbose=False)
        print(f"  $ {cmd}  ->  rc={res.returncode if res is not None else '(超时)'}")
        if res:
            for line in ((res.stdout or "") + "\n" + (res.stderr or "")).splitlines():
                if line.strip():
                    print(f"      {line.strip()}")
    res = run("systemctl is-active bluealsa bluetooth", check=False,
              timeout=8, verbose=False)
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
            log_bt_disconnect_reason()
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
            uuids = ("0000111e-0000-1000-8000-00805f9b34fb",
                     "",
                     "0000111f-0000-1000-8000-00805f9b34fb")
            request_hfp_profile(device, uuid=uuids[hfp_variant % len(uuids)])
            hfp_variant += 1
            if kill_bt_profile_conflicts():
                print("    -> 清理了重新出现的冲突进程，重启 bluealsa 重新注册 profile")
                run("systemctl restart bluealsa", check=False, verbose=False)
                time.sleep(3)

        # 约 25 秒仍无 SLC：强制断开 ACL 触发 Windows 自动重连。实测旧版
        # 就是 Windows 重连之后才拉起 HFP 并成功传送的——干等会等到超时。
        # 例外：电脑正在用 A2DP 放音（只当耳机用、没启用麦克风）时不能断，
        # 否则会把正在播放的音频掐断，且重连也不会让 Windows 去开麦克风
        if not slc_up and not forced_disconnect and time.time() - start >= 25:
            forced_disconnect = True
            if HEADPHONE_ENABLE and a2dp_stream_active(device):
                print("  -> 25 秒仍无 HFP 服务级连接，但检测到 A2DP 正在播放音频")
                print("     （电脑只把它当耳机、未启用麦克风）：不主动断开，继续等")
                print("     如需使用麦克风，请在 Windows 声音设置→输入选 Hands-Free")
            else:
                print("  -> 25 秒仍无 HFP 服务级连接：主动断开蓝牙，触发 Windows")
                print("     自动重连并重试 Hands-Free 服务（Windows 数秒内重连）")
                run(f"bluetoothctl disconnect {device}", check=False,
                    timeout=15, verbose=False)
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
        "'/tmp/bt_downlink_meter.py$'",
        "'/tmp/bt_downlink_mix.py$'",
        "'/tmp/bt_uplink_mix.py$'",
        "'aplay .*bluealsa:.*--period-time'",
        "'arecord .*--period-time'",
        "'bluealsa-aplay'",
        # 耳机下行管道（arecord 读 BlueALSA 下行 PCM → aplay 写耳机口）：
        # arecord 那条已被上面的 'arecord .*--period-time' 覆盖，aplay 写的是
        # plughw 而不是 bluealsa，所以要单独匹配，否则残留进程会占住声卡
        "'aplay .*plughw:.*--period-time'",
    ):
        run(f"pkill -f {pat} 2>/dev/null", check=False, timeout=8, verbose=False)


# ---------------- 蓝牙耳机（A2DP Sink / HFP 下行 -> 3.5mm 耳机口） ----------------
#
# 目标：树莓派既能当麦克风（HFP-HF 上行，见上），也能当蓝牙耳机——电脑把音频
# 传到树莓派，由 reSpeaker HAT 的 3.5mm 耳机口放出来。两条下行通路并存，
# Windows 声音设置里选哪个输出都能出声：
#   1. A2DP Sink（UUID 0x110B）：Windows 里的 "<设备名> Stereo"，44.1/48kHz
#      立体声，正常蓝牙耳机音质。BlueALSA 必须带 -p a2dp-sink 启动才会注册
#      （旧版本只注册 HFP/HSP，所以电脑里根本看不到耳机输出项）。
#   2. HFP/HSP 下行（SCO source，UUID 0x111E）：Windows 里的 "<设备名>
#      Hands-Free AG Audio"，16kHz 单声道，音质差，但麦克风通道建立后这条
#      链路本来就在，电脑把它当免手持设备时靠它出声。
#
# 播放用 arecord|aplay 管道（不用 bluealsa-aplay，原因见 build_downlink_player）：
# A2DP 接收流是 source 方向，arecord 读出下行音频、aplay 写到耳机口声卡，
# 采样率/声道差异交给 plughw 转换，与麦克风上行链路同源、兼容性最好。
#
# 关键前提（与 HFP 的教训相同）：Windows 只在配对那一刻缓存设备提供的服务。
# 若配对时树莓派还没注册 A2DP Sink，Windows 永远只把它当免手持设备，声音
# 设置里不会出现立体声输出项——必须在 Windows 删除设备，等本程序日志显示
# A2DP Sink 已注册后重新配对。


def write_headphone_status(**extra):
    """把耳机（扬声器）输出状态写入 HEADPHONE_STATUS_FILE，供 --status 查看。"""
    lines = ["updated=%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    if _A2DP_REGISTERED[0]:
        lines.append("a2dp_sink=%s" % _A2DP_REGISTERED[0])
    else:
        lines.append("a2dp_sink=(未注册)")
    for key, val in extra.items():
        if val is not None:
            lines.append("%s=%s" % (key, val))
    tmp = HEADPHONE_STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, HEADPHONE_STATUS_FILE)
    except OSError:
        pass


def _parse_alsa_pcm_lines(stdout):
    """解析 `aplay -l` / `arecord -l` 的声卡列表，返回 [(card, device, label)]。"""
    devices = []
    for line in (stdout or "").splitlines():
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
        card_name = re.sub(r"\s*\[.*?\]\s*$", "", rest[: m_dev.start()].strip()).strip()
        label = f"{card_name} / {m_dev.group(2).strip()}".strip(" /")
        devices.append((card, dev, label))
    return devices


def probe_playback_devices():
    """列出所有播放设备（aplay -l），返回 [(card, device, label)]。"""
    result = run("aplay -l", check=False, timeout=8, verbose=False)
    if not result or result.returncode != 0:
        return []
    return _parse_alsa_pcm_lines(result.stdout)


# 输出设备的偏好分四档（详见 select_playback_device）：
#   HAT_OUTPUT_HINTS      = reSpeaker HAT（wm8960）等外接声卡，最优先——耳机插在它的孔上
#   其它声卡（USB 声卡等）
#   ONBOARD_OUTPUT_HINTS  = 树莓派板载 3.5mm（"bcm2835 Headphones"），其次
#   HDMI_OUTPUT_HINTS     = HDMI 输出，最后（对耳机口毫无意义）
# 注意板载匹配串要写全 "bcm2835 headphones"：reSpeaker 声卡的设备名里也含
# bcm2835（bcm2835-i2s-wm8960-hifi），用 "bcm2835" 会把它一起误判
HAT_OUTPUT_HINTS = ("seeed", "respeaker", "re-speaker", "wm8960", "voicecard", "2mic")
ONBOARD_OUTPUT_HINTS = ("bcm2835 headphones", "bcm2711", "bcm2835 analog")
HDMI_OUTPUT_HINTS = ("hdmi", "vc4")


def label_is_onboard_output(label):
    """该设备是不是树莓派板载 3.5mm/HDMI（而不是 HAT 上的耳机口）。"""
    low = (label or "").lower()
    return any(k in low for k in ONBOARD_OUTPUT_HINTS + HDMI_OUTPUT_HINTS)


def test_alsa_playback(dev, rate=48000, channels=2):
    """实测播放设备能否打开（写 2 秒静音），返回 (可用?, 失败原因)。"""
    if not shutil.which("aplay"):
        return False, "缺少 aplay（请安装 alsa-utils）"
    cmd = (
        f"timeout 2 aplay -t raw -D '{dev}' -f {FORMAT} -r {rate} -c {channels} "
        "/dev/zero 2>&1"
    )
    result = run(cmd, check=False, timeout=6, verbose=False)
    if result is None:
        return False, "aplay 超时"
    if result.returncode in (0, 124):
        return True, ""
    err = " ".join(((result.stdout or "") + " " + (result.stderr or "")).split())
    return False, err[-180:]


def select_playback_device(mic_device=None):
    """探测耳机所在播放设备，返回 (设备名, 说明)。

    mic_device = 麦克风设备名（如 plughw:3,0），用来避开"同卡时钟冲突"：本机的
    麦克风与 HAT 的 3.5mm 口在同一张卡上，那张卡的收发共用 codec 的一路 I2S 时钟，
    两个方向只能同一个采样率。所以**优先选与麦克风不同卡的声卡**（板载 3.5mm、
    USB 声卡），跨声卡时两个方向互不影响、也没有音质取舍。

    偏好顺序：
      1. PLAYBACK_DEVICE（环境变量或 /etc/default/bt-mic 里配置）
      2. 与麦克风**不同卡**的声卡：HAT 类 → 其它 → 板载 3.5mm → HDMI
      3. 与麦克风同卡的那张（例如耳机插在 HAT 的 3.5mm 孔上）——能用，但两个
         方向会抢同一个时钟，这里会打印对策开关
    用 plughw 打开，让 ALSA 负责采样率/声道转换。找不到时返回 (None, 原因)。
    """
    if not shutil.which("aplay"):
        return None, "缺少 aplay（请安装 alsa-utils）"
    if PLAYBACK_DEVICE:
        ok, err = test_alsa_playback(PLAYBACK_DEVICE)
        if ok:
            return PLAYBACK_DEVICE, f"配置的 PLAYBACK_DEVICE={PLAYBACK_DEVICE}"
        print(f"  !! PLAYBACK_DEVICE={PLAYBACK_DEVICE} 打不开：{err}")
        print("     改配置：sudo nano %s 或 sudo python3 %s --find-output"
              % (CONFIG_FILE, os.path.basename(__file__)))
    mic_card = _playback_card(mic_device) if mic_device else None
    hat, others, onboard, hdmi = [], [], [], []
    for card, dev, label in probe_playback_devices():
        low = label.lower()
        same = mic_card is not None and str(card) == mic_card
        item = (card, dev, label, same)
        if any(k in low for k in HAT_OUTPUT_HINTS):
            hat.append(item)
        elif any(k in low for k in HDMI_OUTPUT_HINTS):
            hdmi.append(item)
        elif label_is_onboard_output(label):
            onboard.append(item)
        else:
            others.append(item)
    # 不同卡的排前面（跨声卡没有时钟冲突）；同卡的放最后兜底
    ordered = []
    for group in (hat, others, onboard, hdmi):
        ordered += [x for x in group if not x[3]]
    for group in (hat, others, onboard, hdmi):
        ordered += [x for x in group if x[3]]
    for card, dev, label, same in ordered:
        cand = f"plughw:{card},{dev}"
        ok, err = test_alsa_playback(cand)
        print("  -> 播放设备: %s（%s）%s%s"
              % (cand, label, "可用" if ok else "不可用：" + err,
                 "  [与麦克风同一张声卡]" if same else ""))
        if not ok:
            continue
        if same:
            print("  !! 这张声卡也是麦克风所在卡：两个方向共用 codec 的一路 I2S 时钟，")
            print("     只能同一个采样率（麦克风固定 %d Hz，A2DP 是 44.1/48kHz 会互相挤）"
                  % SAMPLE_RATE)
            print("     对策（二选一，各试一次）：")
            print("       HEADPHONE_CAPTURE_RATE=44100   麦克风按 44.1kHz 采集（放音保住）")
            print("       HEADPHONE_HAT_MODE=1           把下行降到 16kHz 单声道")
            print("     零取舍的办法：把耳机插到另一张声卡（树莓派板载 3.5mm / USB），")
            print("     或跑 sudo python3 %s --find-output 让它带你确认耳机插在哪张卡"
                  % os.path.basename(__file__))
        else:
            print("  -> 与麦克风不是同一张声卡：两个方向互不影响（推荐）")
        return cand, label
    # 一张都打不开：给出板载音频没启用的可能
    names = [l for _c, _d, l in probe_playback_devices()]
    print("  aplay -l 里的播放设备: %s" % (", ".join(names) if names else "（一个都没有）"))
    if not names:
        print("  !! 一个播放设备都没有：板载音频可能被关掉了。要在树莓派板载 3.5mm")
        print("     口出声，请确认 /boot/firmware/config.txt 里有 dtparam=audio=on")
        print("     （或 /boot/config.txt，视系统版本），重启后再试")
    return None, "没有可用的播放设备"


# wm8960（reSpeaker HAT）播放通路相关的混音控制 + 期望值：Playback/Headphone/
# Speaker 是音量（用 HEADPHONE_VOLUME），左右输出混音器 PCM 开关决定 I2S 的
# 播放音频是否接到耳机口，不开就只有静音（默认静音是"耳机口没声音"的常见原因）
PLAYBACK_MIXER_CONTROLS = (
    ("Playback", HEADPHONE_VOLUME),
    ("Headphone", HEADPHONE_VOLUME),
    ("Speaker", HEADPHONE_VOLUME),
    ("Left Output Mixer PCM", "on"),
    ("Right Output Mixer PCM", "on"),
)

# 其它声卡（树莓派板载 bcm2835 Headphones、reSpeaker HAT 的 tlv320aic3x、
# USB 声卡等）的通用播放控制名：这些卡没有 wm8960 那套混音，静音/音量为 0 时
# 同样"插了耳机也没声音"，所以按存在与否逐个尝试设置（音量类的设音量+取消
# 静音，开关类的打开），见 apply_playback_mixer。注意不要放 'PGA'（那是
# 采集侧的模拟增益，由 MIC_PGA_GAIN 单独控制）
GENERIC_PLAYBACK_CONTROLS = (
    "PCM",
    "Master",
    "Headphone",
    "Headphone Playback Volume",
    "HP",
    "Speaker",
    "Digital",
    "Analog",
    "Line",
    "Playback",
    "DAC",
    # tlv320aic3x（reSpeaker 2-Mics HAT）等的 DAC→耳机口 路由开关：
    # 关着的时候耳机插着也完全没声音，所以一并尝试打开
    "HP DAC",
    "Line DAC",
    "Left HP Mixer DACL1",
    "Right HP Mixer DACR1",
)


def amixer_get(card, ctl):
    """读混音控制的原始输出；两种写法都试（不同 amixer 版本接受的形式不同）。"""
    if shutil.which("amixer") is None:
        return None
    for cmd in (
        f"amixer -c {card} sget '{ctl}'",
        f"amixer -c {card} cget name='{ctl}'",
    ):
        res = run(cmd, check=False, timeout=8, verbose=False)
        if res and res.returncode == 0:
            return res.stdout or ""
    return None


def amixer_set(card, ctl, value):
    """设置混音控制（>=2 个参数时逐段尝试，如 '80% unmute'）。"""
    if shutil.which("amixer") is None:
        return False
    for cmd in (
        f"amixer -c {card} sset '{ctl}' {value}",
        f"amixer -c {card} cset name='{ctl}' {value}",
    ):
        res = run(cmd, check=False, timeout=8, verbose=False)
        if res and res.returncode == 0:
            return True
    return False


def _playback_card(playback_device):
    """从 plughw:C,D / hw:C,D 里取声卡号。"""
    m = re.search(r"(?:plug)?hw:(\d+)", playback_device or "")
    return m.group(1) if m else "0"


def list_mixer_control_names(card):
    """列出声卡上的混音控制名（amixer scontrols），失败返回空列表。"""
    if shutil.which("amixer") is None:
        return []
    res = run(f"amixer -c {card} scontrols", check=False, timeout=8, verbose=False)
    if not res:
        return []
    return re.findall(r"Simple mixer control '([^']+)'", res.stdout or "")


def mixer_control_value(card, ctl):
    """读取单个混音控制的当前值（**所有声道**的行，用 " | " 连接）。

    音量类的行形如 "Front Left: Playback 51 [81%] [-6.00dB] [on]"，
    开关类的行形如 "Mono: Playback [on]"，cget 形式则是 ": values=0,0"。
    旧实现只返回第一行，于是"右声道被静音/归零"这类问题在日志里完全看不见
    （而"耳机只有一边有声音"恰恰就是这种形态），所以这里返回全部声道。
    """
    out = amixer_get(card, ctl)
    if out is None:
        return None
    lines = []
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith("Simple mixer control") or s.startswith("numid="):
            continue
        if s.startswith(";"):
            continue
        if ": values=" in s:
            lines.append(s.split(": values=", 1)[1].strip())
        elif "[" in s:
            lines.append(s)
    if not lines:
        return "?"
    return " | ".join(lines)


def mixer_control_channels(raw):
    """把混音读数拆成逐声道的片段（兼容 " | " 连接与原始多行文本两种形式）。"""
    if not raw:
        return []
    parts = []
    for chunk in re.split(r"\s\|\s|\n", raw):
        s = chunk.strip()
        if not s or s.startswith("Simple mixer control") or s.startswith("numid="):
            continue
        if s.startswith(";"):
            continue
        parts.append(s)
    return parts


def read_playback_mixer(playback_device):
    """只读打印耳机口混音控制的当前值（诊断用，不改动任何设置）。

    逐声道打印：立体声控制会同时列出 Front Left / Front Right。旧实现只打印第一
    行，"右声道被静音/归零"这种导致"只有一边有声音"的情况完全看不见。
    """
    if shutil.which("amixer") is None:
        print("  （缺少 amixer，无法读取混音状态）")
        return
    card = _playback_card(playback_device)
    found = False
    for ctl, _want in PLAYBACK_MIXER_CONTROLS:
        cur = mixer_control_value(card, ctl)
        if cur is None:
            continue
        found = True
        print(f"  {ctl} = {cur}")
    for ctl in GENERIC_PLAYBACK_CONTROLS + list(HAT_MIXER_EXTRA_CONTROLS):
        cur = mixer_control_value(card, ctl)
        if cur is None:
            continue
        found = True
        print(f"  {ctl} = {cur}")
    if not found:
        names = list_mixer_control_names(card)
        print(f"  声卡 {card} 可用的混音控制: {', '.join(names) if names else '（读不到）'}")


def dump_all_mixer_controls(playback_device, limit=40):
    """把该声卡上**全部**混音控制连同各声道当前值打出来（--hp-test 用）。

    排查"只有一个耳朵有声音/没有声音"时，需要看到完整状态：某个控制只报了单声道
    （说明驱动把它当单声道控制）、某一路是 off 或 0%，都会直接指向原因。
    只读，不改任何设置。
    """
    if shutil.which("amixer") is None:
        return
    card = _playback_card(playback_device)
    names = list_mixer_control_names(card)
    print("-- 声卡 %s 的全部混音控制（只读；共 %d 个）--" % (card, len(names)))
    if not names:
        print("  （读不到控制名：amixer -c %s scontrols）" % card)
        return
    for ctl in names[:limit]:
        raw = amixer_get(card, ctl) or ""
        values = [
            ln.strip() for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith(("Simple mixer control", "numid="))
            and not ln.strip().startswith(";")
        ]
        if not values:
            print("  %s = ?" % ctl)
            continue
        print("  %s:" % ctl)
        for v in values:
            print("      %s" % v)
    if len(names) > limit:
        print("  …（还有 %d 个未打印）" % (len(names) - limit))



# HAT 兼容模式下额外打开的开关：把两路耳机输出都接到有信号的那一路 DAC。
# tlv320aic3x 的 HP 输出混音器是"两路 DAC → 两路 HP"的交叉矩阵；这台 HAT 的
# 放音实际上是单声道通路（I2S/LRCLK 由 codec 产生，只有一个 DAC 通道在送
# 数据），只开直连那一侧时**另一个耳机就完全没声音**——"插在 3.5mm 孔上的
# 耳机只有一边有声音"就是它。打开交叉那两路后两个耳塞都能出声。
HAT_MIXER_EXTRA_CONTROLS = (
    "Left HP Mixer DACR1",
    "Right HP Mixer DACL1",
    "Left Line Mixer DACR1",
    "Right Line Mixer DACL1",
)

# 本程序设置过的耳机口混音（(控制名, 目标值)），供混音看门狗比对：
# 声卡驱动在开麦克风/重开流时常把混音重置回驱动默认值（默认可能是静音或
# 只开一侧），这就是"开麦克风后耳机没声音"的另一半原因，所以要能发现并重设
_HP_MIXER_APPLIED = []
# HAT 兼容模式下耳机口实际采用的输出格式 [采样率, 声道数]（decide_hat_mode 自检后填）
_HAT_OUT = [SAMPLE_RATE, 1]


def _mixer_state_token(raw):
    """把混音控制的原始读数归一化成可比较的短标记（逐声道，"on"/"80"）。

    多声道控制（"Front Left: ... [80%] [on] | Front Right: ... [0%] [off]"）会
    得到逐声道的合并标记——右声道被关掉/归零必须能被看出来，否则"耳机只有一边
    有声音"这类问题查不到。
    """
    if raw is None:
        return None
    toks = []
    for part in mixer_control_channels(raw):
        if "%" in part:
            pcts = re.findall(r"(\d+)%", part)
            toks.append(",".join(sorted(set(pcts))) if pcts else part.strip())
            continue
        states = [t for t in re.split(r"[,\s]+", part.strip().lower())
                  if t in ("on", "off")]
        if states:
            toks.append(",".join(sorted(set(states))))
            continue
        toks.append(part.strip())
    return " | ".join(toks)


def _mixer_is_silent(token):
    """这个混音读数里是不是**有任何一个声道**被静音（off 或 0 音量）。

    只要有"一个 off / 一个 0"就算——那正是"只有一个耳朵有声音"的软件形态
    （旧实现只看第一行，右声道被归零完全看不见）。看门狗据此重设混音。
    """
    if token is None or token == "":
        return True
    for part in token.split("|"):
        vals = [v.strip() for v in part.split(",") if v.strip() != ""]
        if any(v in ("off", "0") for v in vals):
            return True
    return False


def apply_playback_mixer(playback_device, cross_route=False, quiet=False):
    """打开耳机口的播放通路并设置音量（wm8960 专用控制 + 通用播放控制）。

    wm8960（reSpeaker HAT）：Playback/Headphone/Speaker 音量 + 左右输出混音器。
    其它声卡（板载 bcm2835 Headphones、tlv320aic3x 等 HAT、USB 声卡）：按
    GENERIC_PLAYBACK_CONTROLS 里存在的名字处理——音量控制设 HEADPHONE_VOLUME
    并取消静音（逐声道 `80%,80%`），开关控制直接打开（输出路由开关没开，
    耳机插着也不出声）。控制项不存在就跳过，失败只提示，不影响麦克风链路。

    cross_route=True 时额外打开 HAT_MIXER_EXTRA_CONTROLS（HP 交叉路由，让两个
    耳塞都有声——该卡放音数据只在一路 DAC 上，只开直连那侧时另一个耳塞没声）。
    它和"是否统一下行采样率"是两件事，所以单独一个参数，默认在麦克风与耳机口
    同卡时打开（见 main）。
    所有设置过的控制都会记进 _HP_MIXER_APPLIED 供混音看门狗复查。
    """
    if shutil.which("amixer") is None:
        return
    card = _playback_card(playback_device)
    done, failed = [], []
    controls = list(PLAYBACK_MIXER_CONTROLS) + [
        (name, HEADPHONE_VOLUME) for name in GENERIC_PLAYBACK_CONTROLS
    ]
    if cross_route and HEADPHONE_CROSS_ROUTE:
        controls += [(name, "on") for name in HAT_MIXER_EXTRA_CONTROLS]
    seen = set()
    applied = []
    for ctl, value in controls:
        if ctl in seen:
            continue
        seen.add(ctl)
        cur = mixer_control_value(card, ctl)
        if cur is None:
            continue
        # 开关类判断：带百分比的一定是音量（有些驱动的音量行后面也带 [on] 静音
        # 标志，所以先看百分比再看 on/off）；否则按 on/off 词判定
        is_switch = ("%" not in cur) and bool(
            re.search(r"\[(on|off)\]|\bon\b|\boff\b", cur.lower())
        )
        want = "on" if is_switch else value
        ok = False
        # 立体声控制要**逐声道**设：只写一个值在部分 amixer 版本上只作用于第一个
        # 声道，"右声道保持 0%/off"就是这么来的（听着就是只有一边有声音）
        if not is_switch:
            for form in ("%s,%s" % (want, want), want):
                if amixer_set(card, ctl, form):
                    ok = True
                    break
            if ok:
                amixer_set(card, ctl, "unmute")
        else:
            ok = amixer_set(card, ctl, want)
        if ok:
            done.append("%s=%s" % (ctl, want))
            applied.append((ctl, want))
        else:
            failed.append(ctl)
    if done and not quiet:
        print("  -> 耳机口混音已设置: %s" % ", ".join(done))
    if failed and not quiet:
        print(
            "  !! 以下混音控制设置失败: %s（可用 amixer -c %s scontrols 查看控制名）"
            % (", ".join(failed), card)
        )
    if not done and not failed and not quiet:
        names = list_mixer_control_names(card)
        print(
            "  -> 声卡 %s 上没找到已知的播放混音控制: %s"
            % (card, ", ".join(names) if names else "（读不到控制名）")
        )
        print("     若耳机无声，可用 alsamixer -c %s 手动调音量/取消静音" % card)
    _HP_MIXER_APPLIED[:] = applied
    return applied


def hp_mixer_drift(playback_device):
    """比对当前混音与 apply_playback_mixer 设过的值，返回"变静音了"的改动列表。

    只报"我们设过、现在却变成关掉/音量为 0"的控制——声卡驱动在开采集流/重配
    codec 时会把混音重置回默认（默认往往是静音或只开一侧），表现就是"本来有
    声音、一开麦克风就没声"。返回值只用于日志和自动重设；空列表 = 混音正常。
    音量从 80% 被调成 50% 这类不算（不影响能否出声，不值得反复重设）。
    """
    if shutil.which("amixer") is None or not _HP_MIXER_APPLIED:
        return []
    card = _playback_card(playback_device)
    drift = []
    for ctl, want in _HP_MIXER_APPLIED:
        cur = mixer_control_value(card, ctl)
        if cur is None:
            continue
        tok = _mixer_state_token(cur)
        if _mixer_is_silent(tok) and not _mixer_is_silent(_mixer_state_token(want)):
            drift.append("%s: %s -> %s" % (ctl, want, cur.strip()))
    return drift


def same_sound_card(dev_a, dev_b):
    """两个 ALSA 设备名是否指向同一张声卡（hw:C,D / plughw:C,D / C,D）。"""
    return _playback_card(dev_a) == _playback_card(dev_b)


def decide_hat_mode(playback_device, mic_device):
    """同卡（麦克风与耳机口同一张声卡）时的共存策略，返回"是否统一下行采样率"。

    结论来自实测：这张卡上**44.1kHz 立体声放音是确定可闻的**（--hp-test 的测试音、
    以及麦克风关闭时的正常放音），而把下行强行改成 16kHz 单声道之后**反而彻底没声**
    （打开成功、aplay 不报错、数据也在流，就是不出声）。所以默认不再动下行采样率，
    只做加法（HP 交叉路由 + 混音看门狗），把两个开关留给实测决定：
      HEADPHONE_HAT_MODE=1  → 下行统一到采集采样率（16kHz 单声道，重采样）
      HEADPHONE_CAPTURE_RATE=44100 → 反过来让麦克风按 44.1kHz 采集、上行重采样到 16kHz

    返回 True 表示"下行需要统一到 _HAT_OUT"（只有显式 =1 才会）。
    """
    if not playback_device or not mic_device:
        return False
    if not same_sound_card(playback_device, mic_device):
        print("  -> 麦克风（%s）与耳机口（%s）不在同一张声卡：两个方向不共享时钟，"
              "互不影响" % (mic_device, playback_device))
        return False
    print("  !! 麦克风与耳机口是同一张声卡（%s）：收发共用 codec 的一路 I2S 时钟，"
          "两个方向只能同一个采样率" % playback_device)
    print("     麦克风走 HFP 固定 %d Hz，A2DP 音乐是 44100/48000 Hz —— 同时使用时"
          "会互相挤" % SAMPLE_RATE)
    if MIC_CAPTURE_RATE != SAMPLE_RATE:
        print("  -> 已设 HEADPHONE_CAPTURE_RATE=%d：麦克风按该采样率采集，上行链路"
              "内置重采样到 %d Hz" % (MIC_CAPTURE_RATE, SAMPLE_RATE))
        print("     （放音保持原生采样率：两个方向同率，且放音是实测可闻的配置）")
        return False
    opt = HEADPHONE_HAT_MODE
    if opt in ("1", "on", "true", "yes"):
        tested = []
        for rate, ch, label in ((SAMPLE_RATE, 1, "单声道"), (SAMPLE_RATE, 2, "立体声")):
            ok, _err = test_alsa_playback(playback_device, rate=rate, channels=ch)
            tested.append("%d Hz %s %s" % (rate, label, "可用" if ok else "不可用"))
            if ok:
                _HAT_OUT[:] = [rate, ch]
                print("  -> 同卡兼容模式（HEADPHONE_HAT_MODE=%s）：耳机下行统一为 "
                      "%d Hz %s" % (opt, rate, label))
                print("     注意：本卡 %d Hz 放音实测可能无声（打开成功但听不到），"
                      "若没声请改回 auto 并试 HEADPHONE_CAPTURE_RATE=44100"
                      % SAMPLE_RATE)
                print("     自检：%s" % "；".join(tested))
                return True
        print("  !! 耳机口不支持 %d Hz 放音，无法统一到采集采样率；自检：%s"
              % (SAMPLE_RATE, "；".join(tested)))
        return False
    print("  -> 默认策略（HEADPHONE_HAT_MODE=auto）：放音保持原生采样率（实测可闻），")
    print("     不为了让路而牺牲放音；并行使用时若被时钟冲突挤掉，日志会说明。")
    print("     两个可选对策（各试一次，用 --hp-test 听有没有声音）：")
    print("       HEADPHONE_HAT_MODE=1             下行降到 16kHz 单声道（本卡可能无声）")
    print("       HEADPHONE_CAPTURE_RATE=44100     麦克风按 44.1kHz 采集 + 上行重采样")
    print("     最稳的做法仍是让两个方向用不同声卡：耳机插树莓派板载 3.5mm 或 USB")
    print("     声卡，并 PLAYBACK_DEVICE=plughw:C,D 指定它（跨声卡没有时钟冲突）")
    return False


def playable_bluealsa_pcms(device):
    """本机 MAC 当前可播放（下行）的 BlueALSA PCM 名。

    A2DP（PROFILE=a2dp）与 SCO 下行（PROFILE=sco，即 Hands-Free 放音）都会
    列出；bluealsa-aplay -L 与 bluealsa-cli list-pcms 互相兜底。
    """
    keys = (device.lower(), device.lower().replace(":", ""))
    found = []
    for cmd in ("bluealsa-aplay -L", "bluealsa-cli list-pcms"):
        for s in _collect_bluealsa_lines(cmd):
            low = s.lower()
            if any(k in low for k in keys):
                found.append(s)
    seen, unique = set(), []
    for c in found:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def pcm_profile_of(pcm_name):
    """从 PCM 名判断下行来源: "a2dp" | "sco" | ""。"""
    low = (pcm_name or "").lower()
    if "a2dp" in low:
        return "a2dp"
    if "sco" in low or "hfp" in low or "hsp" in low:
        return "sco"
    return ""


def a2dp_stream_active(device):
    """A2DP 下行是否已建立（电脑正把树莓派当耳机/扬声器输出）。

    只放音乐、不给麦克风的电脑不该被"等不到麦克风 PCM 就强制断开"的逻辑
    掐断（见 find_working_pcm），所以那里会先问这个函数。bluealsa-aplay -L
    与 bluealsa-cli 的 D-Bus 对象路径（a2dpsnk/source = 本机接收的 A2DP 流）
    两个来源都查，bluealsa-aplay 缺失时也能判断。
    """
    if any(pcm_profile_of(p) == "a2dp" for p in playable_bluealsa_pcms(device)):
        return True
    if not shutil.which("bluealsa-cli"):
        return False
    res = run("bluealsa-cli list-pcms", check=False, timeout=8, verbose=False)
    if not res or res.returncode != 0:
        return False
    mac = device.replace(":", "_").lower()
    for line in (res.stdout or "").splitlines():
        low = line.strip().lower()
        if f"dev_{mac}" in low and "a2dp" in low and low.endswith("/source"):
            return True
    return False


def bluealsa_source_pcm_path(device, profile):
    """返回指定 profile 的"下行"（可播放）PCM 的 D-Bus 路径。

    下行 = 本机读取的流：A2DP 接收是 a2dpsnk/source；HFP/HSP 是 hfphf/source
    或 hsphs/source。profile 传 "a2dp"/"sco" 区分。找不到返回 None。
    """
    if not shutil.which("bluealsa-cli"):
        return None
    res = run("bluealsa-cli list-pcms", check=False, timeout=8, verbose=False)
    if not res or res.returncode != 0:
        return None
    mac = device.replace(":", "_").lower()
    want_a2dp = profile == "a2dp"
    for line in (res.stdout or "").splitlines():
        low = line.strip().lower()
        if f"dev_{mac}" not in low or not low.endswith("/source"):
            continue
        if want_a2dp != ("a2dp" in low):
            continue
        return line.strip()
    return None


def bluealsa_pcm_volume_note(device, profile):
    """下行 PCM 的音量与 SoftVolume 摘要（诊断用，取不到返回空串）。

    A2DP 默认开软音量：样本按 PCM 自身音量缩放，音量 0/读不到时"有流也无
    声音"（见 ensure_a2dp_audible），所以诊断里一并打出来。
    """
    path = bluealsa_source_pcm_path(device, profile)
    if not path:
        return ""
    vol = read_bluealsa_volume(path)
    soft = read_bluealsa_soft_volume(path)
    note = "  音量=%s" % ("读不到" if vol is None else "%d/127" % vol)
    note += "  SoftVolume=%s" % _soft_volume_text(soft)
    if soft is True and (vol is None or vol == 0):
        note += "  !! 软音量会把样本缩放到静音（服务运行时会自动抬起）"
    return note


def read_bluealsa_volume(pcm_path):
    """读 BlueALSA PCM 的当前音量（0~127），读不到返回 None。"""
    res = run(f"bluealsa-cli volume {pcm_path}", check=False, timeout=8, verbose=False)
    if not res or res.returncode != 0:
        return None
    m = re.search(r"Volume:\s*(\d+)", (res.stdout or "").strip())
    return int(m.group(1)) if m else None


def read_bluealsa_soft_volume(pcm_path):
    """读 PCM 的 SoftVolume 状态，返回 True/False/None（读不到）。"""
    res = run(
        f"dbus-send --system --print-reply --dest=org.bluealsa {pcm_path} "
        "org.freedesktop.DBus.Properties.Get "
        "string:org.bluealsa.PCM1 string:SoftVolume",
        check=False,
        timeout=8,
        verbose=False,
    )
    if not res or "boolean" not in (res.stdout or ""):
        return None
    return "boolean true" in res.stdout


def _soft_volume_text(value):
    return {True: "true", False: "false", None: "读不到"}[value]


def bluealsa_pcm_format(pcm_path, profile):
    """读下行 PCM 的原生采样率/声道，返回 (rate, channels)；读不到返回 (None, None)。

    用 bluealsa-cli info 的 Rate/Channels 字段；不同版本字段可能缺失，
    所以返回 None 由调用方决定默认值（A2DP 惯例 44100/2、SCO 是 16k 单声道）。
    """
    res = run(
        f"bluealsa-cli info {pcm_path}", check=False, timeout=8, verbose=False
    )
    text = ((res.stdout or "") + "\n" + (res.stderr or "")) if res else ""
    m = re.search(r"Rate:\s*(\d+)", text)
    rate = int(m.group(1)) if m else None
    m = re.search(r"Channels:\s*(\d+)", text)
    channels = int(m.group(1)) if m else None
    return rate, channels


def probe_downlink_params(pcm_name, profile):
    """实测下行 PCM 可用的采样率/声道，返回 (rate, channels, 失败原因)。

    A2DP 的实际采样率由电脑协商决定（44.1k 或 48k），取不到元数据时逐个试。
    """
    cands = [(44100, 2), (48000, 2)] if profile == "a2dp" else [(SAMPLE_RATE, 1)]
    err = ""
    for rate, channels in cands:
        cmd = (
            f"timeout 4 arecord -t raw -D '{pcm_name}' -f {FORMAT} -r {rate} "
            f"-c {channels} -d 1 /dev/null 2>&1"
        )
        res = run(cmd, check=False, timeout=7, verbose=False)
        if res and res.returncode in (0, 124):
            return rate, channels, ""
        text = ((res.stdout or "") + " " + (res.stderr or "")) if res else ""
        err = " ".join(text.split())[-200:]
    return None, None, err


# 下行电平表：原样转发音频，同时每 5 秒向 stderr 报一次峰值。
# 为什么需要它：耳机"先是能出声、后来没声"时，光看日志分不清是
#   ① BlueALSA 根本没有数据/全是 0（音量被缩放到 0、电脑停止推流、链路卡住）
#   ② 有正常数据但耳机口不出声（混音/声卡问题）
# 有这一行日志就能立刻区分，不用再猜。
DOWNLINK_METER_SCRIPT_CONTENT = r"""
import array
import math
import sys
import time

RATE = __RATE__
CHANNELS = __CHANNELS__
REPORT_SECS = 5.0
HEARTBEAT_SECS = __HEARTBEAT__
STRIDE = 16   # 每 16 个采样取一个算峰值，省 CPU（5 秒窗口足够发现静音）
SILENCE_DB = -60.0   # 低于这个电平基本就是数字静音（被缩放到 0 的表现）
LOW_DB = -25.0       # 低于这个电平算明显偏小


def classify(peak):
    if not peak:
        return "silence", None
    db = 20.0 * math.log10(peak / 32768.0)
    if db < SILENCE_DB:
        return "silence", db
    if db < LOW_DB:
        return "low", db
    return "ok", db


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    peak = 0
    last = time.time()
    last_beat = last
    total = 0             # 累计转发字节数（心跳里打印：不涨就是链路卡住了）
    state = None          # 上一次报的电平档位，档位变化时立刻报

    def report(force=False):
        # 报出"上一次报告以来"的峰值；档位变化时立刻报，另外每 HEARTBEAT_SECS 秒
        # 强制报一次心跳——没有心跳时"链路一直有数据但耳机没声"和"链路卡死了"
        # 在日志里长得一模一样（上一轮排查就卡在这里）
        nonlocal peak, last, state, total
        cur, db = classify(peak)
        if cur != state or force:
            if cur == "silence":
                print("[downlink] %d Hz %d ch 全静音（峰值 %s dBFS，已转发 %d 字节）："
                      "链路里几乎没有音频幅度——音频在进入本机之前就被缩放到 0，"
                      "或电脑侧音量为 0/被静音"
                      % (RATE, CHANNELS, "%.1f" % db if db is not None else "-inf",
                         total),
                      file=sys.stderr, flush=True)
            elif cur == "low":
                print("[downlink] %d Hz %d ch 电平明显偏小（峰值 %.1f dBFS，已转发 "
                      "%d 字节）" % (RATE, CHANNELS, db, total),
                      file=sys.stderr, flush=True)
            else:
                print("[downlink] %d Hz %d ch 峰值 %.1f dBFS（正常，已转发 %d 字节）"
                      % (RATE, CHANNELS, db, total), file=sys.stderr, flush=True)
            state = cur
        peak = 0
        last = time.time()

    while True:
        try:
            data = stdin.read1(8192)
        except (AttributeError, OSError):
            data = stdin.read(8192)
        if not data:
            report(force=True)    # 收尾也报一次（能看到最后是静音还是有声）
            break
        stdout.write(data)
        stdout.flush()
        total += len(data)
        n = len(data) // 2
        samples = array.array("h")
        samples.frombytes(data[: n * 2])
        for i in range(0, n, STRIDE):
            v = samples[i]
            if v < 0:
                v = -v
            if v > peak:
                peak = v
        now = time.time()
        if now - last >= REPORT_SECS:
            report()
        elif now - last_beat >= HEARTBEAT_SECS:
            last_beat = now
            report(force=True)


if __name__ == "__main__":
    main()
"""


def write_downlink_meter_script(path, rate, channels):
    """写出下行电平表脚本（与降噪/增益脚本同一套做法：写到 /tmp 再运行）。"""
    # 心跳间隔 <=0 视为关闭（写成很大的数），避免每读一块就报一次
    heartbeat = HP_METER_HEARTBEAT_SECS if HP_METER_HEARTBEAT_SECS > 0 else 1e9
    path.write_text(
        DOWNLINK_METER_SCRIPT_CONTENT.replace("__RATE__", str(rate)).replace(
            "__CHANNELS__", str(channels)
        ).replace("__HEARTBEAT__", repr(float(heartbeat)))
    )


# 下行"统一采样率 + 单声道"阶段（HAT 兼容模式用）：
#   A2DP 是 44.1/48kHz 立体声，麦克风是 16kHz——而这台 HAT 的收发共用 codec 的
#   一路 I2S 时钟（见 HEADPHONE_HAT_MODE 的说明），两个采样率不可能同时跑。
#   所以把下行降到与采集相同的采样率、并折成单声道：
#     1. 左右平均（该卡放音本来就是单声道通路，立体声在这里没有意义）；
#     2. 抗混叠低通（窗函数 sinc FIR，截止 0.45*输出采样率）——不先低通就直接
#        抽点会把 8kHz 以上的音乐内容折回可听频段，听感是刺啦的噪声；
#     3. 线性插值重采样到目标采样率（44.1k→16k 不是整数比，插值最简单可靠）。
#   滤波器状态在块之间传递，所以接缝处是连续的（不会在块边界产生咔哒声）。
#   numpy 不可用时退化为"平均 + 抽点"（能出声，质量差一点）。
DOWNLINK_MIX_SCRIPT_CONTENT = r"""
import array
import sys

IN_RATE = __IN_RATE__
IN_CH = __IN_CH__
OUT_RATE = __OUT_RATE__
OUT_CH = __OUT_CH__          # 1 = 单声道（交给 plughw 展开到两声道输出）
TAPS = 127
READ_BYTES = 16384           # 有多少读多少：读满大块会额外引入延迟


def fallback_loop(step):
    # 无 numpy 的退化路径：左右平均 + 等间隔抽点（不做低通）
    in_frames = 0
    carry = array.array("h")
    while True:
        raw = sys.stdin.buffer.read1(READ_BYTES)
        if not raw:
            break
        if len(raw) % 2:
            raw = raw[:-1]
        samples = array.array("h")
        samples.frombytes(raw)
        if carry:
            samples = carry + samples
            carry = array.array("h")
        frames = len(samples) // IN_CH
        out = array.array("h")
        for i in range(frames):
            base = i * IN_CH
            tot = 0
            for c in range(IN_CH):
                tot += samples[base + c]
            if in_frames % step == 0:
                out.append(tot // IN_CH)
            in_frames += 1
        if out:
            sys.stdout.buffer.write(out.tobytes())
            sys.stdout.buffer.flush()


def main():
    try:
        import numpy as np
    except ImportError:
        print("[hp-mix] 没有 numpy，退化为抽点重采样", file=sys.stderr, flush=True)
        fallback_loop(max(1, int(round(IN_RATE / float(OUT_RATE)))))
        return

    ratio = IN_RATE / float(OUT_RATE)
    if ratio == 1.0:
        # 采样率本来就一致（只做降混）：不用滤波，1 抽头恒等
        taps = 1
        h = np.ones(1, dtype=np.float64)
    else:
        # 抗混叠低通：截止取输出奈奎斯特的 0.45（下手重一点，避免折返噪声）
        taps = TAPS
        fc = 0.45 * min(OUT_RATE, IN_RATE) / float(IN_RATE)
        n = np.arange(taps) - (taps - 1) / 2.0
        h = 2.0 * fc * np.sinc(2.0 * fc * n) * np.hamming(taps)
        h /= h.sum()

    tail = np.zeros(taps - 1, dtype=np.float64)   # 上一块尾部（跨块卷积连续）
    buf = np.zeros(0, dtype=np.float64)           # 已低通、还没输出的样本
    pos = 0.0                                      # 下一个输出点在小数位置
    while True:
        raw = sys.stdin.buffer.read1(READ_BYTES)
        if not raw:
            break
        frames = len(raw) // (2 * IN_CH)
        if frames <= 0:
            continue
        x = np.frombuffer(raw[: frames * 2 * IN_CH], dtype="<i2")
        x = x.reshape(-1, IN_CH).astype(np.float64)
        if IN_CH > 1:
            x = x.mean(axis=1)                    # 左右平均（含降混）
        if taps > 1:
            cat = np.concatenate([tail, x])
            y = np.convolve(cat, h, mode="valid")   # 与 x 等长
            tail = cat[-(taps - 1):]
            buf = np.concatenate([buf, y])
        else:
            buf = x

        avail = len(buf) - 2                      # 需要右邻点 buf[i0+1] 做插值
        if avail <= pos:
            continue
        count = int((avail - pos) / ratio) + 1
        idx = pos + ratio * np.arange(count)
        idx = idx[idx <= avail]
        if not len(idx):
            continue
        i0 = np.floor(idx).astype(np.int64)
        frac = idx - i0
        out = buf[i0] * (1.0 - frac) + buf[i0 + 1] * frac
        pos = float(idx[-1]) + ratio
        drop = int(pos)
        if drop > 0:
            buf = buf[drop:]
            pos -= drop
        emit = np.clip(np.rint(out), -32768, 32767).astype("<i2")
        if OUT_CH > 1:
            emit = np.repeat(emit, OUT_CH)
        sys.stdout.buffer.write(emit.tobytes())
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
"""


def write_downlink_mix_script(path, in_rate, in_channels, out_rate, out_channels):
    """写出下行"统一到采集采样率 + 单声道"脚本。"""
    path.write_text(
        DOWNLINK_MIX_SCRIPT_CONTENT.replace("__IN_RATE__", str(in_rate))
        .replace("__IN_CH__", str(in_channels))
        .replace("__OUT_RATE__", str(out_rate))
        .replace("__OUT_CH__", str(out_channels))
    )


def build_downlink_player(device, playback_device, python=None, prefer=None,
                          hat_mode=False):
    """构造"电脑下行音频 → 耳机口"的播放管道，返回 dict（无下行 PCM 时 None）。

    返回 {"pcm","profile","rate","channels","out_rate","out_channels",
          "stages":[(label, cmd), ...]}，stages 是 arecord [→ 混音/重采样] →
    [电平表] → aplay 的链条。prefer 指定优先通路："a2dp"（默认）/ "sco"
    （Hands-Free 下行）；指定的那条不在就自动用另一条。hat_mode=True 时把
    下行统一到采集的采样率与声道数（见 HEADPHONE_HAT_MODE 的说明：这台 HAT
    收发共用 codec 的 I2S 时钟，两个采样率不能同时跑）。

    为什么不用 bluealsa-aplay：它的命令行选项在各版本间差异极大——实测某个
    版本根本没有指定 ALSA 输出设备的选项，照它的老写法传 -d 会直接
    `invalid option -- 'd'` 立即退出（耳机因此一直没声音）。改用
    `arecord <BlueALSA 下行 PCM> | aplay -D <耳机口>`：读 PCM 与写声卡都用
    最标准的参数，和麦克风上行链路同源，装了 alsa-utils 就能跑。

    中间插入一个纯 Python 电平表（有 python 时）：它原样转发音频，同时每
    5 秒打印一次峰值，用来区分"链路里根本没数据/全静音"和"有数据但耳机没声"
    ——这正是之前一直无法判断的地方。
    """
    pcm_list = playable_bluealsa_pcms(device)
    found = {}
    for p in pcm_list:
        prof = pcm_profile_of(p)
        if prof and prof not in found:
            found[prof] = p
    order = ["sco", "a2dp"] if prefer == "sco" else ["a2dp", "sco"]
    profile = pcm_name = None
    for prof in order:
        if prof in found:
            profile, pcm_name = prof, found[prof]
            break
    if profile is None:
        return None
    rate, channels = None, None
    if profile == "a2dp":
        path = bluealsa_source_pcm_path(device, "a2dp")
        if path:
            rate, channels = bluealsa_pcm_format(path, "a2dp")
        if not rate:
            rate, channels, err = probe_downlink_params(pcm_name, "a2dp")
            if not rate:
                # 只在错误内容变化时提示，避免每 3 秒刷屏
                if err and err != _LAST_DOWNLINK_ERR[0]:
                    _LAST_DOWNLINK_ERR[0] = err
                    print("  !! 打不开 A2DP 下行 PCM（44.1k/48k 都试过）：%s" % err)
                    print("     电脑开始播放后会自动重试")
                return None
        if not channels:
            channels = 2
    else:
        rate, channels = SAMPLE_RATE, 1
    # 输出格式：HAT 兼容模式下与采集一致（见 decide_hat_mode 自检出的格式），
    # 否则与下行原生一致。混音阶段能否落地要先定下来，再据此写 aplay 的参数
    # （写不出脚本就必须放弃统一采样率，否则 aplay 会按错采样率播数据）
    out_rate, out_channels = rate, channels
    if hat_mode:
        out_rate, out_channels = _HAT_OUT[0], _HAT_OUT[1]
    mix_stage = None
    need_mix = python and (rate != out_rate or channels != out_channels)
    if need_mix:
        try:
            write_downlink_mix_script(
                DOWNLINK_MIX_SCRIPT, rate, channels, out_rate, out_channels
            )
            mix_stage = ("hp-mix", [python, str(DOWNLINK_MIX_SCRIPT)])
        except OSError as exc:
            print("  !! 无法写出下行混音脚本（%s）：本次不做采样率统一" % exc)
            out_rate, out_channels = rate, channels
    rec_cmd = [
        "arecord",
        "-t",
        "raw",
        "-D",
        pcm_name,
        "-f",
        FORMAT,
        "-r",
        str(rate),
        "-c",
        str(channels),
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
        playback_device,
        "-f",
        FORMAT,
        "-r",
        str(out_rate),
        "-c",
        str(out_channels),
        "--period-time",
        str(PERIOD_TIME_US),
        "--buffer-time",
        str(BUFFER_TIME_US),
    ]
    # 阶段名统一加 hp- 前缀：原先和麦克风上行管道一样叫 arecord/aplay，
    # 日志里两套管道混在一起（"aplay underrun"到底是谁），排查时白费功夫
    stages = [("hp-rec", rec_cmd)]
    if mix_stage is not None:
        stages.append(mix_stage)
    if python:
        try:
            write_downlink_meter_script(DOWNLINK_METER_SCRIPT, out_rate, out_channels)
            stages.append(("hp-meter", [python, str(DOWNLINK_METER_SCRIPT)]))
        except OSError:
            pass
    stages.append(("hp-play", play_cmd))
    return {
        "pcm": pcm_name,
        "profile": profile,
        "rate": rate,
        "channels": channels,
        "out_rate": out_rate,
        "out_channels": out_channels,
        "stages": stages,
    }


def start_downlink_player(player, lines):
    """把 stages（arecord → [电平表] → aplay）串成管道并启动，返回 [(label, proc)]。

    接线要点（都是踩过的坑）：
      * 中间环节的 stdout 是**音频数据管道**，只交给下一环，并且父进程要立刻
        close 掉自己那份写端；不能拿去当日志读（会吃掉音频，还会刷
        "readline of closed file"）
      * 中间环节的 stderr 必须单独接管道排水——绝不能 STDOUT，否则 arecord 的
        "Recording raw data ..."、overrun 警告会被当成 PCM 样本流进 aplay，
        耳机里就是一片很响的噪声（实测就是这个）
      * 最后一环的 stdout/stderr 才是日志
    """
    procs = []
    prev_stdout = None
    stages = player["stages"]
    for i, (label, cmd) in enumerate(stages):
        is_last = i == len(stages) - 1
        proc = subprocess.Popen(
            cmd,
            stdin=prev_stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if is_last else subprocess.PIPE,
        )
        if prev_stdout is not None:
            prev_stdout.close()  # 父进程释放写端，数据由下游进程直接读
        procs.append((label, proc))
        log_stream = proc.stdout if is_last else proc.stderr
        threading.Thread(
            target=drain_stream, args=(log_stream, label, lines), daemon=True
        ).start()
        prev_stdout = None if is_last else proc.stdout
    return procs


def stop_downlink_player(procs):
    """停掉下行播放管道的全部进程。procs 形如 [(label, proc)]。"""
    for _label, proc in procs or []:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    time.sleep(0.5)
    for _label, proc in procs or []:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass


def meter_bytes(lines):
    """从电平表日志里取最近一次的累计转发字节数（读不到返回 None）。

    有了它才能区分"通路在动、只是没声音"和"通路卡死了"：卡死时字节数不涨。
    电平表每 HP_METER_HEARTBEAT_SECS 秒报一次心跳，里面就带这个数。
    """
    for line in reversed(lines):
        m = re.search(r"已转发\s*(\d+)\s*字节", line)
        if m:
            return int(m.group(1))
    return None


def downlink_level_state(lines):
    """从播放器日志里取电平表最近一次报告，返回 "silence"/"low"/"ok"/None。

    电平表（下行管道中间那一环）每 5 秒报一次峰值，被本线程的排水线程收进
    lines；这里用来判断"电脑在放、耳机没声"时到底是音频在进入本机前就被缩放
    到 0（silence/low），还是链路里幅度正常（ok，问题在耳机口/混音）。
    """
    for line in reversed(lines):
        if "峰值" not in line:
            continue
        if "全静音" in line:
            return "silence"
        if "电平明显偏小" in line:
            return "low"
        return "ok"
    return None


def print_a2dp_silent_hint():
    """下行电平接近 0 时给出两侧的排查点（每会话只提示一次）。"""
    print("  !! 下行电平几乎是数字静音：音频在进入树莓派之前就被缩放到 ~0 了，")
    print("     问题不在耳机口/混音（本机测试音是好的），按下面顺序查：")
    print("     A. 电脑侧（最常见）：")
    print("        * 声音设置→输出→点开本设备，看它的音量滑块是不是被拉到很低；")
    print("        * 控制面板→声音→『通信』选项卡：默认会在检测到通信活动时")
    print("          『将其他声音的音量减少 80%』甚至静音——而本机同时是麦克风")
    print("          （通信设备），所以一开麦克风电脑就会把音乐压掉。改成")
    print("          『不执行任何操作』。")
    print("     B. 树莓派侧（A2DP 音量模型）：当前是 SoftVolume=false 且 bluealsa")
    print("        启动参数里没有 --a2dp-volume，属于未定义的半状态，可能按 AVRCP")
    print("        音量(0)缩放样本。两种正规做法选一个：")
    print("        * 让本机自己控音量：")
    print("          bluealsa-cli soft-volume <A2DP PCM 路径> true")
    print("          bluealsa-cli volume <A2DP PCM 路径> 127")
    print("        * 让电脑滑块控音量：给服务加 drop-in")
    print("          Environment=BLUEALSA_EXTRA_ARGS=--a2dp-volume")
    print("        （A2DP PCM 路径见 sudo python3 %s --audio-info 的输出）"
          % os.path.basename(__file__))


def force_local_a2dp_volume(device):
    """把 A2DP 下行切到"本机控音量"模式：SoftVolume=true + 音量 127。

    bluealsa(8)：A2DP 默认就是软音量（本机客户端控音量、不做 AVRCP 映射）。
    实测这台上处于"SoftVolume=false 且启动参数没有 --a2dp-volume"的半状态，
    这种状态下样本可能按 AVRCP 协商到的音量（可能为 0）缩放，表现就是
    "链路里全是数字静音"。所以实测到下行近乎静音时把状态摆正：
    软音量打开并把音量设到满幅（音量由本机混音/HEADPHONE_VOLUME 控制）。
    调用后靠下行电平表验证效果（有变化就是这里的问题）。
    """
    path = bluealsa_source_pcm_path(device, "a2dp")
    if not path:
        return False
    print("  -> 把 A2DP 下行切到本机控音量：soft-volume true + volume %d" % A2DP_MIN_VOLUME)
    run(f"bluealsa-cli soft-volume {path} true", check=False, timeout=8, verbose=False)
    run(
        f"bluealsa-cli volume {path} {A2DP_MIN_VOLUME}",
        check=False,
        timeout=8,
        verbose=False,
    )
    soft = read_bluealsa_soft_volume(path)
    vol = read_bluealsa_volume(path)
    print("     现在: SoftVolume=%s 音量=%s（若耳机仍无声，看电平表：电平没变化"
          "就说明幅度是电脑侧压掉的）"
          % (_soft_volume_text(soft), "读不到" if vol is None else "%d/127" % vol))
    return True


A2DP_CHECK_SECS = 30  # 耳机输出线程检查"电脑有没有在用 A2DP 音乐通道"的间隔（秒）

# 下行通路健康检查（"这条通路上到底有没有音频数据在流"）：
#   DOWNLINK_CHECK_SECS = 检查间隔。原实现把这件事和 A2DP 检查绑在一起、30 秒
#     才看一次，而且判定"电平近乎静音"还要连着 3 次（≈90 秒）才动手——开麦克风
#     后耳机要哑一分钟以上，用户看到的就是"没声"。
#   DOWNLINK_STALL_SECS = 播放器起来后这么久仍然一个电平报告都没有 →
#     这条通路上根本没有数据（arecord 一直阻塞在 read 上）。这是"开麦克风后
#     耳机没声音"最典型的形态：电脑把输出切到了另一条通路，而这条 PCM 仍在
#     BlueALSA 列表里、也打得开，只是永远没有样本流过来——光看进程状态（都活着）
#     完全看不出问题，旧实现此时既不报日志也不做任何处理（这是本次修复的核心）
#   ROUTE_PROBE_SECS / ROUTE_PROBE_COOLDOWN = 切换通路前探活的读取时长与最小
#     间隔：只有"另一条通路真的读得到字节"才切，避免在两条死通路之间来回跳
DOWNLINK_CHECK_SECS = 10
DOWNLINK_STALL_SECS = 15
ROUTE_PROBE_SECS = 2
ROUTE_PROBE_COOLDOWN = 25
MAX_ROUTE_SWITCHES = 6

ROUTE_LABELS = {
    "a2dp": "A2DP 音乐通路（电脑里的『…  Stereo』，44.1/48kHz 立体声）",
    "sco": "Hands-Free 下行（电脑里的『…  Hands-Free AG Audio』，16kHz 单声道）",
}


def describe_route(profile):
    """下行通路的人话名字（日志/提示里用）。"""
    return ROUTE_LABELS.get(profile, profile or "未知通路")


def alternate_downlink_route(device, cur_profile):
    """除当前通路外，另一条下行通路 (profile, pcm 名)；不存在则 (None, None)。

    电脑只往它**选中的**那条输出送音频，所以两条通路要能互相顶替：
    开麦克风后电脑常把输出从 Stereo 切到 Hands-Free（或反过来）。
    """
    for pcm in playable_bluealsa_pcms(device):
        prof = pcm_profile_of(pcm)
        if prof and prof != cur_profile:
            return prof, pcm
    return None, None


def probe_downlink_pcm_live(pcm_name, profile):
    """实测这条下行 PCM 现在是否真有音频数据流过来（读得到字节 = 有）。

    为什么要探活：**通路存在不等于通路在传音频**。电脑把输出切到另一条通路后，
    这条 PCM 依旧列在 bluealsa 里、arecord 也打得开，但 read 永远返回不了数据
    （A2DP 流被挂起时同理）。所以判据只能是"到底读到了多少字节"，靠进程是否
    活着、PCM 是否在列表里都判断不出来。

    A2DP 的采样率由电脑协商（44.1k/48k），两个都试；SCO 上行链路本身就在用
    16kHz 单声道（见 build_pipeline），下行同名 PCM 同样参数。
    读不到数据时 arecord 会一直阻塞，所以统一用 timeout 兜底。
    """
    cands = ((44100, 2), (48000, 2)) if profile == "a2dp" else ((SAMPLE_RATE, 1),)
    for rate, channels in cands:
        # 输出到 stdout 再数字节：不落盘、读到多少算多少（arecord 被 timeout
        # 掐掉时已经写出的部分仍然会被 wc 统计到）
        cmd = (
            f"timeout {ROUTE_PROBE_SECS + 3} arecord -t raw -D '{pcm_name}' "
            f"-f {FORMAT} -r {rate} -c {channels} -d {ROUTE_PROBE_SECS} "
            "--period-time %d --buffer-time %d - 2>/dev/null | wc -c"
            % (PERIOD_TIME_US, BUFFER_TIME_US)
        )
        res = run(cmd, check=False, timeout=ROUTE_PROBE_SECS + 8, verbose=False)
        if not res:
            continue
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if lines and lines[-1].isdigit() and int(lines[-1]) > 0:
            return True
    return False


def headphone_monitor(device, stop_event, playback_device=None, hat_mode=False,
                      cross_route=False, mic_device=None):
    """耳机输出线程：连接期间把电脑下行音频送到 3.5mm 耳机口。

    独立于麦克风链路（电脑只放音频、不开麦克风时也有声音）。播放方式是
    `arecord <BlueALSA 下行 PCM> | aplay -D <耳机口>`：有下行 PCM 时启动，
    PCM 消失或进程退出后自动重建；每 A2DP_CHECK_SECS 秒确认一次电脑有没有在
    用 A2DP 音乐通道，并顺手纠正 A2DP 的"软音量缩放到静音"（见
    ensure_a2dp_audible）。蓝牙断开或 stop_event 置位时退出。

    hat_mode=True（HEADPHONE_HAT_MODE=1）时把下行统一到采集采样率（见
    build_downlink_player）；cross_route=True（麦克风与耳机口同卡，默认开）
    时额外打开 HP 交叉路由，让两个耳塞都有声——两者独立。
    开麦克风前后还会复查耳机口混音有没有被声卡驱动改回默认
    （hp_mixer_drift / HP_MIXER_CHECK_SECS）。

    "输入音频时放不出音"的三道防线：
      1. 选好的这条通路上**完全没有数据**（DOWNLINK_STALL_SECS 秒读不到任何
         样本）→ 探活另一条通路，有数据就切过去。开麦克风后电脑把输出切到
         Hands-Free 通路时就是这种形态；
      2. 通路有数据但**电平近乎 0**（被缩放到静音）→ 先摆正 A2DP 音量模型，
         再探活另一条通路切换，都不行才打印电脑侧排查指引；
      3. 播放器**反复起不来**（PCM 打不开）→ 换另一条通路试，并且不再"放弃
         本轮"：只要蓝牙还连着就继续退避重试（旧实现在这里直接返回，结果是
         "开过一次麦克风之后耳机再也起不来"，直到下次重连才恢复）。
    """
    if not HEADPHONE_ENABLE:
        print("  -> HEADPHONE_ENABLE=0：跳过蓝牙耳机（扬声器）输出")
        return
    for tool in ("arecord", "aplay"):
        if shutil.which(tool) is None:
            print("  !! 缺少 %s（apt install alsa-utils），蓝牙耳机输出不可用" % tool)
            print("     麦克风功能不受影响")
            write_headphone_status(state="unavailable", reason="no " + tool)
            return
    if playback_device is None:
        playback_device, why = select_playback_device(mic_device)
        if playback_device is None:
            print(f"  !! 未找到耳机口播放设备（{why}）：蓝牙耳机功能跳过")
            write_headphone_status(state="unavailable", reason=why)
            return
        apply_playback_mixer(playback_device, cross_route=cross_route)

    print("  -> 蓝牙耳机输出已就绪（在电脑上选本机为输出设备即出声）")
    print("     播放方式: arecord <下行 PCM> | aplay -D %s%s"
          % (playback_device,
             "（HAT 兼容模式：下行统一为 %d Hz 单声道，与麦克风同率）"
             % SAMPLE_RATE if hat_mode else ""))
    write_headphone_status(
        state="running",
        playback_device=playback_device,
        player="hp-rec|hp-mix|hp-meter|hp-play" if hat_mode else "hp-rec|hp-meter|hp-play",
        hat_mode="yes（下行统一 %d Hz %d ch，与麦克风同率）"
                 % (_HAT_OUT[0], _HAT_OUT[1]) if hat_mode else "no",
    )

    python = get_denoise_python()
    procs = []
    lines = []
    restart = 0
    started = 0.0
    last_bt_check = 0.0
    a2dp_state = None
    # 首次检查要等一个间隔：刚连上时电脑还没把本机选为输出设备，立刻报
    # "没有音乐通道"是误报（修复提示只能给一次，别提前用掉）
    last_a2dp_check = time.time()
    no_a2dp_hint = False
    silent_hint_shown = False
    # 下行优先通路与"近乎静音/没有数据"的自动处理状态（详见检查块里的注释）
    prefer = HEADPHONE_PROFILE if HEADPHONE_PROFILE in ("a2dp", "sco") else "a2dp"
    cur_profile = None      # 当前播放器实际在用的下行通路
    silent_strikes = 0
    volume_fix_done = False
    route_switches = 0
    last_downlink_check = 0.0
    last_route_probe = 0.0
    stall_noted = False     # 当前这轮播放器"没有数据"是否已提示过（防刷屏）
    no_pcm_noted = False    # "本机没有下行 PCM"是否已提示过（防刷屏）
    probe_note = None       # 上一次探活结论（同样的结论只打一次，防刷屏）
    cap_noted = False       # "切换次数已达上限"是否已提示过
    last_mixer_check = 0.0
    uplink_seen = False     # 本轮是否已经历过"麦克风开始/结束"
    mixer_refix = 0         # 混音被改回后的重设次数（防止和驱动来回拉锯）
    mixer_giveup_noted = False
    prev_bytes = None       # 上一次检查时电平表报出的累计字节数（不涨=通路卡死）

    def switch_downlink_route(reason, target=None, force=False):
        """把下行切到另一条（或 target 指定的）通路，返回是否真的切了。

        默认先探活再切（force=True 跳过探活，用于"反复起不来"这类必须换一条
        试试的情况）：两条通路都只有"存在"而不"在传音频"时，盲目切换只会在
        两条死通路之间来回跳，日志刷屏且问题没解决。
        """
        nonlocal prefer, procs, silent_strikes, route_switches
        nonlocal stall_noted, last_route_probe, restart, probe_note, cap_noted
        prof = target
        pcm = None
        if prof is None:
            prof, pcm = alternate_downlink_route(device, cur_profile or prefer)
        if prof is None:
            print("  -> 另一条下行通路当前不存在，暂时无法切换（等电脑建立它）")
            return False
        if pcm is None:
            for cand in playable_bluealsa_pcms(device):
                if pcm_profile_of(cand) == prof:
                    pcm = cand
                    break
        if not force:
            if not pcm:
                print("  -> 另一条下行通路的 PCM 名取不到（列表工具没报出来），先不切")
                return False
            now = time.time()
            if now - last_route_probe < ROUTE_PROBE_COOLDOWN:
                return False      # 刚探过，别反复开关另一条通路的 PCM
            last_route_probe = now
            live = probe_downlink_pcm_live(pcm, prof)
            note = ("live" if live else "dead") + ":" + prof
            if note != probe_note:
                probe_note = note
                if live:
                    print("  -> 探活结果：%s 上读到了音频数据" % describe_route(prof))
                else:
                    print("  -> 探活结果：%s 上也读不到音频数据（电脑没往那条送音频），"
                          "先不切" % describe_route(prof))
            if not live:
                return False
        if route_switches >= MAX_ROUTE_SWITCHES:
            if not cap_noted:
                cap_noted = True
                print("  -> 已切换 %d 次，为避免来回跳不再自动切换"
                      "（可用 HEADPHONE_PROFILE 手工固定通路）" % route_switches)
            return False
        print("  -> 下行通路切换（%s）：%s → %s"
              % (reason, describe_route(cur_profile), describe_route(prof)))
        prefer = prof
        route_switches += 1
        silent_strikes = 0
        stall_noted = False
        restart = 0
        probe_note = None
        stop_downlink_player(procs)
        procs = []
        return True

    try:
        while not stop_event.is_set():
            if time.time() - last_bt_check >= 5:
                last_bt_check = time.time()
                if not is_device_connected(device):
                    print("  -> 耳机输出：蓝牙连接已断开，停止播放")
                    break
            # 每 A2DP_CHECK_SECS 秒确认一次"电脑有没有在用音乐通道"：状态变化
            # 打日志、写状态文件；一直没建立则给一次排查提示（见下）
            if time.time() - last_a2dp_check >= A2DP_CHECK_SECS:
                last_a2dp_check = time.time()
                check_script_updated()
                active = a2dp_stream_active(device)
                if a2dp_state is None:
                    a2dp_state = active
                    if active:
                        print("  -> 检测到 A2DP 音乐通道已建立（电脑正在传音频过来）")
                        ensure_a2dp_audible(device)
                    write_headphone_status(
                        state="running",
                        playback_device=playback_device,
                        a2dp_stream="yes" if active else "no",
                    )
                elif active != a2dp_state:
                    a2dp_state = active
                    print(
                        "  -> 电脑%s音乐通道（A2DP）"
                        % ("开始使用" if active else "停止了")
                    )
                    if active:
                        # 新建立的 A2DP 流可能带回 0/不可读的音量（软音量缩放
                        # 到静音），这里纠正一次，避免"链路全绿但没声音"
                        ensure_a2dp_audible(device)
                    write_headphone_status(
                        state="running",
                        playback_device=playback_device,
                        a2dp_stream="yes" if active else "no",
                    )
                if not active and not no_a2dp_hint and _A2DP_REGISTERED[0]:
                    no_a2dp_hint = True
                    print("  !! 电脑已连接，但还没有建立 A2DP 音乐通道：")
                    print("     Windows 里只有当它缓存了本机的 Audio Sink 服务时，")
                    print("     『声音设置→输出』才会出现『耳机 (设备名 Stereo)』。")
                    print("     若输出列表里只有麦克风/没有本设备，说明配对时的服务列表")
                    print("     是旧的：在 Windows 删除该设备后重新配对，或在树莓派执行")
                    print("     sudo python3 %s --forget 让电脑重新配对。" % os.path.basename(__file__))
                    print("     排查用: sudo python3 %s --audio-info" % os.path.basename(__file__))
            # 混音看门狗：开麦克风（采集流）时声卡驱动常把耳机口混音重置回
            # 默认（音量归零 / 输出路由关掉），那"一开麦克风耳机就没声"就是它。
            # 开麦克风前后各查一次，平时也定期复查，发现变静音立刻重设并说明
            active_now = _UPLINK_ACTIVE.is_set()
            if active_now != uplink_seen:
                uplink_seen = active_now
                if active_now:
                    print("  -> 麦克风上行已启动：复查耳机口混音有没有被声卡"
                          "驱动改回默认")
                last_mixer_check = 0.0      # 立刻检查一次
            if (playback_device and _HP_MIXER_APPLIED
                    and time.time() - last_mixer_check >= HP_MIXER_CHECK_SECS):
                last_mixer_check = time.time()
                drift = hp_mixer_drift(playback_device)
                if drift:
                    print("  !! 耳机口混音被声卡驱动改回默认（采集流打开/重配 "
                          "codec 时会这样，正是开麦克风后没声音的原因）：")
                    for line in drift[:8]:
                        print("     %s" % line)
                    if mixer_refix < 5:
                        mixer_refix += 1
                        apply_playback_mixer(playback_device,
                                             cross_route=cross_route, quiet=True)
                        print("  -> 已重新设置耳机口混音（第 %d 次）" % mixer_refix)
                    elif not mixer_giveup_noted:
                        mixer_giveup_noted = True
                        print("  !! 混音被反复改回（与驱动来回拉锯），不再自动重设。")
                        print("     可手工用 alsamixer -c %s 确认，或把耳机改插到"
                              "另一张声卡（PLAYBACK_DEVICE=plughw:C,D）"
                              % _playback_card(playback_device))
            # 下行通路健康检查（每 DOWNLINK_CHECK_SECS 秒）：先看这条通路上
            # 到底有没有数据在流，再看电平是否被缩放到近乎 0
            if procs and time.time() - last_downlink_check >= DOWNLINK_CHECK_SECS:
                last_downlink_check = time.time()
                uptime = time.time() - started
                got_data = any("峰值" in line for line in lines)
                level = downlink_level_state(lines) if got_data else None
                # 有没有在动：看电平表心跳里的累计字节数涨不涨（卡死时不涨）。
                # 上一轮排查的教训：按"有没有报告"判断会漏——电平表加了心跳之后
                # 卡死的通路照样有报告，只是字节数不动
                now_bytes = meter_bytes(lines)
                if now_bytes is None:
                    progressed = got_data           # 旧日志/无电平表：有报告就算在动
                else:
                    progressed = prev_bytes is None or now_bytes > prev_bytes
                prev_bytes = now_bytes
                if not progressed and uptime >= DOWNLINK_STALL_SECS:
                    # 播放器还活着（arecord/aplay 都没退出），却读不到新数据：
                    # 这条通路上没有音频，或者下游（声卡）不再消费了。后者正是
                    # "麦克风一开、放音就哑"的形态（同卡时钟冲突时 aplay 会一直
                    # 阻塞，字节数停在那一刻不再增长）
                    if not stall_noted:
                        stall_noted = True
                        print("  !! 耳机输出：%s 上没有新数据了（已运行 %.0f 秒，"
                              "累计字节 %s）"
                              % (describe_route(cur_profile), uptime,
                                 "未知" if now_bytes is None else now_bytes))
                        print("     通路建好了不等于电脑在往它送音频；也可能是下游"
                              "声卡不再消费（同卡采样率冲突）")
                        print("     正在探活另一条通路…")
                    if switch_downlink_route("当前通路没有新数据"):
                        continue
                elif level in ("silence", "low"):
                    silent_strikes += 1
                    print(
                        "  !! 下行电平%s（连续第 %d 次）：音频在进入耳机口之前"
                        "就被缩放到 ~0"
                        % ("全静音" if level == "silence" else "偏小", silent_strikes)
                    )
                    if silent_strikes == 2 and not volume_fix_done:
                        volume_fix_done = True
                        force_local_a2dp_volume(device)
                        print("     若电平随之变化 → 是树莓派侧的音量模型问题；")
                        print("     若电平毫无变化 → 幅度是电脑侧压掉的（见下面指引）")
                    elif silent_strikes >= 3:
                        # 先探活另一条通路：有数据就切过去（开麦克风后电脑常把
                        # 输出送到另一条）；另一条也没数据就打印电脑侧排查指引
                        if switch_downlink_route("本通路电平几乎为 0"):
                            continue
                        if not silent_hint_shown:
                            silent_hint_shown = True
                            print_a2dp_silent_hint()
                elif level == "ok":
                    if silent_strikes:
                        print("  -> 下行电平恢复正常（峰值已可见），不再算静音")
                    silent_strikes = 0
                    silent_hint_shown = False
            # 播放器管理：只在有下行 PCM 时启动，避免空转
            if not procs:
                player = build_downlink_player(
                    device, playback_device, python, prefer, hat_mode=hat_mode
                )
                if player is None:
                    if not no_pcm_noted:
                        no_pcm_noted = True
                        print("  -> 耳机输出：本机当前没有可用的下行 PCM（电脑还没把")
                        print("     本机选为输出设备，或还没建立 A2DP/Hands-Free 通路）")
                    restart = 0
                    stop_event.wait(3)
                    continue
                lines = []
                procs = start_downlink_player(player, lines)
                started = time.time()
                cur_profile = player["profile"]
                stall_noted = False
                no_pcm_noted = False
                probe_note = None
                prev_bytes = None
                last_downlink_check = time.time()
                print(
                    "  -> 耳机输出播放器已启动：%s 下行，%d Hz %d ch，PID %s"
                    % (
                        player["profile"],
                        player["rate"],
                        player["channels"],
                        ",".join(str(p.pid) for _l, p in procs),
                    )
                )
                for label, cmd in player["stages"]:
                    print("     %-8s %s" % (label + ":", " ".join(cmd)))
                stop_event.wait(2)
                continue
            dead = [
                (label, proc) for label, proc in procs if proc.poll() is not None
            ]
            if not dead:
                if time.time() - started >= 30:
                    restart = 0  # 已稳定运行过，重置失败计数
                stop_event.wait(2)
                continue
            print(
                "  !! 耳机输出播放器退出（%d/%d 个进程：%s）"
                % (len(dead), len(procs), ",".join(label for label, _p in dead))
            )
            for line in lines[-8:]:
                print("    [player] %s" % line)
            stop_downlink_player(procs)
            procs = []
            if not is_device_connected(device):
                # 播放器随蓝牙断开而死：这时候才值得打印 BlueZ 的断开原因
                print("  -> 耳机输出：蓝牙连接已断开，停止播放")
                log_bt_disconnect_reason()
                break
            restart += 1
            # 同一条通路反复起不来（PCM 打不开/立刻退出）时，先换另一条试试：
            # 常见于电脑刚把输出切走、这条通路的 PCM 已经失效或正被重协商
            if restart >= 2 and switch_downlink_route("播放器反复启动失败", force=True):
                restart = 0
                continue
            if restart > MAX_RESTARTS:
                # 不再"放弃本轮"：旧实现直接 return，而耳机输出线程一退出，
                # 本轮会话就再也没人重启播放器了——"开过一次麦克风之后耳机
                # 再也起不来、只能重连"就是这么来的。只要蓝牙还连着就退避重试
                print("  !! 耳机输出连续启动失败 %d 次：30 秒后重试"
                      "（不放弃本轮，麦克风功能不受影响）" % restart)
                write_headphone_status(state="retrying", playback_device=playback_device)
                restart = 0
                stop_event.wait(30)
                continue
            time.sleep(3)
    finally:
        stop_downlink_player(procs)
        write_headphone_status(state="stopped", playback_device=playback_device)



def ensure_a2dp_audible(device):
    """检查 A2DP 下行是不是"被缩放到静音"的状态，返回说明文本（无 A2DP 流时 None）。

    只做**只读检查 + 一种明确的修复**：音量能读到且为 0 时把它抬到
    A2DP_MIN_VOLUME（默认 127）。音量读不到时不再去动 SoftVolume——实测
    "流正在播放时改 PCM 的 SoftVolume/音量属性"会让已建立的 A2DP 流变成
    静音（进程还在、就是没声音），所以这里只提示、不折腾：
    要手工试的开关都写在 --audio-info 的输出里。

    BlueALSA 对 A2DP 默认开 SoftVolume：样本按 PCM 自身音量缩放，音量 0 或
    读不到时整条流可能被缩放到静音（bluealsa(8)：只有带 --a2dp-volume 启动
    时才默认关闭软音量并改用原生 AVRCP 音量）。
    """
    path = bluealsa_source_pcm_path(device, "a2dp")
    if not path:
        return None
    vol = read_bluealsa_volume(path)
    soft = read_bluealsa_soft_volume(path)
    vol_text = "读不到" if vol is None else "%d/127" % vol
    note = "volume=%s softvol=%s" % (vol_text, _soft_volume_text(soft))
    if vol is None:
        print("  -> A2DP 下行音量属性读不到（SoftVolume=%s）" % _soft_volume_text(soft))
        print("     不去改动正在播放的流（实测改软音量会让流变静音）；")
        print("     若确实无声，可手工对照 --audio-info 里给出的命令尝试")
        return note
    if vol > 0:
        print("  -> A2DP 下行音量=%s SoftVolume=%s（正常）"
              % (vol_text, _soft_volume_text(soft)))
        return note
    print("  !! A2DP 下行音量=0：整条流会被缩放到静音，正在抬到 %d" % A2DP_MIN_VOLUME)
    res = run(
        f"bluealsa-cli volume {path} {A2DP_MIN_VOLUME}",
        check=False,
        timeout=8,
        verbose=False,
    )
    vol2 = read_bluealsa_volume(path)
    if res and res.returncode == 0 and vol2:
        print("     已设为 %d/127（音量此后由本机混音控制）" % vol2)
        return "volume=%d/127 softvol=%s" % (vol2, _soft_volume_text(soft))
    print("     设置未生效（音量仍读不到）；不再改动 SoftVolume，避免打断正在播放的流")
    return note


def make_test_tone_raw(seconds=2.0, freq=1000.0, rate=44100, channels=2, side=None):
    """生成 1kHz 正弦 S16_LE 原始样本（头尾各 30ms 淡入淡出，避免爆音）。

    side="l" / "r" 时另一个声道写静音——"耳机只有一边有声音"时用它分清是
    "声卡只有一路在响"还是"另一边根本没接"。
    """
    n = int(seconds * rate)
    fade = max(1, int(0.03 * rate))
    frames = bytearray()
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i > n - fade:
            env = (n - i) / fade
        v = int(20000 * env * math.sin(2.0 * math.pi * freq * i / rate))
        for ch in range(channels):
            if (side == "l" and ch == 1) or (side == "r" and ch == 0):
                frames += b"\x00\x00"
            else:
                frames += int(v).to_bytes(2, "little", signed=True)
    return bytes(frames)


def write_test_tone_wav(path, seconds=2.0, freq=1000.0, rate=44100, channels=2,
                        side=None):
    """生成 1kHz 测试音（WAV）。"""
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(make_test_tone_raw(seconds, freq, rate, channels, side))


def play_raw_measured(dev, rate, channels, seconds=1.5, path=None, label=""):
    """放一段 1kHz 测试音并**测量**客观指标，返回 dict。

    测什么、为什么：
      * rc        = aplay 退出码（打不开/中途死掉会非 0）
      * underrun  = aplay 日志里 "underrun" 出现次数。**这是判断"硬件实际跑的
                    采样率跟我们声明的是否一致"的关键**：声明 44.1k 而 codec 的
                    LRCLK 被采集流改成 16k 时，硬件消费速度对不上，aplay 会持续
                    报 underrun（上一轮日志里那些 "underrun 10073 ms" 就是它）
      * elapsed   = 实际耗时（秒）。2 秒的测试音若耗时明显偏离 2 秒，说明硬件
                    不是按声明的采样率在消费
      * err       = 失败原因（截断）
    """
    path = path or "/tmp/bt_hp_test.raw"
    with open(path, "wb") as f:
        f.write(make_test_tone_raw(seconds, 1000.0, rate, channels))
    cmd = (
        f"timeout {int(seconds) + 8} aplay -t raw -D '{dev}' -f {FORMAT} "
        f"-r {rate} -c {channels} '{path}' 2>&1"
    )
    t0 = time.time()
    res = run(cmd, check=False, timeout=seconds + 14, verbose=False)
    elapsed = time.time() - t0
    text = ((res.stdout or "") + "\n" + (res.stderr or "")) if res else ""
    underruns = text.lower().count("underrun")
    err = " ".join(text.split())
    return {
        "label": label or ("%d Hz %d ch" % (rate, channels)),
        "rate": rate,
        "channels": channels,
        "rc": None if res is None else res.returncode,
        "underrun": underruns,
        "elapsed": elapsed,
        "err": "" if (res is not None and res.returncode == 0) else err[-200:],
    }


def start_capture_for_test(dev, rate, channels):
    """起一个自限时的录音进程（用 -d 让它自己结束，避免留下占卡的残留进程）。

    上一版的坑：用 shell 包 timeout 启动、再 terminate 只杀掉 shell，真正的
    arecord 还在后台占着采集设备，后面每一轮 "边录边放" 的录音都直接 EBUSY，
    测出来的结论完全是错的。
    """
    cmd = [
        "arecord", "-t", "raw", "-D", dev, "-f", FORMAT,
        "-r", str(rate), "-c", str(channels), "-d", "30",
        "--period-time", str(PERIOD_TIME_US), "--buffer-time", str(BUFFER_TIME_US),
        "/dev/null",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def stop_capture(proc):
    """停掉测试用的录音进程并回收（确保设备真的被释放）。"""
    if proc is None:
        return ""
    out = ""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                out = (proc.communicate(timeout=5)[0] or b"").decode(errors="replace")
            except subprocess.TimeoutExpired:
                proc.kill()
                out = (proc.communicate(timeout=5)[0] or b"").decode(errors="replace")
        else:
            out = (proc.stdout.read() or b"").decode(errors="replace")
    except OSError:
        pass
    return out


def headphone_test():
    """--hp-test：耳机口分步自检（定位"只有一边有声音"和"开麦克风就没声"）。

    产出两类信息：
      **客观部分（不需要耳朵）**：每种"采集+放音"组合的 aplay 退出码、underrun
        次数、实际耗时；以及各采样率下的采集电平。硬件是否真的按声明的采样率
        在消费、麦克风是否采到数字静音，都能从这里看出来。
      **主观部分（需要耳朵）**：左/右/双声道分别放 1kHz——用来判断"只有一个
        耳朵有声音"是软件路由（本程序会用降混 + 交叉路由修）还是该卡的另一个
        声道根本没接线（硬件，只能换声卡/加转接）。
    """
    print_status("耳机口分步自检（--hp-test）")
    print("  提示：本自检要独占声卡。若 bt-mic 服务正在运行且设备已连接，先执行")
    print("        sudo systemctl stop bt-mic 再跑本命令（测完 systemctl start）——")
    print("        否则服务的采集/放音流会和这里的测试互相争用声卡，结果不准")
    if shutil.which("aplay") is None or shutil.which("arecord") is None:
        print("  !! 缺少 aplay/arecord（apt install alsa-utils）")
        return
    mic_device, mic_channels = select_and_verify_mic()
    dev, why = select_playback_device(mic_device)
    if dev is None:
        print(f"  !! 未找到可用的播放设备：{why}")
        return
    print(f"  -> 播放设备: {dev}（{why}）")
    # 第 0 步：换过耳机插孔之后"没声音"最常见的原因就是声音还送给原来那张卡。
    # 先把所有播放声卡列出来（标出与麦克风同卡的），需要的话直接逐卡试听并保存
    list_playback_cards(mic_device)
    print("  若耳机已经插到另一个孔上（HAT 3.5mm ↔ 树莓派板载 3.5mm），")
    print("  当前选中的 %s 就不是耳机所在的卡。" % dev)
    try:
        ans = input("  要逐张声卡放测试音、确认耳机插在哪张吗？（y/N）: ").strip().lower()
    except EOFError:
        ans = "n"
        print("  （非交互环境：跳过逐卡试听；可单独跑 sudo python3 %s --find-output）"
              % os.path.basename(__file__))
    if ans.startswith("y"):
        find_output_device()
        return
    dump_all_mixer_controls(dev)

    if mic_device:
        print(f"  -> 麦克风: {mic_device}（{mic_channels} 通道，采集 {MIC_CAPTURE_RATE} Hz）")
        same = same_sound_card(dev, mic_device)
        print("  -> 麦克风与耳机口%s"
              % ("**在同一张声卡上**（收发共用 I2S 时钟，只能同一个采样率）" if same
                 else "是两张不同的声卡（互不影响）"))
    hat_mode = decide_hat_mode(dev, mic_device) if mic_device else False
    cross_route = bool(mic_device) and same_sound_card(dev, mic_device)
    print("-- 按上面的判断设置耳机口混音（含交叉路由）--")
    apply_playback_mixer(dev, cross_route=cross_route)

    print_status("第 1 步：左/右/双声道各放一遍 1kHz —— 请仔细听哪边响")
    tone = "/tmp/bt_hp_test.wav"
    for side, label, tip in (
        ("l", "只有左声道有信号", "应该只有左耳响"),
        ("r", "只有右声道有信号", "应该只有右耳响"),
        (None, "左右声道都有信号", "应该两个耳朵都响"),
    ):
        try:
            write_test_tone_wav(tone, 2.0, 1000.0, 44100, 2, side=side)
        except OSError as exc:
            print(f"  !! 生成测试音失败（{exc}）")
            return
        print(f"  -> 【请听】{label}（{tip}）...")
        res = run(f"aplay -D '{dev}' '{tone}'", check=False, timeout=25, verbose=False)
        if res is not None and res.returncode != 0:
            err = " ".join(((res.stdout or "") + (res.stderr or "")).split())
            print(f"     !! aplay 失败: {err[-200:]}")
        time.sleep(0.6)
    print("  ★ 请记住这三步里各是哪边响（例如：三步都只有左耳响）")

    print_status("第 2 步：耳机口各格式的客观表现（无需耳朵）")
    print("  判据：underrun=0 且耗时≈放音时长 → 该格式硬件按声明的采样率在跑；")
    print("        耗时明显偏长 → 硬件实际采样率与我们声明的不一致（同卡时钟冲突）")
    results = []
    for rate, ch in ((44100, 2), (SAMPLE_RATE, 1), (48000, 2)):
        r = play_raw_measured(dev, rate, ch, seconds=2.0)
        results.append(r)
        print("  %5d Hz %d ch -> 退出码 %s，underrun %d 次，耗时 %.1fs（期望 2.0s）%s"
              % (rate, ch, r["rc"], r["underrun"], r["elapsed"],
                 "  " + r["err"] if r["err"] else ""))

    print_status("第 3 步：麦克风采集电平（各采样率，无需耳朵）")
    if not mic_device:
        print("  （没找到可用麦克风，跳过）")
    else:
        for cap_rate in (SAMPLE_RATE, 44100, 48000):
            lvl = measure_capture_level(mic_device, cap_rate, mic_channels, seconds=1)
            print("  采集 %5d Hz -> %s" % (cap_rate, lvl or "打不开/读不到"))
        print("  说明：能采到正常电平（不是数字静音）的采样率才能用；")
        print("        若 44100 Hz 可采，建议 HEADPHONE_CAPTURE_RATE=44100：")
        print("        放音保持 44.1kHz 立体声、麦克风同率采集，两个方向都不哑")

    print_status("第 4 步：边录边放（麦克风真在跑的时候放音）")
    print("  每轮先起录音（-d 30 自限时、结束时能确保释放设备），再放 2 秒测试音；")
    print("  录音侧报 'Device or resource busy' 说明采集打不开（设备被占），")
    print("  放音侧 underrun 变多或耗时变长说明两个方向在抢同一个时钟")
    if not mic_device:
        print("  （没找到可用麦克风，跳过）")
    else:
        for cap_rate in (SAMPLE_RATE, 44100):
            for rate, ch in ((rate, ch) for rate, ch in ((44100, 2), (SAMPLE_RATE, 1))):
                cap = start_capture_for_test(mic_device, cap_rate, mic_channels)
                time.sleep(1.5)
                alive = cap.poll() is None
                r = play_raw_measured(dev, rate, ch, seconds=2.0)
                cap_out = stop_capture(cap)
                cap_err = ""
                for line in cap_out.splitlines():
                    low = line.strip().lower()
                    if "busy" in low or "error" in low or "overrun" in low:
                        cap_err = "；录音侧: " + line.strip()[:110]
                        break
                print("  采集 %5d Hz + 放音 %5d Hz %d ch -> 放音 underrun %d 次，"
                      "耗时 %.1fs%s%s"
                      % (cap_rate, rate, ch, r["underrun"], r["elapsed"],
                         "" if alive else "；录音没能启动",
                         cap_err))
                time.sleep(0.5)
    print_status("怎么读结果 / 下一步")
    print("  1) 第 1 步里『只有左耳响』或『只有右耳响』且三步都一样 → 该卡另一个")
    print("     声道没接线（硬件）。软件已经把下行折成单声道并打开了 HP 交叉路由，")
    print("     仍然只响一边就只能换声卡：耳机插树莓派板载 3.5mm 或 USB 声卡，")
    print("     并用 PLAYBACK_DEVICE=plughw:C,D 指定（aplay -l 看声卡号）")
    print("  2) 第 4 步里『采集 16000 + 放音 44100』那行的 underrun 明显多于")
    print("     『采集 16000 + 放音 16000』，就证实了同卡采样率冲突。对策各试一次：")
    print("       HEADPHONE_CAPTURE_RATE=44100  （推荐：放音保住 44.1kHz 立体声）")
    print("       HEADPHONE_HAT_MODE=1          （下行降到 16kHz 单声道）")
    print("  3) 若第 2 步里 16kHz 那行 underrun=0 却听不到，而 44.1kHz 能听到")
    print("     → 本卡 16kHz 放音不可靠，务必用 HEADPHONE_CAPTURE_RATE 那条路")
    print("  4) 每个组合之间都会停顿，方便你分辨；日志里 hp-play 的 underrun 也")
    print("     可以事后对照（服务运行时阶段名是 hp-*，不会再和麦克风管道混淆）")


def list_playback_cards(mic_device=None):
    """打印所有播放声卡（plughw 全名 + 名称 + 是否与麦克风同卡），返回列表。

    换耳机插孔之后"没声音"最常见的原因就是：声音还送给原来那张卡，而耳机已经插到
    另一张卡的孔上了（本机有 HAT 的 3.5mm 和树莓派板载 3.5mm 两个孔）。
    """
    if shutil.which("aplay") is None:
        print("  !! 缺少 aplay（apt install alsa-utils）")
        return []
    mic_card = _playback_card(mic_device) if mic_device else None
    cards = probe_playback_devices()
    if not cards:
        print("  aplay -l 里没有任何播放设备：板载音频可能被关掉了")
        print("  要在板载 3.5mm 出声，确认 /boot/firmware/config.txt 有 dtparam=audio=on")
        return []
    print("-- 全部播放声卡（耳机插在哪张卡的孔上，声音就得送给哪张）--")
    for idx, (card, dev, label) in enumerate(cards, 1):
        cand = "plughw:%d,%d" % (card, dev)
        mark = "  [与麦克风同一张声卡]" if (mic_card and str(card) == mic_card) else ""
        print("  [%d] %-14s %s%s" % (idx, cand, label, mark))
    return cards


def find_output_device():
    """--find-output：逐张声卡放测试音，让你确认耳机插在哪张卡上，并写入配置。

    这是"换了耳机插孔/换了声卡以后没声音"最快的一步到位修法：不用猜、不用手工
    编辑 systemd 单元，听到哪张卡出声就把它记进 /etc/default/bt-mic 的
    PLAYBACK_DEVICE，服务重启后自动使用。
    """
    print_status("找出耳机插在哪张声卡上（--find-output）")
    print("  提示：先 sudo systemctl stop bt-mic，否则服务会占着声卡、测试音可能不正常")
    mic_device = None
    if os.environ.get("MIC_DEVICE"):
        mic_device = os.environ["MIC_DEVICE"]
    else:
        caps = probe_input_devices()
        if caps:
            mic_device = "plughw:%d,%d" % (caps[0][0], caps[0][1])
    if mic_device:
        print("  麦克风设备: %s（下面标出与它同卡的声卡）" % mic_device)
    cards = list_playback_cards(mic_device)
    if not cards:
        return
    try:
        write_test_tone_wav("/tmp/bt_find_output.wav", 2.5, 1000.0, 44100, 2)
    except OSError as exc:
        print("  !! 生成测试音失败（%s）" % exc)
        return
    chosen = None
    for idx, (card, dev, label) in enumerate(cards, 1):
        cand = "plughw:%d,%d" % (card, dev)
        ok, err = test_alsa_playback(cand)
        if not ok:
            print("  [%d] %s 打不开（%s），跳过" % (idx, cand, err))
            continue
        print("  [%d] 正在通过 %s（%s）放 2.5 秒 1kHz【请听】…" % (idx, cand, label))
        # 先把这张卡的音量拉起来/取消静音：混音为 0 或静音时用户会误判"不是这张卡"
        apply_playback_mixer(cand, quiet=True)
        run(f"aplay -D '{cand}' /tmp/bt_find_output.wav", check=False, timeout=25,
            verbose=False)
        try:
            ans = input("      听到了吗？（y=就是这张 / 回车=不是）: ").strip().lower()
        except EOFError:
            print("      （非交互环境，跳过询问：请记下能出声的卡，用 "
                  "PLAYBACK_DEVICE= 指定）")
            continue
        if ans.startswith("y"):
            chosen = cand
            break
    if chosen is None:
        print("  -> 没有确认到出声的声卡。可参考：")
        print("     * 耳机插在树莓派板载 3.5mm 孔 → 需要 dtparam=audio=on（config.txt）")
        print("     * 耳机插在 HAT 的 3.5mm 孔 → 那张卡是 seeed/respeaker/voicecard")
        print("     * 音量/静音：alsamixer -c <卡号> 里把 PCM 调大并取消静音")
        print("     * 完整诊断：sudo python3 %s --audio-info" % os.path.basename(__file__))
        return
    print("  -> 确定使用: %s（%s）" % (chosen, dict((f"plughw:{c},{d}", l)
                                                for c, d, l in cards).get(chosen, "")))
    ok, info = save_config_key("PLAYBACK_DEVICE", chosen)
    if ok:
        print("  -> 已写入配置 %s：PLAYBACK_DEVICE=%s" % (info, chosen))
        print("     生效：sudo systemctl restart bt-mic（或前台重跑本程序）")
        try:
            print("     当前内容：")
            print("\n".join("       " + l for l in
                            Path(info).read_text(encoding="utf-8").splitlines()))
        except OSError:
            pass
    else:
        print("  !! 写配置失败（%s）：请手工加一行" % info)
        print("     PLAYBACK_DEVICE=%s   到 %s" % (chosen, CONFIG_FILE))


def play_test_tone():
    """--play-test: 探测耳机口并播放 1kHz 测试音，验证 3.5mm 耳机口是否出声。

    只涉及本机声卡（不经蓝牙），用来区分"耳机口/混音没打开"和"蓝牙没传过来"。
    """
    print("=== 耳机口（3.5mm）测试 ===")
    caps = probe_input_devices()
    mic_hint = ("plughw:%d,%d" % (caps[0][0], caps[0][1])) if caps else None
    dev, why = select_playback_device(mic_hint)
    if dev is None:
        print(f"  !! 未找到可用的播放设备：{why}")
        return
    print(f"  -> 使用播放设备: {dev}（{why}）")
    # 该卡同时是麦克风所在声卡时（reSpeaker HAT 就是），把交叉路由也打开：
    # 与服务工作时的混音状态保持一致，两个耳机都能出声；细查用 --hp-test
    same_card_mic = any(
        str(card) == _playback_card(dev) for card, _d, _label in probe_input_devices()
    )
    apply_playback_mixer(dev, cross_route=same_card_mic)
    if same_card_mic:
        print("  -> 该声卡也是麦克风所在声卡：已打开 HP 交叉路由（两路输出都出声）；")
        print("     想确认左右声道/边录边放是否正常，跑 sudo python3 %s --hp-test"
              % os.path.basename(__file__))
    path = "/tmp/bt_headphone_test.wav"
    try:
        write_test_tone_wav(path)
    except OSError as exc:
        print(f"  !! 生成测试音失败（{exc}），请检查 /tmp 是否可写")
        return
    print(f"  -> 播放 2 秒 1kHz 测试音到 {dev}（请戴好/插好耳机）...")
    run(f"aplay -D '{dev}' '{path}'", check=False, timeout=20)
    print("  -> 测试结束：听到提示音说明耳机口与混音正常；")
    print("     无声请确认耳机插的是 reSpeaker HAT 的 3.5mm 孔、插紧，")
    print("     并可调高音量（HEADPHONE_VOLUME=100% 后重跑本命令）")


def print_audio_info():
    """--audio-info: 蓝牙"音频输出"（耳机/扬声器）全链路只读诊断。

    针对"电脑能连上、能被当麦克风，但不能设为音频输出设备"这类问题：
    电脑（Windows）只在配对那一刻缓存设备的服务列表，所以这里把树莓派
    实际注册的服务、bluealsa 参数、耳机口状态、当前下行 PCM 全部列出来，
    并给出结论与处理办法。
    """
    print("=== 蓝牙音频输出（耳机/扬声器）诊断 ===")
    print("  构建版本: %s" % BUILD_ID)
    print("  （若这里显示的版本比磁盘上的脚本旧，说明服务还在跑旧进程：")
    print("    执行 sudo systemctl restart bt-mic 后再诊断）")

    print("-- 蓝牙适配器 --")
    hci = get_hci_name()
    cls = name = powered = None
    show = run("bluetoothctl show", check=False, timeout=8, verbose=False)
    for line in ((show.stdout or "") if show else "").splitlines():
        s = line.strip()
        if s.startswith("Class:"):
            cls = s.split(":", 1)[1].strip()
        elif s.startswith("Name:"):
            name = s.split(":", 1)[1].strip()
        elif s.startswith("Powered:"):
            powered = s.split(":", 1)[1].strip()
    print(f"  {hci}: Name={name}  Powered={powered}  Class={cls}")
    print("  Class 应为 0x240404（音频设备 / 可穿戴耳机）：Windows 按它决定")
    print("  把树莓派归到哪一类设备，进而决定建哪些端点（麦克风 / 耳机输出）")

    print("-- BlueALSA 启动参数 --")
    exec_line = "(未找到 override 文件，bluealsa 可能用系统默认参数)"
    try:
        lines = Path(BLUEALSA_OVERRIDE).read_text(encoding="utf-8").strip().splitlines()
        exec_line = lines[-1] if lines else exec_line
    except OSError:
        pass
    print(f"  {exec_line}")
    has_a2dp_arg = "a2dp" in exec_line.lower()
    print(
        "  含 A2DP Sink: %s（缺了它电脑永远看不到『耳机/立体声』输出项）"
        % ("有" if has_a2dp_arg else "没有")
    )
    st = run("systemctl is-active bluealsa", check=False, timeout=8, verbose=False)
    print(f"  服务状态: {st.stdout.strip() if st else '?'}")
    print("  若 A2DP 注册失败，看: journalctl -u bluealsa -n 50 --no-pager")

    print("-- 本机 SDP 服务记录（电脑配对时看到的就是这些）--")
    names = sdp_local_service_names()
    a2dp_sdp = sdp_has_a2dp_sink(names)
    hfp_sdp = sdp_has_handsfree(names)
    if names is None:
        print("  （sdptool 不可用或查询失败，无法确认）")
    else:
        for n in names:
            print(f"  - {n}")
        print(
            "  -> Handsfree/Headset（当麦克风需要）: %s"
            % ("有" if hfp_sdp else "缺失")
        )
        print(
            "  -> Audio Sink / A2DP（当输出设备需要）: %s"
            % ("有" if a2dp_sdp else "缺失")
        )

    print("-- 全部声卡（aplay -l / arecord -l 原始输出）--")
    for cmd in ("aplay -l", "arecord -l"):
        print(f"  $ {cmd}")
        res = run(cmd, check=False, timeout=8, verbose=False)
        text = ((res.stdout or "") + (res.stderr or "")) if res else "（执行失败）"
        shown = False
        for line in text.splitlines():
            if line.strip():
                print(f"    {line.rstrip()}")
                shown = True
        if not shown:
            print("    （无输出）")
    pb = probe_playback_devices()
    cap = probe_input_devices()
    hat_pb = [d for d in pb if any(k in d[2].lower() for k in HAT_OUTPUT_HINTS)]
    hat_cap = [d for d in cap if any(k in d[2].lower() for k in HAT_OUTPUT_HINTS)]
    print(f"  播放设备 {len(pb)} 个（其中 reSpeaker/wm8960 声卡 {len(hat_pb)} 个）")
    print(f"  录音设备 {len(cap)} 个（其中 reSpeaker/wm8960 声卡 {len(hat_cap)} 个）")
    if not hat_pb and hat_cap:
        print("  !! reSpeaker 声卡只出现在录音列表里（没有播放设备）：该卡的播放")
        print("     通路没被驱动注册，HAT 的 3.5mm 口无法放音")
    elif not hat_pb and not hat_cap:
        print("  !! 没有任何 seeed/respeaker/wm8960 声卡：HAT 驱动未加载（或没接 HAT），")
        print("     HAT 上的 3.5mm 耳机口不会有声音——插在树莓派板载 3.5mm 口才有声音")

    print("-- 耳机口（输出设备）--")
    dev, why = select_playback_device(
        ("plughw:%d,%d" % (cap[0][0], cap[0][1])) if cap else None)
    # 采集设备名（用于判断"耳机口和麦克风是不是同一张声卡"——同卡时服务会把
    # 下行统一到采集采样率，见 HEADPHONE_HAT_MODE）
    mic_dev_hint = ""
    if cap:
        mic_dev_hint = "plughw:%d,%d" % (cap[0][0], cap[0][1])
    if dev:
        print(f"  播放设备: {dev}（{why}）")
        read_playback_mixer(dev)
        if mic_dev_hint and same_sound_card(dev, mic_dev_hint):
            print("  -> 耳机口与麦克风在同一张声卡上（收发共用 I2S 时钟，只能同一个")
            print("     采样率）：服务会启用同卡兼容模式，把耳机下行统一为 %d Hz %d ch"
                  % (_HAT_OUT[0], _HAT_OUT[1]))
            print("     两个方向都用这张卡时，A2DP 立体声会降到该采样率（音质换共存）")
        else:
            print("  -> 耳机口与麦克风不在同一张声卡：两个方向互不影响，无需兼容模式")
    else:
        print(f"  未找到可用的播放设备：{why}")

    print("-- 当前下行（可播放）PCM --")
    devices = get_connected_devices()
    a2dp_now = False
    if not devices:
        print("  （当前没有已连接的蓝牙设备）")
    else:
        pcm_list = playable_bluealsa_pcms(devices[0])
        a2dp_now = a2dp_stream_active(devices[0])
        if pcm_list:
            for p in pcm_list:
                prof = pcm_profile_of(p) or "?"
                print(f"  {p}    [{prof}]{bluealsa_pcm_volume_note(devices[0], prof)}")
                path = bluealsa_source_pcm_path(devices[0], prof)
                if not path:
                    continue
                rate, ch = bluealsa_pcm_format(path, prof)
                if not rate:
                    rate, ch = (44100, 2) if prof == "a2dp" else (SAMPLE_RATE, 1)
                if not ch:
                    ch = 2 if prof == "a2dp" else 1
                print(f"      D-Bus 路径: {path}")
                print("      手工复现（与程序内播放方式相同，验证 BlueALSA → 耳机口 这一段）:")
                print("        arecord -D '%s' -f %s -r %d -c %d -t raw | "
                      "aplay -D '%s' -f %s -r %d -c %d -t raw"
                      % (p, FORMAT, rate, ch, dev or "plughw:C,D", FORMAT, rate, ch))
                if dev and same_sound_card(dev, mic_dev_hint):
                    # 同卡（HAT）时服务会把下行统一到采集采样率，手工复现也要这么写，
                    # 否则会看到"服务里没声、手工却有"这种误导性的对比结果
                    print("      ↑ 但麦克风与耳机口在同一张声卡上（收发共用 I2S 时钟），")
                    print("        服务实际是把下行降到最后这个格式再送耳机口的：")
                    print("        arecord -D '%s' -f %s -r %d -c %d -t raw "
                          "| %s /tmp/bt_downlink_mix.py | aplay -D '%s' "
                          "-f %s -r %d -c %d -t raw"
                          % (p, FORMAT, rate, ch,
                             os.path.basename(get_denoise_python() or "python3"),
                             dev, FORMAT, _HAT_OUT[0], _HAT_OUT[1]))
                if prof == "a2dp":
                    print("      A2DP 音量模型（下行被缩放到 ~0 时按这里查）：")
                    print("        当前: SoftVolume=%s，bluealsa 启动参数%s --a2dp-volume"
                          % (
                              _soft_volume_text(read_bluealsa_soft_volume(path)),
                              "含" if "--a2dp-volume" in BLUEALSA_EXTRA_ARGS else "不含",
                          ))
                    print("        正规做法二选一（当前若是 false 且无 --a2dp-volume，")
                    print("        属于未定义的半状态，可能按 AVRCP 音量 0 缩放）：")
                    print("        1) 本机控音量(推荐):")
                    print("           bluealsa-cli soft-volume %s true" % path)
                    print("           bluealsa-cli volume %s %d" % (path, A2DP_MIN_VOLUME))
                    print("        2) 电脑滑块控音量: 给服务加 drop-in")
                    print("           Environment=BLUEALSA_EXTRA_ARGS=--a2dp-volume")
                    print("        电脑侧还要确认:（a）输出列表里本设备的音量滑块不在最低;")
                    print("        （b）控制面板→声音→『通信』选项卡改成『不执行任何操作』")
                    print("        ——默认设置会在检测到通信活动(我们在当麦克风)时把其他")
                    print("        声音降低 80% 甚至静音，表现就是『一开始有声后面没声』")
        else:
            print("  （没有可播放的 PCM：电脑还没把本机选为输出设备）")
        print(f"  已连接设备: {', '.join(devices)}")

    print("-- 耳机输出播放方式与服务状态 --")
    print("  播放方式: arecord <下行 PCM> | aplay -D <耳机口>（本程序内置）")
    print("  不用 bluealsa-aplay：它的选项在不同版本间差异极大，实测有的版本")
    print("  没有指定输出设备的选项，传 -d 会 `invalid option -- 'd'` 直接退出")
    res = run(
        "ps -ef | grep -E 'arecord .*bluealsa:|aplay -t raw -D plughw' | grep -v grep",
        check=False,
        timeout=8,
        verbose=False,
    )
    running = [l.strip() for l in ((res.stdout or "") if res else "").splitlines() if l.strip()]
    if running:
        print("  当前正在运行的下行/转发进程:")
        for line in running:
            print(f"    {line}")
    else:
        print("  当前没有下行播放进程 —— 电脑连接并由本程序启动后才会出现；")
        print("  若服务没在跑（systemctl status bt-mic），耳机不会出声")
    if os.path.exists(HEADPHONE_STATUS_FILE):
        print("  状态文件 %s:" % HEADPHONE_STATUS_FILE)
        try:
            for line in Path(HEADPHONE_STATUS_FILE).read_text(
                encoding="utf-8", errors="replace"
            ).strip().splitlines():
                print(f"    {line}")
        except OSError:
            pass
    res = run(
        "journalctl -u bt-mic -n 200 --no-pager 2>&1",
        check=False,
        timeout=10,
        verbose=False,
    )
    keys = ("耳机", "音乐通道", "bluealsa-aplay", "A2DP", "播放器退出")
    hits = [
        l.strip()
        for l in ((res.stdout or "") if res else "").splitlines()
        if any(k in l for k in keys)
    ]
    if hits:
        print("  bt-mic 服务日志里与耳机输出相关的最近记录:")
        for line in hits[-8:]:
            print(f"    {line}")

    print("-- 结论与处理 --")
    if not has_a2dp_arg:
        print("  1) bluealsa 没带 A2DP Sink 参数 → 电脑不可能出现音频输出项。")
        print("     确认 bluez-alsa 带 A2DP 支持（apt install bluez-alsa-utils），")
        print("     再重启服务让本程序重新注册: systemctl restart bt-mic")
    elif a2dp_sdp is False:
        print("  1) 本机 SDP 里没有 Audio Sink 记录 → A2DP 没注册成功，")
        print("     看 journalctl -u bluealsa 找原因（profile 名不被支持等）")
    else:
        print("  1) 本机 SDP 已有 Audio Sink 记录（树莓派这一侧没问题）")
    if a2dp_now:
        print("  2) 电脑正在使用 A2DP 音乐通道（蓝牙链路本身没问题）。听不到")
        print("     声音时按顺序查这三处：")
        print("     a) 上面【耳机口】一节：选到的播放设备是不是你插耳机的那个孔？")
        print("        板载口会显示 bcm2835 Headphones；HAT 口会显示 seeed/wm8960。")
        print("        选错孔（比如耳机插在 HAT 上、声音却送给板载口）就是静音。")
        print("     b) 跑 sudo python3 %s --play-test：本机直接放 1kHz 测试音。"
              % os.path.basename(__file__))
        print("        有声音 = 耳机口和混音正常，问题在蓝牙侧；没声音 = 孔或混音问题")
        print("     c) 上面 PCM 行里 A2DP 的『音量=0 / 读不到』：BlueALSA 对 A2DP")
        print("        默认开软音量，样本按该音量缩放，0 或读不到就是整条流静音")
        print("        （服务运行时会自动抬到 %d；手工复现命令已列在上面）" % A2DP_MIN_VOLUME)
    else:
        print("  2) 电脑还没建立 A2DP 音乐通道。Windows 只有加载了 A2DP（Audio")
        print("     Sink）才会给设备建音频输出端点，只加载 HFP 时就只显示麦克风：")
        print("       A. 重新配对（最有效）：本机执行")
        print("          sudo python3 %s --forget" % os.path.basename(__file__))
        print("          然后在电脑上点一下该设备重新连接")
        print("          或在 Windows: 设置→蓝牙和其他设备→删除该设备 → 重新添加")
        print("       B. Windows 侧还要确认这三项（都会让输出端点消失）：")
        print("          * 设置→蓝牙和其他设备→更多蓝牙选项：")
        print("            勾选『允许蓝牙设备播放音频』（取消后所有蓝牙音频输出被禁）")
        print("          * 控制面板→设备和打印机→右键本设备→属性→『服务』：")
        print("            勾上『音频接收器/Audio Sink』『远程控制』，")
        print("            取掉『免提电话』后断开重连（这一步常能直接逼出输出项）")
        print("          * services.msc：『Bluetooth Audio Gateway Service』")
        print("            (BTAGService) 需为『正在运行』；Win11 24H2 若开了")
        print("            『使用 LE 音频』请关掉")
        print("     重新配对后，『声音设置→输出』会出现两个本设备条目：")
        print("       『耳机 (设备名 Stereo)』       = A2DP，44.1/48kHz 立体声")
        print("       『耳机 (设备名 Hands-Free…)』  = HFP，16kHz 单声道")
        print("     找不到就打开传统面板看: Win+R → mmsys.cpl → 播放/录制")
        print("     （灰色或已禁用的设备右键可『启用』，也可先『显示已断开的设备』）")


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


RAW_PEAK_SCRIPT = r"""
# 读一段 S16_LE 原始录音，打印峰值（dBFS）与是否为数字静音
import array
import math
import sys

path = sys.argv[1]
try:
    with open(path, "rb") as f:
        raw = f.read()
except OSError:
    print("读不到 %s" % path)
    sys.exit(2)
n = len(raw) // 2
samples = array.array("h")
samples.frombytes(raw[: n * 2])
peak = 0
for i in range(0, n, 8):
    v = samples[i]
    if v < 0:
        v = -v
    if v > peak:
        peak = v
if not peak:
    print("数字静音（峰值 0）")
else:
    print("峰值 %.1f dBFS" % (20.0 * math.log10(peak / 32768.0)))
"""


def measure_capture_level(dev, rate=None, channels=1, seconds=1):
    """采一小段音频，返回峰值文本（如 "峰值 -28.4 dBFS"），失败返回 None。

    比"arecord 退出码为 0"有价值得多：设备能打开但采到的是数字静音时，
    退出码同样是 0——麦克风无声、选错设备、混音静音都表现为这种情况。
    """
    rate = rate or MIC_CAPTURE_RATE
    path = "/tmp/bt_mic_probe.raw"
    cmd = (
        f"timeout {seconds + 4} arecord -t raw -D '{dev}' -f {FORMAT} "
        f"-r {rate} -c {channels} -d {seconds} {path} 2>/dev/null || true"
    )
    run(cmd, check=False, timeout=seconds + 8, verbose=False)
    script = "/tmp/bt_raw_peak.py"
    try:
        Path(script).write_text(RAW_PEAK_SCRIPT)
    except OSError:
        return None
    res = run(f"python3 {script} {path}", check=False, timeout=20, verbose=False)
    if not res:
        return None
    text = " ".join((res.stdout or "").split())
    return text or None


def verify_mic(dev, channels=CHANNELS):
    """测试录音设备是否可用，并报告采集电平（能打开但全是静音也要提示）。"""
    rate = MIC_CAPTURE_RATE
    print(f"  测试录音设备 {dev}（{channels} 通道，{rate} Hz）...")
    cmd = (
        f"arecord -t raw -D '{dev}' -f {FORMAT} -r {rate} "
        f"-c {channels} -d 1 /dev/null 2>&1"
    )
    result = run(cmd, check=False, timeout=10, verbose=False)
    if result and result.returncode == 0:
        level = measure_capture_level(dev, rate, channels)
        if level and "数字静音" in level:
            print(f"  !! 麦克风 {dev} 能打开，但采到的是{level}：")
            print("     选错设备/输入被静音/该卡的采集通路有问题——检查 amixer 的")
            print("     采集侧开关（如 'Mic2L/Mic2R'、'PGA'）与麦克风是否接好")
        print(f"  -> 麦克风 {dev} 可用（{channels} 通道，{rate} Hz"
              f"{('，' + level) if level else ''}）")
        return True
    if result:
        err = (result.stdout or "").strip().splitlines()
        tail = err[-1] if err else "未知错误"
        print(f"  !! {dev}（{channels} 通道，{rate} Hz）不可用: {tail}")
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
    # 接缝拼接时本块主体必须取当前窗口（wins[-1][xf:unit]）：曾误取上一
    # 窗口 a[xf:unit]，导致每个块头 15ms 与上一块主体顺序错乱、块边界
    # 硬切——听感即 320ms 周期的爆破音。
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
                    wins[-1][xf:win_unit[0]],
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
    # 写输出阻塞超过该秒数 = 下游（aplay/SCO）停止消费，即一次停顿
    STALL_BLOCK = 0.5
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
    # 停顿恢复只裁一次：写输出阻塞 ≥STALL_BLOCK 后置位，见主循环裁剪处
    recover_once = False

    def trim_to(keep_bytes):
        # 把 pending 裁到只保留最后 keep_bytes（按采样帧对齐），接缝
        # 20ms 交叉淡化；返回丢弃的字节数（0 = 未裁剪）
        nonlocal pending, pending_bytes
        if pending_bytes <= keep_bytes:
            return 0
        drop = pending_bytes - keep_bytes
        drop -= drop % step
        xfade = min(drop, SR * 20 // 1000 * step)
        all_b = b"".join(pending)
        old_s = np.frombuffer(all_b[drop - xfade: drop],
                              dtype=np.int16).astype(np.float32)
        new_s = np.frombuffer(all_b[drop: drop + xfade],
                              dtype=np.int16).astype(np.float32)
        n = min(len(old_s), len(new_s))
        if n > 0:
            t = (np.arange(n) + 0.5) / n
            blend = (old_s[:n] * (1.0 - t) + new_s[:n] * t)
            blend = blend.astype(np.int16).tobytes()
            rest = all_b[drop + n * 2:]
            pending = [blend, rest] if rest else [blend]
        else:
            pending = [all_b[drop:]]
        pending_bytes = sum(len(b) for b in pending)
        return drop

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
        # 使延迟封顶在 MAX_BACKLOG_MS，而不是无限增长。
        # 接缝交叉淡化 20ms：直接硬切在环境噪声上会"咔哒"
        if recover_once and pending_bytes > chunk * step:
            # SCO 停顿恢复（实测教训见 gain 阶段注释，勿再加"恢复窗口
            # 反复裁剪+静音补空"）：写输出被下游阻塞 ≥STALL_BLOCK 秒说明
            # aplay/SCO 停止消费，停顿期间输入端攒下的是整段陈旧音频；
            # 恢复后若照常重放，用户会把停顿前/中说的话延迟 1~2s 再
            # 听到一遍（卡顿失真）。一次性裁到只留最近一块，之后实时
            # 到达的新音频完整透传，绝不重复裁剪；全程仅一处淡化接缝。
            # 只在真正裁掉积压时才清标志：停顿积压不足一块时不动它
            # （陈旧量小，正常处理即可），标志留到积压真的超出时再用
            recover_once = False
            dropped = trim_to(chunk * step)
            if dropped:
                log("SCO 停顿恢复，裁剪 %.0f ms 陈旧音频"
                    % (dropped / step * 1000.0 / SR))
        elif pending_bytes > max_backlog * step:
            dropped = trim_to(max_backlog * step)
            log("backlog 超限，丢弃 %.0f ms（跳音保低延迟）"
                % (dropped / step * 1000.0 / SR))

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
            w0 = time.time()
            sys.stdout.buffer.write(out.tobytes())
            sys.stdout.buffer.flush()
            # 写输出阻塞 = 下游（aplay/SCO）停止消费，即一次 SCO 停顿；
            # 恢复后置位 recover_once，主循环下一轮一次性裁剪陈旧积压
            if time.time() - w0 >= STALL_BLOCK:
                recover_once = True
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

        # 积压截断：丢弃最旧音频（按采样帧对齐），使延迟封顶。
        # 接缝交叉淡化 20ms：直接硬切在环境噪声上会"咔哒"
        if pending_bytes > max_backlog * step:
            dropped = pending_bytes - max_backlog * step
            dropped -= dropped % step
            log("backlog 超限，丢弃 %.0f ms（跳音保低延迟）"
                % (dropped * 1000.0 / (SR * step)))
            xfade = min(dropped, SR * 20 // 1000 * step)
            all_b = b"".join(pending)
            old_s = np.frombuffer(all_b[dropped - xfade: dropped],
                                  dtype=np.int16).astype(np.float32)
            new_s = np.frombuffer(all_b[dropped: dropped + xfade],
                                  dtype=np.int16).astype(np.float32)
            n = min(len(old_s), len(new_s))
            if n > 0:
                t = (np.arange(n) + 0.5) / n
                blend = (old_s[:n] * (1.0 - t) + new_s[:n] * t)
                blend = blend.astype(np.int16).tobytes()
                rest = all_b[dropped + n * 2:]
                pending = [blend, rest] if rest else [blend]
            else:
                pending = [all_b[dropped:]]
            pending_bytes = sum(len(b) for b in pending)

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
    except ValueError:
        # 管道已被关闭（进程退出/重启时父进程 close 了读端）：正常收尾即可，
        # 不要让线程抛异常刷日志
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def uplink_pre_denoise_channels(mic_channels):
    """上行链路里"进降噪之前"的声道数。

    插了 mic-rate 重采样阶段时它已经把数据降成单声道（该阶段顺带做左右平均），
    所以降噪/谱减脚本要按单声道准备；否则保持采集的声道数。
    """
    return 1 if MIC_CAPTURE_RATE != SAMPLE_RATE else mic_channels


def build_pipeline(mic_device, bluealsa_pcm, venv_python, mode, mic_channels=1):
    """mode: "df"=DeepFilterNet / "spec"=纯 numpy 谱减法 / None=不降噪"""
    out_channels = 1
    mix_channels = uplink_pre_denoise_channels(mic_channels)
    # 显式指定 ALSA period/buffer，避免默认大缓冲（数百毫秒）增加端到端延迟
    # 采集采样率：默认与 SCO 一致（16kHz）；HEADPHONE_CAPTURE_RATE 设成 44.1/48k 时
    # 按那个采样率采集（为了与耳机放音同率），随后由 mic-rate 阶段降回 16kHz
    rec_cmd = [
        "arecord",
        "-t",
        "raw",
        "-D",
        mic_device,
        "-f",
        FORMAT,
        "-r",
        str(MIC_CAPTURE_RATE),
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
    if MIC_CAPTURE_RATE != SAMPLE_RATE:
        # 高采样率采集时先降到 SCO 的采样率（重采样 + 左右平均，见
        # DOWNLINK_MIX_SCRIPT_CONTENT：同一套抗混叠重采样代码）
        write_downlink_mix_script(
            UPLINK_MIX_SCRIPT, MIC_CAPTURE_RATE, mic_channels, SAMPLE_RATE, 1
        )
        stages.append(("mic-rate", [venv_python, str(UPLINK_MIX_SCRIPT)]))
    if mode == "df":
        stages.append(("denoise", [venv_python, str(DENOISE_SCRIPT)]))
    elif mode == "spec":
        write_spec_script(SPEC_SCRIPT, mix_channels)
        stages.append(("specdenoise", [venv_python, str(SPEC_SCRIPT)]))
    elif mic_channels == 2 and mix_channels == 2:
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
        # 预热进程只在"还活着且 stdin 仍可用"时才能复用：上一轮 launch_pipeline
        # 已经把它的 stdin 交给上游进程并 close 掉，再拿它当 stdout 传进 Popen 会
        # 抛 ValueError: I/O operation on closed file，把主进程一起带崩
        # （实测：管道启动失败重试时，预热降噪进程已被用过 → 服务崩溃重启）
        if pre_proc is not None and (
            pre_proc.poll() is not None
            or pre_proc.stdin is None
            or pre_proc.stdin.closed
        ):
            pre_proc = None
        if pre_label in logs and pre_lines is not None:
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
        if pre_proc is not None and label == pre_label and not used_pre \
                and pre_proc.poll() is None:
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
        p = subprocess.Popen(
            cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE
        )
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
        write_spec_script(SPEC_SCRIPT, uplink_pre_denoise_channels(mic_channels))
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
#   5. SCO 链路会周期性停顿数秒（Windows/bluealsa 停止消费，日志表现为
#      aplay underrun 数千毫秒 + Pausing/Prepared 重协商循环）。不做
#      "停顿检测+恢复窗口裁剪+静音补空"：实测恢复窗口把实时到达的每个
#      320ms 突发裁到 40ms、空隙补静音，噪声↔静音边界是硬切，在环境
#      噪声里爆破声反而成倍增多。停顿期间积压的陈旧数据（denoise 输出
#      管道最多 64KB≈2s）在恢复时被非阻塞排空一次性读入，超过 800ms
#      积压上限即一次性丢最旧、留最近，只产生一处交叉淡化接缝；gain 与
#      降噪阶段的所有裁剪接缝一律 20ms 交叉淡化——有环境噪声时硬切点
#      就是爆破声


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
    # SCO 停顿恢复策略（实测教训，勿再加"停顿检测+恢复窗口裁剪+
    # 静音补空"）：恢复窗口会把实时到达的每个 320ms 突发裁到 40ms、
    # 空隙补静音——噪声↔静音边界是硬切，在环境噪声里爆破声反而
    # 成倍增多（22:09:31-35 实测：13 次"停顿恢复裁剪"+连续 underrun）。
    # 正确做法：停顿期间下游积压的陈旧数据在恢复时被非阻塞排空
    # 一次性读入（denoise 输出管道 64KB≈2s），超过 CAP 即由积压截断
    # 一次性丢最旧、留最近，只产生一处 20ms 交叉淡化的接缝；aplay
    # 自己管道里 ≤256ms（F_SETPIPE_SZ 已缩小）的陈旧重放是连续音频、
    # 没有拼接点，不会产生爆破声。短停顿整段晚点重放即可，比切一刀
    # 更顺滑；数据空隙不会让 aplay underrun——underrun 是 SCO 传输侧
    # （Windows 停止消费）造成的，补静音阻止不了它，只会加硬切点
    # 裁剪接缝交叉淡化：截断处新旧两侧各取 XFADE 个样本线性混合。
    # 直接硬切在静音里听不见、在环境噪声里就是"咔哒"——有噪音时的
    # 爆破声很大一部分来自这类拼接点
    XFADE = SAMPLE_RATE * 20 // 1000
    pending = []
    pending_bytes = 0

    # 裁剪 pending 只保留最后 keep_bytes，接缝处交叉淡化 XFADE 样本。
    # 返回被丢弃的字节数（0 = 未裁剪）。
    def do_trim(keep_bytes):
        nonlocal pending, pending_bytes
        if pending_bytes <= keep_bytes:
            return 0
        dropped = pending_bytes - keep_bytes
        all_b = b"".join(pending)
        cut = len(all_b) - keep_bytes
        old_s = array.array("h")
        old_s.frombytes(all_b[max(0, cut - XFADE * 2): cut])
        new_s = array.array("h")
        new_s.frombytes(all_b[cut: cut + XFADE * 2])
        n = min(len(old_s), len(new_s))
        blend = array.array("h")
        for k in range(n):
            t = (k + 0.5) / n
            blend.append(int(old_s[k] * (1.0 - t) + new_s[k] * t))
        rest = all_b[cut + n * 2:]
        pending = []
        if blend:
            pending.append(blend.tobytes())
        if rest:
            pending.append(rest)
        pending_bytes = sum(len(b) for b in pending)
        return dropped

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

        # 2) 积压截断：积压超过 CAP（SCO 真卡死/长停顿恢复时非阻塞
        #    排空一次性读入的陈旧数据）丢最旧、保留最近 KEEP，接缝
        #    交叉淡化。不做恢复窗口裁剪/静音补空：恢复窗口把实时
        #    音频裁成碎片、静音边界硬切，环境噪声下爆破声反而更多
        if pending_bytes > CAP:
            dropped = do_trim(KEEP)
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
    # sget/cget 两种写法都试：部分 amixer 版本不接受 name= 形式（见 amixer_get）
    cget = amixer_get(card, "PGA")
    if cget is None:
        return None
    cur = None
    for line in (cget or "").splitlines():
        line = line.strip()
        if line.startswith(": values="):
            cur = line.split("=", 1)[1].strip()
    print(f"  -> 模拟 PGA 增益（声卡 {card}）：{cur if cur else '未知'}"
          f"{'（可通过 MIC_PGA_GAIN 提升，如 20dB）' if not gain_spec else ''}")
    if not gain_spec:
        return cur
    # PGA 前的输入开关：麦克风一般接在 Mic2L/Mic2R，确保通路打开
    for sw in ("Left PGA Mixer Mic2L", "Right PGA Mixer Mic2R"):
        amixer_set(card, sw, "on")
    if amixer_set(card, "PGA", gain_spec):
        print(f"  -> 已设置 PGA = {gain_spec}")
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
                    db = 20 * math.log10(math.sqrt(level / 15.0)) if level > 0 else float("-inf")
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
        if (
            last_level is not None
            and not warned_no_change
            and time.time() - start > 20
        ):
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
    print("=== 耳机（扬声器）状态 ===")
    if os.path.exists(HEADPHONE_STATUS_FILE):
        try:
            print(Path(HEADPHONE_STATUS_FILE).read_text().strip())
        except OSError as exc:
            print("  读取失败: %s" % exc)
    else:
        print("  尚未记录：bt-mic 服务未运行，或耳机输出未启动")


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
    # 降噪阶段看到的是 mic-rate 重采样阶段之后的数据（那里的声道数可能是 1）
    mic_channels = uplink_pre_denoise_channels(mic_channels)
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
                        "  !! 基准进程退出码 %s，df 包信息（供诊断）："
                        % res.returncode
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
                print(
                    "  !! DeepFilterNet 各块大小 RTF 均超限，自动改用轻量谱减法降噪"
                )
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
        return build_pipeline(
            mic_device, bluealsa_pcm, venv_python, mode, mic_channels
        )

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
    # 通知耳机输出线程"麦克风开始/停止"：声卡驱动在打开采集流时可能把耳机口
    # 混音重置回默认，它据此立刻复查一次（见 headphone_monitor 的混音看门狗）
    _UPLINK_ACTIVE.set()
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
                        log_bt_disconnect_reason()
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
        _UPLINK_ACTIVE.clear()
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
    print(f"  -> 构建版本: {BUILD_ID}")
    run("systemctl daemon-reload", check=False, verbose=False)
    run("systemctl enable bt-mic.service", check=False, verbose=False)
    # 必须是 restart 而不是 start：服务已经在运行时 start 是空操作，于是"复制了
    # 新脚本再 --install"看起来安装成功、日志里跑的还是旧进程（实际踩过两次）
    run("systemctl restart bt-mic.service", check=False, verbose=False)
    time.sleep(2)
    status = run(
        "systemctl is-active bt-mic.service", check=False, timeout=10, verbose=False
    )
    if status and status.stdout.strip() == "active":
        print("  -> bt-mic.service 已安装并（重新）启动（active），开机将自动运行")
        print("     本次已重启服务，新脚本立即生效")
        print("  查看日志: journalctl -u bt-mic -f")
    else:
        print("  !! 服务可能未成功启动，请检查:")
        print("     systemctl status bt-mic.service")
        print("     journalctl -u bt-mic -n 100")


def uninstall_systemd_unit():
    print_status("卸载 systemd 服务 bt-mic.service")
    run("systemctl disable bt-mic.service 2>/dev/null || true", check=False, verbose=False)
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

    if "--play-test" in sys.argv:
        # 只测本机耳机口（不动蓝牙、不触发恢复流程）；放在 ensure_root 之后，
        # 非 audio 组用户也能靠 sudo 设置 wm8960 混音
        _RESTORED = True
        play_test_tone()
        return
    if "--find-output" in sys.argv:
        # 逐张声卡放测试音，确认耳机插在哪张卡上并写入配置（换插孔后没声音的首选）
        _RESTORED = True
        find_output_device()
        return
    if "--hp-test" in sys.argv:
        # 分步自检：左/右/双声道放音 + 支持格式 + 边录边放（定位"只有一边有声音"
        # 和"开麦克风就没声音"到底是哪一层的问题）
        _RESTORED = True
        headphone_test()
        return
    if "--audio-info" in sys.argv:
        # 只读诊断：蓝牙音频输出（耳机/扬声器）链路为什么不通
        _RESTORED = True
        print_audio_info()
        return
    if "--forget" in sys.argv:
        # 删除树莓派侧配对，让电脑重新配对并重新枚举服务（修"电脑里没有输出项"）
        # 同样不要在退出时触发恢复流程，否则刚删掉的配对又会被重新断开/清理
        _RESTORED = True
        forget_paired_devices()
        return
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
    print("同时作为蓝牙耳机/扬声器：电脑音频送到耳机口（自动选，优先与麦克风不同卡")
    print("的声卡；换了耳机插孔没声音时跑 --find-output 重新确认是哪个孔）")
    print("构建版本: %s" % BUILD_ID)
    if _CONFIG_APPLIED:
        print("已读取配置 %s: %s"
              % (CONFIG_FILE, ", ".join("%s=%s" % kv for kv in _CONFIG_APPLIED)))
    else:
        print("（无配置文件 %s；改配置可直接往里写 KEY=VALUE，或跑 --find-output）"
              % CONFIG_FILE)
    check_script_updated()
    disable_wifi_powersave()

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

        # 耳机（扬声器）输出设备：reSpeaker HAT 的 3.5mm 耳机口，一次性探测好，
        # 每次会话开始起一个独立的输出线程（见 headphone_monitor）
        playback_device = None
        hat_mode = False
        cross_route = False
        if HEADPHONE_ENABLE:
            print_status("探测耳机（扬声器）输出设备")
            playback_device, play_why = select_playback_device(mic_device)
            if playback_device:
                print(f"  -> 耳机口播放设备: {playback_device}（{play_why}）")
                # 同卡时：① 打开 HP 交叉路由（该卡放音数据只在一路 DAC 上，
                # 不开交叉时另一个耳塞完全没声）；② 由 decide_hat_mode 说明
                # "两个方向抢同一个 I2S 时钟"这件事并给出可实测的对策开关
                cross_route = same_sound_card(playback_device, mic_device)
                hat_mode = decide_hat_mode(playback_device, mic_device)
                _HAT_MODE[0] = hat_mode
                apply_playback_mixer(playback_device, cross_route=cross_route)
            else:
                print(f"  !! 未找到可用的播放设备（{play_why}）：")
                print("     蓝牙耳机（扬声器）功能不可用；麦克风功能不受影响")
        else:
            print("  -> HEADPHONE_ENABLE=0：不启用蓝牙耳机（扬声器）输出")

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

            # 耳机（扬声器）输出：独立线程，与麦克风链路并行。放在这里而不是
            # 转发启动之后，是因为电脑经常只把它当耳机放音、不开麦克风——
            # 那种情况下麦克风 PCM 永远等不到，耳机输出必须已经能工作
            hp_stop = threading.Event()
            hp_thread = threading.Thread(
                target=headphone_monitor,
                args=(device, hp_stop, playback_device),
                kwargs={"hat_mode": hat_mode, "cross_route": cross_route,
                        "mic_device": mic_device},
                daemon=True,
            )
            hp_thread.start()
            try:
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
            finally:
                hp_stop.set()
                hp_thread.join(timeout=8)
                print("  -> 耳机输出已停止（本次会话结束）")

    except KeyboardInterrupt:
        print("\n  用户中断程序")
    except (RuntimeError, FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        print(f"\n[错误] {e}")
        sys.exit(1)
    finally:
        restore_default()


def disable_wifi_powersave():
    """关掉无线网卡的省电模式（best-effort）。

    Pi4 的 WiFi 与蓝牙共用一颗二合一芯片：WiFi 省电会让芯片在蓝牙音频传输
    期间周期性休眠/抢占，表现为音频卡顿、SCO 反复停顿，甚至链路超时断开
    （"连上后突然断开、要反复重连"常见原因之一）。`iw set power_save off`
    只影响本次运行、不写任何配置文件，重启即恢复系统默认。
    WIFI_POWERSAVE=keep 时不动系统设置。
    """
    if WIFI_POWERSAVE != "off":
        print("  -> WIFI_POWERSAVE=%s：保留系统 WiFi 省电设置" % WIFI_POWERSAVE)
        return
    if shutil.which("iw") is None:
        print("  -> 未安装 iw，跳过关闭 WiFi 省电（可 apt install iw）")
        return
    res = run("ls /sys/class/net", check=False, timeout=8, verbose=False)
    ifaces = [
        i
        for i in ((res.stdout or "") if res else "").split()
        if i.startswith(("wlan", "wlp"))
    ]
    if not ifaces:
        return
    for iface in ifaces:
        r = run(
            f"iw dev {iface} set power_save off",
            check=False,
            timeout=8,
            verbose=False,
        )
        if r and r.returncode == 0:
            print(f"  -> 已关闭 {iface} 的 WiFi 省电（减少蓝牙音频卡顿/掉线）")
        else:
            print(f"  !! 关闭 {iface} WiFi 省电失败（不影响其它功能）")


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
