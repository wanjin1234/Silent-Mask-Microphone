import time
from pyftdi.gpio import GpioController

# 与 test_ultrasonic.py 相同的引脚定义
TRIG_PINS = [0, 2, 4]   # D0, D2, D4
ECHO_PINS = [1, 3, 5]   # D1, D3, D5
ANGLES = [-45, 0, 45]


def main():
    gpio = GpioController()
    try:
        gpio.open_from_url('ftdi://ftdi:232h/1')
    except Exception as e:
        print(f"无法打开FT232H: {e}")
        return
    print("FT232H 已打开\n")

    # ---------- 第1步：验证输出引脚能否被拉高 ----------
    # 先把 D0~D5 全设为输出，逐个拉高，读回检查
    gpio.set_direction(0b00111111, 0b00111111)   # D0~D5 全部设为输出
    gpio.write(0x00)
    time.sleep(0.01)
    print("=== 第1步：TRIG 输出引脚自检 ===")
    print("如果下面某个 bit 不是 1，说明该引脚的物理位置/丝印与代码映射不符")
    for trig in TRIG_PINS:
        gpio.write(1 << trig)
        time.sleep(0.005)
        v = gpio.read()
        gpio.write(0x00)
        bit = 1 if (v & (1 << trig)) else 0
        print(f"  拉高 D{trig} -> 读回 0b{format(v, '08b')}  bit{trig}={bit}  {'OK' if bit else '!!! 异常'}")

    # ---------- 第2步：设置正确方向，读空闲状态 ----------
    print("\n=== 第2步：TRIG输出 / ECHO输入，空闲电平 ===")
    direction = 0b00010101   # 输出掩码: D0/D2/D4(TRIG)，ECHO 为输入
    gpio.set_direction(0b00111111, direction)
    gpio.write(0x00)
    time.sleep(0.01)
    v = gpio.read()
    print(f"  空闲读回 = 0b{format(v, '08b')}")
    print("  期望：ECHO(D1,D3,D5) 空闲应为 0（bit1/3/5 = 0）")
    print("  若 bit1/3/5 一直是 1，可能 Echo 接线反了、悬空被拉高、或传感器异常\n")

    # ---------- 第3步：触发每个传感器，看 ECHO 是否有高电平 ----------
    print("=== 第3步：逐个触发，检测 ECHO 回波 ===")
    print("请把障碍物（书本/纸板）正对某个探头约 50cm 处")
    for i, (trig, echo) in enumerate(zip(TRIG_PINS, ECHO_PINS)):
        gpio.write(0x00)
        time.sleep(0.002)
        gpio.write(1 << trig)
        time.sleep(0.00002)   # 20us 触发脉冲（bitbang 实际会更长，足够）
        gpio.write(0x00)

        t0 = time.time()
        high_seen = False
        while time.time() - t0 < 0.2:
            if gpio.read() & (1 << echo):
                high_seen = True
                break
        if high_seen:
            start = time.time()
            while gpio.read() & (1 << echo):
                if time.time() - t0 > 0.2:
                    break
            dur = time.time() - start
            print(f"  角度{ANGLES[i]:+d}° (Trig D{trig}, Echo D{echo}): "
                  f"检测到高电平, 脉宽≈{dur*1e6:.0f}us, 距离≈{dur*17150:.1f}cm")
        else:
            print(f"  角度{ANGLES[i]:+d}° (Trig D{trig}, Echo D{echo}): 200ms 内无高电平")
        time.sleep(0.05)

    print("\n诊断结束。Ctrl+C 退出前，可手动短接测试输入读取（见说明）")
    gpio.close()


if __name__ == "__main__":
    main()
