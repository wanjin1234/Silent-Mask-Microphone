"""检测树莓派 GPIO 占用情况，判断哪些引脚空闲可用于超声波。

运行方式（在树莓派上）：
    sudo python3 gpio_probe.py

原理：
    1. 读取 /sys/kernel/debug/gpio，解析内核已认领的 GPIO 及其标签
       （如 PCM_DOUT 表示 I2S 占用，i2c 表示 I2C 占用）。
    2. 对照 ReSpeaker 2-Mic Pi HAT 的常见占用，给出推荐空闲引脚。
"""

import os
import subprocess
import sys

# ReSpeaker 2-Mic Pi HAT 常见占用（BCM 编号）
RESPEAKER_KNOWN = {
    2: 'I2C SDA (HAT EEPROM/音频芯片)',
    3: 'I2C SCL (HAT EEPROM/音频芯片)',
    18: 'I2S BCLK',
    19: 'I2S FS (LRCK)',
    20: 'I2S DIN',
    21: 'I2S DOUT',
    17: '按键 (部分版本)',
}

# 全部 28 个 BCM GPIO（0-27）
ALL_GPIOS = list(range(28))

# 这些是树莓派固定功能脚，通常不用于普通 GPIO
RESERVED = {0: 'EEPROM ID_SD', 1: 'EEPROM ID_SC'}


def read_kernel_gpio():
    """解析 /sys/kernel/debug/gpio，返回 {gpio: 标签}。"""
    path = '/sys/kernel/debug/gpio'
    if not os.path.exists(path):
        return None
    used = {}
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except PermissionError:
        return None
    for line in lines:
        # 形如 " gpio-18  (PCM_DOUT          |sysfs)"
        if 'gpio-' not in line:
            continue
        part = line.split('(')
        if len(part) < 2:
            continue
        num_part = line.split('gpio-')[1]
        num = ''
        for ch in num_part:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            continue
        gpio = int(num)
        label = part[1].split('|')[0].strip()
        # 通用标签 "GPIOx" 表示未被任何驱动认领，是空闲脚，跳过
        if label == f'GPIO{gpio}':
            continue
        used[gpio] = label
    return used


def read_overlay_status():
    """读取 I2S/I2C/SPI 等设备树接口是否启用。"""
    status = {}
    dt = '/proc/device-tree/soc'
    checks = {
        'i2c': os.path.exists(f'{dt}/i2c@7e804000'),
        'i2s': os.path.exists(f'{dt}/i2s@7e203000'),
        'spi': os.path.exists(f'{dt}/spi@7e204000'),
        'uart': os.path.exists(f'{dt}/serial@7e201000'),
    }
    status.update(checks)
    return status


def main():
    print('=' * 60)
    print('树莓派 GPIO 占用检测')
    print('=' * 60)

    # 1. 内核已认领的 GPIO
    used = read_kernel_gpio()
    if used is None:
        print('\n[警告] 无法读取 /sys/kernel/debug/gpio（需要 sudo 运行）')
        print('请用: sudo python3 gpio_probe.py\n')
    else:
        print('\n[1] 内核当前已认领的 GPIO（带标签）：')
        if not used:
            print('    （空）内核未认领任何 GPIO')
        for gpio in sorted(used):
            print(f'    GPIO{gpio:<3} -> {used[gpio]}')

    # 2. 设备树接口状态
    overlays = read_overlay_status()
    print('\n[2] 设备树接口启用状态：')
    names = {'i2c': 'I2C', 'i2s': 'I2S(音频)', 'spi': 'SPI', 'uart': 'UART(串口)'}
    for key, label in names.items():
        on = overlays.get(key, False)
        print(f'    {label:<12}: {"已启用" if on else "未启用"}')

    # 3. ReSpeaker 已知占用
    print('\n[3] ReSpeaker 2-Mic Pi HAT 已知占用（BCM）：')
    for gpio, desc in sorted(RESPEAKER_KNOWN.items()):
        print(f'    GPIO{gpio:<3} -> {desc}')

    # 4. 汇总：空闲引脚（只考虑 40 针排针上的 BCM 编号 0-27）
    print('\n[4] 结论：可用的空闲 GPIO（建议接超声波）：')
    # 内核用特定标签认领的脚（通用 GPIOx 标签已在上面跳过，不算占用）
    kernel_claimed = set(used or {})
    # ReSpeaker 排针上真正占用的脚
    occupied = set(RESPEAKER_KNOWN.keys())
    occupied.update(RESERVED.keys())
    # 树莓派 5 上，排针 BCM 0-27；SPI/UART 若不用也算空闲，但这里保守排除
    header_gpios = list(range(28))
    free = [g for g in header_gpios
            if g not in occupied and g not in kernel_claimed]

    # 明确列出的推荐空闲脚（避开 I2C/I2S/SPI/UART/按键）
    recommended = [5, 6, 12, 13, 16, 22, 23, 24, 25, 26, 27]

    print(f'    内核特定标签认领的脚：{sorted(kernel_claimed) if kernel_claimed else "无"}')
    print(f'    推荐空闲脚（避开 I2C 2/3、I2S 18-21、按键 17、SPI 8-11、UART 14/15）：')
    print(f'      {recommended}')

    if len(recommended) >= 6:
        print('\n    推荐三路超声接线（BCM）：')
        print(f'      左 -45°: Trig=GPIO{recommended[5]}, Echo=GPIO{recommended[6]}')
        print(f'      中   0°: Trig=GPIO{recommended[7]}, Echo=GPIO{recommended[8]}')
        print(f'      右 +45°: Trig=GPIO{recommended[9]}, Echo=GPIO{recommended[10]}')
    else:
        print('      （空闲脚不足，需改用其他方案）')

    print('\n' + '=' * 60)
    print('注意：Echo 是 5V 输出，接树莓派前必须分压到 3.3V！')
    print('=' * 60)


if __name__ == '__main__':
    main()
