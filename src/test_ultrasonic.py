import time
from pyftdi.gpio import GpioController

# 引脚定义：TRIG -> D0,D2,D4，ECHO -> D1,D3,D5
TRIG_PINS = [0, 2, 4]
ECHO_PINS = [1, 3, 5]
ANGLES = [-45, 0, 45]

def main():
    try:
        gpio = GpioController()
        gpio.open_from_url('ftdi://ftdi:232h/1')
    except Exception as e:
        print(f"无法打开FT232H: {e}")
        return

    # 设置方向：TRIG为输出(0)，ECHO为输入(1)
    direction = 0b00101010  # 0=输出, 1=输入
    gpio.set_direction(direction, 0b00111111)
    gpio.write(0x00)  # 所有引脚拉低

    def measure(trig, echo):
        # 发送10us触发脉冲
        gpio.write(0x00)
        time.sleep(0.000002)
        gpio.write(1 << trig)
        time.sleep(0.00001)
        gpio.write(0x00)

        # 等待ECHO变高
        timeout = time.time() + 0.1
        while not (gpio.read() & (1 << echo)):
            if time.time() > timeout:
                return None
        start = time.time()

        # 等待ECHO变低
        while (gpio.read() & (1 << echo)):
            if time.time() > timeout:
                return None
        end = time.time()

        # 计算距离（厘米）
        duration = end - start
        distance_cm = duration * 17150  # 声速 343m/s
        return distance_cm

    print("开始测试超声波传感器...")
    print("请将传感器前方分别放置障碍物，观察距离变化。按 Ctrl+C 停止。\n")

    try:
        while True:
            for i, (trig, echo) in enumerate(zip(TRIG_PINS, ECHO_PINS)):
                dist = measure(trig, echo)
                if dist is not None and 20 < dist < 450:
                    print(f"角度{ANGLES[i]:+d}°: {dist:.2f} cm")
                else:
                    print(f"角度{ANGLES[i]:+d}°: 无回波")
            time.sleep(0.3)
            print("-" * 40)
    except KeyboardInterrupt:
        gpio.close()
        print("\n测试结束")

if __name__ == "__main__":
    main()
