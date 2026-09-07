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
