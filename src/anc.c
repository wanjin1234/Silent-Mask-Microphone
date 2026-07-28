#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <alsa/asoundlib.h>
#include "S_coeffs.h"
#include <stdint.h>
#include <sys/time.h>
#include <signal.h>


// ---------- 可调参数 ----------
#define FILTER_LEN  128       // 自适应滤波器阶数
#define MU          0.0001f   // 收敛步长
#define LEAKY       0.9999f   // 泄露因子

// ---------- 全局变量 ----------
static float W[FILTER_LEN];
#define X_BUF_LEN  (FILTER_LEN + S_LEN)
static float x_buf[X_BUF_LEN];
static int   x_idx = 0;
static float fxl_buf[FILTER_LEN];

// ALSA 设备句柄
static snd_pcm_t *cap_handle, *play_handle;
static snd_pcm_uframes_t period_size = 64;
static unsigned int sample_rate = 16000;

FILE *rec_file = NULL;
int rec_duration = 10;         // 录制时长（秒）
int rec_samples_target = 0;   // 目标样本数
int rec_samples_written = 0;  // 已写入样本数
struct timeval start_time;    // 程序启动时间

// 写入 WAV 文件头（16kHz, 16bit, mono）
void write_wav_header(FILE *f, int sample_rate, int num_samples) {
    int byte_rate = sample_rate * 2;  // 16bit mono
    int data_size = num_samples * 2;
    // RIFF header
    fwrite("RIFF", 1, 4, f);
    int32_t chunk_size = 36 + data_size;
    fwrite(&chunk_size, 4, 1, f);
    fwrite("WAVE", 1, 4, f);
    // fmt subchunk
    fwrite("fmt ", 1, 4, f);
    int32_t subchunk1_size = 16;
    fwrite(&subchunk1_size, 4, 1, f);
    int16_t audio_format = 1; // PCM
    fwrite(&audio_format, 2, 1, f);
    int16_t num_channels = 1;
    fwrite(&num_channels, 2, 1, f);
    fwrite(&sample_rate, 4, 1, f);
    fwrite(&byte_rate, 4, 1, f);
    int16_t block_align = 2;
    fwrite(&block_align, 2, 1, f);
    int16_t bits_per_sample = 16;
    fwrite(&bits_per_sample, 2, 1, f);
    // data subchunk
    fwrite("data", 1, 4, f);
    fwrite(&data_size, 4, 1, f);
}


// ---------- 初始化自适应滤波器 ----------
void anc_init() {
    memset(W, 0, sizeof(W));
    memset(x_buf, 0, sizeof(x_buf));
    memset(fxl_buf, 0, sizeof(fxl_buf));
    x_idx = 0;
}

// ---------- FxLMS 核心处理 ----------
float anc_process(float ref, float err) {
    // 1. 更新参考延迟线
    x_buf[x_idx] = ref;

    // 2. 计算扬声器输出 y = W * x
    float y = 0.0f;
    for (int i = 0; i < FILTER_LEN; i++) {
        int pos = (x_idx + 1 + i) % X_BUF_LEN;
        y += W[i] * x_buf[pos];
    }

    // 3. 计算 filtered‑x: fxl = S * x（只对最近 FILTER_LEN 个样本）
    for (int i = 0; i < FILTER_LEN; i++) {
        float sum = 0.0f;
        for (int j = 0; j < S_LEN; j++) {
            int pos = (x_idx + 1 + i + j) % X_BUF_LEN;
            sum += S[j] * x_buf[pos];
        }
        fxl_buf[i] = sum;
    }

    // 4. 更新权重 W[i] = leaky*W[i] + 2*mu*err*fxl_buf[i]
    for (int i = 0; i < FILTER_LEN; i++) {
        W[i] = LEAKY * W[i] + 2.0f * MU * err * fxl_buf[i];
    }

    // 5. 索引前移
    x_idx = (x_idx + 1) % X_BUF_LEN;

    // 6. 限幅并输出反相波
    if (y > 1.0f) y = 1.0f;
    if (y < -1.0f) y = -1.0f;
    return y;
}

// ---------- 设置 ALSA 实时音频 ----------
void audio_setup() {
    int err;
    snd_pcm_hw_params_t *hw_params;

    // 打开录音设备（双声道）
    if ((err = snd_pcm_open(&cap_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_CAPTURE, 0)) < 0) {
        fprintf(stderr, "无法打开录音设备: %s\n", snd_strerror(err));
        exit(1);
    }
    snd_pcm_hw_params_alloca(&hw_params);
    snd_pcm_hw_params_any(cap_handle, hw_params);
    snd_pcm_hw_params_set_access(cap_handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(cap_handle, hw_params, SND_PCM_FORMAT_S16_LE);
    snd_pcm_hw_params_set_rate_near(cap_handle, hw_params, &sample_rate, 0);
    snd_pcm_hw_params_set_channels(cap_handle, hw_params, 2);
    snd_pcm_hw_params_set_period_size_near(cap_handle, hw_params, &period_size, 0);
    snd_pcm_uframes_t buffer_size = period_size * 4;
    snd_pcm_hw_params_set_buffer_size_near(cap_handle, hw_params, &buffer_size);
    snd_pcm_hw_params(cap_handle, hw_params);
    snd_pcm_hw_params_get_period_size(hw_params, &period_size, 0);
    printf("录音 period_size = %u, buffer_size = %u\n", period_size, buffer_size);

    // 打开播放设备（双声道，左：反相波，右：误差信号）
    if ((err = snd_pcm_open(&play_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_PLAYBACK, 0)) < 0) {
        fprintf(stderr, "无法打开播放设备: %s\n", snd_strerror(err));
        exit(1);
    }
    snd_pcm_hw_params_any(play_handle, hw_params);
    snd_pcm_hw_params_set_access(play_handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(play_handle, hw_params, SND_PCM_FORMAT_S16_LE);
    snd_pcm_hw_params_set_rate_near(play_handle, hw_params, &sample_rate, 0);
    snd_pcm_hw_params_set_channels(play_handle, hw_params, 2);       // 立体声输出
    snd_pcm_hw_params_set_period_size_near(play_handle, hw_params, &period_size, 0);
    snd_pcm_hw_params_set_buffer_size_near(play_handle, hw_params, &buffer_size);
    snd_pcm_hw_params(play_handle, hw_params);
}

// ---------- 主函数：实时循环 ----------
int main() {
    anc_init();
    gettimeofday(&start_time, NULL);
rec_samples_target = sample_rate * rec_duration;
rec_file = fopen("anc_rec.wav", "wb");
if (rec_file) {
    // 先预留 WAV 头空间，稍后更新
    fseek(rec_file, 44, SEEK_SET);
}
    audio_setup();

    short *cap_buf = malloc(period_size * 2 * sizeof(short)); // 双声道输入
    short *play_buf = malloc(period_size * 2 * sizeof(short)); // 双声道输出（左=反相波，右=误差）

    printf("ANC 实时降噪已启动，按 Ctrl+C 停止\n");
volatile int keep_running = 1;
void int_handler(int sig) { keep_running = 0; }
signal(SIGINT, int_handler);
    while (keep_running) {
        int frames = snd_pcm_readi(cap_handle, cap_buf, period_size);
        if (frames < 0) {
            frames = snd_pcm_recover(cap_handle, frames, 0);
            continue;
        }

        for (int i = 0; i < frames; i++) {
            float ref = cap_buf[i*2]   / 32768.0f;   // 参考麦克风
            float err = cap_buf[i*2+1] / 32768.0f;   // 误差麦克风
            float anti = anc_process(ref, err);

            short anti_out = (short)(anti * 32767.0f);
play_buf[i*2]   = 0;                  // 左声道静音（你的左耳不响）
play_buf[i*2+1] = anti_out;           // 右声道：反相波
            if (rec_file && rec_samples_written < rec_samples_target) {
    // 写入误差信号（右声道），16bit signed little-endian
    short err_short = (short)(err * 32767.0f);
fwrite(&err_short, sizeof(short), 1, rec_file);
    rec_samples_written++;
}
        }

        int written = snd_pcm_writei(play_handle, play_buf, frames);
        if (written < 0) {
            snd_pcm_recover(play_handle, written, 0);
        }
    }
if (rec_file) {
    // 更新 WAV 头
    fseek(rec_file, 0, SEEK_SET);
    write_wav_header(rec_file, sample_rate, rec_samples_written);
    fclose(rec_file);
    printf("录音已保存为 anc_rec.wav (%d samples)\n", rec_samples_written);
}

    free(cap_buf);
    free(play_buf);
    snd_pcm_close(cap_handle);
    snd_pcm_close(play_handle);
    return 0;
}