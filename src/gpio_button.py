# -*- coding: utf-8 -*-
"""用树莓派 GPIO 接一个普通按钮，按一次触发一次人体存在扫描。

复用超声波已在用的 pigpio 守护进程（sudo pigpiod），不额外占串口。
按钮一端接 GPIO（BCM 编号，默认 GPIO4），另一端接 GND，使用内部上拉，
按下（下降沿）即触发。

为什么用后台线程 + wait_for_edge 而不是主循环轮询：
  pigpio 的 wait_for_edge 在守护进程侧阻塞等待下降沿，主循环即使被串口
  读取阻塞、或 pygame 帧率抖动，也不会漏掉按键。

消抖：FALLING_EDGE 只在"按下"瞬间触发，按住不重复触发；两次触发之间的
最小间隔由 BUTTON_DEBOUNCE_S 控制（默认 0.2s），扫描自身的 2s 冷却再兜底。

环境变量：
  BUTTON_GPIO         按钮引脚 BCM 编号，默认 4
  BUTTON_DEBOUNCE_S   两次触发最小间隔秒数，默认 0.2
  BUTTON_DEBUG        设为 1 时打印按键事件，便于在树莓派上验证
"""

import os
import threading
import time

try:
    import pigpio
except ImportError:
    pigpio = None


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


class GpioButton:
    def __init__(self, gpio=None, debounce_s=None):
        if gpio is None:
            gpio = os.getenv('BUTTON_GPIO', '4')
        try:
            self.gpio = int(gpio)
        except (TypeError, ValueError):
            self.gpio = None

        self.debounce_s = debounce_s if debounce_s is not None else _env_float('BUTTON_DEBOUNCE_S', 0.2)
        self.debug = os.getenv('BUTTON_DEBUG', '0') == '1'

        self.pi = None
        self._pending = False
        self._lock = threading.Lock()
        self._last_trigger = 0.0
        self._stop = False
        self._thread = None

        if pigpio is None or self.gpio is None:
            if self.debug:
                print("[BUTTON] 未启用：pigpio 不可用或 BUTTON_GPIO 无效")
            return
        try:
            self.pi = pigpio.pi()
            if not self.pi.connected:
                self.pi = None
                if self.debug:
                    print("[BUTTON] pigpiod 未运行，请先执行: sudo pigpiod")
                return
            self.pi.set_mode(self.gpio, pigpio.INPUT)
            self.pi.set_pull_up_down(self.gpio, pigpio.PUD_UP)
            if self.debug:
                print(f"[BUTTON] 已初始化 GPIO{self.gpio}（内部上拉，按下接 GND）")
        except Exception as e:
            if self.debug:
                print(f"[BUTTON] 初始化失败: {e}")
            self._cleanup_pi()
            return

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """后台线程：阻塞等待下降沿，把事件放到 _pending 标志。"""
        while not self._stop:
            try:
                if self.pi is None:
                    break
                # 阻塞等待下降沿；每 0.5s 超时一次以便响应 stop
                if self.pi.wait_for_edge(self.gpio, pigpio.FALLING_EDGE, 0.5):
                    with self._lock:
                        self._pending = True
                    if self.debug:
                        print("[BUTTON] 检测到下降沿（按下）")
            except Exception as e:
                if self.debug:
                    print(f"[BUTTON] 边沿等待异常: {e}")
                time.sleep(0.1)

    @property
    def enabled(self):
        """是否成功初始化了 GPIO 按钮。"""
        return self.pi is not None

    def poll(self):
        """主循环每帧调用；在"一次按下"时返回 True，其余情况返回 False。"""
        if self.pi is None:
            return False
        with self._lock:
            pending = self._pending
            self._pending = False
        if not pending:
            return False
        now = time.time()
        if now - self._last_trigger >= self.debounce_s:
            self._last_trigger = now
            if self.debug:
                print("[BUTTON] 触发一次扫描")
            return True
        return False

    def _cleanup_pi(self):
        try:
            self.pi.stop()
        except Exception:
            pass
        self.pi = None

    def close(self):
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._cleanup_pi()
