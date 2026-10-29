"""Lossy path simplification, bounded by a distance tolerance.

Works on subpaths in final coordinates: (start, ops) with ops ("L", p),
("Q", c, p), ("C", c1, c2, p) and ("Z",).

- Runs of smoothly joined segments are refitted as one cubic
  (Schneider's method), never across a corner.
- Straight runs of lines become one line.
- A cubic that is nearly flat becomes a line, one that is nearly a
  quadratic becomes a quadratic.
- For strokes, a subpath that starts where the previous one ended is
  joined onto it.

Every change keeps the new curve within the tolerance of the old one.
"""

import math

CORNER_COS = math.cos(math.radians(12))  # joins sharper than this stay
MAX_RUN = 24
SAMPLES = 8


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def mul(a, k):
    return (a[0] * k, a[1] * k)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def length(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1])


def unit_vec(a):
    n = length(a)
    return (a[0] / n, a[1] / n) if n > 1e-12 else None


def bezier(p, t):
    u = 1 - t
    return (
        u * u * u * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t * t * t * p[3][0],
        u * u * u * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t * t * t * p[3][1],
    )


def bezier_d1(p, t):
    u = 1 - t
    return add(add(mul(sub(p[1], p[0]), 3 * u * u), mul(sub(p[2], p[1]), 6 * u * t)), mul(sub(p[3], p[2]), 3 * t * t))


def bezier_d2(p, t):
    return add(mul(add(sub(p[2], mul(p[1], 2)), p[0]), 6 * (1 - t)), mul(add(sub(p[3], mul(p[2], 2)), p[1]), 6 * t))


def as_cubic(cur, op):
    if op[0] == "C":
        return (cur, op[1], op[2], op[3])
    if op[0] == "Q":
        c = op[1]
        return (cur, add(cur, mul(sub(c, cur), 2 / 3)), add(op[2], mul(sub(c, op[2]), 2 / 3)), op[2])
    p = op[1]
    return (cur, add(cur, mul(sub(p, cur), 1 / 3)), add(cur, mul(sub(p, cur), 2 / 3)), p)


def start_tangent(c):
    for q in (c[1], c[2], c[3]):
        t = unit_vec(sub(q, c[0]))
        if t:
            return t
    return None


def end_tangent(c):
    for q in (c[2], c[1], c[0]):
        t = unit_vec(sub(q, c[3]))
        if t:
            return t
    return None


def point_line_dist(p, a, b):
    ab = sub(b, a)
    n = length(ab)
    if n < 1e-12:
        return length(sub(p, a))
    t = max(0.0, min(1.0, dot(sub(p, a), ab) / (n * n)))
    return length(sub(p, add(a, mul(ab, t))))


# ---------------------------------------------------------------- fitting

def fit_cubic(pts, t0, t1):
    """Fit one cubic to pts with fixed end tangents. Returns (curve, error)."""
    d = [0.0]
    for i in range(1, len(pts)):
        d.append(d[-1] + length(sub(pts[i], pts[i - 1])))
    if d[-1] < 1e-9:
        return None, math.inf
    u = [x / d[-1] for x in d]
    curve = None
    for _ in range(4):
        curve = generate(pts, u, t0, t1)
        u = [newton(curve, p, ui) for p, ui in zip(pts, u)]
    err = max(length(sub(bezier(curve, ui), p)) for p, ui in zip(pts, u))
    return curve, err


def generate(pts, u, t0, t1):
    p0, p3 = pts[0], pts[-1]
    c00 = c01 = c11 = x0 = x1 = 0.0
    for p, t in zip(pts, u):
        u = 1 - t
        a0 = mul(t0, 3 * u * u * t)
        a1 = mul(t1, 3 * u * t * t)
        c00 += dot(a0, a0)
        c01 += dot(a0, a1)
        c11 += dot(a1, a1)
        b = u * u * u + 3 * u * u * t
        e = 3 * u * t * t + t * t * t
        tmp = sub(p, add(mul(p0, b), mul(p3, e)))
        x0 += dot(a0, tmp)
        x1 += dot(a1, tmp)
    det = c00 * c11 - c01 * c01
    seg = length(sub(p3, p0))
    if abs(det) > 1e-12:
        alpha0 = (x0 * c11 - c01 * x1) / det
        alpha1 = (c00 * x1 - c01 * x0) / det
    else:
        alpha0 = alpha1 = seg / 3
    if alpha0 < 1e-6 * seg or alpha1 < 1e-6 * seg:
        alpha0 = alpha1 = seg / 3
    return (p0, add(p0, mul(t0, alpha0)), add(p3, mul(t1, alpha1)), p3)


def newton(c, p, t):
    d = sub(bezier(c, t), p)
    d1 = bezier_d1(c, t)
    d2 = bezier_d2(c, t)
    num = dot(d, d1)
    den = dot(d1, d1) + dot(d, d2)
    if abs(den) < 1e-12:
        return t
    return max(0.0, min(1.0, t - num / den))


def sample_run(cubics):
    pts = [cubics[0][0]]
    for c in cubics:
        for i in range(1, SAMPLES + 1):
            pts.append(bezier(c, i / SAMPLES))
    return pts


# ------------------------------------------------------------- reducing

def reduce_single(c, tol):
    """A cubic as a line or quadratic, if that is within tol."""
    p0, c1, c2, p3 = c
    if point_line_dist(c1, p0, p3) <= tol and point_line_dist(c2, p0, p3) <= tol:
        return ("L", p3)
    # Distance from the best quadratic is at most sqrt(3)/36 * |p3 - 3 c2 + 3 c1 - p0|.
    k = sub(add(sub(p3, mul(c2, 3)), mul(c1, 3)), p0)
    if math.sqrt(3) / 36 * length(k) <= tol:
        q = mul(sub(add(mul(c1, 3), mul(c2, 3)), add(p0, p3)), 0.25)
        return ("Q", q, p3)
    return ("C", c1, c2, p3)


def smooth_join(a, b):
    ta = end_tangent(a)
    tb = start_tangent(b)
    if not ta or not tb:
        return False
    # end_tangent points backwards, so smooth means they point opposite ways.
    return -dot(ta, tb) >= CORNER_COS


def simplify_ops(start, ops, tol):
    out = []
    cur = start
    i = 0
    while i < len(ops):
        op = ops[i]
        if op[0] == "Z":
            out.append(op)
            cur = start
            i += 1
            continue
        first = as_cubic(cur, op)
        if op[0] == "L":
            # Straight runs of lines: keep the furthest end that stays in tol.
            j = i + 1
            pts = [cur, op[1]]
            while j < len(ops) and ops[j][0] == "L" and j - i < MAX_RUN:
                cand = pts + [ops[j][1]]
                if all(point_line_dist(p, cand[0], cand[-1]) <= tol for p in cand[1:-1]):
                    pts = cand
                    j += 1
                else:
                    break
            out.append(("L", pts[-1]))
            cur = pts[-1]
            i = j
            continue
        # Smooth curve runs: grow while one cubic still fits.
        run = [first]
        best = first
        j = i + 1
        while j < len(ops) and ops[j][0] in ("C", "Q") and j - i < MAX_RUN:
            nxt = as_cubic(run[-1][3], ops[j])
            if not smooth_join(run[-1], nxt):
                break
            cand = run + [nxt]
            t0 = start_tangent(cand[0])
            t1 = end_tangent(cand[-1])
            fit, err = fit_cubic(sample_run(cand), t0, t1)
            if fit is None or err > tol:
                break
            run, best = cand, fit
            j += 1
        out.append(reduce_single(best, tol))
        cur = best[3]
        i = j
    return out


def join_strokes(subpaths, eps):
    """Join stroke subpaths where one starts at the end of the last."""
    out = []
    for start, ops in subpaths:
        if out:
            pstart, pops = out[-1]
            last = pops[-1]
            end = pstart if last[0] == "Z" else last[-1]
            if last[0] != "Z" and length(sub(end, start)) <= eps:
                pops.extend(ops)
                continue
        out.append((start, list(ops)))
    return out


def simplify(subpaths, tol, is_stroke):
    if tol <= 0:
        return subpaths
    if is_stroke:
        subpaths = join_strokes(subpaths, tol / 4)
    return [(s, simplify_ops(s, ops, tol)) for s, ops in subpaths]
