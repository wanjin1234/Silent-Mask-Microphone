import cffi
import numpy as np

ffi = cffi.FFI()
ffi.cdef("""
    typedef struct DenoiseState DenoiseState;
    DenoiseState* rnnoise_create(void);
    void rnnoise_destroy(DenoiseState* st);
    float rnnoise_process_frame(DenoiseState* st, float* out, const float* in);
""")

lib = ffi.dlopen("/home/wanjin1234/anc_env/rnnoise_lib/lib/librnnoise.so")


class RNNoise:
    def __init__(self):
        self.st = lib.rnnoise_create()

    def filter(self, frame):
        # frame: numpy float32 array of 480 samples
        out = np.zeros(480, dtype=np.float32)
        in_ptr = ffi.cast("float*", frame.ctypes.data)
        out_ptr = ffi.cast("float*", out.ctypes.data)
        lib.rnnoise_process_frame(self.st, out_ptr, in_ptr)
        return out

    def __del__(self):
        lib.rnnoise_destroy(self.st)
