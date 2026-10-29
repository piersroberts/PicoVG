// Renders TinyVG files, or every image in PicoVG files, into a grid and
// saves it as a BMP.
// Usage: tvgsheet out.bmp cell_width columns file.tvg|file.pvg...

#include <SDL.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "pvg.h"
#include "tvg.h"

typedef struct {
    const char *path;
    uint8_t *data;
    size_t len;
    int index;  // -1 for TinyVG
} entry;

static int is_pvg(const char *path) {
    size_t n = strlen(path);
    return n > 4 && strcmp(path + n - 4, ".pvg") == 0;
}

int main(int argc, char **argv) {
    if (argc < 5) {
        fprintf(stderr, "usage: %s out.bmp cell_width columns file.tvg|file.pvg...\n", argv[0]);
        return 1;
    }
    int cell_w = atoi(argv[2]);
    int cols = atoi(argv[3]);
    const int gap = 8;

    entry *entries = NULL;
    int count = 0;
    for (int i = 4; i < argc; i++) {
        size_t len;
        uint8_t *data = SDL_LoadFile(argv[i], &len);
        if (!data) {
            fprintf(stderr, "%s: cannot read\n", argv[i]);
            return 1;
        }
        int n = is_pvg(argv[i]) ? pvg_count(data, len) : 1;
        if (n < 0) {
            fprintf(stderr, "%s: not a PicoVG file\n", argv[i]);
            return 1;
        }
        entries = realloc(entries, sizeof(entry) * (size_t)(count + n));
        for (int k = 0; k < n; k++)
            entries[count++] = (entry){argv[i], data, len, is_pvg(argv[i]) ? k : -1};
    }

    // Size every cell from the first image's aspect ratio.
    int fw, fh;
    int ok = entries[0].index < 0
                 ? tvg_size(entries[0].data, entries[0].len, &fw, &fh)
                 : pvg_info(entries[0].data, entries[0].len, 0, &fw, &fh, NULL, NULL);
    if (ok != 0) {
        fprintf(stderr, "%s: bad file\n", entries[0].path);
        return 1;
    }
    int cell_h = cell_w * fh / fw;
    int rows = (count + cols - 1) / cols;

    SDL_Surface *sheet = SDL_CreateRGBSurfaceWithFormat(
        0, cols * (cell_w + gap) + gap, rows * (cell_h + gap) + gap, 32, SDL_PIXELFORMAT_RGBA32);
    SDL_FillRect(sheet, NULL, SDL_MapRGBA(sheet->format, 20, 90, 50, 255));

    for (int i = 0; i < count; i++) {
        entry *e = &entries[i];
        char err[256];
        Uint32 t0 = SDL_GetTicks();
        uint8_t *px = e->index < 0
                          ? tvg_render(e->data, e->len, cell_w, cell_h, err, sizeof err)
                          : pvg_render(e->data, e->len, e->index, cell_w, cell_h, err, sizeof err);
        Uint32 t1 = SDL_GetTicks();
        if (!px) {
            fprintf(stderr, "%s[%d]: %s\n", e->path, e->index, err);
            continue;
        }
        printf("%s[%d]: %u ms\n", e->path, e->index, t1 - t0);
        SDL_Surface *img = SDL_CreateRGBSurfaceWithFormatFrom(
            px, cell_w, cell_h, 32, cell_w * 4, SDL_PIXELFORMAT_RGBA32);
        SDL_SetSurfaceBlendMode(img, SDL_BLENDMODE_BLEND);
        SDL_Rect dst = {gap + (i % cols) * (cell_w + gap), gap + (i / cols) * (cell_h + gap),
                        cell_w, cell_h};
        SDL_BlitSurface(img, NULL, sheet, &dst);
        SDL_FreeSurface(img);
        free(px);
    }

    // 24 bit, because some viewers cannot read SDL's 32 bit BMPs.
    SDL_Surface *rgb = SDL_ConvertSurfaceFormat(sheet, SDL_PIXELFORMAT_BGR24, 0);
    if (!rgb || SDL_SaveBMP(rgb, argv[1]) != 0) {
        fprintf(stderr, "save failed: %s\n", SDL_GetError());
        return 1;
    }
    SDL_FreeSurface(rgb);
    SDL_FreeSurface(sheet);
    // File data is freed on exit; several entries share each buffer.
    free(entries);
    return 0;
}
