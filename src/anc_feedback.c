#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <alsa/asoundlib.h>

#define FB_FILTER_LEN  128
#define FB_MU          0.005f
#define FB_LEAKY       0.999f

static float W_fb[FB_FILTER_LEN];
static float err_buf[FB_FILTER_LEN];
static int   err_idx = 0;       // 指向下一个要写入的位置（环形缓冲区）

static snd_pcm_t *cap_handle, *play_handle;
static snd_pcm_uframes_t period_size = 2048;
static unsigned int sample_rate = 16000;

volatile int keep_running = 1;
void int_handler(int sig) { keep_running = 0; }

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

void anc_init_fb() {
    memset(W_fb, 0, sizeof(W_fb));
    memset(err_buf, 0, sizeof(err_buf));
    err_idx = 0;
}

/**
 * 反馈 ANC 处理（单样本）
 * @param err  当前误差麦克风信号（归一化到 -1..1）
 * @return     扬声器输出（反相波，归一化到 -1..1）
 */
float anc_process_fb(float err) {
    // -------- 1. 用过去的误差样本计算滤波器输出 --------
    float y = 0.0f;
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        // 延迟 i 个样本：最新样本在 (err_idx - 1)，往前 i 个
        int pos = (err_idx - 1 - i + FB_FILTER_LEN) % FB_FILTER_LEN;
        y += W_fb[i] * err_buf[pos];
    }

    // -------- 2. LMS 权重更新 --------
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        int pos = (err_idx - 1 - i + FB_FILTER_LEN) % FB_FILTER_LEN;
        // w(n+1) = leaky * w(n) + 2 * mu * e(n) * x(n)   (x 是过去误差)
        W_fb[i] = FB_LEAKY * W_fb[i] + 2.0f * FB_MU * err * err_buf[pos];
    }

    // -------- 3. 保存当前误差，移动指针 --------
    err_buf[err_idx] = err;
    err_idx = (err_idx + 1) % FB_FILTER_LEN;

    // -------- 4. 限幅并反相输出 --------
    if (y > 1.0f)  y = 1.0f;
    if (y < -1.0f) y = -1.0f;
    return -y;   // 反相（增益可根据需要调整，比如 -y * 2.0f）
}

void audio_setup() {
    int err;
    snd_pcm_hw_params_t *hw_params;

    // ---------- 采集 ----------
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
    snd_pcm_hw_params_set_channels(cap_handle, hw_params, 2);
    snd_pcm_hw_params_set_period_size_near(cap_handle, hw_params, &period_size, 0);
    snd_pcm_uframes_t buffer_size = period_size * 4;
    snd_pcm_hw_params_set_buffer_size_near(cap_handle, hw_params, &buffer_size);
    if ((err = snd_pcm_hw_params(cap_handle, hw_params)) < 0) {
        fprintf(stderr, "采集硬件参数错误: %s\n", snd_strerror(err));
        exit(1);
    }

    // ---------- 播放 ----------
    if ((err = snd_pcm_open(&play_handle, "plughw:seeed2micvoicec",
                            SND_PCM_STREAM_PLAYBACK, 0)) < 0) {
        fprintf(stderr, "无法打开播放设备: %s\n", snd_strerror(err));
        exit(1);
    }
    snd_pcm_hw_params_alloca(&hw_params);
    snd_pcm_hw_params_any(play_handle, hw_params);
    snd_pcm_hw_params_set_access(play_handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(play_handle, hw_params, SND_PCM_FORMAT_S16_LE);
    snd_pcm_hw_params_set_rate_near(play_handle, hw_params, &sample_rate, 0);
    snd_pcm_hw_params_set_channels(play_handle, hw_params, 2);
    snd_pcm_hw_params_set_period_size_near(play_handle, hw_params, &period_size, 0);
    snd_pcm_hw_params_set_buffer_size_near(play_handle, hw_params, &buffer_size);
    if ((err = snd_pcm_hw_params(play_handle, hw_params)) < 0) {
        fprintf(stderr, "播放硬件参数错误: %s\n", snd_strerror(err));
        exit(1);
    }

    printf("ALSA 设备已打开。采样率=%u Hz，周期大小=%lu 帧\n",
           sample_rate, period_size);
}

int main() {
    signal(SIGINT, int_handler);
    anc_init_fb();
    audio_setup();

    int rec_dur = 10;
    int max_samples = sample_rate * rec_dur;
    short *err_rec  = malloc(max_samples * sizeof(short));
    short *anti_rec = malloc(max_samples * sizeof(short));
    int rec_count = 0;

    short *cap_buf  = malloc(period_size * 2 * sizeof(short));
    short *play_buf = malloc(period_size * 2 * sizeof(short));

    // 初始化 play_buf，避免输出随机垃圾
    memset(play_buf, 0, period_size * 2 * sizeof(short));

    printf("反馈 ANC 启动，录制 %d 秒...\n", rec_dur);

    while (keep_running && rec_count < max_samples) {
        int frames = snd_pcm_readi(cap_handle, cap_buf, period_size);
        if (frames < 0) {
            fprintf(stderr, "读取错误: %s\n", snd_strerror(frames));
            snd_pcm_recover(cap_handle, frames, 0);
            continue;
        }

        for (int i = 0; i < frames; i++) {
            // 假设右声道为误差麦克风（索引 1），左声道为参考/扬声器反馈？
            // 根据你的硬件连接调整声道索引
            float err = cap_buf[i * 2 + 1] / 32768.0f;   // 误差信号，归一化
            float anti = anc_process_fb(err);
            short anti_out = (short)(anti * 32767.0f);

            // 左声道输出反相波，右声道静音
            play_buf[i * 2    ] = anti_out;
            play_buf[i * 2 + 1] = 0;

            if (rec_count < max_samples) {
                err_rec[rec_count]  = (short)(err * 32767.0f);
                anti_rec[rec_count] = anti_out;
                rec_count++;
            }
        }

        int ret = snd_pcm_writei(play_handle, play_buf, frames);
        if (ret < 0) {
            fprintf(stderr, "写入错误: %s\n", snd_strerror(ret));
            snd_pcm_recover(play_handle, ret, 0);
        }
    }

    // 保存误差麦克风录音
    FILE *f = fopen("anc_rec.wav", "wb");
    if (f) {
        write_wav_header(f, sample_rate, rec_count);
        fwrite(err_rec, sizeof(short), rec_count, f);
        fclose(f);
    }

    // 保存反相波录音
    f = fopen("anti_fb.wav", "wb");
    if (f) {
        write_wav_header(f, sample_rate, rec_count);
        fwrite(anti_rec, sizeof(short), rec_count, f);
        fclose(f);
    }

    free(err_rec);
    free(anti_rec);
    free(cap_buf);
    free(play_buf);
    snd_pcm_close(cap_handle);
    snd_pcm_close(play_handle);

    printf("完成。anc_rec.wav 和 anti_fb.wav 已保存。\n");
    return 0;
}