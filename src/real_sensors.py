import serial
import time
import traceback
from pyftdi.gpio import GpioController

class RealSensorHub:
    def __init__(self, radar_ports=['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2'],
                 radar_angles=[-45, 0, 45], debug=False):
        self.debug = debug
        self.radars = []
        for port, angle in zip(radar_ports, radar_angles):
            try:
                ser = serial.Serial(port, 115200, timeout=0.1)
                rid = len(self.radars)
                print(f"已打开雷达串口: {port} -> id={rid}, angle={angle}")
                self.radars.append({'serial': ser, 'angle': angle, 'id': rid})
            except Exception as e:
                print(f"雷达 {port} 打开失败: {e}")
                if self.debug:
                    traceback.print_exc()
                self.radars.append({'serial': None, 'angle': angle, 'id': len(self.radars)})

        self.ultra_gpio = None
        try:
            self.ultra_gpio = GpioController()
            self.ultra_gpio.open_from_url('ftdi://ftdi:232h/1')
            direction = 0b00101010   # TRIG 输出(0)，ECHO 输入(1)
            self.ultra_gpio.set_direction(direction, 0b00111111)
            self.ultra_gpio.write(0x00)
        except Exception as e:
            print(f"FT232H 初始化失败: {e}")

        self.ultra_pins = [
            {'trig': 0, 'echo': 1, 'angle': -45},
            {'trig': 2, 'echo': 3, 'angle': 0},
            {'trig': 4, 'echo': 5, 'angle': 45}
        ]

        # 雷达数据缓冲区
        self.radar_buffers = [bytearray() for _ in self.radars]

    def _parse_radar_frame(self, frame):
        """解析32字节雷达帧，返回 (distance_m, presence) 或 None

        观察到实际帧中有多种可能的距离/状态字段位置，增加候选偏移以提高兼容性。
        """
        if len(frame) != 32:
            return None
        if not (frame[0] == 0xFA and frame[1] == 0xF5 and frame[2] == 0xAA and frame[3] == 0xA5):
            return None

        # 候选距离字段（小端）：尝试这些偏移对 (low_index, high_index)
        candidates = [(5, 6), (7, 8), (20, 21), (21, 22), (22, 23), (23, 24)]
        dist_cm = None
        used_offset = None
        for lo, hi in candidates:
            val = frame[lo] | (frame[hi] << 8)
            if 0 < val <= 900:
                dist_cm = val
                used_offset = (lo, hi)
                break

        if dist_cm is None:
            if self.debug:
                print(f"未在候选偏移中找到有效距离，帧预览: {frame.hex()[:200]}")
            return None

        dist_m = dist_cm / 100.0

        # 存在状态：在多个位置查找 0x01 标志（保守策略）
        presence = 0
        for i in range(24, 32):
            if frame[i] == 0x01:
                presence = 1
                break

        if self.debug:
            print(f"解析到距离 {dist_m:.2f}m (cm={dist_cm}) 在偏移 {used_offset}, presence={presence}")

        return dist_m, presence

    def read_radars(self):
        results = []
        for idx, radar in enumerate(self.radars):
            if radar['serial'] is None:
                if self.debug:
                    print(f"雷达 idx={idx} angle={radar['angle']} 未打开（serial=None）")
                continue
            try:
                ser = radar['serial']
                # 读取所有可用数据（若 in_waiting 为 0 也尝试短读以防缓冲未报告）
                try:
                    waiting = ser.in_waiting
                except Exception:
                    waiting = 0
                if waiting:
                    chunk = ser.read(waiting)
                else:
                    # 如果没有报告可用字节，尝试读取一些字节（非阻塞，由 timeout 控制）
                    chunk = ser.read(32)
                if chunk:
                    if self.debug:
                        print(f"雷达 idx={idx} 读到 {len(chunk)} 字节: {chunk.hex()[:200]}")
                    self.radar_buffers[idx].extend(chunk)

                # 查找完整帧
                while True:
                    buf = self.radar_buffers[idx]
                    start = buf.find(b'\xFA\xF5\xAA\xA5')
                    if start == -1 or len(buf) < start + 32:
                        break
                    frame = bytes(buf[start:start+32])
                    del buf[:start+32]

                    result = self._parse_radar_frame(frame)
                    if result:
                        dist_m, presence = result
                        print(f"雷达{radar['angle']:+d}°: 距离={dist_m:.2f}m, 有人={presence}")
                        results.append({
                            'distance': dist_m,
                            'presence': presence,
                            'angle': radar['angle'],
                            'valid': True
                        })
                        break  # 每轮只取一个有效帧
            except Exception as e:
                print(f"雷达读取错误 idx={idx} angle={radar.get('angle')} : {e}")
                if self.debug:
                    traceback.print_exc()
        return results

    def read_ultrasonics(self):
        results = []
        if self.ultra_gpio is None:
            return results
        for sensor in self.ultra_pins:
            trig = sensor['trig']
            echo = sensor['echo']
            # 触发
            self.ultra_gpio.write(0x00)
            time.sleep(0.000002)
            self.ultra_gpio.write(1 << trig)
            time.sleep(0.00001)
            self.ultra_gpio.write(0x00)
            # 等待回波
            timeout = time.time() + 0.1
            while not (self.ultra_gpio.read() & (1 << echo)):
                if time.time() > timeout:
                    break
            start = time.time()
            while (self.ultra_gpio.read() & (1 << echo)):
                if time.time() > timeout:
                    break
            end = time.time()
            duration = end - start
            dist_m = (duration * 17150) / 100.0
            if 0.2 < dist_m < 4.5:
                results.append({
                    'distance': dist_m,
                    'presence': 0,
                    'angle': sensor['angle'],
                    'valid': True
                })
            time.sleep(0.02)
        return results

    def close(self):
        for radar in self.radars:
            if radar['serial']:
                radar['serial'].close()
        if self.ultra_gpio:
            self.ultra_gpio.close()
