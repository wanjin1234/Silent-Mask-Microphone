# C4002 serial parser wrapper
# Parses C4002 frames from serial port and exposes a read_data() compatible dict

import time
import serial
import os
from collections import deque

# Frame constants (from DFRobot library)
FRAME_HEADER1 = 0xFA
FRAME_HEADER2 = 0xF5
FRAME_HEADER3 = 0xAA
FRAME_HEADER4 = 0xA5
FRAME_TYPE_NOTIFICATION = 0x04
NOTE_RESULT_CMD = 0x60

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
DEFAULT_HISTORY_LEN = int(os.getenv('C4002_HISTORY_LEN', '9'))
DEFAULT_PRESENCE_ON = int(os.getenv('C4002_PRESENCE_ON', '3'))
DEFAULT_PRESENCE_OFF = int(os.getenv('C4002_PRESENCE_OFF', '3'))
DEFAULT_ENERGY_SMOOTH_LEN = int(os.getenv('C4002_ENERGY_SMOOTH_LEN', '5'))
# Breath-detection specific defaults
DEFAULT_BREATH_ENERGY_THRESHOLD = int(os.getenv('C4002_BREATH_ENERGY_THRESHOLD', '6'))
DEFAULT_BREATH_SMOOTH_LEN = int(os.getenv('C4002_BREATH_SMOOTH_LEN', '15'))
DEFAULT_BREATH_ON = int(os.getenv('C4002_BREATH_ON', '3'))
DEFAULT_BREATH_OFF = int(os.getenv('C4002_BREATH_OFF', '4'))
# Motion detection defaults (on-count uses DEFAULT_PRESENCE_ON by default)
DEFAULT_MOTION_ENERGY_THRESHOLD = int(os.getenv('C4002_MOTION_ENERGY_THRESHOLD', '10'))
DEFAULT_DISTANCE_VARIANCE = float(os.getenv('C4002_DISTANCE_VARIANCE', '0.15'))
# Cap used to reduce influence of saturated (maxed) energy readings when averaging
DEFAULT_ENERGY_CAP = int(os.getenv('C4002_ENERGY_CAP', '80'))
DEFAULT_SATURATION_DISTANCE = float(os.getenv('C4002_SATURATION_DISTANCE', '2.0'))  # meters


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

    def _read_n_bytes(self, n, timeout=None):
        if self.ser is None:
            return None
        return self.ser.read(n)

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
                    if i == base + 14:
                        ann += ' <move_en>'
                    mapping.append(f'{i}:{pdata[i]:02x}{ann}')
                print(f"[C4002 BYTES] sensor:{self.sensor_id} base:{base} data_len:{data_len} bytes: {' '.join(mapping)}")
            except Exception:
                pass

        # Parse fields (use explicit bounds checks). Fields follow the original DFRobot layout:
        # 0: target_status
        # 1-2: light (uint16, little-endian)
        # 3-6: exist gate index (uint32, little-endian)
        # 7-8: exist count down (uint16)
        # 9-10: exist target distance (uint16, cm)
        # 11: exist target energy (uint8)
        # 12: move target distance (uint8, cm)
        # 13: move target speed (uint8)
        # 14: move target energy (uint8)
        # 15: move target direction (uint8)
        target_status = pdata[base + 0] if (base + 0) < len(pdata) else 0
        light = ((pdata[base + 2] << 8) | pdata[base + 1]) if (base + 2) < len(pdata) else 0
        exist_gate_index = (
            (pdata[base + 6] << 24) | (pdata[base + 5] << 16) | (pdata[base + 4] << 8) | pdata[base + 3]
        ) if (base + 6) < len(pdata) else 0
        exist_count_down = ((pdata[base + 8] << 8) | pdata[base + 7]) if (base + 8) < len(pdata) else 0
        exist_target_distance = ((pdata[base + 10] << 8) | pdata[base + 9]) if (base + 10) < len(pdata) else 0
        exist_target_energy = pdata[base + 11] if (base + 11) < len(pdata) else 0
        # move target fields (single-byte per original protocol)
        move_target_distance = pdata[base + 12] if (base + 12) < len(pdata) else 0
        move_target_speed = pdata[base + 13] if (base + 13) < len(pdata) else 0
        move_target_energy = pdata[base + 14] if (base + 14) < len(pdata) else 0
        move_target_direct = pdata[base + 15] if (base + 15) < len(pdata) else 0

        # choose the most likely valid target by energy
        # energies are uint8, apply a minimum threshold to reduce noise
        # selection uses a (separate) threshold so presence tuning doesn't suppress distance selection
        SELECTION_THRESHOLD = DEFAULT_ENERGY_THRESHOLD_SELECT
        PRESENCE_THRESHOLD = DEFAULT_ENERGY_THRESHOLD_PRESENCE
        chosen_distance_cm = 0
        chosen_energy = 0
        chosen_type = 'none'
        # Prefer the target with higher energy; require it to be >= selection threshold to be chosen as primary
        if move_target_energy >= exist_target_energy and move_target_energy >= SELECTION_THRESHOLD:
            chosen_distance_cm = move_target_distance
            chosen_energy = move_target_energy
            chosen_type = 'move'
        elif exist_target_energy >= SELECTION_THRESHOLD:
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

        # breath energy smoothing (use move_target_energy as micro-motion indicator), cap saturated values
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
                # choose the closer valid distance (non-zero)
                exist_d_m = exist_target_distance * 0.01 if exist_target_distance > 0 else None
                move_d_m = move_target_distance * 0.01 if move_target_distance > 0 else None
                chosen_override = None
                if exist_d_m is not None and move_d_m is not None:
                    chosen_override = 'move' if move_d_m < exist_d_m else 'exist'
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

        if presence_evidence:
            self.presence_on_count += 1
            self.presence_off_count = 0
        else:
            self.presence_off_count += 1
            self.presence_on_count = 0

        # breath hysteresis counters
        # Require micro-motion energy AND that the chosen distance is within a plausible human range
        # to avoid counting environmental noise or distant/super-close reflections as breathing.
        min_breath_distance = 0.2  # meters (ignore very close readings)
        max_breath_distance = DEFAULT_SATURATION_DISTANCE  # meters (configurable)
        if (raw_presence and avg_move_energy >= DEFAULT_BREATH_ENERGY_THRESHOLD
                and (distance_m >= min_breath_distance and distance_m <= max_breath_distance)):
            self.breath_on_count += 1
            self.breath_off_count = 0
        else:
            self.breath_off_count += 1
            self.breath_on_count = 0

        # compute stable distance as median, but ignore zeros (no target)
        try:
            nonzero = [d for d in self.dist_history if d > 0]
            if nonzero:
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

        # motion detection: either move_target_energy exceeds a motion threshold OR distance varies sufficiently
        MOTION_ENERGY_THRESHOLD = DEFAULT_MOTION_ENERGY_THRESHOLD
        DISTANCE_VARIANCE_THRESHOLD = DEFAULT_DISTANCE_VARIANCE
        motion_condition = (move_target_energy >= MOTION_ENERGY_THRESHOLD) or (distance_variation >= DISTANCE_VARIANCE_THRESHOLD)

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

        # debug logging if enabled
        if getattr(self, 'debug', False):
            print(f"[C4002] sensor:{self.sensor_id} type:{chosen_type} raw_exist_cm:{exist_target_distance} exist_en:{exist_target_energy} move_cm:{move_target_distance} move_en:{move_target_energy} chosen_m:{distance_m:.2f} med:{med:.2f} avg_en:{avg_energy:.1f} avg_move_en:{avg_move_energy:.1f} sel_th:{SELECTION_THRESHOLD} pres_th:{PRESENCE_THRESHOLD} breath_th:{DEFAULT_BREATH_ENERGY_THRESHOLD} breath_on:{self.breath_on_count}/{DEFAULT_BREATH_ON} mot_on:{self.motion_on_count}/{DEFAULT_PRESENCE_ON} mot_en_th:{MOTION_ENERGY_THRESHOLD} dist_var:{distance_variation:.3f} dist_var_th:{DISTANCE_VARIANCE_THRESHOLD} motion:{motion_condition} saturated:{saturated} on_count:{self.presence_on_count} off_count:{self.presence_off_count} pres_hist:{list(self.presence_history)}")

        return {
            'distance': float(med),
            'signal': int(chosen_energy),
            'presence': int(stable_presence),
            'presence_raw': int(raw_presence),
            'presence_stable': int(stable_presence),
            'angle': self.angle,
            'sensor_id': self.sensor_id,
            'valid': True if med > 0 else False,
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
            self.radars.append(sensor)
        # keep attribute compatibility with SimulatedSensorHub
        self.ultrasonics = []

    def scan_all(self):
        results = []
        for r in self.radars:
            results.append(r.read_data())
        # include ultrasonic placeholders (none) for compatibility
        for u in self.ultrasonics:
            results.append({'valid': False})
        return results
