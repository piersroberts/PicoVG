// Renders every image in two PicoVG bundles and compares them by name.
// Usage: pvgdiff [-t max_mean] width old.pvg new.pvg
// Prints images whose mean difference per channel (0-255) is over
// max_mean (default 1), and exits 1 if there are any, or if an image is
// missing from new.pvg or fails to render.

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pvg.h"

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

int main(int argc, char **argv) {
    double max_mean = 1.0;
    if (argc > 2 && strcmp(argv[1], "-t") == 0) {
        max_mean = atof(argv[2]);
        argc -= 2;
        argv += 2;
    }
    if (argc != 4) {
        fprintf(stderr, "usage: pvgdiff [-t max_mean] width old.pvg new.pvg\n");
        return 1;
    }
    int width = atoi(argv[1]);
    if (width <= 0) {
        fprintf(stderr, "width must be a positive number\n");
        return 1;
    }
    size_t old_len, new_len;
    uint8_t *old_data = read_file(argv[2], &old_len);
    uint8_t *new_data = read_file(argv[3], &new_len);
    if (!old_data || !new_data) {
        fprintf(stderr, "%s: cannot read\n", old_data ? argv[3] : argv[2]);
        return 1;
    }
    int count = pvg_count(old_data, old_len);
    if (count < 0 || pvg_count(new_data, new_len) < 0) {
        fprintf(stderr, "not a PicoVG file\n");
        return 1;
    }

    int failed = 0, over = 0;
    double total = 0, worst = 0;
    char worst_name[256] = "";
    for (int i = 0; i < count; i++) {
        int fw, fh;
        const char *name_ptr;
        size_t name_len;
        if (pvg_info(old_data, old_len, i, &fw, &fh, &name_ptr, &name_len) != 0) {
            fprintf(stderr, "%s: image %d: bad header\n", argv[2], i);
            failed = 1;
            continue;
        }
        char name[256];
        snprintf(name, sizeof name, "%.*s", (int)name_len, name_ptr);
        int j = pvg_find(new_data, new_len, name);
        if (j < 0) {
            printf("%s: missing from %s\n", name, argv[3]);
            failed = 1;
            continue;
        }
        int height = width * fh / fw;
        if (height < 1) height = 1;
        char err[256];
        uint8_t *a = pvg_render(old_data, old_len, i, width, height, err, sizeof err);
        uint8_t *b = a ? pvg_render(new_data, new_len, j, width, height, err, sizeof err) : NULL;
        if (!b) {
            printf("%s: %s\n", name, err);
            free(a);
            failed = 1;
            continue;
        }
        size_t n = (size_t)width * (size_t)height * 4;
        double sum = 0;
        for (size_t k = 0; k < n; k++) sum += abs(a[k] - b[k]);
        free(a);
        free(b);
        double mean = sum / (double)n;
        total += mean;
        if (mean > worst) {
            worst = mean;
            snprintf(worst_name, sizeof worst_name, "%s", name);
        }
        if (mean > max_mean) {
            printf("%s: mean diff %.2f\n", name, mean);
            over++;
        }
    }
    printf("%d images, mean diff %.3f, worst %.2f (%s), %d over %.2f\n", count,
           count ? total / count : 0.0, worst, worst_name, over, max_mean);
    free(old_data);
    free(new_data);
    return failed || over ? 1 : 0;
}
