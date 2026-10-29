#ifndef PVG_H
#define PVG_H

#include <stddef.h>
#include <stdint.h>

// PicoVG: TinyVG plus shared symbols and many named images per file.
// See docs/picovg.md. Images are addressed by index, 0 to count - 1.

// Returns the number of images, or -1 if the data is not a PicoVG file.
int pvg_count(const uint8_t *data, size_t len);

// Returns the index of the image with this name, or -1.
int pvg_find(const uint8_t *data, size_t len, const char *name);

// Reads an image's size in display units and its name (not NUL terminated,
// points into data). Any output may be NULL. Returns 0 on success, -1 on error.
int pvg_info(const uint8_t *data, size_t len, int index, int *width, int *height,
             const char **name, size_t *name_len);

// Renders one image into a new width * height buffer of RGBA bytes
// (straight alpha, sRGB). Returns NULL on error and writes a message to err.
// Free the result with free().
uint8_t *pvg_render(const uint8_t *data, size_t len, int index, int width, int height,
                    char *err, size_t err_len);

#endif
