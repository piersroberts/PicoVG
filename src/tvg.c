// TinyVG and PicoVG decoder and software rasteriser.
// Follows https://tinyvg.tech/download/specification.txt (version 1).
// PicoVG adds symbols, compact paths and many images per file, see
// docs/picovg.md.
//
// Shapes become edge lists in pixel space. Each draw command is scanline
// rasterised with SUBSAMPLES sub-scanlines per pixel row and exact
// horizontal coverage. Fills use the even-odd rule, as the spec requires.
// Lines are unions of round-capped capsules, filled with the non-zero rule.
// Blending and gradients run in linear light.

#include "tvg.h"
#include "pvg.h"

#include <limits.h>
#include <math.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SUBSAMPLES 8
#define FLATNESS_PX 0.2f  // max error when flattening curves, in pixels
#define PI_F 3.14159265f
#define MAX_USE_DEPTH 16

typedef struct { float x, y; } vec2;
typedef struct { float r, g, b, a; } colour;  // linear light, straight alpha

enum { STYLE_FLAT = 0, STYLE_LINEAR = 1, STYLE_RADIAL = 2, STYLE_CURRENT = 3 };

enum {
    USE_TRANSLATE = 0x01,
    USE_SCALE = 0x02,
    USE_SCALE_Y = 0x04,
    USE_ROTATE = 0x08,
    USE_MATRIX = 0x10,
    USE_COLOUR = 0x20,
    USE_HALF_TURN = 0x40,
};

typedef struct {
    int kind;
    colour c0, c1;
    vec2 p0, p1;  // gradient points, in pixels
} style;

typedef struct {
    float x0, y0, x1, y1, dxdy;
    int dir;
} edge;

// A flattened path segment, in file units. widths[i] is the line width
// of the edge that ends at pts[i].
typedef struct {
    vec2 *pts;
    float *widths;
    int n, cap;
} polyline;

typedef struct {
    const uint8_t *data;
    size_t len, pos;
    int scale_bits, colour_enc, range;
    float m[6];  // file units to pixels: x' = m0 x + m2 y + m4, y' = m1 x + m3 y + m5
    float tol;   // flattening tolerance, in file units

    // PicoVG only.
    int pvg;
    uint32_t symbol_count;
    size_t *symbol_pos;  // start of each symbol's commands
    uint32_t symbol_limit;  // symbols below this index may be used here
    float grid;             // compact path step, in file units
    colour current;
    int has_current;
    int depth;

    colour *colours;
    uint32_t colour_count;

    polyline *lines;
    int nlines, cap_lines;
    uint32_t *seg_counts;
    int cap_seg_counts;

    edge *edges;
    int nedges, cap_edges;
    int *active;
    float *xs;
    int *dirs;
    int cap_scan;

    int w, h;
    float *px;     // premultiplied linear RGBA
    float *cover;  // coverage for the current row

    jmp_buf fail;
    char *err;
    size_t err_len;
} ctx;

static void fail(ctx *c, const char *fmt, ...) {
    if (c->err && c->err_len) {
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(c->err, c->err_len, fmt, ap);
        va_end(ap);
    }
    longjmp(c->fail, 1);
}

static void *grow(ctx *c, void *ptr, int *cap, int need, size_t elem) {
    if (need <= *cap) return ptr;
    int n = *cap ? *cap : 16;
    while (n < need) {
        if (n > INT_MAX / 2) fail(c, "too much data");
        n *= 2;
    }
    void *p = realloc(ptr, (size_t)n * elem);
    if (!p) fail(c, "out of memory");
    *cap = n;
    return p;
}

// ------------------------------------------------------------- reading

static uint8_t rd_u8(ctx *c) {
    if (c->pos >= c->len) fail(c, "unexpected end of file at byte %zu", c->pos);
    return c->data[c->pos++];
}

static uint32_t rd_var(ctx *c) {
    uint32_t result = 0;
    for (int i = 0; i < 5; i++) {
        uint8_t b = rd_u8(c);
        result |= (uint32_t)(b & 0x7F) << (7 * i);
        if (!(b & 0x80)) return result;
    }
    fail(c, "bad VarUInt at byte %zu", c->pos);
    return 0;
}

static float rd_unit(ctx *c) {
    int32_t v;
    if (c->range == 1) {
        v = (int8_t)rd_u8(c);
    } else if (c->range == 0) {
        uint16_t lo = rd_u8(c);
        uint16_t hi = rd_u8(c);
        v = (int16_t)(lo | hi << 8);
    } else {
        uint32_t u = 0;
        for (int i = 0; i < 4; i++) u |= (uint32_t)rd_u8(c) << (8 * i);
        v = (int32_t)u;
    }
    return (float)v / (float)(1 << c->scale_bits);
}

static vec2 rd_point(ctx *c) {
    vec2 p;
    p.x = rd_unit(c);
    p.y = rd_unit(c);
    return p;
}

static float rd_f32(ctx *c) {
    uint32_t u = 0;
    for (int i = 0; i < 4; i++) u |= (uint32_t)rd_u8(c) << (8 * i);
    float f;
    memcpy(&f, &u, sizeof f);
    return f;
}

static float to_linear(float v) { return powf(v, 2.2f); }
static float to_srgb(float v) { return powf(v, 1.0f / 2.2f); }

static void read_colour_table(ctx *c) {
    c->colours = calloc(c->colour_count ? c->colour_count : 1, sizeof(colour));
    if (!c->colours) fail(c, "out of memory");
    for (uint32_t i = 0; i < c->colour_count; i++) {
        colour *k = &c->colours[i];
        if (c->colour_enc == 0) {
            k->r = to_linear(rd_u8(c) / 255.0f);
            k->g = to_linear(rd_u8(c) / 255.0f);
            k->b = to_linear(rd_u8(c) / 255.0f);
            k->a = rd_u8(c) / 255.0f;
        } else if (c->colour_enc == 1) {
            uint16_t lo = rd_u8(c);
            uint16_t hi = rd_u8(c);
            uint16_t v = lo | hi << 8;
            k->r = to_linear((v & 0x1F) / 31.0f);
            k->g = to_linear(((v >> 5) & 0x3F) / 63.0f);
            k->b = to_linear(((v >> 11) & 0x1F) / 31.0f);
            k->a = 1.0f;
        } else if (c->colour_enc == 2) {
            // scRGB is already linear.
            k->r = rd_f32(c);
            k->g = rd_f32(c);
            k->b = rd_f32(c);
            k->a = rd_f32(c);
        } else {
            fail(c, "custom colour encoding is not supported");
        }
    }
}

static colour rd_colour_ref(ctx *c) {
    uint32_t i = rd_var(c);
    if (i >= c->colour_count) fail(c, "colour index %u out of range", i);
    return c->colours[i];
}

static vec2 to_px(ctx *c, vec2 p) {
    const float *m = c->m;
    vec2 r = {m[0] * p.x + m[2] * p.y + m[4], m[1] * p.x + m[3] * p.y + m[5]};
    return r;
}

// Sets the matrix and the flattening tolerance that goes with it.
static void set_matrix(ctx *c, const float *m) {
    memcpy(c->m, m, sizeof c->m);
    float stretch = fmaxf(sqrtf(m[0] * m[0] + m[1] * m[1]), sqrtf(m[2] * m[2] + m[3] * m[3]));
    c->tol = FLATNESS_PX / fmaxf(stretch, 1e-6f);
}

// How much the matrix scales lengths, for line widths.
static float matrix_scale(const float *m) { return sqrtf(fabsf(m[0] * m[3] - m[1] * m[2])); }

static style rd_style(ctx *c, int kind) {
    style s;
    memset(&s, 0, sizeof s);
    s.kind = kind;
    if (kind == STYLE_FLAT) {
        s.c0 = rd_colour_ref(c);
    } else if (kind == STYLE_LINEAR || kind == STYLE_RADIAL) {
        s.p0 = to_px(c, rd_point(c));
        s.p1 = to_px(c, rd_point(c));
        s.c0 = rd_colour_ref(c);
        s.c1 = rd_colour_ref(c);
    } else if (kind == STYLE_CURRENT && c->pvg) {
        if (!c->has_current) fail(c, "current colour used but never set");
        s.kind = STYLE_FLAT;
        s.c0 = c->current;
    } else {
        fail(c, "bad style kind %d", kind);
    }
    return s;
}

// ------------------------------------------------------------ geometry

// Polylines keep their buffers between commands, so slots are reused.
static polyline *new_line(ctx *c) {
    int old = c->cap_lines;
    c->lines = grow(c, c->lines, &c->cap_lines, c->nlines + 1, sizeof(polyline));
    if (c->cap_lines > old) memset(c->lines + old, 0, (size_t)(c->cap_lines - old) * sizeof(polyline));
    polyline *pl = &c->lines[c->nlines++];
    pl->n = 0;
    return pl;
}

static void add_point(ctx *c, polyline *pl, vec2 p, float width) {
    if (pl->n == pl->cap) {
        int cap = pl->cap;
        pl->pts = grow(c, pl->pts, &cap, pl->n + 1, sizeof(vec2));
        float *w = realloc(pl->widths, (size_t)cap * sizeof(float));
        if (!w) fail(c, "out of memory");
        pl->widths = w;
        pl->cap = cap;
    }
    pl->pts[pl->n] = p;
    pl->widths[pl->n] = width;
    pl->n++;
}

static float vlen(vec2 v) { return sqrtf(v.x * v.x + v.y * v.y); }

// Float to int, clamped to [lo, hi], NaN giving lo. Casting a float that
// does not fit is undefined behaviour, so clamp first.
static int clampf_int(float v, int lo, int hi) {
    if (!(v >= (float)lo)) return lo;
    if (v >= (float)hi) return hi;
    return (int)v;
}

static void flatten_quad(ctx *c, polyline *pl, vec2 p0, vec2 p1, vec2 p2, float w) {
    vec2 dd = {p0.x - 2 * p1.x + p2.x, p0.y - 2 * p1.y + p2.y};
    int n = clampf_int(ceilf(sqrtf(0.25f * vlen(dd) / c->tol)), 1, 512);
    for (int i = 1; i <= n; i++) {
        float t = (float)i / n, u = 1 - t;
        vec2 p = {u * u * p0.x + 2 * u * t * p1.x + t * t * p2.x,
                  u * u * p0.y + 2 * u * t * p1.y + t * t * p2.y};
        add_point(c, pl, p, w);
    }
}

static void flatten_cubic(ctx *c, polyline *pl, vec2 p0, vec2 p1, vec2 p2, vec2 p3, float w) {
    // Wang's formula for the number of segments.
    vec2 d1 = {p0.x - 2 * p1.x + p2.x, p0.y - 2 * p1.y + p2.y};
    vec2 d2 = {p1.x - 2 * p2.x + p3.x, p1.y - 2 * p2.y + p3.y};
    float dd = fmaxf(vlen(d1), vlen(d2));
    int n = clampf_int(ceilf(sqrtf(0.75f * dd / c->tol)), 1, 512);
    for (int i = 1; i <= n; i++) {
        float t = (float)i / n, u = 1 - t;
        float a = u * u * u, b = 3 * u * u * t, cc = 3 * u * t * t, d = t * t * t;
        vec2 p = {a * p0.x + b * p1.x + cc * p2.x + d * p3.x,
                  a * p0.y + b * p1.y + cc * p2.y + d * p3.y};
        add_point(c, pl, p, w);
    }
}

static float vec_angle(float ux, float uy, float vx, float vy) {
    return atan2f(ux * vy - uy * vx, ux * vx + uy * vy);
}

// SVG style endpoint arc. rotation is in degrees.
static void flatten_arc(ctx *c, polyline *pl, vec2 p0, float rx, float ry, float rotation,
                        int large, int sweep, vec2 p1, float w) {
    if (p0.x == p1.x && p0.y == p1.y) return;
    rx = fabsf(rx);
    ry = fabsf(ry);
    if (rx == 0 || ry == 0) {
        add_point(c, pl, p1, w);
        return;
    }
    float phi = rotation * PI_F / 180.0f;
    float cp = cosf(phi), sp = sinf(phi);
    float dx = (p0.x - p1.x) / 2, dy = (p0.y - p1.y) / 2;
    float x1 = cp * dx + sp * dy;
    float y1 = -sp * dx + cp * dy;
    // Scale the radii up if the endpoints are too far apart.
    float lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry);
    if (lam > 1) {
        float s = sqrtf(lam);
        rx *= s;
        ry *= s;
    }
    float num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1;
    float den = rx * rx * y1 * y1 + ry * ry * x1 * x1;
    float coef = den > 0 ? sqrtf(fmaxf(0, num / den)) : 0;
    if (large == sweep) coef = -coef;
    float cx1 = coef * rx * y1 / ry;
    float cy1 = -coef * ry * x1 / rx;
    float cx = cp * cx1 - sp * cy1 + (p0.x + p1.x) / 2;
    float cy = sp * cx1 + cp * cy1 + (p0.y + p1.y) / 2;

    float ux = (x1 - cx1) / rx, uy = (y1 - cy1) / ry;
    float vx = (-x1 - cx1) / rx, vy = (-y1 - cy1) / ry;
    float t1 = vec_angle(1, 0, ux, uy);
    float dt = vec_angle(ux, uy, vx, vy);
    if (!sweep && dt > 0) dt -= 2 * PI_F;
    if (sweep && dt < 0) dt += 2 * PI_F;

    float r = fmaxf(rx, ry);
    float step = 2 * acosf(fmaxf(1 - c->tol / r, 0));
    int n = clampf_int(ceilf(fabsf(dt) / fmaxf(step, 1e-3f)), 1, 1024);
    for (int i = 1; i < n; i++) {
        float t = t1 + dt * i / n;
        float ex = rx * cosf(t), ey = ry * sinf(t);
        vec2 p = {cp * ex - sp * ey + cx, sp * ex + cp * ey + cy};
        add_point(c, pl, p, w);
    }
    add_point(c, pl, p1, w);
}

// Reads Path(segment_count) into c->lines.
static void rd_path(ctx *c, uint32_t segments, float width) {
    c->nlines = 0;
    // Each segment needs at least a byte, which also keeps the count in int range.
    if (segments > c->len - c->pos) fail(c, "segment count %u too large", segments);
    c->seg_counts = grow(c, c->seg_counts, &c->cap_seg_counts, (int)segments, sizeof(uint32_t));
    for (uint32_t i = 0; i < segments; i++) c->seg_counts[i] = rd_var(c) + 1;
    for (uint32_t i = 0; i < segments; i++) {
        polyline *pl = new_line(c);
        vec2 start = rd_point(c);
        vec2 cur = start;
        add_point(c, pl, start, width);
        for (uint32_t j = 0; j < c->seg_counts[i]; j++) {
            uint8_t tag = rd_u8(c);
            if (tag & 0x10) width = rd_unit(c);
            switch (tag & 0x07) {
            case 0:  // line
                cur = rd_point(c);
                add_point(c, pl, cur, width);
                break;
            case 1:  // horizontal line
                cur.x = rd_unit(c);
                add_point(c, pl, cur, width);
                break;
            case 2:  // vertical line
                cur.y = rd_unit(c);
                add_point(c, pl, cur, width);
                break;
            case 3: {  // cubic bezier
                vec2 c0 = rd_point(c);
                vec2 c1 = rd_point(c);
                vec2 p1 = rd_point(c);
                flatten_cubic(c, pl, cur, c0, c1, p1, width);
                cur = p1;
                break;
            }
            case 4: {  // arc circle
                uint8_t flags = rd_u8(c);
                float r = rd_unit(c);
                vec2 p1 = rd_point(c);
                flatten_arc(c, pl, cur, r, r, 0, flags & 1, (flags >> 1) & 1, p1, width);
                cur = p1;
                break;
            }
            case 5: {  // arc ellipse
                uint8_t flags = rd_u8(c);
                float rx = rd_unit(c);
                float ry = rd_unit(c);
                float rot = rd_unit(c);
                vec2 p1 = rd_point(c);
                flatten_arc(c, pl, cur, rx, ry, rot, flags & 1, (flags >> 1) & 1, p1, width);
                cur = p1;
                break;
            }
            case 6:  // close path
                if (cur.x != start.x || cur.y != start.y) add_point(c, pl, start, width);
                cur = start;
                break;
            case 7: {  // quadratic bezier
                vec2 c0 = rd_point(c);
                vec2 p1 = rd_point(c);
                flatten_quad(c, pl, cur, c0, p1, width);
                cur = p1;
                break;
            }
            }
        }
    }
}

static void skip(ctx *c, size_t n);

static int64_t rd_varint(ctx *c) {
    uint32_t u = rd_var(c);
    return u & 1 ? -(int64_t)(u >> 1) - 1 : (int64_t)(u >> 1);
}

// Reads a PicoVG compact path into c->lines. Each subpath is: op count,
// op kinds packed 2 bits each, then zigzag offsets in grid steps, each from
// the previous point. A subpath starts relative to the last one's end.
static void rd_compact_path(ctx *c, uint32_t segments, float width) {
    c->nlines = 0;
    const float g = c->grid;
    int64_t pen_x = 0, pen_y = 0;
    for (uint32_t i = 0; i < segments; i++) {
        // Short form packs 1 to 3 op kinds in the header byte itself.
        uint8_t head = rd_u8(c);
        uint32_t nops;
        size_t kinds;
        int shift;
        if (head & 3) {
            nops = head & 3;
            kinds = c->pos - 1;
            shift = 2;
        } else {
            if (head != 0) fail(c, "bad compact path head 0x%02x", head);
            nops = rd_var(c);
            kinds = c->pos;
            shift = 0;
            skip(c, ((size_t)nops + 3) / 4);
        }
        int64_t sx = pen_x + rd_varint(c), sy = pen_y + rd_varint(c);
        int64_t x = sx, y = sy;
        polyline *pl = new_line(c);
        vec2 start = {sx * g, sy * g};
        add_point(c, pl, start, width);
        for (uint32_t j = 0; j < nops; j++) {
            vec2 cur = {x * g, y * g};
            uint32_t bit = (uint32_t)shift + 2 * j;
            switch ((c->data[kinds + bit / 8] >> (bit % 8)) & 3) {
            case 0:  // line
                x += rd_varint(c);
                y += rd_varint(c);
                add_point(c, pl, (vec2){x * g, y * g}, width);
                break;
            case 1: {  // quadratic bezier
                x += rd_varint(c);
                y += rd_varint(c);
                vec2 q = {x * g, y * g};
                x += rd_varint(c);
                y += rd_varint(c);
                flatten_quad(c, pl, cur, q, (vec2){x * g, y * g}, width);
                break;
            }
            case 2: {  // cubic bezier
                x += rd_varint(c);
                y += rd_varint(c);
                vec2 c1 = {x * g, y * g};
                x += rd_varint(c);
                y += rd_varint(c);
                vec2 c2 = {x * g, y * g};
                x += rd_varint(c);
                y += rd_varint(c);
                flatten_cubic(c, pl, cur, c1, c2, (vec2){x * g, y * g}, width);
                break;
            }
            case 3:  // close
                if (x != sx || y != sy) add_point(c, pl, start, width);
                x = sx;
                y = sy;
                break;
            }
        }
        pen_x = x;
        pen_y = y;
    }
}

// Reads count points into a single polyline in c->lines.
static void rd_points(ctx *c, uint32_t count, float width) {
    c->nlines = 0;
    polyline *pl = new_line(c);
    for (uint32_t i = 0; i < count; i++) add_point(c, pl, rd_point(c), width);
}

static void rect_line(ctx *c, vec2 pos, vec2 size, float width) {
    c->nlines = 0;
    polyline *pl = new_line(c);
    vec2 p[4] = {{pos.x, pos.y}, {pos.x + size.x, pos.y},
                 {pos.x + size.x, pos.y + size.y}, {pos.x, pos.y + size.y}};
    for (int i = 0; i < 4; i++) add_point(c, pl, p[i], width);
}

// ----------------------------------------------------------- rasterise

static void add_edge(ctx *c, vec2 a, vec2 b) {
    if (a.y == b.y) return;
    if (!isfinite(a.x) || !isfinite(a.y) || !isfinite(b.x) || !isfinite(b.y)) return;
    c->edges = grow(c, c->edges, &c->cap_edges, c->nedges + 1, sizeof(edge));
    edge *e = &c->edges[c->nedges++];
    if (a.y < b.y) {
        *e = (edge){a.x, a.y, b.x, b.y, 0, 1};
    } else {
        *e = (edge){b.x, b.y, a.x, a.y, 0, -1};
    }
    e->dxdy = (e->x1 - e->x0) / (e->y1 - e->y0);
}

// Every polyline is a closed polygon.
static void fill_edges(ctx *c) {
    for (int i = 0; i < c->nlines; i++) {
        polyline *pl = &c->lines[i];
        if (pl->n < 2) continue;
        for (int j = 0; j < pl->n; j++) {
            vec2 a = to_px(c, pl->pts[j]);
            vec2 b = to_px(c, pl->pts[(j + 1) % pl->n]);
            add_edge(c, a, b);
        }
    }
}

// A segment with round caps (a stadium). Wound the same way for every
// segment, so the non-zero rule gives their union.
static void add_capsule(ctx *c, vec2 a, vec2 b, float r) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float ang = (dx != 0 || dy != 0) ? atan2f(dy, dx) : 0;
    float step = 2 * acosf(fmaxf(1 - FLATNESS_PX / r, 0));
    int n = clampf_int(ceilf(PI_F / fmaxf(step, 1e-3f)), 2, 64);
    vec2 first = {0}, prev = {0};
    for (int side = 0; side < 2; side++) {
        vec2 centre = side == 0 ? b : a;
        float base = ang - PI_F / 2 + side * PI_F;
        for (int i = 0; i <= n; i++) {
            float t = base + PI_F * i / n;
            vec2 p = {centre.x + r * cosf(t), centre.y + r * sinf(t)};
            if (side == 0 && i == 0) first = p;
            else add_edge(c, prev, p);
            prev = p;
        }
    }
    add_edge(c, prev, first);
}

static void stroke_edges(ctx *c, int closed) {
    float scale = matrix_scale(c->m);
    for (int i = 0; i < c->nlines; i++) {
        polyline *pl = &c->lines[i];
        if (pl->n == 0) continue;
        if (pl->n == 1) {
            float r = fmaxf(pl->widths[0] * scale, 1.0f) / 2;
            add_capsule(c, to_px(c, pl->pts[0]), to_px(c, pl->pts[0]), r);
            continue;
        }
        int count = closed ? pl->n : pl->n - 1;
        for (int j = 0; j < count; j++) {
            int k = (j + 1) % pl->n;
            // Lines thinner than a pixel are drawn one pixel wide.
            float r = fmaxf(pl->widths[k] * scale, 1.0f) / 2;
            add_capsule(c, to_px(c, pl->pts[j]), to_px(c, pl->pts[k]), r);
        }
    }
}

static int cmp_edge(const void *a, const void *b) {
    float ya = ((const edge *)a)->y0, yb = ((const edge *)b)->y0;
    return (ya > yb) - (ya < yb);
}

static colour lerp_colour(colour a, colour b, float t) {
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    colour r = {a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t,
                a.b + (b.b - a.b) * t, a.a + (b.a - a.a) * t};
    return r;
}

static colour sample(const style *s, float x, float y) {
    if (s->kind == STYLE_FLAT) return s->c0;
    float dx = s->p1.x - s->p0.x, dy = s->p1.y - s->p0.y;
    float px = x - s->p0.x, py = y - s->p0.y;
    float t;
    if (s->kind == STYLE_LINEAR) {
        float l2 = dx * dx + dy * dy;
        t = l2 > 0 ? (px * dx + py * dy) / l2 : 0;
    } else {
        float r = sqrtf(dx * dx + dy * dy);
        t = r > 0 ? sqrtf(px * px + py * py) / r : 1;
    }
    return lerp_colour(s->c0, s->c1, t);
}

static void add_span(ctx *c, float xa, float xb, int *lo, int *hi) {
    const float wgt = 1.0f / SUBSAMPLES;
    if (xa < 0) xa = 0;
    if (xb > c->w) xb = (float)c->w;
    if (xb <= xa) return;
    int ia = (int)xa, ib = (int)xb;
    if (ia < *lo) *lo = ia;
    if (ib > *hi) *hi = ib;
    if (ia == ib) {
        c->cover[ia] += (xb - xa) * wgt;
        return;
    }
    c->cover[ia] += (ia + 1 - xa) * wgt;
    for (int i = ia + 1; i < ib; i++) c->cover[i] += wgt;
    if (ib < c->w) c->cover[ib] += (xb - ib) * wgt;
}

static void rasterise(ctx *c, int nonzero, const style *s) {
    if (c->nedges == 0) return;
    qsort(c->edges, (size_t)c->nedges, sizeof(edge), cmp_edge);
    int cap = c->cap_scan;
    c->active = grow(c, c->active, &cap, c->nedges, sizeof(int));
    cap = c->cap_scan;
    c->xs = grow(c, c->xs, &cap, c->nedges, sizeof(float));
    cap = c->cap_scan;
    c->dirs = grow(c, c->dirs, &cap, c->nedges, sizeof(int));
    c->cap_scan = cap;

    float ymax_f = 0;
    for (int i = 0; i < c->nedges; i++) ymax_f = fmaxf(ymax_f, c->edges[i].y1);
    int y0 = clampf_int(floorf(c->edges[0].y0), 0, c->h);
    int y1 = clampf_int(ceilf(ymax_f), 0, c->h);

    int next = 0, nactive = 0;
    for (int y = y0; y < y1; y++) {
        int lo = c->w, hi = -1;
        for (int sub = 0; sub < SUBSAMPLES; sub++) {
            float sy = y + (sub + 0.5f) / SUBSAMPLES;
            while (next < c->nedges && c->edges[next].y0 <= sy) c->active[nactive++] = next++;
            int k = 0, nx = 0;
            for (int i = 0; i < nactive; i++) {
                const edge *e = &c->edges[c->active[i]];
                if (e->y1 <= sy) continue;
                c->active[k++] = c->active[i];
                float x = e->x0 + (sy - e->y0) * e->dxdy;
                // Insertion sort by x, carrying the direction along.
                int j = nx++;
                while (j > 0 && c->xs[j - 1] > x) {
                    c->xs[j] = c->xs[j - 1];
                    c->dirs[j] = c->dirs[j - 1];
                    j--;
                }
                c->xs[j] = x;
                c->dirs[j] = e->dir;
            }
            nactive = k;
            int wind = 0;
            float xa = 0;
            for (int i = 0; i < nx; i++) {
                int was_in = nonzero ? wind != 0 : (wind & 1);
                wind += c->dirs[i];
                int is_in = nonzero ? wind != 0 : (wind & 1);
                if (!was_in && is_in) xa = c->xs[i];
                else if (was_in && !is_in) add_span(c, xa, c->xs[i], &lo, &hi);
            }
        }
        if (hi < lo) continue;
        if (hi >= c->w) hi = c->w - 1;
        float *row = c->px + (size_t)y * c->w * 4;
        for (int x = lo; x <= hi; x++) {
            float cv = c->cover[x];
            c->cover[x] = 0;
            if (cv <= 0) continue;
            if (cv > 1) cv = 1;
            colour k = sample(s, x + 0.5f, y + 0.5f);
            float a = k.a * cv;
            float *d = row + x * 4;
            d[0] = k.r * a + d[0] * (1 - a);
            d[1] = k.g * a + d[1] * (1 - a);
            d[2] = k.b * a + d[2] * (1 - a);
            d[3] = a + d[3] * (1 - a);
        }
    }
    c->nedges = 0;
}

static void fill(ctx *c, const style *s) {
    fill_edges(c);
    rasterise(c, 0, s);
}

static void stroke(ctx *c, const style *s, int closed) {
    stroke_edges(c, closed);
    rasterise(c, 1, s);
}

// ------------------------------------------------------------- commands

static uint32_t rd_size(ctx *c);

static int rd_header(ctx *c, int *width, int *height) {
    if (c->len < 4 || c->data[0] != 0x72 || c->data[1] != 0x56) return -1;
    if (c->data[2] != 1) return -1;
    c->pos = 3;
    uint8_t b = rd_u8(c);
    c->scale_bits = b & 0x0F;
    c->colour_enc = (b >> 4) & 0x03;
    c->range = (b >> 6) & 0x03;
    if (c->range == 3) return -1;
    *width = (int)rd_size(c);
    *height = (int)rd_size(c);
    return 0;
}

static void run_commands(ctx *c);

// PicoVG "use symbol": draws a symbol with a transform, then carries on.
static void use_symbol(ctx *c) {
    if (!c->pvg) fail(c, "command 11 is PicoVG only");
    uint32_t index = rd_var(c);
    if (index >= c->symbol_limit) fail(c, "symbol %u is not defined before this use", index);
    uint8_t flags = rd_u8(c);
    if (flags & 0x80) fail(c, "reserved use flag set");
    float tx = 0, ty = 0, sx = 1, sy = 1, deg = 0;
    float lin[4] = {1, 0, 0, 1};  // a b c d
    if (flags & USE_TRANSLATE) {
        tx = rd_unit(c);
        ty = rd_unit(c);
    }
    if (flags & USE_MATRIX) {
        for (int i = 0; i < 4; i++) lin[i] = rd_f32(c);
    } else {
        if (flags & USE_SCALE) sx = sy = rd_f32(c);
        if (flags & USE_SCALE_Y) sy = rd_f32(c);
        if (flags & USE_ROTATE) deg = rd_f32(c);
        if (flags & USE_HALF_TURN) deg += 180;
        // Rotate, after scaling.
        float r = deg * PI_F / 180.0f, cs = cosf(r), sn = sinf(r);
        lin[0] = cs * sx;
        lin[1] = sn * sx;
        lin[2] = -sn * sy;
        lin[3] = cs * sy;
    }
    colour saved_current = c->current;
    int saved_has = c->has_current;
    if (flags & USE_COLOUR) {
        c->current = rd_colour_ref(c);
        c->has_current = 1;
    }
    if (++c->depth > MAX_USE_DEPTH) fail(c, "symbols nested too deep");

    // New matrix = current matrix * local transform.
    float saved_m[6], m[6];
    memcpy(saved_m, c->m, sizeof saved_m);
    const float *p = saved_m;
    m[0] = p[0] * lin[0] + p[2] * lin[1];
    m[1] = p[1] * lin[0] + p[3] * lin[1];
    m[2] = p[0] * lin[2] + p[2] * lin[3];
    m[3] = p[1] * lin[2] + p[3] * lin[3];
    m[4] = p[0] * tx + p[2] * ty + p[4];
    m[5] = p[1] * tx + p[3] * ty + p[5];
    set_matrix(c, m);

    size_t saved_pos = c->pos;
    uint32_t saved_limit = c->symbol_limit;
    c->pos = c->symbol_pos[index];
    c->symbol_limit = index;  // a symbol may only use earlier symbols
    run_commands(c);

    c->pos = saved_pos;
    c->symbol_limit = saved_limit;
    set_matrix(c, saved_m);
    c->current = saved_current;
    c->has_current = saved_has;
    c->depth--;
}

static void run_commands(ctx *c) {
    for (;;) {
        uint8_t b = rd_u8(c);
        int cmd = b & 0x3F;
        int kind = b >> 6;
        switch (cmd) {
        case 0:  // end of document
            return;
        case 1: {  // fill polygon
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            rd_points(c, n, 0);
            fill(c, &s);
            break;
        }
        case 2: {  // fill rectangles
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            for (uint32_t i = 0; i < n; i++) {
                vec2 pos = rd_point(c);
                vec2 size = rd_point(c);
                rect_line(c, pos, size, 0);
                fill(c, &s);
            }
            break;
        }
        case 3: {  // fill path
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            rd_path(c, n, 0);
            fill(c, &s);
            break;
        }
        case 4: {  // draw lines
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            float w = rd_unit(c);
            c->nlines = 0;
            for (uint32_t i = 0; i < n; i++) {
                polyline *pl = new_line(c);
                add_point(c, pl, rd_point(c), w);
                add_point(c, pl, rd_point(c), w);
            }
            stroke(c, &s, 0);
            break;
        }
        case 5:    // draw line loop
        case 6: {  // draw line strip
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            float w = rd_unit(c);
            rd_points(c, n, w);
            stroke(c, &s, cmd == 5);
            break;
        }
        case 7: {  // draw line path
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            float w = rd_unit(c);
            rd_path(c, n, w);
            stroke(c, &s, 0);
            break;
        }
        case 8: {  // outline fill polygon
            uint8_t h = rd_u8(c);
            uint32_t n = (h & 0x3F) + 1u;
            style fs = rd_style(c, kind);
            style ls = rd_style(c, h >> 6);
            float w = rd_unit(c);
            rd_points(c, n, w);
            fill(c, &fs);
            stroke(c, &ls, 1);
            break;
        }
        case 9: {  // outline fill rectangles
            uint8_t h = rd_u8(c);
            uint32_t n = (h & 0x3F) + 1u;
            style fs = rd_style(c, kind);
            style ls = rd_style(c, h >> 6);
            float w = rd_unit(c);
            for (uint32_t i = 0; i < n; i++) {
                vec2 pos = rd_point(c);
                vec2 size = rd_point(c);
                rect_line(c, pos, size, w);
                fill(c, &fs);
                stroke(c, &ls, 1);
            }
            break;
        }
        case 10: {  // outline fill path
            uint8_t h = rd_u8(c);
            uint32_t n = (h & 0x3F) + 1u;
            style fs = rd_style(c, kind);
            style ls = rd_style(c, h >> 6);
            float w = rd_unit(c);
            rd_path(c, n, w);
            fill(c, &fs);
            stroke(c, &ls, 0);
            break;
        }
        case 11:  // use symbol (PicoVG)
            if (kind != 0) fail(c, "use symbol must have style kind 0");
            use_symbol(c);
            break;
        case 12: {  // compact fill path (PicoVG)
            if (!c->pvg) fail(c, "command 12 is PicoVG only");
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            rd_compact_path(c, n, 0);
            fill(c, &s);
            break;
        }
        case 13: {  // compact line path (PicoVG)
            if (!c->pvg) fail(c, "command 13 is PicoVG only");
            uint32_t n = rd_var(c) + 1;
            style s = rd_style(c, kind);
            float w = rd_unit(c);
            rd_compact_path(c, n, w);
            stroke(c, &s, 0);
            break;
        }
        default:
            fail(c, "unknown command %d at byte %zu", cmd, c->pos - 1);
        }
    }
}

static void free_ctx(ctx *c) {
    for (int i = 0; i < c->cap_lines; i++) {
        free(c->lines[i].pts);
        free(c->lines[i].widths);
    }
    free(c->lines);
    free(c->seg_counts);
    free(c->colours);
    free(c->edges);
    free(c->active);
    free(c->xs);
    free(c->dirs);
    free(c->px);
    free(c->cover);
    free(c->symbol_pos);
}

// Runs the commands at c->pos into a new RGBA buffer of c->w by c->h.
// fw and fh are the image size in file units.
static uint8_t *render_image(ctx *c, int fw, int fh) {
    int width = c->w, height = c->h;
    if (fw <= 0 || fh <= 0 || width <= 0 || height <= 0) fail(c, "bad image size");
    float m[6] = {(float)width / fw, 0, 0, (float)height / fh, 0, 0};
    set_matrix(c, m);

    c->px = calloc((size_t)width * height * 4, sizeof(float));
    c->cover = calloc((size_t)width + 1, sizeof(float));
    if (!c->px || !c->cover) fail(c, "out of memory");

    run_commands(c);

    uint8_t *out = malloc((size_t)width * height * 4);
    if (!out) fail(c, "out of memory");
    for (size_t i = 0; i < (size_t)width * height; i++) {
        const float *p = c->px + i * 4;
        uint8_t *o = out + i * 4;
        float a = p[3];
        if (a <= 0) {
            o[0] = o[1] = o[2] = o[3] = 0;
            continue;
        }
        for (int k = 0; k < 3; k++) {
            float v = to_srgb(fminf(fmaxf(p[k] / a, 0), 1));
            o[k] = (uint8_t)(v * 255 + 0.5f);
        }
        o[3] = (uint8_t)(fminf(a, 1) * 255 + 0.5f);
    }
    return out;
}

static void init_ctx(ctx *c, const uint8_t *data, size_t len, char *err, size_t err_len) {
    memset(c, 0, sizeof *c);
    c->data = data;
    c->len = len;
    c->err = err;
    c->err_len = err_len;
}

int tvg_size(const uint8_t *data, size_t len, int *width, int *height) {
    ctx c;
    init_ctx(&c, data, len, NULL, 0);
    if (setjmp(c.fail)) return -1;
    return rd_header(&c, width, height);
}

uint8_t *tvg_render(const uint8_t *data, size_t len, int width, int height,
                    char *err, size_t err_len) {
    ctx c;
    init_ctx(&c, data, len, err, err_len);
    c.w = width;
    c.h = height;
    if (setjmp(c.fail)) {
        free_ctx(&c);
        return NULL;
    }
    int fw, fh;
    if (rd_header(&c, &fw, &fh) != 0) fail(&c, "not a TinyVG v1 file");
    c.colour_count = rd_var(&c);
    read_colour_table(&c);
    uint8_t *out = render_image(&c, fw, fh);
    free_ctx(&c);
    return out;
}

// ---------------------------------------------------------------- PicoVG

typedef struct {
    const uint8_t *name;
    size_t name_len;
    int width, height;
    size_t body;  // offset of the image's commands
} pvg_image;

static uint32_t rd_size(ctx *c) {
    uint32_t v = 0;
    int bytes = c->range == 1 ? 1 : c->range == 0 ? 2 : 4;
    for (int i = 0; i < bytes; i++) v |= (uint32_t)rd_u8(c) << (8 * i);
    return v;
}

static void skip(ctx *c, size_t n) {
    if (n > c->len - c->pos) fail(c, "unexpected end of file at byte %zu", c->pos);
    c->pos += n;
}

// Reads the header, colours and symbol table. Leaves c->pos at the image
// count and returns it.
static uint32_t pvg_open(ctx *c) {
    if (c->len < 4 || c->data[0] != 'P' || c->data[1] != 'V' || c->data[2] != 1)
        fail(c, "not a PicoVG v1 file");
    c->pos = 3;
    uint8_t b = rd_u8(c);
    c->scale_bits = b & 0x0F;
    c->colour_enc = (b >> 4) & 0x03;
    c->range = (b >> 6) & 0x03;
    if (c->range == 3) fail(c, "bad coordinate range");
    uint8_t grid = rd_u8(c);
    if (grid > 15) fail(c, "bad path grid %u", grid);
    c->grid = (float)(1 << grid) / (float)(1 << c->scale_bits);
    c->pvg = 1;
    c->colour_count = rd_var(c);
    read_colour_table(c);
    c->symbol_count = rd_var(c);
    c->symbol_pos = malloc(sizeof(size_t) * (c->symbol_count ? c->symbol_count : 1));
    if (!c->symbol_pos) fail(c, "out of memory");
    for (uint32_t i = 0; i < c->symbol_count; i++) {
        uint32_t n = rd_var(c);
        c->symbol_pos[i] = c->pos;
        skip(c, n);
    }
    return rd_var(c);
}

// Walks the image list from c->pos (just after the count) to image index.
static pvg_image pvg_seek(ctx *c, uint32_t count, int index) {
    if (index < 0 || (uint32_t)index >= count) fail(c, "no image %d", index);
    pvg_image img = {0};
    for (int i = 0; i <= index; i++) {
        img.name_len = rd_var(c);
        img.name = c->data + c->pos;
        skip(c, img.name_len);
        img.width = (int)rd_size(c);
        img.height = (int)rd_size(c);
        uint32_t n = rd_var(c);
        img.body = c->pos;
        skip(c, n);
    }
    return img;
}

int pvg_count(const uint8_t *data, size_t len) {
    ctx c;
    init_ctx(&c, data, len, NULL, 0);
    if (setjmp(c.fail)) {
        free_ctx(&c);
        return -1;
    }
    int n = (int)pvg_open(&c);
    free_ctx(&c);
    return n;
}

int pvg_info(const uint8_t *data, size_t len, int index, int *width, int *height,
             const char **name, size_t *name_len) {
    ctx c;
    init_ctx(&c, data, len, NULL, 0);
    if (setjmp(c.fail)) {
        free_ctx(&c);
        return -1;
    }
    uint32_t count = pvg_open(&c);
    pvg_image img = pvg_seek(&c, count, index);
    if (width) *width = img.width;
    if (height) *height = img.height;
    if (name) *name = (const char *)img.name;
    if (name_len) *name_len = img.name_len;
    free_ctx(&c);
    return 0;
}

int pvg_find(const uint8_t *data, size_t len, const char *name) {
    ctx c;
    init_ctx(&c, data, len, NULL, 0);
    if (setjmp(c.fail)) {
        free_ctx(&c);
        return -1;
    }
    uint32_t count = pvg_open(&c);
    size_t want = strlen(name);
    int found = -1;
    for (uint32_t i = 0; i < count && found < 0; i++) {
        // Seeking to index 0 reads the image at c->pos and moves past it.
        pvg_image img = pvg_seek(&c, count, 0);
        if (img.name_len == want && memcmp(img.name, name, want) == 0) found = (int)i;
    }
    free_ctx(&c);
    return found;
}

uint8_t *pvg_render(const uint8_t *data, size_t len, int index, int width, int height,
                    char *err, size_t err_len) {
    ctx c;
    init_ctx(&c, data, len, err, err_len);
    c.w = width;
    c.h = height;
    if (setjmp(c.fail)) {
        free_ctx(&c);
        return NULL;
    }
    uint32_t count = pvg_open(&c);
    pvg_image img = pvg_seek(&c, count, index);
    c.pos = img.body;
    c.symbol_limit = c.symbol_count;
    uint8_t *out = render_image(&c, img.width, img.height);
    free_ctx(&c);
    return out;
}
