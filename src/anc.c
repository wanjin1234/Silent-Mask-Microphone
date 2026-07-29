#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <time.h>
#include <alsa/asoundlib.h>
#include "S_coeffs.h"

/* ---------- 可调参数 ---------- */
#define FILTER_LEN      128         // 自适应滤波器长度
#define MU              0.005f      // 步长（原 0.0001 过小，且去除了多余的 2.0 因子）
#define LEAKY           0.999f      // 泄漏因子（稍加强泄漏，防止权重漂移）
#define NORM_EPS        1e-6f       // 归一化防止除零
#define OUTPUT_LIMIT    0.8f        // 输出软限幅阈值（避免硬截断产生高频谐波）

/* ---------- 缓冲区定义 ---------- */
#define X_BUF_LEN       (FILTER_LEN + S_LEN)   // 参考信号历史缓冲
static float x_buf[X_BUF_LEN];                // 环形缓冲区
static int   x_ptr = -1;                      // 当前最新样本位置（初始 -1 表示空）

static float W[FILTER_LEN];                   // 自适应滤波器权重
static float fxl_buf[FILTER_LEN];             // 滤波‑参考信号（用于权重更新）
static float xf_power = 0.0f;                 // 滤波参考信号功率（归一化用）

/* ---------- 音频设备 ---------- */
static snd_pcm_t *cap_handle, *play_handle;
static snd_pcm_uframes_t period_size = 64;    // 单次中断帧数（16kHz 下 ≈ 4 ms）
static unsigned int sample_rate = 16000;

volatile int keep_running = 1;
void int_handler(int sig) { keep_running = 0; }

/* ---------- WAV 文件头 ---------- */
void write_wav_header(FILE *f, int sr, int num_samples) {
    int byte_rate   = sr * 2;               // 单声道 16‑bit
    int data_size   = num_samples * 2;
    fwrite("RIFF", 1, 4, f);
    int32_t chunk_size = 36 + data_size; fwrite(&chunk_size, 4, 1, f);
    fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f);
    int32_t subchunk1_size = 16; fwrite(&subchunk1_size, 4, 1, f);
    int16_t audio_format = 1; fwrite(&audio_format, 2, 1, f);
    int16_t num_channels = 1; fwrite(&num_channels, 2, 1, f);
    fwrite(&sr, 4, 1, f);
    fwrite(&byte_rate, 4, 1, f);
    int16_t block_align = 2; fwrite(&block_align, 2, 1, f);
    int16_t bits_per_sample = 16; fwrite(&bits_per_sample, 2, 1, f);
    fwrite("data", 1, 4, f);
    fwrite(&data_size, 4, 1, f);
}

/* ---------- ANC 初始化 ---------- */
void anc_init(void) {
    memset(W, 0, sizeof(W));
    memset(x_buf, 0, sizeof(x_buf));
    memset(fxl_buf, 0, sizeof(fxl_buf));
    x_ptr = -1;
    xf_power = 1e-3f;   // 避免开局除零
    srand(time(NULL));
}

/* ---------- 核心处理（每样本调用） ---------- */
float anc_process(float ref, float err) {
    /* 1. 写入环形缓冲区（x_ptr 始终指向最新样本） */
    x_ptr = (x_ptr + 1) % X_BUF_LEN;
    x_buf[x_ptr] = ref;

    /* 2. 计算滤波器输出 y(n) = W^T * x(n) */
    float y = 0.0f;
    for (int i = 0; i < FILTER_LEN; i++) {
        // 从最新样本往回数 i 步
        int idx = (x_ptr - i + X_BUF_LEN) % X_BUF_LEN;
        y += W[i] * x_buf[idx];
    }

    /* 3. 计算滤波‑参考信号 x'(n) = S * x(n)，
     *    同时为每个 W[i] 准备 x'(n-i) */
    float xf_inst = 0.0f;   // x'(n) 瞬时值（用于功率估计）
    for (int i = 0; i < FILTER_LEN; i++) {
        float sum = 0.0f;
        for (int j = 0; j < S_LEN; j++) {
            // x(n - i - j)
            int idx = (x_ptr - i - j + 2 * X_BUF_LEN) % X_BUF_LEN;
            sum += S[j] * x_buf[idx];
        }
        fxl_buf[i] = sum;
        if (i == 0) xf_inst = sum;   // i=0 对应 x'(n)
    }

    /* 4. 归一化步长（提高稳定性） */
    xf_power = 0.99f * xf_power + 0.01f * (xf_inst * xf_inst);
    float mu_norm = MU / (xf_power * (float)FILTER_LEN + NORM_EPS);

    /* 5. 权重更新（泄漏 FxLMS） */
    for (int i = 0; i < FILTER_LEN; i++) {
        W[i] = LEAKY * W[i] + mu_norm * err * fxl_buf[i];
    }

    /* 6. 输出软限幅（避免硬截断带来的高频失真） */
    if (y >  OUTPUT_LIMIT) y =  OUTPUT_LIMIT;
    if (y < -OUTPUT_LIMIT) y = -OUTPUT_LIMIT;

    /* 7. 返回反相波（根据实测，该硬件需反相） */
    return -y;
}

/* ---------- ALSA 初始化 ---------- */
void audio_setup(void) {
    int err;
    snd_pcm_hw_params_t *hw_params;

    /* 打开 capture 设备 */
    if ((err = snd_pcm_open(&cap_handle, "plughw:seeed2micvoicec",
                            SND_PCM_STREAM_CAPTURE, 0)) < 0) {
        fprintf(stderr, "无法打开采集设备: %s\n", snd_strerror(err));
        exit(1);
    }
    snd_pcm_hw_params_alloca(&hw_params);
    snd_pcm_hw_params_any(cap_handle, hw_params);
    snd_pcm_hw_params_set_access(cap_handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(cap_handle, hw_params, SND_PCM_FORMAT_S16_LE);
    snd_pcm_hw_params_set_rate_near(cap_handle, hw_params, &sample_rate, 0);
    snd_pcm_hw_params_set_channels(cap_handle, hw_params, 2);   // 左=参考, 右=误差
    snd_pcm_hw_params_set_period_size_near(cap_handle, hw_params, &period_size, 0);
    snd_pcm_uframes_t buffer_size = period_size * 4;
    snd_pcm_hw_params_set_buffer_size_near(cap_handle, hw_params, &buffer_size);
    if ((err = snd_pcm_hw_params(cap_handle, hw_params)) < 0) {
        fprintf(stderr, "采集硬件参数错误: %s\n", snd_strerror(err));
        exit(1);
    }

    /* 打开 playback 设备 */
    if ((err = snd_pcm_open(&play_handle, "plughw:seeed2micvoicec",
                            SND_PCM_STREAM_PLAYBACK, 0)) < 0) {
        fprintf(stderr, "无法打开回放设备: %s\n", snd_strerror(err));
        exit(1);
    }
    snd_pcm_hw_params_any(play_handle, hw_params);
    snd_pcm_hw_params_set_access(play_handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(play_handle, hw_params, SND_PCM_FORMAT_S16_LE);
    snd_pcm_hw_params_set_rate_near(play_handle, hw_params, &sample_rate, 0);
    snd_pcm_hw_params_set_channels(play_handle, hw_params, 2);
    snd_pcm_hw_params_set_period_size_near(play_handle, hw_params, &period_size, 0);
    snd_pcm_hw_params_set_buffer_size_near(play_handle, hw_params, &buffer_size);
    if ((err = snd_pcm_hw_params(play_handle, hw_params)) < 0) {
        fprintf(stderr, "回放硬件参数错误: %s\n", snd_strerror(err));
        exit(1);
    }

    printf("音频设备已配置: %u Hz, period = %lu frames\n",
           sample_rate, period_size);
}

/* ---------- 主循环 ---------- */
int main(void) {
    signal(SIGINT, int_handler);
    anc_init();
    audio_setup();

    int max_samples = sample_rate * 10;   // 10 秒录制
    short *rec_buffer = (short *)malloc(max_samples * sizeof(short));
    if (!rec_buffer) { fprintf(stderr, "内存分配失败\n"); exit(1); }
    int rec_count = 0;

    short *cap_buf  = malloc(period_size * 2 * sizeof(short));
    short *play_buf = malloc(period_size * 2 * sizeof(short));
    if (!cap_buf || !play_buf) { fprintf(stderr, "内存分配失败\n"); exit(1); }

    printf("前馈 ANC 启动（Mask 模式），录制 10 s ...\n");
    printf("参照麦克风: 左声道 | 误差麦克风: 右声道\n");
    printf("输出: 左声道静音, 右声道反相波\n");

    long total_frames = 0;
    while (keep_running && rec_count < max_samples) {
        /* 1. 采集一帧 */
        int frames = snd_pcm_readi(cap_handle, cap_buf, period_size);
        if (frames < 0) {
            frames = snd_pcm_recover(cap_handle, frames, 0);
            if (frames < 0) {
                fprintf(stderr, "采集恢复失败: %s\n", snd_strerror(frames));
                break;
            }
            continue;   // 恢复后重试
        }
        if (frames == 0) continue;

        /* 2. 逐样本处理 */
        for (int i = 0; i < frames; i++) {
            float ref = cap_buf[i * 2]     / 32768.0f;   // 参考麦（内部语音）
            float err = cap_buf[i * 2 + 1] / 32768.0f;   // 误差麦（外部残留）

            float anti = anc_process(ref, err);

            /* 输出组装（双声道） */
            short anti_out = (short)(anti * 32767.0f);
            play_buf[i * 2]     = 0;                      // 左声道静音（避免回声到左耳）
            play_buf[i * 2 + 1] = anti_out;               // 右声道播放反相波

            /* 录制误差信号（用于离线分析） */
            if (rec_count < max_samples) {
                rec_buffer[rec_count++] = (short)(err * 32767.0f);
            }
        }

        /* 3. 播放一帧 */
        int written = snd_pcm_writei(play_handle, play_buf, frames);
        if (written < 0) {
            written = snd_pcm_recover(play_handle, written, 0);
            if (written < 0) {
                fprintf(stderr, "回放恢复失败: %s\n", snd_strerror(written));
                break;
            }
        }

        /* 4. 实时监控（每秒打印 RMS） */
        total_frames += frames;
        if (total_frames % sample_rate < frames) {
            double rms = 0.0;
            int start = rec_count > sample_rate ? rec_count - sample_rate : 0;
            for (int k = start; k < rec_count; k++) {
                double val = rec_buffer[k] / 32768.0;
                rms += val * val;
            }
            if (rec_count > start) {
                rms = sqrt(rms / (rec_count - start));
                printf("[%03ld s] 误差 RMS = %.6f\n", total_frames / sample_rate, rms);
            }
        }
    }

    /* 保存录音 */
    if (rec_count > 0) {
        FILE *f = fopen("anc_rec.wav", "wb");
        if (f) {
            write_wav_header(f, sample_rate, rec_count);
            fwrite(rec_buffer, sizeof(short), rec_count, f);
            fclose(f);
            printf("已保存 %d 样本到 anc_rec.wav\n", rec_count);
        } else {
            fprintf(stderr, "无法保存录音文件\n");
        }
    }

    /* 清理 */
    free(rec_buffer);
    free(cap_buf);
    free(play_buf);
    snd_pcm_close(cap_handle);
    snd_pcm_close(play_handle);
    printf("ANC 已停止.\n");
    return 0;
}