"""Upscaled textures for HELLDIVERS (2015), as a separate mod: Mods/EnhancedTextures.

Steps, each its own command, so a long upscale can stop and resume:

  python tools/textures.py select [--min 1024] [--limit N]    which textures (colour ones, by size), minus tools/textures_exclude.txt
  python tools/textures.py extract                             each as PNG jobs, wrap-padded
  python tools/textures.py upscale --esrgan <realesrgan-ncnn-vulkan.exe> [--model realesrgan-x4plus] [--tile 200]
  python tools/textures.py upscale --lanczos                   a plain resize, to test the other steps
  python tools/textures.py pack [--texconv <texconv.exe>]      back to the game's format, and the mod
  python tools/textures.py check                               every packed texture against the original
  python tools/textures.py status                              how far each step got

Work files go in EnhancedVisuals/internal/textures-work (gitignored). The game is read from HD1_CONTENTS,
or the default Steam folder.

What the game has (tools/texture_survey.py): 2,045 distinct textures in 13,072 entries, the same file in
several level bundles; plain DDS at offset 0, every mip, then a 4-byte category kept as it was. Only DXT1,
DXT3 and DXT5 are taken, at power-of-two sizes: ATI2 (normal maps), uncompressed ones (UI, colour-grading
lookup tables) and screen-sized pictures (loading screens, menus) stay as they are. Every copy is replaced, or the old texture shows wherever one was left out.

Channels: where R, G and B move together (a picture), they go to the upscaler as one RGB image; where they
don't (packed masks: gloss, metal, occlusion), each goes alone as a grey image, so one map can't bleed into
another. Alpha goes alone. Each job is padded by wrapping the opposite edge around, so tiling textures
still meet at the seams, then cropped back. The upscaler works at 4x; the result is brought down to 2x
with Lanczos, the usual way to get a clean 2x from a 4x model.
"""
import argparse
import csv
import json
import shutil
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stingray as st  # noqa: E402
import texture_survey as ts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "internal" / "textures-work"
MOD = ROOT.parent / "EnhancedTextures"
PAD = 16
SCALE = 2
FORMATS = {"DXT1": "BC1_UNORM", "DXT3": "BC2_UNORM", "DXT5": "BC3_UNORM"}


def load_json(name, default=None):
    p = WORK / name
    return json.loads(p.read_text()) if p.exists() else default


def save_json(name, data):
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / name).write_text(json.dumps(data, indent=1))


def cmd_select(args):
    survey = ROOT / "internal" / "research" / "textures.csv"
    if not survey.exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "texture_survey.py")], check=True)
    by = defaultdict(list)
    for r in csv.DictReader(survey.open()):
        by[r["name"]].append(r)
    exclude = excluded()
    chosen = []
    for name, copies in by.items():
        if name in exclude:
            continue
        r = copies[0]
        w, h = int(r["w"]), int(r["h"])
        # World textures are powers of two; 1920x1080 and the like are loading screens and menus.
        square_ish = w & (w - 1) == 0 and h & (h - 1) == 0
        if r["format"] in FORMATS and square_ish and args.min <= max(w, h) <= args.max:
            chosen.append({"name": name, "format": r["format"], "w": int(r["w"]), "h": int(r["h"]),
                           "bundles": sorted({c["bundle"] for c in copies})})
    chosen.sort(key=lambda t: -t["w"] * t["h"])
    if args.limit:
        chosen = chosen[:args.limit]
    save_json("list.json", chosen)
    print(f"{len(chosen)} textures, {sum(len(t['bundles']) for t in chosen)} copies ({len(exclude)} on the exclude list)")


def excluded():
    """Hashes in tools/textures_exclude.txt, one per line; text after # is a note."""
    p = ROOT / "tools" / "textures_exclude.txt"
    if not p.exists():
        return set()
    return {line.split("#")[0].strip() for line in p.read_text(encoding="utf-8").splitlines()} - {""}


def original(t):
    for k, n, (data, _) in ts.entries_with_streams(st.CONTENTS / t["bundles"][0]):
        if k == ts.TEXTURE and f"{n:016x}" == t["name"]:
            return data
    raise SystemExit(f"{t['name']} not in {t['bundles'][0]}")


def decode(dds):
    import io
    return np.asarray(Image.open(io.BytesIO(dds)).convert("RGBA"))


def correlated(rgb):
    x = rgb.reshape(-1, 3).astype(np.float32)
    if x.std(0).min() < 1.0:
        return x.std(0).max() < 1.0   # flat channels: one image is fine
    c = np.corrcoef(x.T)
    return min(c[0, 1], c[0, 2], c[1, 2]) > 0.85


def cmd_extract(args):
    items = load_json("list.json")
    (WORK / "src").mkdir(parents=True, exist_ok=True)
    (WORK / "in").mkdir(parents=True, exist_ok=True)
    plan = {}
    for i, t in enumerate(items):
        data = original(t)
        (WORK / "src" / f"{t['name']}.texture").write_bytes(data)
        img = decode(data[:-4])
        rgb, a = img[..., :3], img[..., 3]
        jobs = []
        if correlated(rgb):
            jobs.append(("rgb", rgb))
        else:
            jobs += [(c, np.repeat(rgb[..., k:k + 1], 3, -1)) for k, c in enumerate("rgb")]
        if a.min() < 250:
            jobs.append(("a", np.repeat(a[..., None], 3, -1)))
        for tag, px in jobs:
            padded = np.pad(px, ((PAD, PAD), (PAD, PAD), (0, 0)), mode="wrap")
            Image.fromarray(padded).save(WORK / "in" / f"{t['name']}__{tag}.png")
        plan[t["name"]] = [tag for tag, _ in jobs]
        print(f"{i + 1}/{len(items)} {t['name']} {t['w']}x{t['h']} {t['format']} jobs {plan[t['name']]}")
    save_json("plan.json", plan)


def cmd_upscale(args):
    src, out = WORK / "in", WORK / "up"
    out.mkdir(parents=True, exist_ok=True)
    todo = [p for p in sorted(src.glob("*.png")) if not (out / p.name).exists()]
    print(f"{len(todo)} images to upscale")
    if args.lanczos:
        for p in todo:
            im = Image.open(p)
            im.resize((im.width * SCALE, im.height * SCALE), Image.Resampling.LANCZOS).save(out / p.name)
        return
    if not args.esrgan:
        raise SystemExit("give --esrgan <path to realesrgan-ncnn-vulkan.exe> or --lanczos")
    exe = Path(args.esrgan)
    raw = WORK / "up4"
    raw.mkdir(exist_ok=True)
    for i, p in enumerate(todo):
        dst = raw / p.name
        if not dst.exists():
            r = subprocess.run([str(exe), "-i", str(p), "-o", str(dst), "-n", args.model, "-s", "4", "-f", "png",
                                "-t", str(args.tile)],
                               cwd=exe.parent, capture_output=True, text=True)
            if r.returncode != 0 or not dst.exists():
                raise SystemExit(f"upscaler failed on {p.name}:\n{r.stdout}\n{r.stderr}")
        im = Image.open(dst)
        im.resize((im.width * SCALE // 4, im.height * SCALE // 4), Image.Resampling.LANCZOS).save(out / p.name)
        print(f"{i + 1}/{len(todo)} {p.name}")


def assemble(name, tags, w, h):
    w2, h2, pad = w * SCALE, h * SCALE, PAD * SCALE
    planes = {}
    for tag in tags:
        im = np.asarray(Image.open(WORK / "up" / f"{name}__{tag}.png").convert("RGB"))
        planes[tag] = im[pad:pad + h2, pad:pad + w2]
    out = np.full((h2, w2, 4), 255, np.uint8)
    if "rgb" in planes:
        out[..., :3] = planes["rgb"]
    else:
        for k, c in enumerate("rgb"):
            out[..., k] = planes[c][..., 0]
    if "a" in planes:
        out[..., 3] = planes["a"][..., 0]
    return out


def shrink(img, size, method=Image.Resampling.BOX):
    """A resize of each channel on its own: Pillow resizes RGBA with the colour weighted by alpha, which
    darkens colour under low alpha, and these textures don't store colour that way."""
    return Image.merge(img.mode, [c.resize(size, method) for c in img.split()])


def encode(rgba, fmt, template):
    """A DDS in the game's own layout: the original's header with the new size and mip count, then every
    mip down to 1x1, each mip a box filter of the one above, compressed by Pillow."""
    import io
    levels, img = [], Image.fromarray(rgba)
    while True:
        b = io.BytesIO()
        img.save(b, "DDS", pixel_format=fmt)
        levels.append(b.getvalue()[128:])
        if img.width == 1 and img.height == 1:
            break
        img = shrink(img, (max(1, img.width // 2), max(1, img.height // 2)))
    h, w = rgba.shape[:2]
    head = bytearray(template[:128])
    struct.pack_into("<IIII", head, 12, h, w, len(levels[0]), 0)
    struct.pack_into("<I", head, 28, len(levels))
    return bytes(head) + b"".join(levels)


def cmd_pack(args):
    items, plan = load_json("list.json"), load_json("plan.json")
    exe = Path(args.texconv) if args.texconv else None
    final, dds = WORK / "final", WORK / "dds"
    final.mkdir(exist_ok=True)
    dds.mkdir(exist_ok=True)
    res = MOD / "res"
    res.mkdir(parents=True, exist_ok=True)
    resources = []
    for i, t in enumerate(items):
        png = final / f"{t['name']}.png"
        rgba = assemble(t["name"], plan[t["name"]], t["w"], t["h"])
        if args.tint:   # a probe build: magenta stripes show where the game draws the new texture
            rgba = rgba.copy()
            stripe = (np.arange(rgba.shape[1]) // 64 % 2 == 0)
            rgba[:, stripe, 0], rgba[:, stripe, 1], rgba[:, stripe, 2] = 255, 0, 255
        Image.fromarray(rgba).save(png)
        src = (WORK / "src" / f"{t['name']}.texture").read_bytes()
        if exe:
            # texconv compresses better than Pillow; -dx9 keeps the classic header the game uses.
            cmd = [str(exe), "-nologo", "-y", "-dx9", "-m", "0", "-f", FORMATS[t["format"]], "-o", str(dds), str(png)]
            if t["format"] == "DXT1":
                cmd[1:1] = ["-at", "0.5"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            out = dds / f"{t['name']}.dds"
            if r.returncode != 0 or not out.exists():
                raise SystemExit(f"texconv failed on {t['name']}:\n{r.stdout}\n{r.stderr}")
            body = out.read_bytes()
        else:
            body = encode(rgba, t["format"], src)
        (res / f"{t['name']}.texture").write_bytes(body + src[-4:])
        for b in t["bundles"]:
            resources.append({"type": "texture", "name": f"#{t['name']}", "bundle": b, "data": f"res/{t['name']}.texture"})
        print(f"{i + 1}/{len(items)} {t['name']} -> {t['w'] * SCALE}x{t['h'] * SCALE} {FORMATS[t['format']]}, {len(t['bundles'])} bundles")
    manifest = {
        "obsidian": 1, "id": "alomare.enhancedtextures", "version": "0.1.0",
        "name": {"en": "Enhanced Textures"},
        "description": {"en": f"{len(items)} of the game's largest textures at twice the size, upscaled by an AI model and compressed back to the game's own formats."},
        "authors": ["Alomare"], "scope": "client", "loader_api": 1, "resources": resources,
    }
    (MOD / "obsidian.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {MOD / 'obsidian.json'}: {len(resources)} resources")


def header(data):
    h, w = struct.unpack_from("<II", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    fourcc = data[84:88].decode("latin-1")
    return w, h, mips, fourcc


def cmd_check(args):
    items = load_json("list.json")
    bad = 0
    for t in items:
        new = (MOD / "res" / f"{t['name']}.texture").read_bytes()
        old = (WORK / "src" / f"{t['name']}.texture").read_bytes()
        w, h, mips, fourcc = header(new)
        full = int(np.log2(max(w, h))) + 1
        problems = []
        if (w, h) != (t["w"] * SCALE, t["h"] * SCALE):
            problems.append(f"size {w}x{h}")
        if fourcc != t["format"]:
            problems.append(f"format {fourcc}")
        if mips != full:
            problems.append(f"{mips} mips of {full}")
        if new[-4:] != old[-4:]:
            problems.append("tail")
        a = decode(new[:-4]).astype(np.float32)
        small = np.asarray(shrink(Image.fromarray(a.astype(np.uint8)), (t["w"], t["h"]))).astype(np.float32)
        ref = decode(old[:-4]).astype(np.float32)
        mse = ((small - ref) ** 2).mean()
        psnr = 99 if mse == 0 else 10 * np.log10(255 ** 2 / mse)
        if psnr < 24:
            problems.append(f"differs from the original (PSNR {psnr:.1f} dB)")
        bad += bool(problems)
        print(f"{'BAD' if problems else 'ok '} {t['name']} {w}x{h} {fourcc} {mips} mips, PSNR {psnr:.1f} dB {'; '.join(problems)}")
    print(f"{len(items) - bad} ok, {bad} bad")
    sys.exit(1 if bad else 0)


def cmd_status(args):
    def count(folder, pattern):
        return len(list(folder.glob(pattern))) if folder.exists() else 0
    print(f"selected {len(load_json('list.json', []))}; jobs {count(WORK / 'in', '*.png')}; "
          f"upscaled {count(WORK / 'up', '*.png')}; packed {count(MOD / 'res', '*.texture')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--min", type=int, default=1024)
    s.add_argument("--max", type=int, default=2048, help="larger ones would become 8192 and up")
    s.add_argument("--limit", type=int, default=0)
    sub.add_parser("extract")
    u = sub.add_parser("upscale")
    u.add_argument("--esrgan")
    u.add_argument("--model", default="realesrgan-x4plus")
    u.add_argument("--lanczos", action="store_true")
    u.add_argument("--tile", type=int, default=0, help="Real-ESRGAN tile size, 0 = automatic; 200 or 100 if video memory runs out")
    p = sub.add_parser("pack")
    p.add_argument("--texconv")
    p.add_argument("--tint", action="store_true", help="probe build only: magenta stripes over each texture")
    sub.add_parser("check")
    sub.add_parser("status")
    args = ap.parse_args()
    {"select": cmd_select, "extract": cmd_extract, "upscale": cmd_upscale, "pack": cmd_pack,
     "check": cmd_check, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
