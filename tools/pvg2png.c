// Renders PicoVG images, or a TinyVG file, to PNG with transparency.
// Usage: pvg2png [-s factor] width in.pvg out_dir [name...]
//        pvg2png [-s factor] width in.tvg out.png
// With no names, writes every image in the bundle as out_dir/NAME.png.
// Height follows each image's aspect ratio. -s renders at factor times the
// size and shrinks the result, which keeps thin outlines from looking heavy.

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <zlib.h>

#include "pvg.h"
#include "tvg.h"

static uint8_t *read_file(const char *path, size_t *len) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *data = n > 0 ? malloc((size_t)n) : NULL;
    if (data && fread(data, 1, (size_t)n, f) != (size_t)n) {
        free(data);
        data = NULL;
    }
    fclose(f);
    *len = (size_t)n;
    return data;
}

static int is_pvg(const char *path) {
    size_t n = strlen(path);
    return n > 4 && strcmp(path + n - 4, ".pvg") == 0;
}

static void put_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static void write_chunk(FILE *f, const char *type, const uint8_t *data, uint32_t len) {
    uint8_t head[8];
    put_u32(head, len);
    memcpy(head + 4, type, 4);
    uLong crc = crc32(0, head + 4, 4);
    crc = crc32(crc, data, len);
    uint8_t tail[4];
    put_u32(tail, (uint32_t)crc);
    fwrite(head, 1, 8, f);
    fwrite(data, 1, len, f);
    fwrite(tail, 1, 4, f);
}

// Writes 8 bit RGBA, straight alpha, which is what the renderer returns.
static int write_png(const char *path, const uint8_t *px, int w, int h) {
    // Each row gets a leading filter byte (0, none).
    size_t row = (size_t)w * 4;
    size_t raw_len = (row + 1) * (size_t)h;
    uint8_t *raw = malloc(raw_len);
    for (int y = 0; y < h; y++) {
        raw[y * (row + 1)] = 0;
        memcpy(raw + y * (row + 1) + 1, px + y * row, row);
    }
    uLongf z_len = compressBound(raw_len);
    uint8_t *z = malloc(z_len);
    int ok = compress2(z, &z_len, raw, raw_len, 9) == Z_OK;
    free(raw);

    FILE *f = ok ? fopen(path, "wb") : NULL;
    if (f) {
        static const uint8_t sig[8] = {137, 'P', 'N', 'G', '\r', '\n', 26, '\n'};
        uint8_t ihdr[13] = {0};
        put_u32(ihdr, (uint32_t)w);
        put_u32(ihdr + 4, (uint32_t)h);
        ihdr[8] = 8;  // bit depth
        ihdr[9] = 6;  // RGBA
        fwrite(sig, 1, 8, f);
        write_chunk(f, "IHDR", ihdr, 13);
        write_chunk(f, "IDAT", z, (uint32_t)z_len);
        write_chunk(f, "IEND", NULL, 0);
        ok = fclose(f) == 0;
    } else {
        ok = 0;
    }
    free(z);
    return ok ? 0 : -1;
}

static int scale = 1;

// Averages each scale * scale block into one pixel, in premultiplied linear
// light like the renderer, so thin dark lines do not darken. Frees px.
static uint8_t *shrink(uint8_t *px, int w, int h) {
    if (scale == 1) return px;
    int sw = w * scale;
    float lut[256];
    for (int i = 0; i < 256; i++) lut[i] = powf(i / 255.0f, 2.2f);
    uint8_t *out = malloc((size_t)w * (size_t)h * 4);
    float n = (float)(scale * scale);
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            float r = 0, g = 0, b = 0, a = 0;
            for (int dy = 0; dy < scale; dy++) {
                const uint8_t *p = px + ((size_t)(y * scale + dy) * (size_t)sw + (size_t)(x * scale)) * 4;
                for (int dx = 0; dx < scale; dx++, p += 4) {
                    float pa = p[3] / 255.0f;
                    r += lut[p[0]] * pa;
                    g += lut[p[1]] * pa;
                    b += lut[p[2]] * pa;
                    a += pa;
                }
            }
            uint8_t *o = out + ((size_t)y * (size_t)w + (size_t)x) * 4;
            if (a > 0) {
                o[0] = (uint8_t)lroundf(powf(r / a, 1 / 2.2f) * 255);
                o[1] = (uint8_t)lroundf(powf(g / a, 1 / 2.2f) * 255);
                o[2] = (uint8_t)lroundf(powf(b / a, 1 / 2.2f) * 255);
            } else {
                o[0] = o[1] = o[2] = 0;
            }
            o[3] = (uint8_t)lroundf(a / n * 255);
        }
    }
    free(px);
    return out;
}

static int render_pvg(const uint8_t *data, size_t len, int index, int width, const char *dir) {
    int fw, fh;
    const char *name;
    size_t name_len;
    if (pvg_info(data, len, index, &fw, &fh, &name, &name_len) != 0) {
        fprintf(stderr, "image %d: bad header\n", index);
        return -1;
    }
    int height = width * fh / fw;
    char err[256];
    uint8_t *px = pvg_render(data, len, index, width * scale, height * scale, err, sizeof err);
    if (!px) {
        fprintf(stderr, "%.*s: %s\n", (int)name_len, name, err);
        return -1;
    }
    px = shrink(px, width, height);
    char path[1024];
    snprintf(path, sizeof path, "%s/%.*s.png", dir, (int)name_len, name);
    int rc = write_png(path, px, width, height);
    free(px);
    if (rc != 0) fprintf(stderr, "%s: cannot write\n", path);
    else printf("%s\n", path);
    return rc;
}

int main(int argc, char **argv) {
    if (argc > 2 && strcmp(argv[1], "-s") == 0) {
        scale = atoi(argv[2]);
        if (scale < 1 || scale > 16) {
            fprintf(stderr, "-s must be 1 to 16\n");
            return 1;
        }
        argc -= 2;
        argv += 2;
    }
    if (argc < 4) {
        fprintf(stderr,
                "usage: pvg2png [-s factor] width in.pvg out_dir [name...]\n"
                "       pvg2png [-s factor] width in.tvg out.png\n");
        return 1;
    }
    int width = atoi(argv[1]);
    if (width <= 0) {
        fprintf(stderr, "width must be a positive number\n");
        return 1;
    }
    size_t len;
    uint8_t *data = read_file(argv[2], &len);
    if (!data) {
        fprintf(stderr, "%s: cannot read\n", argv[2]);
        return 1;
    }

    if (!is_pvg(argv[2])) {
        int fw, fh;
        if (tvg_size(data, len, &fw, &fh) != 0) {
            fprintf(stderr, "%s: not a TinyVG file\n", argv[2]);
            return 1;
        }
        int height = width * fh / fw;
        char err[256];
        uint8_t *px = tvg_render(data, len, width * scale, height * scale, err, sizeof err);
        if (!px) {
            fprintf(stderr, "%s: %s\n", argv[2], err);
            return 1;
        }
        px = shrink(px, width, height);
        if (write_png(argv[3], px, width, height) != 0) {
            fprintf(stderr, "%s: cannot write\n", argv[3]);
            return 1;
        }
        free(px);
        free(data);
        return 0;
    }

    int count = pvg_count(data, len);
    if (count < 0) {
        fprintf(stderr, "%s: not a PicoVG file\n", argv[2]);
        return 1;
    }
    if (mkdir(argv[3], 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "%s: cannot create\n", argv[3]);
        return 1;
    }
    int failed = 0;
    if (argc == 4) {
        for (int i = 0; i < count; i++) failed |= render_pvg(data, len, i, width, argv[3]);
    } else {
        for (int a = 4; a < argc; a++) {
            int index = pvg_find(data, len, argv[a]);
            if (index < 0) {
                fprintf(stderr, "%s: no image named %s\n", argv[2], argv[a]);
                failed = 1;
                continue;
            }
            failed |= render_pvg(data, len, index, width, argv[3]);
        }
    }
    free(data);
    return failed ? 1 : 0;
}
