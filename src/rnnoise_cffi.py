import cffi
import numpy as np

ffi = cffi.FFI()
ffi.cdef("""
    typedef struct DenoiseState DenoiseState;
    DenoiseState* rnnoise_create(void);
    void rnnoise_destroy(DenoiseState* st);
    float rnnoise_process_frame(DenoiseState* st, float* out, const float* in);
""")

# 请确保该路径与你编译的 librnnoise.so 位置一致
lib = ffi.dlopen("/home/wanjin1234/anc_env/rnnoise_lib/lib/librnnoise.so")


class RNNoise:
    def __init__(self):
        self.st = lib.rnnoise_create()
        if self.st == ffi.NULL:
            raise MemoryError("Failed to create RNNoise state")

    def filter(self, frame: np.ndarray) -> np.ndarray:
        """
        输入：一维 numpy float32 数组，长度必须为 480
        返回：降噪后的 float32 数组，长度 480
        """
        if frame.shape[0] != 480:
            raise ValueError(f"RNNoise 要求帧长为 480，实际为 {frame.shape[0]}")

        # 确保内存连续且为 float32
        frame = np.ascontiguousarray(frame, dtype=np.float32)
        out = np.zeros(480, dtype=np.float32)

        in_ptr = ffi.cast("float*", frame.ctypes.data)
        out_ptr = ffi.cast("float*", out.ctypes.data)

        # 返回值为接收到的语音活动概率，这里忽略
        _ = lib.rnnoise_process_frame(self.st, out_ptr, in_ptr)
        return out

    def close(self):
        if self.st is not None:
            lib.rnnoise_destroy(self.st)
            self.st = None

    def __del__(self):
        self.close()
