"""Decode ASR forest animations (SuCubicAnimation16 = type 1, SuCubicAnimation16Reduced2 = type 4).

Input: one animation entry of forest_to_obj's <name>.anims.json (masks, streams, float_data /
vector_data). Output: per-frame local samples per bone (translation, rotation quaternion xyzw, scale,
visible) on top of the tree's rest pose, and per-frame UV-bone transforms.

Layout (from SlMod's SuAnimation reader, coefficients worked out from the data):
  * channel masks: 4 bits per bone (1 translation, 2 rotation, 4 scale, 8 visibility), then 4 bits per
    UV bone (1 translation, 2 rotation, 4 scale), then 1 bit per float stream; packed 8 nibbles per u32
  * key headers (streams), one per animated channel in that order: i16 frame count, i16 key count n,
    n x i16 key frames; segment i runs from the previous key (0 for the first) to key i
  * type 1 coefficients (vector_data, float4s): per segment and component one float4 (a, b, c, d),
    value(t) = ((a t + b) t + c) t + d with t in [0, 1] across the segment; components: translation 3,
    rotation 4 (quaternion xyzw), scale 3, UV translation 2, UV rotation 1, UV scale 2, float 1
  * type 4 coefficients (float_data): per channel the start value (component count floats), then per
    segment three vectors (start tangent, end tangent, end value), Hermite-interpolated from the
    previous segment's end value
  * visibility channels have keys only: visibility toggles at each key, starting visible
Rotation samples are normalised.
"""

from __future__ import annotations

import numpy as np

T, R, S, V = 1, 2, 4, 8
COMPONENTS = {("bone", T): 3, ("bone", R): 4, ("bone", S): 3, ("uv", T): 2, ("uv", R): 1, ("uv", S): 2,
              ("float", 1): 1}


def _channels(anim: dict):
    """(kind, index, channel bit) for every animated channel, in data order."""
    masks = [m & 0xFFFFFFFF for m in anim["masks"]]
    bits = []
    for m in masks:
        bits.extend((m >> i) & 1 for i in range(32))
    pos = 0
    out = []
    for kind, count, nibble in (("bone", anim["bones"], 4), ("uv", anim["uv_bones"], 4)):
        for i in range(count):
            for b in range(nibble):
                if bits[pos + b]:
                    out.append((kind, i, 1 << b))
            pos += nibble
    for i in range(anim["float_streams"]):
        if bits[pos]:
            out.append(("float", i, 1))
        pos += 1
    return out


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    """(..., 4) xyzw -> (..., 3, 3)."""
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1)], -2)


def trs(t: np.ndarray, q: np.ndarray, s: np.ndarray) -> np.ndarray:
    """(..., 4, 4) = T * R * S, the forest's local branch matrix (forest_to_obj BuildLocalMatrix)."""
    m = np.zeros(np.broadcast_shapes(t.shape[:-1], q.shape[:-1], s.shape[:-1]) + (4, 4))
    m[..., :3, :3] = quat_to_matrix(q) * s[..., None, :]
    m[..., :3, 3] = t
    m[..., 3, 3] = 1.0
    return m


def branch_worlds(tree: dict, anim: dict, motion_only: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(rest world (B, 4, 4), animated world per frame (F, B, 4, 4), visible (F, B)) in ASR space.

    motion_only: ignore scale and visibility channels (rest scale, always visible). The placed objects'
    type-4 loops (balloons' "Auto") decode to a scale of 0 and swapped visibility after frame 1 --
    not what the game shows (balloons float there until hit); their translation/rotation is fine."""
    branches = tree["branches"]
    frames, nb = anim["frames"], len(branches)
    ch = decode(anim)
    rest_t = np.array([b["t"] for b in branches], np.float64)
    rest_r = np.array([b["r"] for b in branches], np.float64)
    rest_s = np.array([b["s"] for b in branches], np.float64)
    if motion_only:  # as forest_to_obj exports placed objects: a zero rest scale (spawn-in) kept as 1
        rest_s = np.where(np.abs(rest_s) < 1e-6, 1.0, rest_s)
    rest_local = trs(rest_t, rest_r, rest_s)
    rest = np.zeros((nb, 4, 4))
    world = np.zeros((frames, nb, 4, 4))
    visible = np.ones((frames, nb), bool)
    order, done = [], set()

    def visit(i):
        if i in done:
            return
        p = branches[i]["parent"]
        if 0 <= p < nb:
            visit(p)
        done.add(i)
        order.append(i)

    for i in range(nb):
        visit(i)
    for i in order:
        t = ch.get(("bone", i, T), np.broadcast_to(rest_t[i], (frames, 3)))
        q = ch.get(("bone", i, R))
        if q is None:
            q = np.broadcast_to(rest_r[i], (frames, 4))
        else:  # fill empty samples (zero quaternions) from the previous frame / the rest pose
            q = q.copy()
            bad = np.linalg.norm(q, axis=1) < 0.5
            last = rest_r[i]
            for f in range(frames):
                if bad[f]:
                    q[f] = last
                else:
                    last = q[f]
        s = np.broadcast_to(rest_s[i], (frames, 3)) if motion_only else ch.get(("bone", i, S), np.broadcast_to(rest_s[i], (frames, 3)))
        local = trs(np.asarray(t), np.asarray(q), np.asarray(s))
        p = branches[i]["parent"]
        if 0 <= p < nb:
            rest[i] = rest[p] @ rest_local[i]
            world[:, i] = world[:, p] @ local
            visible[:, i] = visible[:, p]
        else:
            rest[i] = rest_local[i]
            world[:, i] = local
        if ("bone", i, V) in ch and not motion_only:
            visible[:, i] &= ch[("bone", i, V)][:, 0] > 0.5
    return rest, world, visible


def _hermite(p0, m0, m1, p1, t):
    t2, t3 = t * t, t * t * t
    return ((2 * t3 - 3 * t2 + 1)[:, None] * p0 + (t3 - 2 * t2 + t)[:, None] * m0
            + (-2 * t3 + 3 * t2)[:, None] * p1 + (t3 - t2)[:, None] * m1)


def decode(anim: dict, normalize: bool = True) -> dict:
    """{(kind, index, channel): array (frames, components)} for every animated channel."""
    frames = anim["frames"]
    streams = anim["streams"]
    vec = np.array(anim.get("vector_data", []), np.float64).reshape(-1, 4)
    flt = np.array(anim.get("float_data", []), np.float64)
    vpos = fpos = spos = 0
    result = {}
    f = np.arange(frames, dtype=np.float64)
    for kind, index, ch in _channels(anim):
        n = streams[spos + 1]
        keys = streams[spos + 2:spos + 2 + n]
        spos += 2 + n
        if ch == V and kind == "bone":
            vis = np.ones(frames, bool)
            state = True
            prev = 0
            for k in keys:
                vis[min(prev, frames):min(k, frames)] = state
                state = not state
                prev = k
            vis[min(prev, frames):] = state
            result[(kind, index, ch)] = vis[:, None].astype(np.float64)
            continue
        comps = COMPONENTS[(kind, ch if kind != "float" else 1)]
        out = np.zeros((frames, comps))
        starts = [0] + list(keys[:-1])
        if anim["type"] == 1:
            for seg, (k0, k1) in enumerate(zip(starts, keys)):
                coeff = vec[vpos:vpos + comps]  # one float4 (a, b, c, d) per component
                vpos += comps
                sel = (f >= k0) & (f <= k1)
                t = (f[sel] - k0) / max(k1 - k0, 1)
                out[sel] = (((coeff[:, 0][None] * t[:, None] + coeff[:, 1][None]) * t[:, None]
                             + coeff[:, 2][None]) * t[:, None] + coeff[:, 3][None])
        else:
            p0 = flt[fpos:fpos + comps]
            fpos += comps
            for k0, k1 in zip(starts, keys):
                m0 = flt[fpos:fpos + comps]
                m1 = flt[fpos + comps:fpos + 2 * comps]
                p1 = flt[fpos + 2 * comps:fpos + 3 * comps]
                fpos += 3 * comps
                sel = (f >= k0) & (f <= k1)
                t = (f[sel] - k0) / max(k1 - k0, 1)
                out[sel] = _hermite(p0, m0, m1, p1, t)
                p0 = p1
        if kind == "bone" and ch == R and normalize:
            out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
        result[(kind, index, ch)] = out
    return result
