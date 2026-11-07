"""Runs the mod's shaders, with the game's own where the chain uses them, on WARP over a scene drawn here,
and writes internal/preview_*.png to look at.

The scene: flat ground, crates, a wall and some boulders, seen as the game's camera sees a mission
(17 m away, 65 degrees down, 60 degrees of vertical field of view). Its linear depth is ray cast here.

Ambient occlusion goes as in the mod's render config: the mod's vo_ssao at half size, the game's
ssao_blur x then y, the game's ssao_upscale; the result is the alpha that lands in the mask target.
The game's own vo_ssao runs beside it, with max_distance at 0 as it ships, for comparison.
Sharpening runs over a picture lit here from the same scene, at each strength.

Usage: python tools/preview.py
"""
import math
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dxbc  # noqa: E402
import shaderlib  # noqa: E402
import stingray as st  # noqa: E402
import warp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "internal"
W, H = 1280, 720
NEAR, FAR = 0.1, 500.0


def camera():
    pitch, dist, fov = math.radians(65), 17.0, math.radians(60)
    fwd = np.array([0.0, math.cos(pitch), -math.sin(pitch)])
    pos = -fwd * dist + np.array([0, 0, 1.0])
    right = np.array([1.0, 0, 0])
    up = np.cross(right, fwd)
    return pos, fwd, right, up, math.tan(fov / 2)


def boxes():
    out = [((-1.2, -1.2, 0), (1.2, 1.2, 1.4)),     # a crate
           ((2.0, -0.5, 0), (2.8, 0.3, 0.8)),      # a small crate
           ((-7, 3, 0), (7, 3.8, 2.5)),            # a wall
           ((-5, -4, 0), (-4.2, -3.2, 3.0)),       # a pillar
           ((4.5, -4.5, 0), (6.5, -2.5, 0.3))]     # a low slab
    return out


SPHERES = [((-2.5, 1.0, -0.3), 1.0), ((5.0, 1.5, -0.6), 1.5), ((0.5, -5.0, -0.2), 0.7)]


def raycast():
    pos, fwd, right, up, th = camera()
    ys, xs = np.mgrid[0:H, 0:W]
    ndc_x = (xs + 0.5) / W * 2 - 1
    ndc_y = 1 - (ys + 0.5) / H * 2
    dirs = fwd[None, None] + ndc_x[..., None] * th * (W / H) * right + ndc_y[..., None] * th * up
    t = np.full((H, W), np.inf)
    normal = np.zeros((H, W, 3))
    # ground
    tg = np.where(dirs[..., 2] < 0, -pos[2] / dirs[..., 2], np.inf)
    hit = tg < t
    t[hit], normal[hit] = tg[hit], (0, 0, 1)
    for lo, hi in boxes():
        lo, hi = np.array(lo), np.array(hi)
        with np.errstate(divide="ignore", invalid="ignore"):
            t1 = (lo - pos) / dirs
            t2 = (hi - pos) / dirs
        tmin = np.minimum(t1, t2)
        tmax = np.maximum(t1, t2)
        tn = tmin.max(-1)
        tf = tmax.min(-1)
        hit = (tn <= tf) & (tn > 0) & (tn < t)
        axis = tmin.argmax(-1)
        t[hit] = tn[hit]
        n = np.zeros((H, W, 3))
        for a in range(3):
            m = hit & (axis == a)
            n[m, a] = -np.sign(dirs[m, a])
        normal[hit] = n[hit]
    for c, r in SPHERES:
        c = np.array(c)
        oc = pos - c
        b = (dirs * oc).sum(-1)
        dd = (dirs * dirs).sum(-1)
        disc = b * b - dd * ((oc * oc).sum() - r * r)
        ts = (-b - np.sqrt(np.maximum(disc, 0))) / dd
        hit = (disc > 0) & (ts > 0) & (ts < t)
        t[hit] = ts[hit]
        p = pos + dirs[hit] * ts[hit][:, None]
        normal[hit] = (p - c) / r
    z = np.where(np.isfinite(t), t * 1.0, FAR)   # dirs have unit forward, so t is the view depth
    return z.astype(np.float32), normal


def shade(normal, z):
    sun = np.array([0.4, -0.5, 0.75])
    sun /= np.linalg.norm(sun)
    lam = np.clip((normal * sun).sum(-1), 0, 1)
    base = np.array([0.55, 0.5, 0.42])
    checker = ((np.floor(np.arange(W)[None] / 16) + np.floor(np.arange(H)[:, None] / 16)) % 2) * 0.06
    albedo = base[None, None] * (0.94 + checker[..., None])
    return albedo, lam


def cb(*parts):
    return b"".join(parts)


def f32(*v):
    return struct.pack(f"<{len(v)}f", *v)


def q8(img):
    """As an RGBA8 target holds it."""
    return np.clip(np.round(img * 255), 0, 255).astype(np.uint8)


def ps_of(name):
    e = shaderlib.load("1a9c33dcd0301dfd", st.murmur64(name))
    return [s for s in shaderlib.stages(e) if s[3] == "ps"][0][2]


def rotation():
    t = st.find("1a9c33dcd0301dfd", "texture", "core/rendering/vo_rotation")
    return np.frombuffer(t[128:128 + 64], np.float16).reshape(4, 4, 2).copy()


def ao_chain(dev, vo_blob, z, radius=0.1, strength=1.0, max_distance=0.0):
    camera_cb = f32(NEAR, FAR, 0.0, 0.0)
    rot = rotation()
    half = (W // 2, H // 2)
    raw = dev.run(vo_blob, half, textures=[z, rot], cbuffers=[camera_cb, cb(bytes(64), f32(W, H, radius, max_distance))],
                  samplers=[dev.linear, dev.point_wrap])
    a = q8(raw)
    inv = f32(1 / half[0], 1 / half[1])
    bx = q8(dev.run(ps_of("ssao_blur:separable_gaussian_13tap_x"), half, textures=[a], cbuffers=[camera_cb, cb(bytes(64), inv)]))
    by = q8(dev.run(ps_of("ssao_blur:separable_gaussian_13tap_y"), half, textures=[bx], cbuffers=[camera_cb, cb(bytes(64), inv)]))
    up = dev.run(ps_of("ssao_upscale"), (W, H), textures=[by], cbuffers=[cb(bytes(64), f32(1 / half[0], 1 / half[1], strength, 0))])
    return raw[..., 0], up[..., 3]


def save(name, img):
    Image.fromarray(q8(np.clip(img, 0, 1))[..., :3] if img.ndim == 3 else q8(np.clip(img, 0, 1))).save(OUT / name)
    print("wrote", OUT / name)


def main():
    OUT.mkdir(exist_ok=True)
    dev = warp.Device()
    z, normal = raycast()
    albedo, lam = shade(normal, z)
    ambient = 0.45

    mine = dxbc.compile((ROOT / "shaders" / "ssao.hlsl").read_text(), name="ssao.hlsl")
    rows = []
    for label, blob, md in (("game vo_ssao, as shipped", ps_of("vo_ssao"), 0.0),
                            ("game vo_ssao, max_distance 1", ps_of("vo_ssao"), 1.0),
                            ("mod", mine, 0.0)):
        raw, ao = ao_chain(dev, blob, z, strength=1.0, max_distance=md)
        lit = albedo * (lam[..., None] * 0.9 + ambient * ao[..., None])
        print(f"{label:30s} AO mean {ao.mean():.3f} min {ao.min():.3f}  raw mean {raw.mean():.3f}")
        rows.append(np.concatenate([np.repeat(ao[..., None], 3, -1), lit], 1))
    save("preview_ao.png", np.concatenate(rows, 0)[::2, ::2])

    for strength in (0.6, 1.0, 1.6):
        _, ao = ao_chain(dev, mine, z, radius=strength / 10)
        print(f"mod, strength {strength}: AO mean {ao.mean():.3f}, darkest {ao.min():.3f}")

    # Sharpening over the lit picture, softened a little as FXAA leaves it.
    _, ao = ao_chain(dev, mine, z, strength=1.0)
    lit = np.clip(albedo * (lam[..., None] * 0.9 + ambient * ao[..., None]), 0, 1)
    soft = (lit + np.roll(lit, 1, 0) + np.roll(lit, 1, 1) + np.roll(np.roll(lit, 1, 0), 1, 1)) / 4
    src = np.concatenate([q8(soft), np.full((H, W, 1), 255, np.uint8)], -1)
    crops = [soft]
    for s in (0.3, 0.6, 1.0):
        blob = dxbc.compile(f"#define SHARPNESS {s}\n" + (ROOT / "shaders" / "sharpen.hlsl").read_text(), name="sharpen.hlsl")
        out = dev.run(blob, (W, H), textures=[src], cbuffers=[cb(bytes(64), f32(1 / W, 1 / H))])
        d = np.abs(out[..., :3] - soft).mean()
        print(f"sharpen {s}: mean change {d:.4f}, range {out[..., :3].min():.3f}..{out[..., :3].max():.3f}")
        crops.append(out[..., :3])
    y0, x0 = 180, 420
    save("preview_sharpen.png", np.concatenate([c[y0:y0 + 300, x0:x0 + 440] for c in crops], 1))


if __name__ == "__main__":
    main()
