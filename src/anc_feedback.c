#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <unistd.h>
#include <alsa/asoundlib.h>

/*===== 参数 =====*/
#define FB_FILTER_LEN       32      // 短滤波器（~2 ms），适合近距离声反馈
#define FB_MU               0.1f    // NLMS 步长
#define FB_LEAKY            0.999f  // 轻微泄漏
#define PILOT_FREQ          18000.0f// 高频导频音（人耳不可闻）
#define PILOT_AMP           0.001f  // 导频音幅度（-60 dB）
#define EPSILON             1e-6f

static float W_fb[FB_FILTER_LEN];
static float err_buf[FB_FILTER_LEN];
static int   err_idx = 0;
static float phase = 0.0f;          // 导频音相位

static snd_pcm_t *cap_handle = NULL, *play_handle = NULL;
static unsigned int sample_rate = 16000;
static unsigned int period_time_us = 40000;
static unsigned int buffer_time_us = 160000;

volatile int keep_running = 1;
void int_handler(int sig) { keep_running = 0; }

/* 写 WAV 头（单声道） */
void write_wav_header(FILE *f, int sr, int num) {
    int br = sr * 2, ds = num * 2;
    fwrite("RIFF", 1, 4, f);
    int32_t cs = 36 + ds; fwrite(&cs, 4, 1, f);
    fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f);
    int32_t sz = 16; fwrite(&sz, 4, 1, f);
    int16_t af = 1, nc = 1; fwrite(&af, 2, 1, f); fwrite(&nc, 2, 1, f);
    fwrite(&sr, 4, 1, f);
    fwrite(&br, 4, 1, f);
    int16_t ba = 2, bps = 16; fwrite(&ba, 2, 1, f); fwrite(&bps, 2, 1, f);
    fwrite("data", 1, 4, f);
    fwrite(&ds, 4, 1, f);
}

void anc_init() {
    memset(W_fb, 0, sizeof(W_fb));
    memset(err_buf, 0, sizeof(err_buf));
    err_idx = 0;
    phase = 0.0f;
}

/* NLMS 反馈 ANC 处理，返回反相波（归一化） */
float anc_process(float err) {
    // 1. 用过去的误差样本计算滤波器输出
    float y = 0.0f;
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        int pos = (err_idx - 1 - i + FB_FILTER_LEN) % FB_FILTER_LEN;
        y += W_fb[i] * err_buf[pos];
    }

    // 2. 计算归一化步长
    float power = 0.0f;
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        int pos = (err_idx - 1 - i + FB_FILTER_LEN) % FB_FILTER_LEN;
        power += err_buf[pos] * err_buf[pos];
    }
    float mu = FB_MU / (power + EPSILON);

    // 3. LMS 权重更新
    for (int i = 0; i < FB_FILTER_LEN; i++) {
        int pos = (err_idx - 1 - i + FB_FILTER_LEN) % FB_FILTER_LEN;
        W_fb[i] = FB_LEAKY * W_fb[i] + 2.0f * mu * err * err_buf[pos];
    }

    // 4. 保存当前误差，移动指针
    err_buf[err_idx] = err;
    err_idx = (err_idx + 1) % FB_FILTER_LEN;

    // 5. 限幅
    if (y > 1.0f)  y = 1.0f;
    if (y < -1.0f) y = -1.0f;

    // 6. 叠加高频导频音（保持滤波器活跃）
    phase += 2.0f * M_PI * PILOT_FREQ / sample_rate;
    if (phase > 2.0f * M_PI) phase -= 2.0f * M_PI;
    float pilot = PILOT_AMP * sinf(phase);

    return -(y + pilot);
}

/* ALSA PCM 初始化 */
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
    if (rate != sample_rate) { sample_rate = rate; }
    snd_pcm_hw_params_set_channels(*handle, hw_params, 2);
    unsigned int period_us = period_time_us;
    snd_pcm_hw_params_set_period_time_near(*handle, hw_params, &period_us, &dir);
    unsigned int buffer_us = buffer_time_us;
    snd_pcm_hw_params_set_buffer_time_near(*handle, hw_params, &buffer_us, &dir);
    if ((err = snd_pcm_hw_params(*handle, hw_params)) < 0) {
        fprintf(stderr, "硬件参数错误: %s\n", snd_strerror(err));
        return err;
    }
    snd_pcm_uframes_t pf, bf;
    snd_pcm_hw_params_get_period_size(hw_params, &pf, &dir);
    snd_pcm_hw_params_get_buffer_size(hw_params, &bf);
    printf("%s: 率=%u Hz, 周期=%lu, 缓冲=%lu\n",
           stream == SND_PCM_STREAM_CAPTURE ? "CAP" : "PLAY",
           sample_rate, pf, bf);
    return 0;
}

int main() {
    signal(SIGINT, int_handler);
    anc_init();

    if (setup_pcm(&cap_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_CAPTURE) < 0) return 1;
    if (setup_pcm(&play_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_PLAYBACK) < 0) {
        snd_pcm_close(cap_handle); return 1;
    }

    snd_pcm_hw_params_t *hw_params;
    snd_pcm_hw_params_alloca(&hw_params);
    snd_pcm_hw_params_current(cap_handle, hw_params);
    snd_pcm_uframes_t cp; int dir;
    snd_pcm_hw_params_get_period_size(hw_params, &cp, &dir);
    snd_pcm_hw_params_current(play_handle, hw_params);
    snd_pcm_uframes_t pp;
    snd_pcm_hw_params_get_period_size(hw_params, &pp, &dir);
    snd_pcm_uframes_t frames = cp < pp ? cp : pp;

    short *cap_buf  = malloc(frames * 2 * sizeof(short));
    short *play_buf = calloc(frames * 2, sizeof(short));

    // 预填充
    for (int i = 0; i < 3; i++) snd_pcm_writei(play_handle, play_buf, frames);

    int rec_max = sample_rate * 10;
    short *err_rec  = calloc(rec_max, sizeof(short));
    short *anti_rec = calloc(rec_max, sizeof(short));
    int rec_cnt = 0;

    printf("口罩反馈 ANC 启动，滤波器长度=%d，NLMS mu=%.3f\n", FB_FILTER_LEN, FB_MU);
    printf("正在运行... 请说话或制造噪声。\n");

    while (keep_running && rec_cnt < rec_max) {
        int f = snd_pcm_readi(cap_handle, cap_buf, frames);
        if (f < 0) { snd_pcm_recover(cap_handle, f, 0); continue; }

        for (int i = 0; i < f; i++) {
            // 假设右声道为内部误差麦克风（请根据硬件调整）
            float err = cap_buf[i*2+1] / 32768.0f;
            float anti = anc_process(err);
            short out = (short)(anti * 32767.0f);

            play_buf[i*2    ] = out;   // 左声道 -> 扬声器
            play_buf[i*2+1  ] = 0;

            if (rec_cnt < rec_max) {
                err_rec[rec_cnt]  = (short)(err * 32767.0f);
                anti_rec[rec_cnt] = out;
                rec_cnt++;
            }
        }

        int ret = snd_pcm_writei(play_handle, play_buf, f);
        if (ret < 0) {
            if (ret == -EPIPE) {
                snd_pcm_prepare(play_handle);
                memset(play_buf, 0, f * 2 * sizeof(short));
                snd_pcm_writei(play_handle, play_buf, f);
            } else snd_pcm_recover(play_handle, ret, 0);
        }
    }

    FILE *fp = fopen("err_mask.wav", "wb");
    if (fp) { write_wav_header(fp, sample_rate, rec_cnt); fwrite(err_rec, sizeof(short), rec_cnt, fp); fclose(fp); }
    fp = fopen("anti_mask.wav", "wb");
    if (fp) { write_wav_header(fp, sample_rate, rec_cnt); fwrite(anti_rec, sizeof(short), rec_cnt, fp); fclose(fp); }

    free(err_rec); free(anti_rec); free(cap_buf); free(play_buf);
    snd_pcm_close(cap_handle); snd_pcm_close(play_handle);
    printf("完成。\n");
    return 0;
}