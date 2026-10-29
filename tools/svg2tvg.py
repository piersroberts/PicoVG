#!/usr/bin/env python3
"""Convert an SVG to binary TinyVG.

Covers this SVG subset: svg, g, use, symbol,
path, rect, circle, ellipse, userSpaceOnUse patterns and two stop linear and
radial gradients, and clip-path on fills. TinyVG fills are even-odd, so
non-zero fills that would draw differently are rebuilt without overlaps.
TinyVG has no transforms, so every point is transformed here. Arcs and
rounded rects become cubic beziers so they survive rotation and non-uniform
scale.

Usage: svg2tvg.py in.svg out.tvg
"""

import math
import struct
import sys
import xml.etree.ElementTree as ET

import pathops
import svgelements

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# 16 bit units with 3 fraction bits: 1/8 unit precision, range -4096..4095.
# Fewer fraction bits make the low bytes repeat more, so files gzip better.
SCALE_BITS = 3

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

# SVG initial values for the properties the converter reads.
DEFAULT_STYLE = {"fill": "black", "stroke": "none", "stroke-width": "1"}


# ---------------------------------------------------------------- matrices

def mat_mul(m, n):
    """Return m * n, so n is applied first."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (
        a * A + c * B,
        b * A + d * B,
        a * C + c * D,
        b * C + d * D,
        a * E + c * F + e,
        b * E + d * F + f,
    )


def mat_apply(m, p):
    a, b, c, d, e, f = m
    x, y = p
    return (a * x + c * y + e, b * x + d * y + f)


def translate(tx, ty):
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def scale(sx, sy):
    return (sx, 0.0, 0.0, sy, 0.0, 0.0)


def mat_scale(m):
    """How much the matrix scales lengths, for line widths."""
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def from_matrix(mx):
    return (mx.a, mx.b, mx.c, mx.d, mx.e, mx.f)


def parse_transform(text):
    return from_matrix(svgelements.Matrix(text or ""))


def viewbox_matrix(viewbox, x, y, w, h, par):
    """Map a viewBox into the viewport (x, y, w, h) per preserveAspectRatio."""
    vx, vy, vw, vh = viewbox
    return parse_transform(svgelements.Viewbox.viewbox_transform(x, y, w, h, vx, vy, vw, vh, par))


# ------------------------------------------------------------- path parser

def xy(p):
    return (float(p.x), float(p.y))


def path_subpaths(path):
    """An svgelements Path as subpaths of absolute ops.

    Each subpath is (start, ops). Ops are ("L", p), ("Q", c, p),
    ("C", c1, c2, p) and ("Z",). Arcs become one cubic per quarter turn.
    """
    subpaths = []
    ops = None
    for seg in path:
        if isinstance(seg, svgelements.Move):
            ops = []
            subpaths.append((xy(seg.end), ops))
            continue
        if ops is None:
            # A command after Z without M starts a new subpath at the start.
            ops = []
            subpaths.append((xy(seg.start), ops))
        if isinstance(seg, svgelements.Close):
            ops.append(("Z",))
            ops = None
        elif isinstance(seg, svgelements.Line):
            ops.append(("L", xy(seg.end)))
        elif isinstance(seg, svgelements.QuadraticBezier):
            ops.append(("Q", xy(seg.control), xy(seg.end)))
        elif isinstance(seg, svgelements.CubicBezier):
            ops.append(("C", xy(seg.control1), xy(seg.control2), xy(seg.end)))
        elif isinstance(seg, svgelements.Arc):
            if seg.start == seg.end:
                continue
            if seg.rx == 0 or seg.ry == 0:
                ops.append(("L", xy(seg.end)))
                continue
            for c1, c2, end in arc_cubics(seg):
                ops.append(("C", c1, c2, end))
    return [(s, o) for s, o in subpaths if o]


def arc_cubics(arc):
    """An arc as (c1, c2, end) cubics, one per quarter turn or less.

    Uses k = 4/3 tan(step/4), which is closer to a circle than the
    formula in svgelements' as_cubic_curves().
    """
    n = max(1, math.ceil(abs(arc.sweep) / (math.pi / 2) - 1e-9))
    step = arc.sweep / n
    k = 4 / 3 * math.tan(step / 4)
    rx, ry = arc.rx, arc.ry
    cx, cy = xy(arc.center)
    rot = float(arc.get_rotation())
    cp, sp = math.cos(rot), math.sin(rot)

    def point(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return (cp * x - sp * y + cx, sp * x + cp * y + cy)

    def deriv(t):
        x, y = -rx * math.sin(t), ry * math.cos(t)
        return (cp * x - sp * y, sp * x + cp * y)

    out = []
    t = arc.get_start_t()
    for i in range(n):
        pa, pb = point(t), point(t + step)
        da, db = deriv(t), deriv(t + step)
        c1 = (pa[0] + k * da[0], pa[1] + k * da[1])
        c2 = (pb[0] - k * db[0], pb[1] - k * db[1])
        out.append((c1, c2, xy(arc.end) if i == n - 1 else pb))
        t += step
    return out


def parse_path(d):
    return path_subpaths(svgelements.Path(d))


def rect_path(x, y, w, h, rx, ry):
    return path_subpaths(svgelements.Path(svgelements.Rect(x, y, w, h, rx=rx, ry=ry)))


def ellipse_path(cx, cy, rx, ry):
    return path_subpaths(svgelements.Path(svgelements.Ellipse(cx, cy, rx, ry)))


# Areas below this, in square units, count as empty.
AREA_EPS = 1e-4


def to_skia(subpaths, evenodd=False):
    path = pathops.Path(fillType=pathops.FillType.EVEN_ODD if evenodd else pathops.FillType.WINDING)
    for start, ops in subpaths:
        path.moveTo(*start)
        for op in ops:
            if op[0] == "L":
                path.lineTo(*op[1])
            elif op[0] == "Q":
                path.quadTo(*op[1], *op[2])
            elif op[0] == "C":
                path.cubicTo(*op[1], *op[2], *op[3])
        # Fills close every subpath.
        path.close()
    return path


def from_skia(path):
    subpaths, ops = [], None
    for verb, pts in path.segments:
        if verb == "moveTo":
            ops = []
            subpaths.append((pts[0], ops))
        elif verb == "lineTo":
            ops.append(("L", pts[0]))
        elif verb == "qCurveTo":
            ops.append(("Q", pts[0], pts[1]))
        elif verb == "curveTo":
            ops.append(("C", pts[0], pts[1], pts[2]))
        elif verb == "closePath":
            ops.append(("Z",))
    return [(st, o) for st, o in subpaths if o]


def nonzero_to_evenodd(subpaths):
    """A non-zero fill as subpaths that fill the same area under even-odd.

    The two rules only differ where the path overlaps itself wound the same
    way. Paths where they agree come back unchanged. The rest are rebuilt
    by Skia without overlaps.
    """
    nonzero = to_skia(subpaths)
    if abs(pathops.op(nonzero, to_skia(subpaths, True), pathops.PathOp.XOR).area) < AREA_EPS:
        return subpaths
    nonzero.simplify()
    return from_skia(nonzero)


def transform_subpaths(m, subpaths):
    out = []
    for start, ops in subpaths:
        new_ops = []
        for op in ops:
            new_ops.append((op[0],) + tuple(mat_apply(m, p) for p in op[1:]))
        out.append((mat_apply(m, start), new_ops))
    return out


# ------------------------------------------------------------- converter

def parse_length(text):
    """A length like "1.5", "1.5px" or "2pt", in user units."""
    v = svgelements.Length(text).value(ppi=96)
    if not isinstance(v, float | int):
        raise ValueError(f"unsupported length {text!r}")
    return float(v)


def parse_css(text):
    """Declarations from a style attribute, as a dict."""
    out = {}
    for decl in (text or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def parse_colour(text):
    c = svgelements.Color(text.strip())
    return (c.red, c.green, c.blue)


def parse_offset(text):
    text = text.strip()
    return float(text[:-1]) / 100 if text.endswith("%") else float(text)


def bbox(subpaths):
    """Bounds of the points and control points, which contain the curves."""
    pts = [p for start, ops in subpaths for p in [start] + [q for op in ops for q in op[1:]]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def boxes_overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def inside_round_rect(p, x, y, w, h, rx, ry):
    px, py = p
    if px < x or px > x + w or py < y or py > y + h:
        return False
    # Distance into a corner region, tested against the corner ellipse.
    cx = min(max(px, x + rx), x + w - rx)
    cy = min(max(py, y + ry), y + h - ry)
    if rx == 0 or ry == 0:
        return True
    dx, dy = (px - cx) / rx, (py - cy) / ry
    return dx * dx + dy * dy <= 1.0


class Converter:
    def __init__(self, root=None):
        self.set_root(root)
        self.colours = []
        self.commands = []  # dicts: kind ("fill"|"line"), colour, width, subpaths
        self.warnings = []
        self.clip = None  # pathops.Path in output coordinates, or None

    def set_root(self, root):
        self.root = root
        self.ids = {el.get("id"): el for el in root.iter() if el.get("id")} if root is not None else {}

    def href(self, el):
        """The element a <use> points at."""
        return self.ids[el.get(XLINK_HREF, el.get("href", ""))[1:]]

    def symbol_matrix(self, el, target, m):
        """Maps the <symbol> target's viewBox into the box of the <use> el."""
        vb = [float(v) for v in target.get("viewBox").split()]
        x, y = float(el.get("x", 0)), float(el.get("y", 0))
        w = float(el.get("width", vb[2]))
        h = float(el.get("height", vb[3]))
        return mat_mul(m, viewbox_matrix(vb, x, y, w, h, target.get("preserveAspectRatio")))

    def colour_index(self, rgb, alpha=1.0):
        rgba = tuple(rgb) + (max(0, min(255, round(alpha * 255))),)
        if rgba not in self.colours:
            self.colours.append(rgba)
        return self.colours.index(rgba)

    # Properties that pass from parent to child, as SVG defines them.
    INHERITED = ("fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity", "visibility",
                 "fill-rule")

    def style(self, el, inherited):
        """Resolve the style for el. CSS in style="" beats attributes."""
        s = {k: v for k, v in inherited.items() if k in self.INHERITED or k == "group-opacity"}
        css = parse_css(el.get("style"))
        for key in self.INHERITED + ("opacity", "display"):
            value = css.get(key, el.get(key))
            if value is not None:
                s[key] = value
        # Group opacity cannot be composited in TinyVG, so multiply it down.
        s["group-opacity"] = float(inherited.get("group-opacity", 1)) * float(s.pop("opacity", 1))
        return s

    def hidden(self, el, s):
        return s.get("display") == "none" or s.get("visibility") in ("hidden", "collapse")

    def alpha(self, s, which):
        return float(s.get(which + "-opacity", 1)) * float(s.get("group-opacity", 1))

    def off_page(self, subpaths, margin):
        """True if the shape lies wholly outside the image, so it cannot show."""
        page = getattr(self, "page", None)
        if page is None or getattr(self, "in_symbol", 0):
            return False
        x0, y0, x1, y1 = bbox(subpaths)
        return x1 < -margin or y1 < -margin or x0 > page[0] + margin or y0 > page[1] + margin

    def gradient_attrs(self, el):
        """Attributes and stops, following href to the gradients they extend."""
        attrs, stops = {}, None
        while el is not None:
            for k, v in el.attrib.items():
                attrs.setdefault(k, v)
            if stops is None and el.find(SVG_NS + "stop") is not None:
                stops = el.findall(SVG_NS + "stop")
            href = el.get(XLINK_HREF, el.get("href"))
            el = self.ids[href[1:]] if href else None
        return attrs, stops or []

    def paint(self, value, m, s, which, subpaths):
        """A flat colour index, or a gradient: (kind, p0, p1, colour0, colour1).

        TinyVG gradients have two colours, so only the first and last stops
        are kept. Their offsets move the gradient's end points.
        """
        alpha = self.alpha(s, which)
        if not value.startswith("url("):
            return self.colour_index(parse_colour(value), alpha), None
        el = self.ids[value[5:-1]]
        attrs, stops = self.gradient_attrs(el)
        if len(stops) < 2:
            raise ValueError(f"gradient {el.get('id')} needs two stops")
        if len(stops) > 2:
            self.warnings.append(f"gradient {el.get('id')}: kept first and last of {len(stops)} stops")
        colours, offsets = [], []
        for stop in (stops[0], stops[-1]):
            css = parse_css(stop.get("style"))
            colour = parse_colour(css.get("stop-color", stop.get("stop-color", "black")))
            a = float(css.get("stop-opacity", stop.get("stop-opacity", 1)))
            colours.append(self.colour_index(colour, a * alpha))
            offsets.append(parse_offset(stop.get("offset", "0")))
        g = IDENTITY
        if attrs.get("gradientUnits") != "userSpaceOnUse":
            x0, y0, x1, y1 = bbox(subpaths)
            g = (x1 - x0, 0.0, 0.0, y1 - y0, x0, y0)
        if attrs.get("gradientTransform"):
            g = mat_mul(g, parse_transform(attrs["gradientTransform"]))
        g = mat_mul(m, g)
        o0, o1 = offsets
        if el.tag == SVG_NS + "linearGradient":
            a = (parse_offset(attrs.get("x1", "0")), parse_offset(attrs.get("y1", "0")))
            b = (parse_offset(attrs.get("x2", "1")), parse_offset(attrs.get("y2", "0")))
            p0 = (a[0] + (b[0] - a[0]) * o0, a[1] + (b[1] - a[1]) * o0)
            p1 = (a[0] + (b[0] - a[0]) * o1, a[1] + (b[1] - a[1]) * o1)
            return None, ("linear", mat_apply(g, p0), mat_apply(g, p1), colours[0], colours[1])
        if "fx" in attrs or "fy" in attrs:
            self.warnings.append(f"gradient {el.get('id')}: focal point ignored")
        if o0 > 0:
            self.warnings.append(f"gradient {el.get('id')}: first stop offset ignored")
        c = (parse_offset(attrs.get("cx", "0.5")), parse_offset(attrs.get("cy", "0.5")))
        r = parse_offset(attrs.get("r", "0.5")) * o1
        return None, ("radial", mat_apply(g, c), mat_apply(g, (c[0] + r, c[1])), colours[0], colours[1])

    def emit(self, m, s, subpaths, pattern_rect=None):
        if not subpaths:
            return
        fill = s["fill"]
        width = parse_length(s["stroke-width"]) * mat_scale(m)
        if self.off_page(transform_subpaths(m, subpaths), width):
            return
        if fill.startswith("url(") and self.ids[fill[5:-1]].tag == SVG_NS + "pattern":
            if self.clip is not None:
                self.warnings.append("ignored clip-path on a pattern fill")
            self.emit_pattern(m, s, fill[5:-1], pattern_rect)
        elif fill != "none":
            colour, grad = self.paint(fill, m, s, "fill", subpaths)
            tsub = transform_subpaths(m, subpaths)
            evenodd = s.get("fill-rule") == "evenodd"
            if self.clip is not None:
                tsub = from_skia(pathops.op(to_skia(tsub, evenodd), self.clip, pathops.PathOp.INTERSECTION))
            elif not evenodd:
                tsub = nonzero_to_evenodd(tsub)
            if tsub:
                box = bbox(tsub)
                prev = self.commands[-1] if self.commands else None
                # Even-odd only changes overlapping shapes, so a same coloured
                # shape that overlaps nothing in the previous fill can join it.
                if (prev and prev["kind"] == "fill" and prev["colour"] == colour and prev.get("grad") == grad
                        and not any(boxes_overlap(box, b) for b in prev["boxes"])):
                    prev["subpaths"].extend(tsub)
                    prev["boxes"].append(box)
                else:
                    self.commands.append({"kind": "fill", "colour": colour, "grad": grad,
                                          "subpaths": tsub, "boxes": [box]})
        stroke = s["stroke"]
        if stroke != "none" and self.clip is not None:
            self.warnings.append("ignored clip-path on a stroke")
        if stroke != "none":
            colour, grad = self.paint(stroke, m, s, "stroke", subpaths)
            tsub = transform_subpaths(m, subpaths)
            prev = self.commands[-1] if self.commands else None
            # Strokes union cleanly, so merge runs with the same style.
            if (prev and prev["kind"] == "line" and prev["colour"] == colour and prev.get("grad") == grad
                    and abs(prev["width"] - width) < 1e-6):
                prev["subpaths"].extend(tsub)
            else:
                self.commands.append({"kind": "line", "colour": colour, "grad": grad,
                                      "width": width, "subpaths": tsub})

    def emit_pattern(self, m, s, pattern_id, rect):
        """Tile a pattern over a rect, keeping only tiles fully inside it."""
        pat = self.ids[pattern_id]
        if pat.get("patternUnits") != "userSpaceOnUse" or rect is None:
            raise ValueError("only userSpaceOnUse patterns on rects are supported")
        pw, ph = float(pat.get("width")), float(pat.get("height"))
        x, y, w, h, rx, ry = rect
        # Collect the tile content once, at the origin.
        tile = []
        for child in pat:
            if child.tag == SVG_NS + "path":
                cs = self.style(child, DEFAULT_STYLE)
                tile.append((cs["fill"], parse_path(child.get("d"))))
        by_colour = {}
        ty = math.floor(y / ph) * ph
        while ty < y + h:
            tx = math.floor(x / pw) * pw
            while tx < x + w:
                for fill, subpaths in tile:
                    moved = transform_subpaths(translate(tx, ty), subpaths)
                    pts = [p for st, ops in moved for p in [st] + [op[-1] for op in ops if op[0] != "Z"]]
                    if all(inside_round_rect(p, x, y, w, h, rx, ry) for p in pts):
                        by_colour.setdefault(fill, []).extend(moved)
                tx += pw
            ty += ph
        # Tiles do not overlap, so one even-odd fill per colour is exact.
        for fill, subpaths in by_colour.items():
            tsub = transform_subpaths(m, subpaths)
            self.commands.append({
                "kind": "fill",
                "colour": self.colour_index(parse_colour(fill)),
                "subpaths": tsub,
                "boxes": [bbox(tsub)],
            })

    def walk(self, el, m, s):
        # Editor data such as sodipodi:namedview is in other namespaces.
        if not el.tag.startswith(SVG_NS):
            return
        tag = el.tag.replace(SVG_NS, "")
        if tag in ("defs", "pattern", "symbol", "title", "desc", "metadata",
                   "linearGradient", "radialGradient", "clipPath"):
            return
        s = self.style(el, s)
        if self.hidden(el, s):
            return
        if el.get("transform"):
            m = mat_mul(m, parse_transform(el.get("transform")))
        saved_clip = self.clip
        if el.get("clip-path"):
            self.clip = self.clip_region(el.get("clip-path"), m)
        self.walk_shape(el, tag, m, s)
        self.clip = saved_clip

    def clip_region(self, ref, m):
        """The clip-path ref as a pathops.Path, intersected with any outer clip."""
        cp = self.ids[ref.strip()[5:-1]]
        if cp.get("clipPathUnits") == "objectBoundingBox":
            self.warnings.append("ignored clip-path with objectBoundingBox units")
            return self.clip
        if cp.get("transform"):
            m = mat_mul(m, parse_transform(cp.get("transform")))
        region = pathops.Path()
        for child in cp:
            tag = child.tag.replace(SVG_NS, "")
            cm = mat_mul(m, parse_transform(child.get("transform"))) if child.get("transform") else m
            evenodd = parse_css(child.get("style")).get("clip-rule", child.get("clip-rule")) == "evenodd"
            sub = self.shape_subpaths(child, tag)
            if sub:
                region = pathops.op(region, to_skia(transform_subpaths(cm, sub), evenodd), pathops.PathOp.UNION)
        if self.clip is not None:
            region = pathops.op(region, self.clip, pathops.PathOp.INTERSECTION)
        return region

    def shape_subpaths(self, el, tag):
        """Subpaths of a path, rect, circle or ellipse, else None."""
        if tag == "path":
            return parse_path(el.get("d", ""))
        if tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width")), float(el.get("height"))
            rx = float(el.get("rx")) if el.get("rx") else None
            ry = float(el.get("ry")) if el.get("ry") else None
            return rect_path(x, y, w, h, rx, ry)
        if tag in ("circle", "ellipse"):
            cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
            rx = float(el.get("r" if tag == "circle" else "rx", 0))
            ry = float(el.get("r" if tag == "circle" else "ry", 0))
            return ellipse_path(cx, cy, rx, ry) if rx > 0 and ry > 0 else []
        return None

    def walk_shape(self, el, tag, m, s):
        if tag in ("svg", "g"):
            for child in el:
                self.walk(child, m, s)
        elif tag == "use":
            target = self.href(el)
            if target.tag == SVG_NS + "symbol" and target.get("viewBox"):
                inner = self.symbol_matrix(el, target, m)
                ts = self.style(target, s)
                for child in target:
                    self.walk(child, inner, ts)
            else:
                x, y = float(el.get("x", 0)), float(el.get("y", 0))
                self.walk(target, mat_mul(m, translate(x, y)), s)
        elif tag in ("path", "circle", "ellipse"):
            self.emit(m, s, self.shape_subpaths(el, tag))
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width")), float(el.get("height"))
            rx = float(el.get("rx")) if el.get("rx") else None
            ry = float(el.get("ry")) if el.get("ry") else None
            sub = rect_path(x, y, w, h, rx, ry)
            rxx = rx if rx is not None else (ry or 0.0)
            ryy = ry if ry is not None else (rx or 0.0)
            self.emit(m, s, sub, (x, y, w, h, rxx, ryy))
        elif tag == "text":
            words = " ".join("".join(el.itertext()).split())
            self.warnings.append(f"skipped text {words[:40]!r} (TinyVG has no text)")
        else:
            self.warnings.append(f"skipped <{tag}>")

    def convert(self):
        width, height, m = page_matrix(self.root)
        self.page = (width, height)
        for child in self.root:
            self.walk(child, m, dict(DEFAULT_STYLE))
        return encode(width, height, self.colours, self.commands)


def page_matrix(root):
    """Image size in whole units, and the matrix from the root viewBox to it."""
    vb = [float(v) for v in root.get("viewBox").split()]
    width, height = round(vb[2]), round(vb[3])
    return width, height, viewbox_matrix(vb, 0, 0, width, height, root.get("preserveAspectRatio"))


# ---------------------------------------------------------------- encoder

def varuint(v):
    out = bytearray()
    while v >= 0x80:
        out.append(0x80 | (v & 0x7F))
        v >>= 7
    out.append(v)
    return bytes(out)


def unit(v):
    i = round(v * (1 << SCALE_BITS))
    if not -32768 <= i <= 32767:
        raise ValueError(f"coordinate {v} out of range")
    return struct.pack("<h", i)


def point(p):
    return unit(p[0]) + unit(p[1])


STYLE_KINDS = {None: 0, "linear": 1, "radial": 2}


def style_kind(cmd):
    grad = cmd.get("grad")
    return STYLE_KINDS[grad[0] if grad else None]


def encode_style(cmd):
    """Style payload: a colour index, or gradient points and two colours."""
    grad = cmd.get("grad")
    if not grad:
        return varuint(cmd["colour"])
    return point(grad[1]) + point(grad[2]) + varuint(grad[3]) + varuint(grad[4])


def quantise(p):
    return (round(p[0] * (1 << SCALE_BITS)), round(p[1] * (1 << SCALE_BITS)))


def encode_path(subpaths):
    out = bytearray()
    for _, ops in subpaths:
        out += varuint(len(ops) - 1)
    for start, ops in subpaths:
        out += point(start)
        cur = quantise(start)
        for op in ops:
            kind = op[0]
            if kind == "L":
                q = quantise(op[1])
                # Horizontal and vertical lines need only one unit.
                if q[1] == cur[1]:
                    out += bytes([1]) + unit(op[1][0])
                elif q[0] == cur[0]:
                    out += bytes([2]) + unit(op[1][1])
                else:
                    out += bytes([0]) + point(op[1])
            elif kind == "C":
                out += bytes([3]) + point(op[1]) + point(op[2]) + point(op[3])
            elif kind == "Q":
                out += bytes([7]) + point(op[1]) + point(op[2])
            elif kind == "Z":
                out += bytes([6])
            cur = quantise(start) if kind == "Z" else quantise(op[-1])
    return bytes(out)


def encode(width, height, colours, commands):
    out = bytearray(b"\x72\x56\x01")
    # scale u4 | colour_encoding u2 (0 = RGBA8888) | coordinate_range u2 (0 = 16 bit)
    out.append(SCALE_BITS | (0 << 4) | (0 << 6))
    out += struct.pack("<HH", width, height)
    out += varuint(len(colours))
    for rgba in colours:
        out += bytes(rgba)
    for cmd in commands:
        subpaths = cmd["subpaths"]
        if cmd["kind"] == "fill":
            out.append(3 | style_kind(cmd) << 6)  # fill path
            out += varuint(len(subpaths) - 1)
            out += encode_style(cmd)
        else:
            out.append(7 | style_kind(cmd) << 6)  # draw line path
            out += varuint(len(subpaths) - 1)
            out += encode_style(cmd)
            out += unit(cmd["width"])
        out += encode_path(subpaths)
    out.append(0)  # end of document
    return bytes(out)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    root = ET.parse(sys.argv[1]).getroot()
    conv = Converter(root)
    data = conv.convert()
    with open(sys.argv[2], "wb") as f:
        f.write(data)
    for w in dict.fromkeys(conv.warnings):
        print(f"{sys.argv[1]}: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
