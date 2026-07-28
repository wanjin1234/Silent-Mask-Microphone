cd ~/Silent-Mask-Microphone/src
cat > anc.c << 'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <alsa/asoundlib.h>
#include "S_coeffs.h"

#define FILTER_LEN  128
#define MU          0.0001f
#define LEAKY       0.9999f

static float W[FILTER_LEN];
#define X_BUF_LEN  (FILTER_LEN + S_LEN)
static float x_buf[X_BUF_LEN];
static int   x_idx = 0;
static float fxl_buf[FILTER_LEN];

static snd_pcm_t *cap_handle, *play_handle;
static snd_pcm_uframes_t period_size = 64;
static unsigned int sample_rate = 16000;

volatile int keep_running = 1;
void int_handler(int sig) { keep_running = 0; }

void write_wav_header(FILE *f, int sr, int num) {
    int br = sr * 2, ds = num * 2;
    fwrite("RIFF", 1, 4, f); int32_t cs = 36 + ds; fwrite(&cs, 4, 1, f);
    fwrite("WAVE", 1, 4, f); fwrite("fmt ", 1, 4, f);
    int32_t sz = 16; fwrite(&sz, 4, 1, f);
    int16_t af = 1, nc = 1; fwrite(&af, 2, 1, f); fwrite(&nc, 2, 1, f);
    fwrite(&sr, 4, 1, f); fwrite(&br, 4, 1, f);
    int16_t ba = 2, bps = 16; fwrite(&ba, 2, 1, f); fwrite(&bps, 2, 1, f);
    fwrite("data", 1, 4, f); fwrite(&ds, 4, 1, f);
}

void anc_init() {
    memset(W, 0, sizeof(W));
    memset(x_buf, 0, sizeof(x_buf));
    memset(fxl_buf, 0, sizeof(fxl_buf));
    x_idx = 0;
}

float anc_process(float ref, float err) {
    x_buf[x_idx] = ref;
    float y = 0.0f;
    for (int i = 0; i < FILTER_LEN; i++) {
        int pos = (x_idx + 1 + i) % X_BUF_LEN;
        y += W[i] * x_buf[pos];
    }
    for (int i = 0; i < FILTER_LEN; i++) {
        float sum = 0.0f;
        for (int j = 0; j < S_LEN; j++) {
            int pos = (x_idx + 1 + i + j) % X_BUF_LEN;
            sum += S[j] * x_buf[pos];
        }
        fxl_buf[i] = sum;
    }
    for (int i = 0; i < FILTER_LEN; i++) {
        W[i] = LEAKY * W[i] + 2.0f * MU * err * fxl_buf[i];
    }
    x_idx = (x_idx + 1) % X_BUF_LEN;
    if (y > 1.0f) y = 1.0f; if (y < -1.0f) y = -1.0f;
    return -y;   // 固定反相实验已验证需反相
}

void audio_setup() {
    int err; snd_pcm_hw_params_t *hw_params;
    if ((err = snd_pcm_open(&cap_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_CAPTURE, 0)) < 0) exit(1);
    snd_pcm_hw_params_alloca(&hw_params); snd_pcm_hw_params_any(cap_handle, hw_params);
    snd_pcm_hw_params_set_access(cap_handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(cap_handle, hw_params, SND_PCM_FORMAT_S16_LE);
    snd_pcm_hw_params_set_rate_near(cap_handle, hw_params, &sample_rate, 0);
    snd_pcm_hw_params_set_channels(cap_handle, hw_params, 2);
    snd_pcm_hw_params_set_period_size_near(cap_handle, hw_params, &period_size, 0);
    snd_pcm_uframes_t buffer_size = period_size * 4;
    snd_pcm_hw_params_set_buffer_size_near(cap_handle, hw_params, &buffer_size);
    snd_pcm_hw_params(cap_handle, hw_params);
    if ((err = snd_pcm_open(&play_handle, "plughw:seeed2micvoicec", SND_PCM_STREAM_PLAYBACK, 0)) < 0) exit(1);
    snd_pcm_hw_params_any(play_handle, hw_params);
    snd_pcm_hw_params_set_access(play_handle, hw_params, SND_PCM_ACCESS_RW_INTERLEAVED);
    snd_pcm_hw_params_set_format(play_handle, hw_params, SND_PCM_FORMAT_S16_LE);
    snd_pcm_hw_params_set_rate_near(play_handle, hw_params, &sample_rate, 0);
    snd_pcm_hw_params_set_channels(play_handle, hw_params, 2);
    snd_pcm_hw_params_set_period_size_near(play_handle, hw_params, &period_size, 0);
    snd_pcm_hw_params_set_buffer_size_near(play_handle, hw_params, &buffer_size);
    snd_pcm_hw_params(play_handle, hw_params);
}

int main() {
    signal(SIGINT, int_handler); anc_init(); audio_setup();
    int max_samples = sample_rate * 10;
    short *rec_buffer = (short*) malloc(max_samples * sizeof(short));
    int rec_count = 0;
    short *cap_buf = malloc(period_size * 2 * sizeof(short));
    short *play_buf = malloc(period_size * 2 * sizeof(short));
    printf("前馈ANC启动，录制10秒...\n");
    while (keep_running && rec_count < max_samples) {
        int frames = snd_pcm_readi(cap_handle, cap_buf, period_size);
        if (frames < 0) { snd_pcm_recover(cap_handle, frames, 0); continue; }
        for (int i = 0; i < frames; i++) {
            float ref = cap_buf[i*2] / 32768.0f;        // 参考麦（左声道）
            float err = cap_buf[i*2+1] / 32768.0f;      // 误差麦（右声道）
            float anti = anc_process(ref, err);
            short anti_out = (short)(anti * 32767.0f);
            play_buf[i*2]   = 0;                        // 左声道静音（你的左耳不响）
            play_buf[i*2+1] = anti_out;                 // 右声道反相波
            if (rec_buffer && rec_count < max_samples)
                rec_buffer[rec_count++] = (short)(err * 32767.0f);
        }
        snd_pcm_writei(play_handle, play_buf, frames);
    }
    if (rec_buffer && rec_count > 0) {
        FILE *f = fopen("anc_rec.wav", "wb");
        if (f) { write_wav_header(f, sample_rate, rec_count);
                 fwrite(rec_buffer, sizeof(short), rec_count, f); fclose(f); }
        free(rec_buffer);
    }
    free(cap_buf); free(play_buf); snd_pcm_close(cap_handle); snd_pcm_close(play_handle);
    return 0;
}
EOF