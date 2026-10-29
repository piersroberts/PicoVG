# PicoVG

PicoVG is [TinyVG](https://tinyvg.tech) with shared shapes and many images in one file. This repo has the [spec](docs/picovg.md), an SVG converter in Python and a small software renderer in C that reads both TinyVG and PicoVG.

TinyVG can't reuse a shape, and stores every point as a 4 byte absolute coordinate. PicoVG adds:

- a symbol table, drawn with a `use symbol` command and a transform
- a "current colour" style, so one symbol can be drawn in different colours
- many named images per file
- compact paths, which store each point as a small offset from the last

## Build

```sh
make  # builds pvg2png and tvgsheet
```

Needs a C compiler, zlib and Python 3. `tvgsheet` also needs SDL2 (`brew install sdl2`).

## Layout

| Path | What it is |
| --- | --- |
| [src/tvg.c](src/tvg.c) | TinyVG v1 and PicoVG decoder and rasteriser. Covers all commands, path ops and styles in the TinyVG spec. |
| [src/tvg.h](src/tvg.h), [src/pvg.h](src/pvg.h) | C API |
| [tools/pvg2png.c](tools/pvg2png.c) | `pvg2png`, renders images from a `.pvg`, or one `.tvg`, to PNG with transparency |
| [tools/tvgsheet.c](tools/tvgsheet.c) | `tvgsheet`, renders `.tvg` files, or every image in a `.pvg`, into a BMP grid |
| [docs/picovg.md](docs/picovg.md) | PicoVG spec |
| [tools/svg2pvg.py](tools/svg2pvg.py) | Bundles many SVGs into one `.pvg`, sharing repeated shapes |
| [tools/svg2tvg.py](tools/svg2tvg.py) | Converts one SVG to TinyVG |
| [tools/simplify.py](tools/simplify.py) | Lossy path simplifier used by `svg2pvg.py --tolerance` |

## Tools

```sh
python3 tools/svg2pvg.py [--tolerance units] [--grid n] out.pvg in.svg...
python3 tools/svg2tvg.py in.svg out.tvg
./pvg2png [-s factor] width in.pvg out_dir [name...]
./pvg2png [-s factor] width in.tvg out.png
./tvgsheet out.bmp cell_width columns file.tvg|file.pvg...
```

- Images in a bundle are named after their SVG files, without `.svg`.
- `--tolerance` is how far simplified paths may move, in SVG units. Default 0.
- `--grid n` snaps compact path points to steps of 2^n / 8 units. Default 1 (1/4 unit).
- `pvg2png -s 4` renders at 4x and shrinks, which keeps thin outlines from looking heavy.

## C API

Copy the three files in `src/` into your project and add `tvg.c` to your build. It only needs libm.

```c
#include "pvg.h"

int i = pvg_find(data, len, "icon");
int w, h;
pvg_info(data, len, i, &w, &h, NULL, NULL);
char err[128];
uint8_t *rgba = pvg_render(data, len, i, 64, 64, err, sizeof err);
// rgba is 64 * 64 * 4 bytes, straight alpha, sRGB. free() it.
```

`tvg_size` and `tvg_render` in [tvg.h](src/tvg.h) do the same for a plain TinyVG file.

## Renderer notes

- Scanline fill with 8 sub-scanlines per pixel and exact horizontal coverage.
- Fills use even-odd, as the TinyVG spec says. Lines are unions of round-capped capsules (non-zero).
- Blending and gradients run in linear light (gamma 2.2), as the spec says.

## Converter limits

TinyVG and PicoVG have no patterns, text or square line caps, and TinyVG has no transforms. Gradients have two colours. So the converters:

- apply every `transform`, `use` and `symbol` viewBox to the points
- turn arcs and rounded rects into cubic beziers
- tile `userSpaceOnUse` patterns on rects, keeping only tiles fully inside the rect
- store coordinates at 1/8 unit, which gzips about 18% better than 1/64 and looks the same
- keep only the first and last stop of a gradient, and ignore a radial gradient's focal point
- split a non-zero fill into even-odd fills when its subpaths overlap the same way round
- ignore `clip-path` and `<text>`
- draw square line caps as round caps
