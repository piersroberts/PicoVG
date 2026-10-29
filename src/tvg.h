#ifndef TVG_H
#define TVG_H

#include <stddef.h>
#include <stdint.h>

// Reads the image size in display units from a TinyVG header.
// Returns 0 on success, -1 if the data is not a TinyVG file.
int tvg_size(const uint8_t *data, size_t len, int *width, int *height);

// Renders a TinyVG file into a new width * height buffer of RGBA bytes
// (straight alpha, sRGB). Returns NULL on error and writes a message to err.
// Free the result with free().
uint8_t *tvg_render(const uint8_t *data, size_t len, int width, int height,
                    char *err, size_t err_len);

#endif
