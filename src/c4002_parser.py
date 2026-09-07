# C4002 serial parser wrapper
# Parses C4002 frames from serial port and exposes a read_data() compatible dict

import time
import serial
import os
from collections import deque, Counter

# Frame constants (from DFRobot library)
FRAME_HEADER1 = 0xFA
FRAME_HEADER2 = 0xF5
FRAME_HEADER3 = 0xAA
FRAME_HEADER4 = 0xA5
FRAME_TYPE_NOTIFICATION = 0x04
NOTE_RESULT_CMD = 0x60

# Frame types (from official DFRobot_C4002 library)
FRAME_TYPE_WRITE_REQUSET = 0x00
FRAME_TYPE_READ_REQUSET = 0x01
FRAME_TYPE_WRITE_RESPOND = 0x02
FRAME_TYPE_READ_RESPOND = 0x03

# Command codes (from official DFRobot_C4002 library)
CMD_RESTART = 0x00
CMD_ENVIRNMENT_CALIBRATION = 0x60
CMD_SET_REPORT_PERIOD = 0x83
CMD_TARGET_DISAPPEAR_DELAY_TIME = 0x84
CMD_SET_DETECT_RANGE = 0x86
CMD_THRESHOLD_GROUP = 0x87

# Distance gate types (for set_sensitivity)
MOTION_DISTANCE_GATE = 0x00
PRESENCE_DISTANCE_GATE = 0x01

# Sensitivity groups
LOW_THRESH_GROUP = 0x00
MID_THRESH_GROUP = 0x01
HIGH_THRESH_GROUP = 0x02

# Request / response codes
READ_AND_WRITE_REQ = 0x00
SUCCEED = 0x01

# Target states
NO_TARGET = 0
PRESENCE = 1
MOTION = 2
MOTION_OR_PRESENCE = 3
MOTION_OR_NO_TARGET = 4
PRESENCE_OR_NO_TARGET = 5

# Configurable parameters via environment variables
# Backward compatibility: if the old C4002_ENERGY_THRESHOLD is set, prefer it for presence threshold
_legacy_thresh = os.getenv('C4002_ENERGY_THRESHOLD')
_presence_env = os.getenv('C4002_ENERGY_THRESHOLD_PRESENCE')
_select_env = os.getenv('C4002_ENERGY_THRESHOLD_SELECT')
if _presence_env is not None:
    DEFAULT_ENERGY_THRESHOLD_PRESENCE = int(_presence_env)
elif _legacy_thresh is not None:
    DEFAULT_ENERGY_THRESHOLD_PRESENCE = int(_legacy_thresh)
else:
    DEFAULT_ENERGY_THRESHOLD_PRESENCE = int(os.getenv('C4002_ENERGY_THRESHOLD_PRESENCE', '20'))
# Selection threshold (used when choosing which target distance to prefer). Set low/0 to always pick highest-energy target for distance.
if _select_env is not None:
    DEFAULT_ENERGY_THRESHOLD_SELECT = int(_select_env)
else:
    DEFAULT_ENERGY_THRESHOLD_SELECT = int(os.getenv('C4002_ENERGY_THRESHOLD_SELECT', '0'))
DEFAULT_HISTORY_LEN = int(os.getenv('C4002_HISTORY_LEN', '5'))
DEFAULT_PRESENCE_ON = int(os.getenv('C4002_PRESENCE_ON', '3'))
DEFAULT_PRESENCE_OFF = int(os.getenv('C4002_PRESENCE_OFF', '3'))
DEFAULT_ENERGY_SMOOTH_LEN = int(os.getenv('C4002_ENERGY_SMOOTH_LEN', '5'))
# Breath-detection specific defaults
DEFAULT_BREATH_ENERGY_THRESHOLD = int(os.getenv('C4002_BREATH_ENERGY_THRESHOLD', '6'))
DEFAULT_BREATH_SMOOTH_LEN = int(os.getenv('C4002_BREATH_SMOOTH_LEN', '15'))
DEFAULT_BREATH_ON = int(os.getenv('C4002_BREATH_ON', '2'))
DEFAULT_BREATH_OFF = int(os.getenv('C4002_BREATH_OFF', '4'))
# Motion detection defaults (on-count uses DEFAULT_PRESENCE_ON by default)
DEFAULT_MOTION_ENERGY_THRESHOLD = int(os.getenv('C4002_MOTION_ENERGY_THRESHOLD', '10'))
DEFAULT_DISTANCE_VARIANCE = float(os.getenv('C4002_DISTANCE_VARIANCE', '0.15'))
# 呼吸微动 / 运动检测基于 move_target_speed（速度，单位 cm/s）。
# 能量信号（exist_en/move_en）在近距离强反射下会饱和到 99 失去区分度，
# 而速度是真正的多普勒物理量：静止呼吸的人有低速周期性微变，静止墙面恒为 0。
DEFAULT_BREATH_SPEED_MAX = int(os.getenv('C4002_BREATH_SPEED_MAX', '20'))   # 呼吸微动速度上限 cm/s
DEFAULT_MOTION_SPEED_MIN = int(os.getenv('C4002_MOTION_SPEED_MIN', '20'))   # 运动速度下限 cm/s
# Cap used to reduce influence of saturated (maxed) energy readings when averaging
DEFAULT_ENERGY_CAP = int(os.getenv('C4002_ENERGY_CAP', '80'))
DEFAULT_SATURATION_DISTANCE = float(os.getenv('C4002_SATURATION_DISTANCE', '3.5'))  # meters
# Distance smoothing: EMA weight (0-1) and spike rejection threshold (meters)
DEFAULT_DISTANCE_EMA_ALPHA = float(os.getenv('C4002_DISTANCE_EMA_ALPHA', '0.5'))
DEFAULT_DISTANCE_SPIKE = float(os.getenv('C4002_DISTANCE_SPIKE', '1.0'))  # meters
DEFAULT_SPIKE_HOLD_FRAMES = int(os.getenv('C4002_SPIKE_HOLD_FRAMES', '3'))


class C4002Serial:
    def __init__(self, port='/dev/ttyAMA0', baud=115200, angle=0, sensor_id=0, timeout=0.5):
        self.port = port
        self.baud = baud
        self.angle = angle
        self.sensor_id = sensor_id
        self.timeout = timeout
        self.ser = None
        try:
            self.ser = serial.Serial(port, baudrate=baud, bytesize=8, parity='N', stopbits=1, timeout=timeout)
        except Exception:
            self.ser = None
        # smoothing - use configurable history length
        hist_len = DEFAULT_HISTORY_LEN if DEFAULT_HISTORY_LEN > 0 else 7
        self.dist_history = deque(maxlen=hist_len)
        self.presence_history = deque(maxlen=hist_len)
        # energy smoothing and hysteresis counters for presence
        energy_len = DEFAULT_ENERGY_SMOOTH_LEN if DEFAULT_ENERGY_SMOOTH_LEN > 0 else 5
        self.energy_history = deque(maxlen=energy_len)
        self.presence_on_count = 0
        self.presence_off_count = 0
        # breath-specific energy smoothing and counters
        breath_len = DEFAULT_BREATH_SMOOTH_LEN if DEFAULT_BREATH_SMOOTH_LEN > 0 else 15
        self.breath_energy_history = deque(maxlen=breath_len)
        self.breath_on_count = 0
        self.breath_off_count = 0
        # motion counters
        self.motion_on_count = 0
        self.motion_off_count = 0
        # distance smoothing state (EMA + spike rejection)
        self.last_distance_ema = None
        self.last_valid_distance = None
        self.spike_hold_count = 0
        self.last_chosen_type = 'none'

    def _read_n_bytes(self, n, timeout=None):
        if self.ser is None:
            return None
        return self.ser.read(n)

    # ------------------------------------------------------------------
    # 配置命令（严格按官方 DFRobot_C4002 协议实现）
    # ------------------------------------------------------------------
    def _send_pack(self, payload, msg_type):
        """构造完整帧并发送。payload 为命令体字节（不含帧头/长度/校验）。"""
        data_len = len(payload)
        frame = bytearray()
        frame += bytes([FRAME_HEADER1, FRAME_HEADER2, FRAME_HEADER3, FRAME_HEADER4])
        total = data_len + 10
        frame += bytes([total & 0xFF, (total >> 8) & 0xFF, 0x00, msg_type & 0xFF])
        frame += payload
        checksum = sum(frame) & 0xFFFF
        frame += bytes([checksum & 0xFF, (checksum >> 8) & 0xFF])
        try:
            self.ser.reset_input_buffer()
            self.ser.write(bytes(frame))
            return True
        except Exception as e:
            if getattr(self, 'debug', False):
                print(f"[C4002 CFG] send error: {e}")
            return False

    def _recv_pack(self, timeout=0.6):
        """读取配置响应帧（跳过上报通知帧），返回 resp_code；失败返回 None。"""
        if self.ser is None:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            header = self.ser.read(8)
            if not header or len(header) < 8:
                continue
            h = list(header)
            if not (h[0] == FRAME_HEADER1 and h[1] == FRAME_HEADER2 and h[2] == FRAME_HEADER3 and h[3] == FRAME_HEADER4):
                continue
            pack_len = (h[5] << 8) | h[4]
            if pack_len < 8 or pack_len > 64:
                continue
            body = self.ser.read(pack_len - 8)
            if not body or len(body) < pack_len - 8:
                continue
            frame = h + list(body)
            calc = sum(frame[:pack_len - 2]) & 0xFFFF
            recv = (frame[pack_len - 1] << 8) | frame[pack_len - 2]
            if calc != recv:
                continue
            pack_type = frame[7]
            # 只有写/读响应帧才返回结果；通知帧（检测数据）跳过继续等
            if pack_type in (FRAME_TYPE_WRITE_RESPOND, FRAME_TYPE_READ_RESPOND):
                return frame[9] if len(frame) > 9 else None
        return None

    def _send_cmd(self, cmd, data):
        """发送一条写命令：命令体格式 [cmd, 0x00, len_lo, len_hi, ...data]。
        发送后读取响应并校验 resp_code == SUCCEED，才认为配置成功。"""
        if self.ser is None:
            return False
        payload = bytearray()
        payload.append(cmd & 0xFF)
        payload.append(READ_AND_WRITE_REQ)
        total = 4 + len(data)
        payload.append(total & 0xFF)
        payload.append((total >> 8) & 0xFF)
        payload.extend(data)
        if not self._send_pack(payload, FRAME_TYPE_WRITE_REQUSET):
            return False
        resp = self._recv_pack()
        return resp == SUCCEED

    def set_sensitivity(self, gate_type, sensitivity):
        """设置检测灵敏度。gate_type: MOTION_DISTANCE_GATE / PRESENCE_DISTANCE_GATE；sensitivity: 0低 1中 2高。"""
        return self._send_cmd(CMD_THRESHOLD_GROUP, [gate_type & 0xFF, sensitivity & 0xFF])

    def set_detect_range(self, closest_cm, farthest_cm):
        """设置检测距离范围，单位 cm，范围 0-1100。"""
        closest_cm = max(0, min(1100, int(closest_cm)))
        farthest_cm = max(0, min(1100, int(farthest_cm)))
        return self._send_cmd(CMD_SET_DETECT_RANGE, [
            closest_cm & 0xFF, (closest_cm >> 8) & 0xFF,
            farthest_cm & 0xFF, (farthest_cm >> 8) & 0xFF,
        ])

    def set_target_disappear_delay(self, seconds):
        """设置目标消失延迟时间，单位 s。"""
        seconds = max(0, min(65535, int(seconds)))
        return self._send_cmd(CMD_TARGET_DISAPPEAR_DELAY_TIME, [seconds & 0xFF, (seconds >> 8) & 0xFF])

    def set_report_period(self, period):
        """设置上报周期，单位 0.1s。"""
        return self._send_cmd(CMD_SET_REPORT_PERIOD, [int(period) & 0xFF])

    def start_env_calibration(self, delay_time=0, cont_time=15):
        """开始环境底噪校准。delay_time: 延时开始(s)，cont_time: 采样持续(s)。"""
        return self._send_cmd(CMD_ENVIRNMENT_CALIBRATION, [
            int(delay_time) & 0xFF, (int(delay_time) >> 8) & 0xFF,
            int(cont_time) & 0xFF, (int(cont_time) >> 8) & 0xFF,
            0x01,
        ])

    def restart(self):
        """重启模块，使配置生效。"""
        return self._send_cmd(CMD_RESTART, [0x00])

    def configure(self):
        """初始化时下发配置（默认保守值，均可通过环境变量覆盖）。

        环境变量：
          C4002_ENABLE_CONFIG         是否在启动时配置，默认 1
          C4002_CONFIG_SENSITIVITY    灵敏度 0=低 1=中 2=高，默认 0（降低强反射误报）
          C4002_CONFIG_RANGE_MIN_CM   检测最近距离 cm，默认 0
          C4002_CONFIG_RANGE_MAX_CM   检测最远距离 cm，默认 1100
          C4002_CONFIG_DISAPPEAR_S    目标消失延迟 s，默认 2
          C4002_CONFIG_RESTART        配置后是否重启生效，默认 0
          C4002_AUTO_CALIBRATE        启动时自动环境底噪校准，默认 0（设为 1 开启）
          C4002_CALIBRATE_CONT_S      校准采样持续时长 s，默认 30
        """
        if self.ser is None:
            return False
        if os.getenv('C4002_ENABLE_CONFIG', '1') != '1':
            return True

        sens = int(os.getenv('C4002_CONFIG_SENSITIVITY', '0'))
        closest = int(os.getenv('C4002_CONFIG_RANGE_MIN_CM', '0'))
        farthest = int(os.getenv('C4002_CONFIG_RANGE_MAX_CM', '1100'))
        disappear = int(os.getenv('C4002_CONFIG_DISAPPEAR_S', '2'))

        ok = True
        ok = self.set_sensitivity(PRESENCE_DISTANCE_GATE, sens) and ok
        time.sleep(0.05)
        ok = self.set_sensitivity(MOTION_DISTANCE_GATE, sens) and ok
        time.sleep(0.05)
        ok = self.set_detect_range(closest, farthest) and ok
        time.sleep(0.05)
        ok = self.set_target_disappear_delay(disappear) and ok
        time.sleep(0.05)

        # 环境底噪校准：让雷达把当前静态背景（如天花板强反射）学习为底噪，
        # 自动生成门阈值，从而抑制"静态背景被误判为人"。
        # 校准时必须保证场景内无人，且雷达静止。
        if os.getenv('C4002_AUTO_CALIBRATE', '0') == '1':
            cont = int(os.getenv('C4002_CALIBRATE_CONT_S', '30'))
            if self.start_env_calibration(0, cont):
                if getattr(self, 'debug', False):
                    print(f"[C4002 CFG] sensor:{self.sensor_id} env calibration started ({cont}s)")
                time.sleep(cont + 1)
            else:
                ok = False

        if os.getenv('C4002_CONFIG_RESTART', '0') == '1':
            self.restart()
            time.sleep(0.1)

        if getattr(self, 'debug', False):
            print(f"[C4002 CFG] sensor:{self.sensor_id} configure result: {ok}")
        return ok

    def reset_detection(self):
        """清空人体检测与距离相关计数器/历史，用于开始一次干净的"静止扫描"。
        同时清空串口接收缓冲，避免上次扫描残留的帧混入本次判定。"""
        self.presence_on_count = 0
        self.presence_off_count = 0
        self.breath_on_count = 0
        self.breath_off_count = 0
        self.motion_on_count = 0
        self.motion_off_count = 0
        self.energy_history.clear()
        self.breath_energy_history.clear()
        self.presence_history.clear()
        self.dist_history.clear()
        self.last_distance_ema = None
        self.last_valid_distance = None
        self.spike_hold_count = 0
        if self.ser is not None:
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass

    def _calc_checksum(self, pdata):
        s = 0
        for b in pdata:
            s = (s + b) & 0xFFFF
        return s

    def _bytes_to_uint16(self, b0, b1):
        return (b1 << 8) | b0

    def _bytes_to_uint32(self, b0, b1, b2, b3):
        return (b3 << 24) | (b2 << 16) | (b1 << 8) | b0

    def read_data(self):
        # Returns a dict with keys used by data_fusion: 'distance','signal','presence','angle','sensor_id','valid','timestamp'
        if self.ser is None:
            return {'valid': False}

        # try to find header
        start = time.time()
        header = self._read_n_bytes(8)
        if not header or len(header) < 8:
            return {'valid': False}
        hb = list(header)
        if not (hb[0] == FRAME_HEADER1 and hb[1] == FRAME_HEADER2 and hb[2] == FRAME_HEADER3 and hb[3] == FRAME_HEADER4):
            # discard until next byte - simple resync
            # read one more byte to advance
            self.ser.read(1)
            return {'valid': False}

        pack_len = (hb[5] << 8) | hb[4]
        remaining = pack_len - 8
        body = self._read_n_bytes(remaining)
        if not body or len(body) < remaining:
            return {'valid': False}
        pdata = hb + list(body)
        # debug: print raw packet hex when debug enabled to verify field offsets
        if getattr(self, 'debug', False):
            try:
                raw_hex = ''.join(f'{b:02x}' for b in pdata)
            except Exception:
                raw_hex = ''.join(['{:02x}'.format(int(x)&0xFF) for x in pdata])
            print(f"[C4002 RAW] sensor:{self.sensor_id} pack_len:{pack_len} raw:{raw_hex}")

        # checksum verification
        calc = 0
        for i in range(pack_len - 2):
            calc = (calc + pdata[i]) & 0xFFFF
        recv_checksum = (pdata[pack_len - 1] << 8) | pdata[pack_len - 2]
        if calc != recv_checksum:
            return {'valid': False}

        pack_type = pdata[7]
        if pack_type != FRAME_TYPE_NOTIFICATION:
            return {'valid': False}

        # data header starts at offset 8
        data_cmd = pdata[8]
        data_resp = pdata[9]
        data_len = (pdata[11] << 8) | pdata[10]

        if data_cmd != NOTE_RESULT_CMD:
            return {'valid': False}

        # the detect result struct starts at pdata[12]
        base = 12
        # Expect at least the full detect result (16 bytes)
        if data_len < 16:
            return {'valid': False}

        # Detailed per-byte debug to verify field offsets when debugging is enabled
        if getattr(self, 'debug', False):
            try:
                # print a window of bytes around the data base so we can inspect exact positions
                window_start = max(0, base - 4)
                window_end = min(len(pdata), base + 20)
                mapping = []
                for i in range(window_start, window_end):
                    ann = ''
                    if i == base + 11:
                        ann = ' <exist_en>'
                    if i == base + 16:
                        ann += ' <move_en>'
                    mapping.append(f'{i}:{pdata[i]:02x}{ann}')
                print(f"[C4002 BYTES] sensor:{self.sensor_id} base:{base} data_len:{data_len} bytes: {' '.join(mapping)}")
            except Exception:
                pass

        # Parse fields (use explicit bounds checks). Fields follow the official DFRobot_C4002 layout:
        # 0: target_status
        # 1-2: light (uint16, little-endian)
        # 3-6: exist gate index (uint32, little-endian)
        # 7-8: exist count down (uint16)
        # 9-10: exist target distance (uint16, cm)
        # 11: exist target energy (uint8)
        # 12-13: move target distance (uint16, cm)
        # 14-15: move target speed (int16)
        # 16: move target energy (uint8)
        # 17: move target direction (uint8)
        target_status = pdata[base + 0] if (base + 0) < len(pdata) else 0
        light = ((pdata[base + 2] << 8) | pdata[base + 1]) if (base + 2) < len(pdata) else 0
        exist_gate_index = (
            (pdata[base + 6] << 24) | (pdata[base + 5] << 16) | (pdata[base + 4] << 8) | pdata[base + 3]
        ) if (base + 6) < len(pdata) else 0
        exist_count_down = ((pdata[base + 8] << 8) | pdata[base + 7]) if (base + 8) < len(pdata) else 0
        exist_target_distance = ((pdata[base + 10] << 8) | pdata[base + 9]) if (base + 10) < len(pdata) else 0
        exist_target_energy = pdata[base + 11] if (base + 11) < len(pdata) else 0
        # move target fields (per official DFRobot_C4002 protocol)
        move_target_distance = ((pdata[base + 13] << 8) | pdata[base + 12]) if (base + 13) < len(pdata) else 0
        move_target_speed = ((pdata[base + 15] << 8) | pdata[base + 14]) if (base + 15) < len(pdata) else 0
        # speed is int16 (signed)
        if move_target_speed & 0x8000:
            move_target_speed -= 0x10000
        move_target_energy = pdata[base + 16] if (base + 16) < len(pdata) else 0
        move_target_direct = pdata[base + 17] if (base + 17) < len(pdata) else 0

        # choose the most likely valid target by energy
        # energies are uint8, apply a minimum threshold to reduce noise
        # selection uses a (separate) threshold so presence tuning doesn't suppress distance selection
        SELECTION_THRESHOLD = DEFAULT_ENERGY_THRESHOLD_SELECT
        PRESENCE_THRESHOLD = DEFAULT_ENERGY_THRESHOLD_PRESENCE
        # 目标选择滞回：exist/move 能量接近时保持上一帧选择，避免距离在两个目标间来回跳变
        SELECT_HYSTERESIS = int(os.getenv('C4002_SELECT_HYSTERESIS', '10'))
        chosen_distance_cm = 0
        chosen_energy = 0
        chosen_type = 'none'
        prev_type = getattr(self, 'last_chosen_type', 'none')
        move_win = move_target_energy > exist_target_energy + SELECT_HYSTERESIS
        exist_win = exist_target_energy > move_target_energy + SELECT_HYSTERESIS
        if not move_win and not exist_win:
            # 能量差距在滞回带内：保持上一帧选择，避免抖动
            if prev_type in ('move', 'move_low', 'move_sat'):
                move_win = True
            else:
                exist_win = True
        # Prefer the target with higher energy; require it to be >= selection threshold to be chosen as primary
        if move_win and move_target_energy >= SELECTION_THRESHOLD:
            chosen_distance_cm = move_target_distance
            chosen_energy = move_target_energy
            chosen_type = 'move'
        elif exist_win and exist_target_energy >= SELECTION_THRESHOLD:
            chosen_distance_cm = exist_target_distance
            chosen_energy = exist_target_energy
            chosen_type = 'exist'
        else:
            # fallback: if energies are low, prefer move if target_status indicates motion, otherwise choose exist
            if target_status in (MOTION, MOTION_OR_PRESENCE, MOTION_OR_NO_TARGET):
                chosen_distance_cm = move_target_distance
                chosen_energy = move_target_energy
                chosen_type = 'move_low'
            else:
                chosen_distance_cm = exist_target_distance
                chosen_energy = exist_target_energy
                chosen_type = 'exist_low'

        # convert distance cm->m
        distance_m = float(chosen_distance_cm) * 0.01 if chosen_distance_cm > 0 else 0.0

        # presence: require target_status indicating presence/motion AND energy above threshold majority
        raw_presence = 1 if target_status in (PRESENCE, MOTION_OR_PRESENCE, PRESENCE_OR_NO_TARGET, MOTION) else 0

        # smoothing: use median of history to avoid spikes
        self.dist_history.append(distance_m)
        # energy smoothing (cap saturated values to reduce their influence on averages)
        e_val = int(chosen_energy)
        if e_val >= 95:
            e_val = DEFAULT_ENERGY_CAP
        self.energy_history.append(e_val)
        avg_energy = sum(self.energy_history) / len(self.energy_history) if len(self.energy_history) > 0 else 0

        # breath energy smoothing (use move_target_energy as micro-motion indicator)
        # 饱和的 move_en 是强反射导致的满量程，cap 到上限即可，不要记 0（否则真实近距离呼吸也会被抹掉）
        m_val = int(move_target_energy)
        if m_val >= 95:
            m_val = DEFAULT_ENERGY_CAP
        self.breath_energy_history.append(m_val)
        avg_move_energy = sum(self.breath_energy_history) / len(self.breath_energy_history) if len(self.breath_energy_history) > 0 else 0

        # hysteresis counters for presence to avoid transient spikes
        # consider presence based on averaged energy normally, but detect saturation (many 99 readings)
        # and, if saturated, require breath/motion evidence rather than raw energy alone.
        sat_count = sum(1 for e in self.energy_history if e >= 95)
        saturated = (len(self.energy_history) > 0) and (sat_count / len(self.energy_history) > 0.6)
        if saturated:
            # when saturated, prefer averaged move-energy (breath) as evidence to avoid false positives from saturated exist_en
            # Additionally, prefer the closer target as distance source (to avoid selecting distant ceiling reflections)
            try:
                # choose the distance source that is most continuous with the previous output
                # (avoid jumping between exist/move multi-path reflections under strong saturation)
                exist_d_m = exist_target_distance * 0.01 if exist_target_distance > 0 else None
                move_d_m = move_target_distance * 0.01 if move_target_distance > 0 else None
                chosen_override = None
                if exist_d_m is not None and move_d_m is not None:
                    prev_dist = getattr(self, 'last_valid_distance', None)
                    if prev_dist is not None and prev_dist > 0:
                        chosen_override = 'move' if abs(move_d_m - prev_dist) < abs(exist_d_m - prev_dist) else 'exist'
                    else:
                        chosen_override = 'exist'
                elif move_d_m is not None:
                    chosen_override = 'move'
                elif exist_d_m is not None:
                    chosen_override = 'exist'

                if chosen_override == 'move':
                    chosen_distance_cm = move_target_distance
                    chosen_energy = move_target_energy
                    chosen_type = 'move_sat'
                elif chosen_override == 'exist':
                    chosen_distance_cm = exist_target_distance
                    chosen_energy = exist_target_energy
                    chosen_type = 'exist_sat'
                # update last energy history entry to reflect chosen energy (with cap)
                if len(self.energy_history) > 0:
                    capped_e = int(chosen_energy)
                    if capped_e >= 95:
                        capped_e = DEFAULT_ENERGY_CAP
                    # replace last appended value
                    try:
                        self.energy_history[-1] = capped_e
                    except Exception:
                        pass
                # recompute avg_energy with possible replaced value
                avg_energy = sum(self.energy_history) / len(self.energy_history) if len(self.energy_history) > 0 else 0
            except Exception:
                pass

            # presence evidence now requires both breath/move energy AND that chosen distance is within a reasonable human range
            distance_m = float(chosen_distance_cm) * 0.01 if chosen_distance_cm > 0 else 0.0
            presence_evidence = raw_presence and (avg_move_energy >= DEFAULT_BREATH_ENERGY_THRESHOLD) and (0.02 < distance_m <= DEFAULT_SATURATION_DISTANCE)
        else:
            presence_evidence = raw_presence and avg_energy >= PRESENCE_THRESHOLD

        # 记录本帧最终目标类型，供下一帧目标选择滞回使用
        self.last_chosen_type = chosen_type

        if presence_evidence:
            self.presence_on_count += 1
            self.presence_off_count = 0
        else:
            self.presence_off_count += 1
            self.presence_on_count = 0

        # breath 证据（单帧，无滞后）——滞后统一交给扫描层聚合判定。
        # 呼吸微动用 move_target_speed 判定：静止呼吸的人会产生低速周期性速度微变，
        # 而静止天花板/墙的速度恒为 0。能量信号在强反射下饱和无区分度，故弃用。
        abs_speed = abs(move_target_speed)
        breath_evidence = raw_presence and (0 < abs_speed <= DEFAULT_BREATH_SPEED_MAX)

        # breath hysteresis counters（带滞后清零）——保留用于 stable_presence 兼容字段
        if breath_evidence:
            self.breath_on_count += 1
            self.breath_off_count = 0
        else:
            self.breath_off_count += 1
            if self.breath_off_count >= DEFAULT_BREATH_OFF:
                self.breath_on_count = 0

        # compute stable distance：用众数（mode）抓住主流值，剔除少量离群跳变；
        # 距离量化到 0.1m 统计频次，无重复值时退化为中位数。忽略零值（无目标）。
        try:
            nonzero = [d for d in self.dist_history if d > 0]
            if nonzero:
                buckets = [round(d * 10) / 10.0 for d in nonzero]
                cnt = Counter(buckets)
                top_val, top_count = cnt.most_common(1)[0]
                if top_count >= 2:
                    med = top_val
                else:
                    sorted_d = sorted(nonzero)
                    med = sorted_d[len(sorted_d)//2]
            else:
                med = 0.0
        except Exception:
            med = distance_m

        # If saturated selection override happened above, ensure distance_m reflects current chosen_distance
        try:
            distance_m = float(chosen_distance_cm) * 0.01 if chosen_distance_cm > 0 else 0.0
        except Exception:
            distance_m = med

        # compute short-term distance variation to detect motion of a target
        distance_variation = 0.0
        if len(nonzero) >= 2:
            distance_variation = max(nonzero) - min(nonzero)

        # motion detection：速度超过运动阈值，或距离显著变化（速度字段偶发为 0 时的兜底）
        DISTANCE_VARIANCE_THRESHOLD = DEFAULT_DISTANCE_VARIANCE
        motion_condition = (abs_speed > DEFAULT_MOTION_SPEED_MIN) or (distance_variation >= DISTANCE_VARIANCE_THRESHOLD)

        # motion hysteresis counters
        if raw_presence and motion_condition:
            self.motion_on_count += 1
            self.motion_off_count = 0
        else:
            self.motion_off_count += 1
            self.motion_on_count = 0

        # decide stable presence using stricter rule requested by user:
        # stable_presence = True ONLY if sustained breath detected. Energy alone is not sufficient in
        # persistent-high-energy indoor environments to avoid constant false positives.
        stable_presence = 0
        if self.breath_on_count >= DEFAULT_BREATH_ON:
            stable_presence = 1

        # force clear if general presence_off_count is high
        if self.presence_off_count >= DEFAULT_PRESENCE_OFF:
            stable_presence = 0

        # keep a short presence history for debug/compatibility
        self.presence_history.append(1 if stable_presence == 1 else 0)

        # Additional sanity checks: clamp distance to sensor range (0.02m - 11m) and discard improbable spikes
        if med > 11.0 or (med < 0.02 and med != 0.0):
            # treat as invalid reading
            med = 0.0

        # 突变剔除 + EMA 平滑：先去除跳变，再做轻度平滑，兼顾准确与响应速度
        out_distance = 0.0
        if med > 0:
            if self.last_valid_distance is not None and abs(med - self.last_valid_distance) > DEFAULT_DISTANCE_SPIKE:
                # 疑似突变：短暂保持；若持续超过 N 帧则视为真实移动并接受新值
                self.spike_hold_count += 1
                if self.spike_hold_count > DEFAULT_SPIKE_HOLD_FRAMES:
                    self.spike_hold_count = 0
                else:
                    med = self.last_valid_distance
            else:
                self.spike_hold_count = 0
            if self.last_distance_ema is None:
                self.last_distance_ema = med
            else:
                self.last_distance_ema = (DEFAULT_DISTANCE_EMA_ALPHA * med
                                          + (1 - DEFAULT_DISTANCE_EMA_ALPHA) * self.last_distance_ema)
            out_distance = self.last_distance_ema
            self.last_valid_distance = out_distance
        else:
            self.last_distance_ema = None
            self.last_valid_distance = None
            self.spike_hold_count = 0

        # debug logging if enabled
        if getattr(self, 'debug', False):
            print(f"[C4002] sensor:{self.sensor_id} type:{chosen_type} raw_exist_cm:{exist_target_distance} exist_en:{exist_target_energy} move_cm:{move_target_distance} move_en:{move_target_energy} move_speed:{move_target_speed} chosen_m:{distance_m:.2f} med:{med:.2f} avg_en:{avg_energy:.1f} avg_move_en:{avg_move_energy:.1f} sel_th:{SELECTION_THRESHOLD} pres_th:{PRESENCE_THRESHOLD} breath_th:{DEFAULT_BREATH_ENERGY_THRESHOLD} breath_on:{self.breath_on_count}/{DEFAULT_BREATH_ON} mot_on:{self.motion_on_count}/{DEFAULT_PRESENCE_ON} mot_speed_min:{DEFAULT_MOTION_SPEED_MIN} dist_var:{distance_variation:.3f} dist_var_th:{DISTANCE_VARIANCE_THRESHOLD} motion:{motion_condition} saturated:{saturated} on_count:{self.presence_on_count} off_count:{self.presence_off_count} pres_hist:{list(self.presence_history)}")

        return {
            'distance': float(out_distance),
            'signal': int(chosen_energy),
            'presence': int(stable_presence),
            'presence_raw': int(raw_presence),
            'presence_stable': int(stable_presence),
            'breath_evidence': int(breath_evidence),
            'motion': int(motion_condition),
            'target_status': int(target_status),
            'angle': self.angle,
            'sensor_id': self.sensor_id,
            'valid': True if out_distance > 0 else False,
            'timestamp': time.time()
        }


# Helper hub: create multiple radar wrappers based on an ordered list of ports
class RealSensorHub:
    def __init__(self, ports=None, angles=None, baud=115200):
        # ports: list of serial port paths
        if ports is None:
            ports = ['/dev/ttyAMA0']
        if angles is None:
            # default angles for up to 3 sensors
            angles = [-45, 0, 45]
        self.radars = []
        for i, p in enumerate(ports):
            ang = angles[i] if i < len(angles) else 0
            sensor = C4002Serial(port=p, baud=baud, angle=ang, sensor_id=i)
            # enable debug via env var C4002_DEBUG=1
            import os
            if os.getenv('C4002_DEBUG') == '1':
                sensor.debug = True
            # 下发初始化配置（低灵敏度 + 检测范围 + 消失延迟），可通过 C4002_ENABLE_CONFIG=0 关闭
            sensor.configure()
            self.radars.append(sensor)
        # 超声波：树莓派板载 GPIO（pigpio 硬件定时，微秒级精度）
        self.ultrasonics = []
        self._ultra_backend = None
        try:
            from ultrasonic_rpigpio import RpiUltrasonic, UltrasonicSensor
            self._ultra_backend = RpiUltrasonic()
            ultra_angles = [-45, 0, 45]
            for trig, echo, ang in zip([22, 24, 5], [23, 25, 6], ultra_angles):
                self.ultrasonics.append(
                    UltrasonicSensor(self._ultra_backend, trig, echo, ang))
        except Exception as e:
            print(f"超声波初始化失败（pigpiod 未启动或 RPi.GPIO 不可用）: {e}")
            self.ultrasonics = []

    def scan_all(self):
        results = []
        for r in self.radars:
            results.append(r.read_data())
        for u in self.ultrasonics:
            results.append(u.read_data())
        return results
