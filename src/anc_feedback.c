#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <unistd.h>
#include <alsa/asoundlib.h>

#define FB_FILTER_LEN  128
#define FB_MU          0.01f      // 适当加大步长
#define FB_LEAKY       0.999f
#define PROBE_NOISE    0.0005f    // 探测噪声幅度（-60 dB 左右）

static float W_fb[FB_FILTER_LEN];
static float err_buf[FB_FILTER_LEN];
static int   err_idx = 0;

static snd_pcm_t *cap_handle = NULL;
static snd_pcm_t *play_handle = NULL;

static unsigned int sample_rate = 16000;
static unsigned int period_time_us = 40000;
static unsigned int buffer_time_us = 160000;

volatile int keep_running = 1;
void int_handler(int sig) { keep_running = 0; }

#define LOOPBACK_TEST 0   // 设为 1 进行回环测试，0 运行 ANC

/* 写 WAV 头 */
void write_wav_header(FILE *f, int sr, int num) {
    int br = sr * 2, ds = num * 2;
    fwrite("RIFF", 1, 4, f);
    int32_t cs = 36 + ds;
    fwrite(&cs, 4, 1, f);
    fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f);
    int32_t sz = 16;
    fwrite(&sz, 4, 1, f);
    int16_t af = 1, nc = 1;
    fwrite(&af, 2, 1, f);
    fwrite(&nc, 2, 1, f);
    fwrite(&sr, 4, 1, f);
    fwrite(&br, 4, 1, f);
    int16_t ba = 2, bps = 16;
    fwrite(&ba, 2, 1, f);
    fwrite(&bps, 2, 1, f);
    fwrite("data", 1, 4, f);
    fwrite(&ds, 4, 1, f);
}

/* 将 W_fb 初始化为小的随机值，防止初始输出全为零 */
void anc_init_fb() {
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        W_fb[i] = (float)rand() / RAND_MAX * 0.001f - 0.0005f;  // ±0.0005
    }
    memset(err_buf, 0, sizeof(err_buf));
    err_idx = 0;
}

/* 反馈 ANC 处理（单样本） */
float anc_process_fb(float err) {
    // 1. 计算滤波器输出
    float y = 0.0f;
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        int pos = (err_idx - 1 - i + FB_FILTER_LEN) % FB_FILTER_LEN;
        y += W_fb[i] * err_buf[pos];
    }

    // 2. LMS 权重更新
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        int pos = (err_idx - 1 - i + FB_FILTER_LEN) % FB_FILTER_LEN;
        W_fb[i] = FB_LEAKY * W_fb[i] + 2.0f * FB_MU * err * err_buf[pos];
    }

    // 3. 保存当前误差，移动指针
    err_buf[err_idx] = err;
    err_idx = (err_idx + 1) % FB_FILTER_LEN;

    // 4. 限幅并反相输出，叠加微弱探测噪声
    if (y > 1.0f)  y = 1.0f;
    if (y < -1.0f) y = -1.0f;

    // 探测噪声（防止权重坍塌）
    float probe = ((float)rand() / RAND_MAX * 2.0f - 1.0f) * PROBE_NOISE;
    return -(y + probe);
}

/* 设置 ALSA PCM（采集或播放） */
int setup_pcm(snd_pcm_t **handle, const char *device, snd_pcm_stream_t stream) {
    int err;
    snd_pcm_hw_params_t *hw_params;

    if ((err = snd_pcm_open(handle, device, stream, 0)) < 0) {
        fprintf(stderr, "无法打开设备 %s: %s\n", device, snd_strerror(err));
        return err;
    }

    snd_pcm_hw_params_alloca(&hw_params);
    snd_pcm_hw_params_any(*handle, hw_params);

    snd_pcm_hw_params_set_access(*handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(*handle, hw_params, SND_PCM_FORMAT_S16_LE);

    unsigned int rate = sample_rate;
    int dir = 0;
    snd_pcm_hw_params_set_rate_near(*handle, hw_params, &rate, &dir);
    if (rate != sample_rate) {
        fprintf(stderr, "采样率调整为 %u Hz\n", rate);
        sample_rate = rate;
    }

    snd_pcm_hw_params_set_channels(*handle, hw_params, 2);

    unsigned int period_us = period_time_us;
    dir = 0;
    snd_pcm_hw_params_set_period_time_near(*handle, hw_params, &period_us, &dir);

    unsigned int buffer_us = buffer_time_us;
    dir = 0;
    snd_pcm_hw_params_set_buffer_time_near(*handle, hw_params, &buffer_us, &dir);

    if ((err = snd_pcm_hw_params(*handle, hw_params)) < 0) {
        fprintf(stderr, "硬件参数设置失败: %s\n", snd_strerror(err));
        return err;
    }

    snd_pcm_uframes_t period_frames, buffer_frames;
    snd_pcm_hw_params_get_period_size(hw_params, &period_frames, &dir);
    snd_pcm_hw_params_get_buffer_size(hw_params, &buffer_frames);
    printf("%s: 采样率=%u Hz, 周期=%lu 帧, 缓冲区=%lu 帧\n",
           stream == SND_PCM_STREAM_CAPTURE ? "采集" : "播放",
           sample_rate, period_frames, buffer_frames);

    return 0;
}

int main() {
    signal(SIGINT, int_handler);
    anc_init_fb();

    if (setup_pcm(&cap_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_CAPTURE) < 0)
        return 1;
    if (setup_pcm(&play_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_PLAYBACK) < 0) {
        snd_pcm_close(cap_handle);
        return 1;
    }

    // 获取实际周期大小
    snd_pcm_hw_params_t *hw_params;
    snd_pcm_hw_params_alloca(&hw_params);
    snd_pcm_hw_params_current(cap_handle, hw_params);
    snd_pcm_uframes_t cap_period;
    int dir;
    snd_pcm_hw_params_get_period_size(hw_params, &cap_period, &dir);

    snd_pcm_hw_params_current(play_handle, hw_params);
    snd_pcm_uframes_t play_period;
    snd_pcm_hw_params_get_period_size(hw_params, &play_period, &dir);

    snd_pcm_uframes_t frames = cap_period < play_period ? cap_period : play_period;
    printf("使用周期大小: %lu 帧\n", frames);

    // 分配内存
    short *cap_buf  = malloc(frames * 2 * sizeof(short));
    short *play_buf = calloc(frames * 2, sizeof(short));

    // 预填充播放缓冲区（静音）
    printf("预填充播放缓冲区...\n");
    for (int i = 0; i < 3; i++) {
        snd_pcm_writei(play_handle, play_buf, frames);
    }

#if LOOPBACK_TEST == 1
    printf("回环测试模式 (LOOPBACK_TEST=1)，按 Ctrl+C 停止...\n");
    while (keep_running) {
        int f = snd_pcm_readi(cap_handle, cap_buf, frames);
        if (f < 0) { snd_pcm_recover(cap_handle, f, 0); continue; }
        // 直接回环：采集 → 播放（可根据声道情况调整）
        for (int i = 0; i < f; i++) {
            // 尝试交换左右声道，看扬声器是否发声
            play_buf[i*2    ] = cap_buf[i*2+1];  // 假设麦克风在右声道
            play_buf[i*2+1  ] = 0;
            // 如果不行，改用下面一行：
            // play_buf[i*2] = cap_buf[i*2];   // 假设麦克风在左声道
        }
        int ret = snd_pcm_writei(play_handle, play_buf, f);
        if (ret < 0) {
            if (ret == -EPIPE) snd_pcm_prepare(play_handle);
            else snd_pcm_recover(play_handle, ret, 0);
        }
    }
#else
    int rec_dur = 10;
    int max_samples = sample_rate * rec_dur;
    short *err_rec  = calloc(max_samples, sizeof(short));
    short *anti_rec = calloc(max_samples, sizeof(short));
    int rec_count = 0;

    printf("反馈 ANC 启动，录制 %d 秒...\n", rec_dur);
    while (keep_running && rec_count < max_samples) {
        int f = snd_pcm_readi(cap_handle, cap_buf, frames);
        if (f < 0) { snd_pcm_recover(cap_handle, f, 0); continue; }

        for (int i = 0; i < f; i++) {
            // 根据回环测试确定的声道映射修改这里！
            float err = cap_buf[i*2+1] / 32768.0f;  // 假设麦克风在右声道
            float anti = anc_process_fb(err);
            short anti_out = (short)(anti * 32767.0f);

            play_buf[i*2    ] = anti_out;   // 左声道输出反相波
            play_buf[i*2+1  ] = 0;

            if (rec_count < max_samples) {
                err_rec[rec_count]  = (short)(err * 32767.0f);
                anti_rec[rec_count] = anti_out;
                rec_count++;
            }
        }

        int ret = snd_pcm_writei(play_handle, play_buf, f);
        if (ret < 0) {
            if (ret == -EPIPE) {
                snd_pcm_prepare(play_handle);
                memset(play_buf, 0, f * 2 * sizeof(short));
                snd_pcm_writei(play_handle, play_buf, f);
            } else {
                snd_pcm_recover(play_handle, ret, 0);
            }
        }
    }

    // 保存文件
    FILE *fp = fopen("anc_rec.wav", "wb");
    if (fp) { write_wav_header(fp, sample_rate, rec_count);
              fwrite(err_rec, sizeof(short), rec_count, fp); fclose(fp); }
    fp = fopen("anti_fb.wav", "wb");
    if (fp) { write_wav_header(fp, sample_rate, rec_count);
              fwrite(anti_rec, sizeof(short), rec_count, fp); fclose(fp); }

    free(err_rec); free(anti_rec);
#endif

    free(cap_buf); free(play_buf);
    snd_pcm_close(cap_handle);
    snd_pcm_close(play_handle);
    printf("完成。\n");
    return 0;
}