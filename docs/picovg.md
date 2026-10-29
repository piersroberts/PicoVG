# PicoVG format, version 1

PicoVG is [TinyVG v1](https://tinyvg.tech/download/specification.txt) with four additions:

- many named images in one file
- a symbol table: command lists stored once and drawn many times with a transform
- a "current colour" style, so one symbol can be drawn in different colours
- compact paths, which store each point as a small offset from the last one

Everything not listed here works as in TinyVG: units, VarUInt, colour tables, styles, paths, commands 0 to 10, and the rendering rules. Integers are little endian.

## File layout

| Field | Type | Notes |
| --- | --- | --- |
| magic | `[2]u8` | `'P', 'V'` (0x50 0x56) |
| version | `u8` | 1 |
| scale | `u4` | As TinyVG |
| color_encoding | `u2` | As TinyVG |
| coordinate_range | `u2` | As TinyVG. Also sets the size of image width and height. |
| path_grid | `u8` | 0 to 15. Compact path coordinates step in `2^path_grid` Units. |
| color_count | VarUInt | |
| color_table | | As TinyVG. Shared by every symbol and image. |
| symbol_count | VarUInt | |
| symbols | `[symbol_count]Symbol` | |
| image_count | VarUInt | |
| images | `[image_count]Image` | |

### Symbol

| Field | Type | Notes |
| --- | --- | --- |
| length | VarUInt | Byte length of `commands` |
| commands | | Commands, ending with an end of document command (0x00) |

Symbols have no size. Their coordinates are in their own space, and a `use symbol` command places them.

### Image

| Field | Type | Notes |
| --- | --- | --- |
| name_length | VarUInt | |
| name | `[name_length]u8` | UTF-8, no terminator. Names should be unique. |
| width | `u8`, `u16` or `u32` | Display units, sized by coordinate_range |
| height | `u8`, `u16` or `u32` | |
| length | VarUInt | Byte length of `commands` |
| commands | | Commands, ending with 0x00 |

The length fields let a reader skip to any symbol or image without decoding the commands.

## Current colour style

Style kind 3, unused in TinyVG, means "flat, in the current colour". It has no payload. It is valid anywhere a style is, including the secondary style of the outline commands.

The current colour is set by a `use symbol` command with the colour flag. It is unset at the start of an image, and using style 3 while it is unset is an error.

## Command 11: use symbol

Draws a symbol's commands with a transform, then carries on with the next command. `prim_style_kind` must be 0.

| Field | Type | Present if |
| --- | --- | --- |
| symbol_index | VarUInt | Always |
| flags | `u8` | Always |
| translate | Point | flag 0x01 |
| matrix | `[4]f32` (a, b, c, d) | flag 0x10 |
| scale | `f32` | flag 0x02, and 0x10 clear |
| scale_y | `f32` | flag 0x04, and 0x10 clear |
| rotate | `f32`, degrees | flag 0x08, and 0x10 clear |
| color_index | VarUInt | flag 0x20 |

Fields come in the order of the table. Flag 0x40 is a half turn: it adds 180 degrees to `rotate` and has no payload. Flag 0x80 must be 0.

The local transform maps a symbol point `p` to `L p + t`:

- `t` is `translate`, or (0, 0).
- With flag 0x10, `L` is the matrix `[[a, c], [b, d]]`, and flags 0x02, 0x04, 0x08 and 0x40 are ignored.
- Otherwise `L = R(rotate) * S(scale, scale_y)`. `scale` defaults to 1. `scale_y` defaults to `scale`. `rotate` defaults to 0 and turns clockwise on screen, as in SVG.

Inside the symbol, the transform applies to every point, including gradient points. Line widths scale by `sqrt(|det L|)`. Lines thinner than one display pixel still draw one pixel wide, as in TinyVG.

The flag 0x20 colour becomes the current colour while the symbol runs. Without it, the symbol keeps the caller's current colour. The caller's current colour comes back after the symbol ends.

### Rules

- An image may use any symbol.
- Symbol `i` may only use symbols below `i`. This rules out loops. Readers may also limit nesting depth (this one stops at 16).
- Commands 11 to 13 are errors in a plain TinyVG file.

## Commands 12 and 13: compact paths

| Command | Fields | Draws like |
| --- | --- | --- |
| 12 compact fill path | segment_count VarUInt (value + 1), fill_style, CompactPath | 3 fill path |
| 13 compact line path | segment_count VarUInt (value + 1), line_style, line_width Unit, CompactPath | 7 draw line path |

So fills use the even-odd rule and lines have round caps, as in TinyVG. Compact paths have no arcs and no per-op line width. Use commands 3 and 7 for those.

### CompactPath

A list of `segment_count` subpaths. Each subpath is:

| Field | Type | Notes |
| --- | --- | --- |
| head | `u8` | See below |
| op_count | VarUInt | Long form only |
| kinds | `[(op_count + 3) / 4]u8` | Long form only. 2 bits per op, low bits first. |
| start | 2 VarInt | Offset from the pen |
| ops | | One entry per op |

`head` has two forms:

- **Short**, when `head & 3` is 1 to 3: that is the op count, and the op kinds sit in bits 2 to 7, 2 bits each, low bits first.
- **Long**, when `head & 3` is 0: `op_count` and `kinds` follow. The other head bits must be 0.

Op kinds:

| Kind | Op | Data, each a pair of VarInt |
| --- | --- | --- |
| 0 | line | end |
| 1 | quadratic bezier | control, end |
| 2 | cubic bezier | control 1, control 2, end |
| 3 | close | none. Draws a line back to the subpath start. |

**Offsets.** Every pair is `(dx, dy)` from the previous point in the stream, in grid steps of `2^path_grid` Units. That covers control points too: control 1 is relative to the current point, control 2 to control 1, and the end to control 2. The pen starts at (0, 0) for each command. After a subpath, the pen is at that subpath's last point, which is its start if it ended with a close.

**VarInt.** A zigzag VarUInt: values 0, 1, 2, 3, 4 mean 0, -1, 1, -2, 2.

Fill subpaths close themselves, so writers can leave the final close out.

## Writer notes

[tools/svg2pvg.py](../tools/svg2pvg.py) writes these files. It:

- stores each symbol at the scale of its largest use, so precision lands where it shows
- writes symbols used only once inline, since a `use` costs more than it saves
- picks the smallest transform encoding that fits, such as translate only, or a half turn
- writes every path as a compact path, on a 1/4 unit grid by default (`--grid`)
- orders subpaths so each starts near the end of the last, which shrinks start offsets. Even-odd fills and unions of lines do not depend on order.
- with `--tolerance`, simplifies paths first (see [tools/simplify.py](../tools/simplify.py))

## Example bytes

With 3 scale bits (Units of 1/8) and `path_grid` 1 (grid steps of 1/4).

A symbol drawn at (100, 20), turned 180°, in colour 2:

```
0B           command 11, style kind 0
05           symbol 5
61           flags: translate | colour | half turn
20 03 A0 00  translate (100.0, 20.0) as Units of 1/8
02           colour 2
```

A triangle (0, 0), (4, 0), (2, 3) filled with colour 1. In grid steps that's (0, 0), (16, 0), (8, 12):

```
0C           command 12, style kind 0
00           1 subpath
01           colour 1
02           short head: 2 ops, both lines
00 00        start (0, 0)
20 00        line by (16, 0)
0F 18        line by (-8, 12), then the fill closes itself
```
