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
DEFAULT_HISTORY_LEN = int(os.getenv('C4002_HISTORY_LEN', '7'))
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
        if data_len < 11:
            return {'valid': False}

        target_status = pdata[base + 0]
        light = (pdata[base + 2] << 8) | pdata[base + 1]
        exist_gate_index = (pdata[base + 6] << 24) | (pdata[base + 5] << 16) | (pdata[base + 4] << 8) | pdata[base + 3]
        exist_count_down = (pdata[base + 8] << 8) | pdata[base + 7]
        exist_target_distance = (pdata[base + 10] << 8) | pdata[base + 9]
        exist_target_energy = pdata[base + 11] if (base + 11) < len(pdata) else 0
        # move target fields
        move_target_distance = (pdata[base + 13] << 8) | pdata[base + 12] if (base + 13) < len(pdata) else 0
        move_target_energy = pdata[base + 16] if (base + 16) < len(pdata) else 0

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
        # energy smoothing
        self.energy_history.append(int(chosen_energy))
        avg_energy = sum(self.energy_history) / len(self.energy_history) if len(self.energy_history) > 0 else 0

        # hysteresis counters for presence to avoid transient spikes
        # consider presence only when raw_presence is true and averaged energy is above presence threshold
        if raw_presence and avg_energy >= PRESENCE_THRESHOLD:
            self.presence_on_count += 1
            self.presence_off_count = 0
        else:
            self.presence_off_count += 1
            self.presence_on_count = 0

        # breath energy smoothing (use move_target_energy as micro-motion indicator)
        self.breath_energy_history.append(int(move_target_energy))
        avg_move_energy = sum(self.breath_energy_history) / len(self.breath_energy_history) if len(self.breath_energy_history) > 0 else 0

        # breath hysteresis counters
        if raw_presence and avg_move_energy >= DEFAULT_BREATH_ENERGY_THRESHOLD:
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

        # decide stable presence using breath OR motion counters (both use hysteresis)
        stable_presence = 0
        if self.breath_on_count >= DEFAULT_BREATH_ON:
            stable_presence = 1
        elif self.motion_on_count >= DEFAULT_PRESENCE_ON:
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
            print(f"[C4002] sensor:{self.sensor_id} type:{chosen_type} raw_exist_cm:{exist_target_distance} exist_en:{exist_target_energy} move_cm:{move_target_distance} move_en:{move_target_energy} chosen_m:{distance_m:.2f} med:{med:.2f} avg_en:{avg_energy:.1f} avg_move_en:{avg_move_energy:.1f} sel_th:{SELECTION_THRESHOLD} pres_th:{PRESENCE_THRESHOLD} breath_th:{DEFAULT_BREATH_ENERGY_THRESHOLD} breath_on:{self.breath_on_count}/{DEFAULT_BREATH_ON} mot_on:{self.motion_on_count}/{DEFAULT_PRESENCE_ON} mot_en_th:{MOTION_ENERGY_THRESHOLD} dist_var:{distance_variation:.3f} dist_var_th:{DISTANCE_VARIANCE_THRESHOLD} motion:{motion_condition} on_count:{self.presence_on_count} off_count:{self.presence_off_count} pres_hist:{list(self.presence_history)}")

        return {
            'distance': float(med),
            'signal': int(chosen_energy),
            'presence': int(stable_presence),
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
