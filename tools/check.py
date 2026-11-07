"""Checks what tools/build.py wrote, offline:

- each shader library in res/ parses into its stages, the untouched bytes match the game's entry, and
  its pixel shader is accepted by Direct3D 11 (WARP), which validates the container;
- the AO chain runs from the shipped library's own pixel shader, with the game's blur and upscale;
- each render config variant decodes, and differs from the game's only where the build means it to.

Usage: python tools/check.py
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datatree  # noqa: E402
import dxbc  # noqa: E402
import preview  # noqa: E402
import shaderlib  # noqa: E402
import stingray as st  # noqa: E402
import warp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "res"
BUNDLE = "1a9c33dcd0301dfd"


def diff(a, b, path=""):
    if type(a) is not type(b):
        yield f"{path}: {a!r} -> {b!r}"
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                yield f"{path}.{k}: added"
            elif k not in b:
                yield f"{path}.{k}: removed"
            else:
                yield from diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            yield f"{path}: {len(a)} items -> {len(b)}"
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                yield from diff(x, y, f"{path}[{i}]")
    elif a != b:
        yield f"{path}: {a!r} -> {b!r}"


def shipped_ps(manifest, data):
    r = next(r for r in manifest["resources"] if r["data"] == data)
    return next(s for s in shaderlib.stages((ROOT / r["data"]).read_bytes()) if s[3] == "ps")[2]


def check_ambient(dev, manifest, rng):
    """The replaced blend still adds, exactly as the game's, wherever the first input is the smaller
    (the pyramid's sums, and the picture's pass with the inputs turned round while Ambient light is off),
    and gives the ambient look only on the picture's pass. The extract keeps lit surfaces and drops dark."""
    failures = 0
    game_blend = next(s for s in shaderlib.stages(shaderlib.load(BUNDLE, st.murmur64("blend"))) if s[3] == "ps")[2]
    combine = shipped_ps(manifest, "res/ambient_combine_subtle.shader_library")
    coarse = (rng.random((36, 64, 4)) * 3).astype(np.float32)
    fine = (rng.random((144, 256, 4)) * 3).astype(np.float32)
    want = dev.run(game_blend, (256, 144), textures=[coarse, fine])
    got = dev.run(combine, (256, 144), textures=[coarse, fine])
    good = np.abs(got - want).max() < 1e-6
    print(f"{'ok ' if good else 'BAD'} ambient combine: the pyramid's sums are the game's add (max diff {np.abs(got - want).max():.2e})")
    failures += not good

    picture = np.zeros((144, 256, 4), np.float32)
    picture[..., 3] = 1
    picture[:, :64, :3] = 0.25                      # dark, no glow
    picture[:, 64:128, :3] = 0.25                   # dark, glow
    picture[:, 128:192, :3] = 1.0                   # white
    bloom = np.zeros((36, 64, 4), np.float32)
    bloom[:, 16:32, :3] = 0.3
    off = dev.run(combine, (256, 144), textures=[bloom, picture])
    plain = dev.run(game_blend, (256, 144), textures=[picture, bloom])
    on = dev.run(combine, (256, 144), textures=[picture, bloom])
    dark, lit, white, black = on[70, 30, 0], on[70, 96, 0], on[70, 160, 0], on[70, 220, 0]
    good = (np.abs(off - plain).max() < 1e-5 and dark < 0.25 * 0.95 and lit > dark + 0.1 and abs(white - 1) < 1e-5
            and black < 1e-6 and lit <= 0.25 + 0.3 + 1e-5)
    print(f"{'ok ' if good else 'BAD'} ambient combine: off = the game's add; on: dark {dark:.3f} (from 0.25), "
          f"by the glow {lit:.3f}, white {white:.3f}, black {black:.3f}")
    failures += not good

    extract = shipped_ps(manifest, "res/ambient_extract_subtle.shader_library")
    scene = np.zeros((144, 256, 4), np.float32)
    scene[..., :3] = 0.15
    scene[40:80, 40:80, :3] = (1.0, 0.8, 0.5)
    glow = dev.run(extract, (64, 36), textures=[scene])
    inside, outside = glow[15, 15, :3], glow[30, 60, :3]
    good = inside.max() > 0.2 and outside.max() < 0.01 and inside[0] > inside[2] * 1.5 and np.allclose(glow[..., 3], 0)
    print(f"{'ok ' if good else 'BAD'} ambient extract: lit {np.round(inside, 3)}, dark {np.round(outside, 4)}, tint kept")
    failures += not good
    return failures


def check_shadow_distance(manifest):
    """Each patched shading environment differs from the game's in four bytes, the cascade's far end, set
    to the distance its choice names."""
    import struct
    import build
    kind = st.murmur64("shading_environment")
    by_bundle = {}
    for r in manifest["resources"]:
        if r["type"] == "shading_environment":
            by_bundle.setdefault(r["bundle"], []).append(r)
    bad, total = 0, 0
    for bundle, rs in by_bundle.items():
        game = {n: d[0] for k, n, d in st.entries(st.CONTENTS / bundle) if k == kind}
        for r in rs:
            new, old = (ROOT / r["data"]).read_bytes(), game[int(r["name"][1:], 16)]
            diff = [i for i in range(len(old)) if i >= len(new) or new[i] != old[i]]
            far = build.SHADOW_DISTANCE[r["when"]["shadow_distance"]]
            start = diff[0] - diff[0] % 4 if diff else 0
            ok = len(new) == len(old) and diff and diff[-1] < start + 4 \
                and abs(struct.unpack_from("<f", new, start)[0] - far) < 1e-3
            bad += not ok
            total += 1
    print(f"{'ok ' if not bad else 'BAD'} shadow distance: {total} shading environments, {bad} differing beyond the far end")
    return bad


def check_render_scale(dev, manifest, rng):
    """The downsample is the area average at 2x and 1.5x; every patched back_buffer_size reader is
    accepted by Direct3D and disassembles to the game's own code but for the new temp, the resinfo that
    fills it and the operands that read it; the scaled render configs move what they should."""
    import re
    import bbsize
    failures = 0
    down = shipped_ps(manifest, "res/downsample.shader_library")
    src = rng.random((144, 256, 4)).astype(np.float32)
    got = dev.run(down, (128, 72), textures=[src])
    want = src.reshape(72, 2, 128, 2, 4).mean(axis=(1, 3))
    ramp = np.zeros((108, 192, 4), np.float32)
    ramp[..., 0] = np.arange(192)[None, :] / 191.0
    got15 = dev.run(down, (128, 72), textures=[ramp])[36, 1:-1, 0]
    want15 = []
    for i in range(1, 127):   # each texel k holds k / 191 over [k, k + 1); the average over [1.5i, 1.5i + 1.5)
        lo, hi = 1.5 * i, 1.5 * i + 1.5
        want15.append(sum(max(0.0, min(hi, k + 1) - max(lo, k)) * k for k in range(int(lo), int(hi) + 1)) / 1.5 / 191.0)
    want15 = np.array(want15)
    good = np.abs(got - want).max() < 1e-5 and np.abs(got15 - want15).max() < 1e-4
    print(f"{'ok ' if good else 'BAD'} downsample: 2x = 2x2 average (diff {np.abs(got - want).max():.1e}), "
          f"1.5x = area average (diff {np.abs(got15 - want15).max():.1e})")
    failures += not good

    # The way up for transparent_in_world: each pixel takes the texel under it, so at 2x up and back down
    # gives the picture back.
    small = rng.random((72, 128, 4)).astype(np.float32)
    up = dev.run(down, (256, 144), textures=[small])
    back = dev.run(down, (128, 72), textures=[up])
    nearest = np.repeat(np.repeat(small, 2, axis=0), 2, axis=1)
    good = np.abs(up - nearest).max() < 1e-4 and np.abs(back - small).max() < 1e-4
    print(f"{'ok ' if good else 'BAD'} upscale: 2x = nearest texel (diff {np.abs(up - nearest).max():.1e}), "
          f"up and down = the picture (diff {np.abs(back - small).max():.1e})")
    failures += not good

    files = sorted({r["data"] for r in manifest["resources"] if r["data"].startswith("res/render_scale/")})
    stages = bad = 0
    for data_path in files:
        bundle, name = Path(data_path).stem.split("_")
        new_entry = (ROOT / data_path).read_bytes()
        old_entry = shaderlib.load(bundle, int(name, 16))
        for new, old in zip(shaderlib.stages(new_entry), shaderlib.stages(old_entry)):
            if new[2] == old[2]:
                continue
            stages += 1
            ok = bbsize.checksum(new[2]) == new[2][4:20]
            try:
                dev._shader(15, new[2])
            except RuntimeError:
                ok = False
            a = [l for l in dxbc.disassemble(old[2]).splitlines() if not l.startswith("//")]
            b = [l for l in dxbc.disassemble(new[2]).splitlines() if not l.startswith("//")]
            temps = int(next(l for l in a if l.startswith("dcl_temps")).split()[1])
            gv = dict(x.split(":") for x in shaderlib.bindings(old[2]))["global_viewport"][2:]
            fixed = []
            for l in a:
                if l.startswith("dcl_temps"):
                    l = f"dcl_temps {temps + 1}"
                l = re.sub(rf"cb{gv}\[18\]\.([zw]+)", lambda m: f"r{temps}." + m.group(1).replace("z", "x").replace("w", "y"), l)
                fixed.append(l)
            inserted = [l for l in b if l not in fixed]
            rest = [l for l in b if l in fixed]
            ok = ok and rest == fixed and len(inserted) == 1 and inserted[0].startswith(f"resinfo r{temps}.xyzw, l(0), t")
            bad += not ok
    good = stages > 0 and not bad
    print(f"{'ok ' if good else 'BAD'} render scale: {len(files)} entries, {stages} patched stages, {bad} not as meant")
    failures += not good

    sample = [blob for f in sorted(shaderlib.DUMP.glob(f"{BUNDLE}_*.bin"))[:60] for _, blob in dxbc.blobs(f.read_bytes())]
    same = sum(bbsize.checksum(b) == b[4:20] for b in sample)
    print(f"{'ok ' if same == len(sample) else 'BAD'} DXBC checksum reproduced for {same} of {len(sample)} game stages")
    failures += same != len(sample)

    for r in manifest["resources"]:
        if r["type"] != "render_config" or r["when"].get("render_scale") == "off":
            continue
        t = datatree.decode((ROOT / r["data"]).read_bytes())
        scale = {"150": 1.5, "200": 2.0}[r["when"]["render_scale"]]
        albedo = [d for d in walk_tree(t["global_resources"]) if d.get("name") == "albedo" and d.get("depends_on") == "back_buffer"]
        stale = [d for d in walk_tree(t["layer_configs"]["default"]) if d.get("depth_stencil_target") == "output_dst"
                 and d.get("render_targets") == "output_rt"]
        graded = [d for d in walk_tree(t["resource_generators"]["post_processing"]) if d.get("material") == "color_grading"]
        layers = [d.get("name") for d in walk_tree(t["layer_configs"]["default"])]
        tiw = [d for d in walk_tree(t["layer_configs"]["default"]) if d.get("name") == "transparent_in_world"
               and d.get("render_targets") == "ev_world" and d.get("depth_stencil_target") == "output_dst"]
        good = (any(d.get("w_scale") == scale for d in albedo) and not stale and len(tiw) == 1
                and layers.index("ev_world_up") + 1 == layers.index("transparent_in_world", layers.index("ev_world_up"))
                and all(d["input"] == "ev_scaled_down" for d in graded if d.get("output") == "output_rt"))
        print(f"{'ok ' if good else 'BAD'} {r['data']}: albedo at {scale}, back-buffer layers on their own depth, "
              "glass on the scene's depth, grading after the downsample")
        failures += not good
    return failures


def walk_tree(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk_tree(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_tree(v)


MASK_VS = """
struct VS_OUT { float4 position : SV_POSITION; float2 uv : TEXCOORD0; float4 ray : TEXCOORD1; };
VS_OUT vs_main(uint id : SV_VertexID)
{
	VS_OUT o;
	float2 t = float2((id << 1) & 2, id & 2);
	o.uv = t;
	o.position = float4(t.x * 2.0 - 1.0, 1.0 - t.y * 2.0, 0.0, 1.0);
	o.ray = float4((t.x - 0.5) * 0.1, (t.y - 0.5) * 0.1, 1.0, 1.0);
	return o;
}
"""


def check_soft_shadows(dev, manifest):
    """Every soft shadow mask stage, against the game's stage it replaces, over a scene with one straight
    shadow edge across a 512 map: under the compare value (2) the same picture as the game's; otherwise a
    wider edge, the same lit and shadowed levels on either side."""
    failures = 0
    gv = bytearray(320)
    c0 = bytearray(160)
    rows = [(0.5, 0, 0, 0.5), (0, 0.5, 0, 0.5), (0, 0, 0.05, 0), (0, 0, 0, 1)]
    for i, row in enumerate(rows):
        c0[64 + 16 * i:80 + 16 * i] = np.array(row, np.float32).tobytes()
    c0[128:140] = np.array([0.5, 1.0, 1.0], np.float32).tobytes()   # cloud opacity, scale
    shadow = np.ones((512, 512), np.float32)
    shadow[:, :256] = 0.2
    inputs = {"__tex_depth": np.full((64, 128), 10.0, np.float32), "shadow_map": shadow,
              "nv_stereo_params": np.zeros((1, 1, 4), np.float32),
              "__tex_shadow_map_color": np.full((512, 512, 4), 0.1, np.float32),
              "__tex_cloud_shadow_map": np.full((64, 64, 4), 0.5, np.float32)}
    for r in manifest["resources"]:
        if r.get("when", {}).get("soft_shadows") != "soft" or r["bundle"] != BUNDLE:
            continue
        data = (ROOT / r["data"]).read_bytes()
        vanilla = shaderlib.load(BUNDLE, int(r["name"][1:], 16))
        for new, old in zip(shaderlib.stages(data), shaderlib.stages(vanilla)):
            if new[3] != "ps":
                continue
            binds = [b.split(":") for b in shaderlib.bindings(old[2])]
            textures = [inputs[n] for n, slot in sorted((b for b in binds if b[1][0] == "t"), key=lambda b: int(b[1][1:]))]
            samplers = [dev.comparison if n == "shadow_map_sampler" else dev.linear
                        for n, slot in sorted((b for b in binds if b[1][0] == "s"), key=lambda b: int(b[1][1:]))]
            out = {}
            for mode in (1.0, 2.0):
                gv[316:320] = np.array([mode], np.float32).tobytes()
                out[mode] = dev.run(new[2], (128, 64), textures=textures, samplers=samplers,
                                    cbuffers=[bytes(gv), bytes(c0)], vs=MASK_VS)[32, :, 0]
            gv[316:320] = np.array([1.0], np.float32).tobytes()
            game = dev.run(old[2], (128, 64), textures=textures, samplers=samplers, cbuffers=[bytes(gv), bytes(c0)], vs=MASK_VS)[32, :, 0]
            top = game.max()
            edge = lambda row: int(((row > 0.02 * top) & (row < 0.98 * top)).sum())
            same = np.abs(out[2.0] - game).max()
            good = (same < 1e-4 and edge(out[1.0]) >= edge(game) + 3 and abs(out[1.0].max() - top) < 1e-4
                    and out[1.0].min() < 1e-4 and top > 0.2)
            print(f"{'ok ' if good else 'BAD'} soft shadows {r['data'].split('/')[-1]} {'+'.join(n for n, _ in binds if n in inputs)}:"
                  f" compare mode = game (diff {same:.1e}), edge {edge(game)} px -> {edge(out[1.0])} px, lit {top:.3f}")
            failures += not good
    return failures


def main():
    dev = warp.Device()
    failures = 0
    manifest = json.loads((ROOT / "obsidian.json").read_text(encoding="utf-8"))
    for r in manifest["resources"]:
        if r["type"] != "shader_library":
            continue
        data = (ROOT / r["data"]).read_bytes()
        name = int(r["name"][1:], 16)
        vanilla = shaderlib.load(r.get("bundle", BUNDLE), name)
        new_stages, old_stages = shaderlib.stages(data), shaderlib.stages(vanilla)
        assert [s[3] for s in new_stages] == [s[3] for s in old_stages], "stages differ"
        assert data[:old_stages[0][0]] == vanilla[:old_stages[0][0]], "header changed"
        sizes = []
        for i, (new, old) in enumerate(zip(new_stages, old_stages)):
            # The binding table after each stage, up to the next stage (or the end), stays the game's.
            new_end = new_stages[i + 1][0] if i + 1 < len(new_stages) else len(data)
            old_end = old_stages[i + 1][0] if i + 1 < len(old_stages) else len(vanilla)
            assert data[new[1] + len(new[2]):new_end] == vanilla[old[1] + len(old[2]):old_end], f"table after stage {i} changed"
            if new[3] == "vs":
                assert new[2] == old[2], "vertex shader changed"
            elif new[2] != old[2]:
                extra = set(shaderlib.bindings(new[2])) - set(shaderlib.bindings(old[2]))
                assert not extra, f"stage {i} binds what the game's tables don't offer: {sorted(extra)}"
                dev._shader(15, new[2])   # raises if Direct3D refuses it
                sizes.append(len(new[2]))
        print(f"ok  {r['data']}: header and tables as the game's, {len(sizes)} pixel shader(s) accepted {sizes}")

    ssao = next(r for r in manifest["resources"] if r["data"] == "res/ssao.shader_library")
    ps = next(s for s in shaderlib.stages((ROOT / ssao["data"]).read_bytes()) if s[3] == "ps")[2]
    z, _ = preview.raycast()
    _, ao = preview.ao_chain(dev, ps, z, strength=1.0)
    flat = float(np.median(ao))
    print(f"{'ok ' if flat > 0.999 and ao.min() < 0.85 else 'BAD'} AO from the shipped shader: flat ground {flat:.4f}, darkest {ao.min():.3f}")
    failures += not (flat > 0.999 and ao.min() < 0.85)

    # All daylight: the shipped apply shader over a lit picture and a half-size occlusion.
    apply = next(r for r in manifest["resources"] if r["data"] == "res/ao_apply.shader_library")
    ps = next(s for s in shaderlib.stages((ROOT / apply["data"]).read_bytes()) if s[3] == "ps")[2]
    rng = np.random.default_rng(1)
    lit = (rng.random((720, 1280, 4)) * 4).astype(np.float32)
    half = np.ones((360, 640, 4), np.float32)
    half[100:200, 200:300, 0] = 0.5
    out = dev.run(ps, (1280, 720), textures=[lit, half])
    inside, outside = out[300:360, 460:540, :3] / lit[300:360, 460:540, :3], out[:100, :100, :3] / lit[:100, :100, :3]
    good = abs(inside.mean() - 0.5) < 1e-3 and abs(outside.mean() - 1) < 1e-4 and np.allclose(out[..., 3], lit[..., 3])
    print(f"{'ok ' if good else 'BAD'} apply: occluded x{inside.mean():.4f}, open x{outside.mean():.4f}, alpha kept")
    failures += not good

    failures += check_ambient(dev, manifest, rng)
    failures += check_soft_shadows(dev, manifest)
    failures += check_shadow_distance(manifest)
    failures += check_render_scale(dev, manifest, rng)

    vanilla = datatree.decode(st.find(BUNDLE, "render_config", int(BUNDLE, 16)))
    for r in manifest["resources"]:
        if r["type"] != "render_config":
            continue
        tree = datatree.decode((ROOT / r["data"]).read_bytes())
        changes = list(diff(vanilla, tree))
        print(f"ok  {r['data']}: {len(changes)} changes")
        if r["data"].endswith("render_config_8192.bin"):
            for c in changes:
                print("      " + c[:150])
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
