"""GPIO 引脚引出验证脚本。

用途：方案三（40pin 排线延长）下，从排线中间引出的引脚很容易搞错物理顺序。
本脚本依次把 GPIO22~27 拉高，你拿万用表（直流电压档）逐个量引出的杜邦线，
哪根是高电平（约3.3V）就对应哪个 GPIO。

运行：sudo python3 gpio_verify.py
（用 RPi.GPIO，系统自带或 apt 安装；不依赖 pigpio）

使用：
  1. 先确认排线红线(1脚)对准树莓派 Pin1。
  2. 运行脚本，它会提示当前正在拉高哪个 GPIO。
  3. 万用表黑笔接 GND，红笔逐个探你引出的线，量到约 3.3V 的就是当前 GPIO。
"""

import time

try:
    import RPi.GPIO as GPIO
except ImportError:
    print("缺少 RPi.GPIO，请先安装: sudo apt install -y python3-rpi.gpio")
    raise SystemExit(1)

# 需要验证的 GPIO（BCM 编号）
GPIO_LIST = [22, 23, 24, 25, 5, 6]


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    # 全部设为输出
    for g in GPIO_LIST:
        GPIO.setup(g, GPIO.OUT, initial=GPIO.LOW)

    print("引脚验证开始。逐个拉高 GPIO，拿万用表测量引出的杜邦线。")
    print("量到约 3.3V 的那根线，就是当前提示的 GPIO。Ctrl+C 退出。\n")

    try:
        while True:
            for g in GPIO_LIST:
                # 拉高当前 GPIO
                GPIO.output(g, GPIO.HIGH)
                print(f">>> 现在拉高的是 GPIO{g}（保持 3 秒，去量线）")
                time.sleep(3)
                GPIO.output(g, GPIO.LOW)
                time.sleep(0.5)
            print("\n--- 一轮完成，重新开始 ---\n")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n测试结束")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
