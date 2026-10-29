#!/usr/bin/env python3
"""Convert an SVG to binary TinyVG.

Covers this SVG subset: svg, g, use, symbol,
path, rect, circle, ellipse, userSpaceOnUse patterns and two stop linear and
radial gradients. TinyVG fills are even-odd, so non-zero fills whose subpaths
overlap are split into fills that cover the same area. TinyVG has no transforms, so every point is
transformed here. Arcs and rounded rects become cubic beziers so they
survive rotation and non-uniform scale.

Usage: svg2tvg.py in.svg out.tvg
"""

import math
import re
import struct
import sys
import xml.etree.ElementTree as ET

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# 16 bit units with 3 fraction bits: 1/8 unit precision, range -4096..4095.
# Fewer fraction bits make the low bytes repeat more, so files gzip better.
SCALE_BITS = 3

NAMED_COLOURS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "blue": (0, 0, 255),
    "green": (0, 128, 0),
    "navy": (0, 0, 128),
}

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


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


def rotate(deg):
    r = math.radians(deg)
    cs, sn = math.cos(r), math.sin(r)
    return (cs, sn, -sn, cs, 0.0, 0.0)


def parse_transform(text):
    m = IDENTITY
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        v = [float(x) for x in re.split(r"[\s,]+", args.strip()) if x]
        if name == "translate":
            t = translate(v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale":
            t = scale(v[0], v[1] if len(v) > 1 else v[0])
        elif name == "rotate":
            t = rotate(v[0])
            if len(v) == 3:
                t = mat_mul(translate(v[1], v[2]), mat_mul(t, translate(-v[1], -v[2])))
        elif name == "matrix":
            t = tuple(v)
        else:
            raise ValueError(f"unsupported transform {name}")
        m = mat_mul(m, t)
    return m


def viewbox_matrix(viewbox, x, y, w, h, par):
    """Map a viewBox into the viewport (x, y, w, h) per preserveAspectRatio."""
    vx, vy, vw, vh = viewbox
    sx, sy = w / vw, h / vh
    align = (par or "xMidYMid meet").split()[0]
    if align != "none":
        slice_ = "slice" in (par or "")
        s = max(sx, sy) if slice_ else min(sx, sy)
        sx = sy = s
    tx, ty = x - vx * sx, y - vy * sy
    if align != "none":
        free_x, free_y = w - vw * sx, h - vh * sy
        if "xMid" in align:
            tx += free_x / 2
        elif "xMax" in align:
            tx += free_x
        if "YMid" in align:
            ty += free_y / 2
        elif "YMax" in align:
            ty += free_y
    return (sx, 0.0, 0.0, sy, tx, ty)


# ------------------------------------------------------------- path parser

NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class PathScanner:
    def __init__(self, d):
        self.d = d
        self.i = 0

    def skip(self):
        while self.i < len(self.d) and self.d[self.i] in " \t\r\n,":
            self.i += 1

    def at_number(self):
        self.skip()
        return self.i < len(self.d) and (self.d[self.i] in "+-." or self.d[self.i].isdigit())

    def command(self):
        self.skip()
        if self.i >= len(self.d):
            return None
        c = self.d[self.i]
        if not c.isalpha():
            raise ValueError(f"expected path command at {self.i}: {self.d[self.i:self.i+20]!r}")
        self.i += 1
        return c

    def number(self):
        self.skip()
        m = NUMBER_RE.match(self.d, self.i)
        if not m:
            raise ValueError(f"expected number at {self.i}: {self.d[self.i:self.i+20]!r}")
        self.i = m.end()
        return float(m.group())

    def flag(self):
        # Arc flags may be packed with no separator, e.g. "a1 1 0 0110 10".
        self.skip()
        c = self.d[self.i]
        self.i += 1
        return c == "1"


def arc_to_cubics(p0, rx, ry, phi_deg, large, sweep, p1):
    """SVG endpoint arc to a list of (c1, c2, end) cubics."""
    if p0 == p1:
        return []
    if rx == 0 or ry == 0:
        return [(p0, p1, p1)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1 = cp * dx + sp * dy
    y1 = -sp * dx + cp * dy
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cx1 = coef * rx * y1 / ry
    cy1 = -coef * ry * x1 / rx
    cx = cp * cx1 - sp * cy1 + (p0[0] + p1[0]) / 2
    cy = sp * cx1 + cp * cy1 + (p0[1] + p1[1]) / 2

    def angle(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = angle(1, 0, (x1 - cx1) / rx, (y1 - cy1) / ry)
    dt = angle((x1 - cx1) / rx, (y1 - cy1) / ry, (-x1 - cx1) / rx, (-y1 - cy1) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi

    n = max(1, math.ceil(abs(dt) / (math.pi / 2) - 1e-9))
    step = dt / n
    k = 4 / 3 * math.tan(step / 4)

    def point(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return (cp * x - sp * y + cx, sp * x + cp * y + cy)

    def deriv(t):
        x, y = -rx * math.sin(t), ry * math.cos(t)
        return (cp * x - sp * y, sp * x + cp * y)

    out = []
    t = t1
    for i in range(n):
        ta, tb = t, t + step
        pa, pb = point(ta), point(tb)
        da, db = deriv(ta), deriv(tb)
        c1 = (pa[0] + k * da[0], pa[1] + k * da[1])
        c2 = (pb[0] - k * db[0], pb[1] - k * db[1])
        end = p1 if i == n - 1 else pb
        out.append((c1, c2, end))
        t = tb
    return out


def parse_path(d):
    """Parse path data into subpaths of absolute ops, in local coordinates.

    Each subpath is (start, ops). Ops are ("L", p), ("Q", c, p),
    ("C", c1, c2, p) and ("Z",).
    """
    sc = PathScanner(d)
    subpaths = []
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    ops = None
    last_cmd = None
    last_ctrl = None
    cmd = None

    def new_subpath(p):
        nonlocal ops, start
        ops = []
        start = p
        subpaths.append((p, ops))

    while True:
        if sc.at_number() and cmd is not None:
            pass  # implicit repeat of the previous command
        else:
            cmd = sc.command()
            if cmd is None:
                break
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = cur if rel else (0.0, 0.0)

        def pt():
            x = sc.number()
            y = sc.number()
            return (x + ox, y + oy)

        if c == "M":
            p = pt()
            new_subpath(p)
            cur = p
            # Further pairs after a moveto are implicit linetos.
            cmd = "l" if rel else "L"
            last_ctrl = None
            last_cmd = "M"
            continue
        if ops is None:
            new_subpath(cur)
        if c == "Z":
            ops.append(("Z",))
            cur = start
            # A command after Z without M starts a new subpath at the start.
            ops = None
            cmd = None
            last_ctrl = None
            last_cmd = "Z"
            continue
        if c == "L":
            p = pt()
            ops.append(("L", p))
            last_ctrl = None
        elif c == "H":
            x = sc.number() + ox
            p = (x, cur[1])
            ops.append(("L", p))
            last_ctrl = None
        elif c == "V":
            y = sc.number() + (oy if rel else 0.0)
            p = (cur[0], y)
            ops.append(("L", p))
            last_ctrl = None
        elif c == "C":
            c1, c2, p = pt(), pt(), pt()
            ops.append(("C", c1, c2, p))
            last_ctrl = c2
        elif c == "S":
            if last_cmd in ("C", "S") and last_ctrl:
                c1 = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1])
            else:
                c1 = cur
            c2, p = pt(), pt()
            ops.append(("C", c1, c2, p))
            last_ctrl = c2
        elif c == "Q":
            q, p = pt(), pt()
            ops.append(("Q", q, p))
            last_ctrl = q
        elif c == "T":
            if last_cmd in ("Q", "T") and last_ctrl:
                q = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1])
            else:
                q = cur
            p = pt()
            ops.append(("Q", q, p))
            last_ctrl = q
        elif c == "A":
            rx, ry, phi = sc.number(), sc.number(), sc.number()
            large, sweep = sc.flag(), sc.flag()
            p = pt()
            for c1, c2, e in arc_to_cubics(cur, rx, ry, phi, large, sweep, p):
                ops.append(("C", c1, c2, e))
            last_ctrl = None
        else:
            raise ValueError(f"unsupported path command {cmd}")
        cur = p
        last_cmd = c
    return [(s, o) for s, o in subpaths if o]


def rect_path(x, y, w, h, rx, ry):
    if rx is None and ry is None:
        rx = ry = 0.0
    rx = ry if rx is None else rx
    ry = rx if ry is None else ry
    rx, ry = min(rx, w / 2), min(ry, h / 2)
    if rx == 0 or ry == 0:
        d = f"M{x} {y}H{x + w}V{y + h}H{x}Z"
    else:
        d = (
            f"M{x + rx} {y}H{x + w - rx}A{rx} {ry} 0 0 1 {x + w} {y + ry}"
            f"V{y + h - ry}A{rx} {ry} 0 0 1 {x + w - rx} {y + h}"
            f"H{x + rx}A{rx} {ry} 0 0 1 {x} {y + h - ry}"
            f"V{y + ry}A{rx} {ry} 0 0 1 {x + rx} {y}Z"
        )
    return parse_path(d)


def ellipse_path(cx, cy, rx, ry):
    return parse_path(
        f"M{cx + rx} {cy}A{rx} {ry} 0 0 1 {cx} {cy + ry}A{rx} {ry} 0 0 1 {cx - rx} {cy}"
        f"A{rx} {ry} 0 0 1 {cx} {cy - ry}A{rx} {ry} 0 0 1 {cx + rx} {cy}Z"
    )


def flatten(start, ops, steps=8):
    """The subpath as a polygon, with curves cut into straight steps."""
    pts = [start]
    cur = start
    for op in ops:
        if op[0] == "L":
            pts.append(op[1])
        elif op[0] in ("Q", "C"):
            ctrl = [cur] + list(op[1:])
            for k in range(1, steps + 1):
                t = k / steps
                q = ctrl
                while len(q) > 1:
                    q = [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) for a, b in zip(q, q[1:])]
                pts.append(q[0])
        cur = start if op[0] == "Z" else op[-1]
    return pts


def signed_area(poly):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1])) / 2


def winding(poly, p):
    w = 0
    x, y = p
    for a, b in zip(poly, poly[1:] + poly[:1]):
        if a[1] <= y < b[1] or b[1] <= y < a[1]:
            cross = (b[0] - a[0]) * (y - a[1]) - (x - a[0]) * (b[1] - a[1])
            if (cross > 0) == (b[1] > a[1]):
                w += 1 if b[1] > a[1] else -1
    return w


def poly_bbox(poly):
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def nonzero_groups(subpaths):
    """Split a non-zero fill into even-odd fills whose union matches it.

    Even-odd and non-zero agree unless two subpaths wound the same way
    overlap. Then each subpath wound the main way gets its own fill, and
    each hole goes with the smallest such subpath around it.
    """
    if len(subpaths) < 2:
        return [subpaths]
    polys = [flatten(st, ops) for st, ops in subpaths]
    areas = [signed_area(p) for p in polys]
    boxes = [poly_bbox(p) for p in polys]

    def inside(i, pts):
        return any(winding(polys[i], p) != 0 for p in pts)

    clash = False
    for i in range(len(polys)):
        for j in range(len(polys)):
            if (i != j and (areas[i] > 0) == (areas[j] > 0) and boxes_overlap(boxes[i], boxes[j])
                    and inside(i, polys[j][::max(1, len(polys[j]) // 16)])):
                clash = True
                break
        if clash:
            break
    if not clash:
        return [subpaths]
    main = areas[max(range(len(areas)), key=lambda k: abs(areas[k]))] > 0
    groups = {i: [i] for i in range(len(polys)) if (areas[i] > 0) == main}
    for i in range(len(polys)):
        if i in groups:
            continue
        around = [k for k in groups if inside(k, polys[i][:1])]
        if around:
            groups[min(around, key=lambda k: abs(areas[k]))].append(i)
        else:
            groups[i] = [i]
    return [[subpaths[k] for k in sorted(g)] for _, g in sorted(groups.items())]


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
    m = re.match(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(px|pt|mm|cm|in)?\s*$", text)
    if not m:
        raise ValueError(f"unsupported length {text!r}")
    return float(m.group(1)) * {None: 1, "px": 1, "pt": 4 / 3, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96}[m.group(2)]


def parse_css(text):
    """Declarations from a style attribute, as a dict."""
    out = {}
    for decl in (text or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def parse_colour(text):
    text = text.strip()
    if text in NAMED_COLOURS:
        return NAMED_COLOURS[text]
    if text.startswith("#"):
        h = text[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    raise ValueError(f"unsupported colour {text}")


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
    def __init__(self, root):
        self.root = root
        self.ids = {el.get("id"): el for el in root.iter() if el.get("id")}
        self.colours = []
        self.commands = []  # dicts: kind ("fill"|"line"), colour, width, subpaths
        self.warnings = []

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
        width = parse_length(s["stroke-width"]) * math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
        if self.off_page(transform_subpaths(m, subpaths), width):
            return
        if fill.startswith("url(") and self.ids[fill[5:-1]].tag == SVG_NS + "pattern":
            self.emit_pattern(m, s, fill[5:-1], pattern_rect)
        elif fill != "none":
            colour, grad = self.paint(fill, m, s, "fill", subpaths)
            tsub = transform_subpaths(m, subpaths)
            groups = [tsub] if s.get("fill-rule") == "evenodd" else nonzero_groups(tsub)
            for group in groups:
                box = bbox(group)
                prev = self.commands[-1] if self.commands else None
                # Even-odd only changes overlapping shapes, so a same coloured
                # shape that overlaps nothing in the previous fill can join it.
                if (prev and prev["kind"] == "fill" and prev["colour"] == colour and prev.get("grad") == grad
                        and not any(boxes_overlap(box, b) for b in prev["boxes"])):
                    prev["subpaths"].extend(group)
                    prev["boxes"].append(box)
                else:
                    self.commands.append({"kind": "fill", "colour": colour, "grad": grad,
                                          "subpaths": group, "boxes": [box]})
        stroke = s["stroke"]
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
                cs = self.style(child, {"fill": "black", "stroke": "none", "stroke-width": "1"})
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
        if el.get("clip-path"):
            self.warnings.append("ignored clip-path (TinyVG has no clipping)")
        if tag in ("svg", "g"):
            for child in el:
                self.walk(child, m, s)
        elif tag == "use":
            target = self.ids[el.get(XLINK_HREF, el.get("href", ""))[1:]]
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            ttag = target.tag.replace(SVG_NS, "")
            if ttag == "symbol" and target.get("viewBox"):
                vb = [float(v) for v in target.get("viewBox").split()]
                w = float(el.get("width", vb[2]))
                h = float(el.get("height", vb[3]))
                inner = mat_mul(m, viewbox_matrix(vb, x, y, w, h, target.get("preserveAspectRatio")))
                ts = self.style(target, s)
                for child in target:
                    self.walk(child, inner, ts)
            else:
                self.walk(target, mat_mul(m, translate(x, y)), s)
        elif tag == "path":
            self.emit(m, s, parse_path(el.get("d", "")))
        elif tag == "rect":
            x, y = float(el.get("x", 0)), float(el.get("y", 0))
            w, h = float(el.get("width")), float(el.get("height"))
            rx = float(el.get("rx")) if el.get("rx") else None
            ry = float(el.get("ry")) if el.get("ry") else None
            sub = rect_path(x, y, w, h, rx, ry)
            rxx = rx if rx is not None else (ry or 0.0)
            ryy = ry if ry is not None else (rx or 0.0)
            self.emit(m, s, sub, (x, y, w, h, rxx, ryy))
        elif tag in ("circle", "ellipse"):
            cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
            rx = float(el.get("r" if tag == "circle" else "rx", 0))
            ry = float(el.get("r" if tag == "circle" else "ry", 0))
            if rx > 0 and ry > 0:
                self.emit(m, s, ellipse_path(cx, cy, rx, ry))
        elif tag == "text":
            words = " ".join("".join(el.itertext()).split())
            self.warnings.append(f"skipped text {words[:40]!r} (TinyVG has no text)")
        else:
            self.warnings.append(f"skipped <{tag}>")

    def convert(self):
        vb = [float(v) for v in self.root.get("viewBox").split()]
        width, height = round(vb[2]), round(vb[3])
        self.page = (width, height)
        m = viewbox_matrix(vb, 0, 0, width, height, self.root.get("preserveAspectRatio"))
        style = {"fill": "black", "stroke": "none", "stroke-width": "1"}
        for child in self.root:
            self.walk(child, m, style)
        return encode(width, height, self.colours, self.commands)


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
