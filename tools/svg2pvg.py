#!/usr/bin/env python3
"""Bundle many SVGs into one PicoVG file, sharing repeated shapes.

PicoVG is TinyVG plus a symbol table, a "use symbol" command and many
named images per file. See docs/picovg.md.

Shapes become symbols when they repeat anywhere in the bundle:
- every <symbol>, keyed on its content with ids removed, so the same
  <symbol> defined in two files is one symbol
- any path or rect that appears in two or more files
- single colour symbols drop their colour, so a red and a black glyph
  with the same shape share one symbol

Usage: svg2pvg.py [--tolerance units] [--grid n] [--reorder 0|1] out.pvg in.svg...

--tolerance  how far, in SVG units, simplified paths may move (default 0)
--grid       path coordinates step 2**grid / 8 units (default 1, so 1/4)
--reorder    0 to keep subpath order, 1 to start each near the last (default 1)
"""

import copy
import math
import os
import struct
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import svg2tvg  # noqa: E402
from simplify import simplify  # noqa: E402
from svg2tvg import (  # noqa: E402
    DEFAULT_STYLE, IDENTITY, SVG_NS, Converter, encode_style, mat_apply, mat_mul, mat_scale,
    page_matrix, parse_transform, point, scale, style_kind, transform_subpaths, unit, varuint,
)

CMD_USE = 11
CMD_COMPACT_FILL = 12
CMD_COMPACT_LINE = 13
STYLE_CURRENT = 3
KIND_CODES = {"L": 0, "Q": 1, "C": 2, "Z": 3}

USE_TRANSLATE = 0x01
USE_SCALE = 0x02
USE_SCALE_Y = 0x04
USE_ROTATE = 0x08
USE_MATRIX = 0x10
USE_COLOUR = 0x20
USE_HALF_TURN = 0x40

# Max distance, in SVG units, that simplified paths may move.
TOLERANCE = 0.0

# Compact path coordinates step in units of 2**PATH_GRID / 2**SCALE_BITS.
PATH_GRID = 1

# Reorder subpaths so each starts near the last one's end.
REORDER = True


class Symbol:
    def __init__(self, commands, tint):
        self.commands = commands  # in the shape's own space
        self.tint = tint          # True if drawn in the current colour
        self.uses = 0
        self.max_scale = 0.0
        self.index = None


def canonical(el):
    """Element XML with ids removed, so copies from different files match."""
    el = copy.deepcopy(el)
    for e in el.iter():
        e.attrib.pop("id", None)
    return ET.tostring(el)


def geometry_key(commands, tint):
    parts = []
    for c in commands:
        colour = None if tint else c["colour"]
        parts.append((c["kind"], colour, c.get("grad"), round(c.get("width", 0), 6),
                      repr([(s, ops) for s, ops in c["subpaths"]])))
    return repr(parts)


class Bundle(Converter):
    def __init__(self):
        super().__init__()
        self.symbols = []
        self.by_source = {}    # (xml, style) -> (symbol, colour)
        self.by_geometry = {}  # geometry key -> symbol
        self.element_counts = {}
        self.counting = False
        self.in_symbol = 0
        self.images = []

    # -- walking

    def element_key(self, el, s):
        return (canonical(el), tuple(sorted(s.items())))

    def walk(self, el, m, s):
        # Symbols are stored once, so clipped content is drawn inline.
        if self.in_symbol or self.clip is not None or el.get("clip-path"):
            if not self.counting:
                super().walk(el, m, s)
            return
        if not el.tag.startswith(SVG_NS) or self.hidden(el, self.style(el, s)):
            return
        tag = el.tag.replace(SVG_NS, "")
        if tag == "use":
            target = self.href(el)
            if target.tag == SVG_NS + "symbol" and target.get("viewBox"):
                if self.counting:
                    return
                s = self.style(el, s)
                if el.get("transform"):
                    m = mat_mul(m, parse_transform(el.get("transform")))
                self.add_use(target, list(target), self.style(target, s), self.symbol_matrix(el, target, m))
                return
        if tag in ("path", "rect"):
            key = self.element_key(el, self.style(el, s))
            if self.counting:
                self.element_counts.setdefault(key, set()).add(self.file_id)
                return
            if len(self.element_counts.get(key, ())) >= 2:
                # Transforms on the element itself go into the shape.
                self.add_use(el, [el], s, m)
                return
        if self.counting:
            if tag in ("svg", "g"):
                s = self.style(el, s)
                for child in el:
                    self.walk(child, m, s)
            return
        super().walk(el, m, s)

    def add_use(self, source, children, s, m):
        key = self.element_key(source, s)
        if key not in self.by_source:
            saved = self.commands
            self.commands = []
            self.in_symbol += 1
            for child in children:
                Converter.walk(self, child, IDENTITY, s)
            self.in_symbol -= 1
            commands, self.commands = self.commands, saved
            colours = {c["colour"] for c in commands}
            # Gradients keep their own colours, so cannot take a tint.
            tint = len(colours) == 1 and not any(c.get("grad") for c in commands)
            gkey = geometry_key(commands, tint)
            sym = self.by_geometry.get(gkey)
            if sym is None:
                sym = Symbol(commands, tint)
                self.by_geometry[gkey] = sym
                self.symbols.append(sym)
            self.by_source[key] = (sym, colours.pop() if tint else None)
        sym, colour = self.by_source[key]
        if not sym.commands:
            return
        if all(self.off_page(transform_subpaths(m, c["subpaths"]), c.get("width", 0) * mat_scale(m))
               for c in sym.commands):
            return
        sym.uses += 1
        sym.max_scale = max(sym.max_scale, mat_scale(m))
        self.commands.append({"kind": "use", "symbol": sym, "m": m, "colour": colour})

    def load(self, path):
        root = ET.parse(path).getroot()
        self.set_root(root)
        self.file_id = path
        return root

    def count(self, path):
        root = self.load(path)
        self.counting = True
        _, _, m = page_matrix(root)
        for child in root:
            self.walk(child, m, dict(DEFAULT_STYLE))
        self.counting = False

    def add_image(self, path):
        root = self.load(path)
        width, height, m = page_matrix(root)
        self.page = (width, height)
        self.commands = []
        for child in root:
            self.walk(child, m, dict(DEFAULT_STYLE))
        name = os.path.splitext(os.path.basename(path))[0]
        self.images.append((name, width, height, self.commands))

    # -- encoding

    def encode(self):
        # Symbols used once are cheaper inline.
        shared = [s for s in self.symbols if s.uses >= 2]
        for i, sym in enumerate(shared):
            sym.index = i
            # Store each shape at the size of its largest use, so 1/8 unit
            # precision is spent where it shows.
            k = sym.max_scale or 1.0
            sym.norm = k
            sym.stored = [scaled_command(c, k) for c in sym.commands]

        out = bytearray(b"PV\x01")
        out.append(svg2tvg.SCALE_BITS)  # RGBA8888, 16 bit units
        out.append(PATH_GRID)
        out += varuint(len(self.colours))
        for rgba in self.colours:
            out += bytes(rgba)
        out += varuint(len(shared))
        for sym in shared:
            body = b"".join(encode_command(c, sym.tint) for c in sym.stored) + b"\x00"
            out += varuint(len(body)) + body
        out += varuint(len(self.images))
        for name, width, height, commands in self.images:
            body = bytearray()
            for c in commands:
                if c["kind"] != "use":
                    body += encode_command(c, False)
                elif c["symbol"].index is None:
                    for sc in c["symbol"].commands:
                        body += encode_command(transformed_command(sc, c["m"], c["colour"]), False)
                else:
                    body += encode_use(c)
            body += b"\x00"
            raw = name.encode()
            out += varuint(len(raw)) + raw + struct.pack("<HH", width, height)
            out += varuint(len(body)) + body
        return bytes(out)


def transformed_grad(m, grad):
    if not grad:
        return grad
    kind, p0, p1, c0, c1 = grad
    return (kind, mat_apply(m, p0), mat_apply(m, p1), c0, c1)


def scaled_command(c, k):
    c = dict(c, subpaths=transform_subpaths(scale(k, k), c["subpaths"]),
             grad=transformed_grad(scale(k, k), c.get("grad")))
    if "width" in c:
        c["width"] *= k
    return c


def transformed_command(c, m, colour):
    c = dict(c, subpaths=transform_subpaths(m, c["subpaths"]), grad=transformed_grad(m, c.get("grad")))
    if "width" in c:
        c["width"] *= mat_scale(m)
    if colour is not None:
        c["colour"] = colour
    return c


def encode_command(c, tint):
    is_fill = c["kind"] == "fill"
    subpaths = simplify(c["subpaths"], TOLERANCE, not is_fill)
    if REORDER:
        subpaths = nearest_first(subpaths)
    kind = STYLE_CURRENT if tint else style_kind(c)
    style = b"" if tint else encode_style(c)
    if is_fill:
        out = bytes([CMD_COMPACT_FILL | kind << 6]) + varuint(len(subpaths) - 1) + style
    else:
        out = bytes([CMD_COMPACT_LINE | kind << 6]) + varuint(len(subpaths) - 1) + style + unit(c["width"])
    return out + encode_compact_path(subpaths, is_fill)


def varint(v):
    """Zigzag signed VarInt: 0, -1, 1, -2... map to 0, 1, 2, 3..."""
    return varuint((v << 1) ^ (v >> 63))


def subpath_end(start, ops):
    last = ops[-1] if ops else ("Z",)
    return start if last[0] == "Z" else last[-1]


def nearest_first(subpaths):
    """Greedy order: each subpath next is the one starting nearest the pen.

    Fills (even-odd) and strokes (union) do not depend on subpath order.
    """
    left = list(subpaths)
    out = []
    pen = (0.0, 0.0)
    while left:
        i = min(range(len(left)), key=lambda k: abs(left[k][0][0] - pen[0]) + abs(left[k][0][1] - pen[1]))
        sp = left.pop(i)
        out.append(sp)
        pen = subpath_end(*sp)
    return out


def encode_compact_path(subpaths, is_fill):
    step = (1 << svg2tvg.SCALE_BITS) / (1 << PATH_GRID)

    def grid(p):
        return (round(p[0] * step), round(p[1] * step))

    def delta(a, b):
        return varint(b[0] - a[0]) + varint(b[1] - a[1])

    out = bytearray()
    pen = (0, 0)
    for start, ops in subpaths:
        # Fill subpaths close themselves, so a trailing close is redundant.
        if is_fill and ops and ops[-1][0] == "Z":
            ops = ops[:-1]
        kinds = [KIND_CODES[op[0]] for op in ops]
        if 1 <= len(kinds) <= 3:
            # Short form: count in the low 2 bits, kinds above it.
            out.append(len(kinds) | sum(k << (2 + 2 * j) for j, k in enumerate(kinds)))
        else:
            out.append(0)
            out += varuint(len(kinds))
            for i in range(0, len(kinds), 4):
                out.append(sum(k << (2 * j) for j, k in enumerate(kinds[i:i + 4])))
        s = grid(start)
        out += delta(pen, s)
        cur = s
        for op in ops:
            pts = [grid(p) for p in op[1:]]
            for p in pts:
                out += delta(cur, p)
                cur = p
            if op[0] == "Z":
                cur = s
        pen = cur
    return bytes(out)


def encode_use(c):
    sym = c["symbol"]
    # The stored shape is pre-scaled by norm, so undo that first.
    m = mat_mul(c["m"], scale(1 / sym.norm, 1 / sym.norm))
    a, b, cc, d, e, f = m
    sx = math.sqrt(a * a + b * b)
    theta = math.atan2(b, a)
    # Rotate back by theta and see what is left of the second column.
    shear = math.cos(theta) * cc + math.sin(theta) * d
    sy = -math.sin(theta) * cc + math.cos(theta) * d
    eps = 1e-5
    flags = 0
    fields = b""
    if abs(e) > eps or abs(f) > eps:
        flags |= USE_TRANSLATE
        fields += point((e, f))
    if abs(shear) > eps * max(1.0, sx):
        flags |= USE_MATRIX
        fields += struct.pack("<4f", a, b, cc, d)
    else:
        if abs(sx - 1) > eps or abs(sy - 1) > eps:
            flags |= USE_SCALE
            fields += struct.pack("<f", sx)
            if abs(sx - sy) > eps:
                flags |= USE_SCALE_Y
                fields += struct.pack("<f", sy)
        deg = math.degrees(theta)
        if abs(abs(deg) - 180) < 1e-4:
            flags |= USE_HALF_TURN
        elif abs(deg) > 1e-4:
            flags |= USE_ROTATE
            fields += struct.pack("<f", deg)
    if sym.tint:
        flags |= USE_COLOUR
        fields += varuint(c["colour"])
    return bytes([CMD_USE]) + varuint(sym.index) + bytes([flags]) + fields


def main():
    global TOLERANCE, PATH_GRID, REORDER
    args = sys.argv[1:]
    while args and args[0].startswith("--"):
        flag, value = args[0], args[1]
        args = args[2:]
        if flag == "--tolerance":
            TOLERANCE = float(value)
        elif flag == "--grid":
            PATH_GRID = int(value)
        elif flag == "--reorder":
            REORDER = value == "1"
        else:
            sys.exit(f"unknown flag {flag}")
    if len(args) < 2:
        sys.exit(__doc__)
    bundle = Bundle()
    out_path, paths = args[0], args[1:]
    for p in paths:
        bundle.count(p)
    for p in paths:
        bundle.add_image(p)
    data = bundle.encode()
    with open(out_path, "wb") as f:
        f.write(data)
    for w in dict.fromkeys(bundle.warnings):
        print(w, file=sys.stderr)
    shared = sum(1 for s in bundle.symbols if s.uses >= 2)
    print(f"{len(bundle.images)} images, {shared} shared symbols, {len(data)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
