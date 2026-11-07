"""Builds what Enhanced Visuals ships, in res/, from the game's own render config and shaders.

Usage: python tools/build.py

The render config (core/rendering/renderer, bundle 1a9c33dcd0301dfd) is read as a data tree, changed,
and written back in the layout the game's compiler uses (tools/datatree.py round-trips every render
config byte for byte). The changes:

- Ambient occlusion: the game ships a whole SSAO chain (vo_ssao, ssao_blur x and y, ssao_upscale) and
  never runs it. It goes in the deferred shading pass, after the sun's shadow mask and before global
  lighting, at half size in the downscale_half targets the config already allocates. On ambient light
  only (ev_ao_all false), ssao_upscale blends the result into the alpha of the mask target, which
  global lighting multiplies the ambient light by. On all daylight (ev_ao_all true), a pass after global
  lighting multiplies sun and ambient light by it, before point and spot lights add theirs. Runs while
  the render setting ssao_enabled is true.
- Sharpening: a pass after FXAA, as FXAA is set up (output_rt in, output_rt out), while the render
  setting ev_sharpen is true.
- The clear pass also clears albedo and normal, as Photo Mode's sky fix does, so a camera that sees past
  the level shows a cleared picture. Included so either mod's config gives both.
- Shadows: the sun's shadow map at a fixed size (4096 or 8192) instead of the Shadow quality setting's
  (512 to 2048), and the point lights' shadow cube at 2048 instead of 1024.

Variants: only what needs a restart, the shadow map size (game, 4096, 8192). Everything behind a render
setting (AO, the AO mode, sharpening) is on by default in every variant, and the mod's Lua sets those
settings live from the options at once.

Shaders: vo_ssao's pixel shader replaced by shaders/ssao.hlsl, luminance_adaption's (unused) by
shaders/ao_apply.hlsl, and filter:gaussian_blur_13tap's (unused) by shaders/sharpen.hlsl at two
strengths.
"""
import copy
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bbsize  # noqa: E402
import datatree  # noqa: E402
import dxbc  # noqa: E402
import shaderlib  # noqa: E402
import stingray as st  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "res"
BUNDLE = "1a9c33dcd0301dfd"

SHADOWS = {"game": None, "4096": 4096, "8192": 8192}
SHARPNESS = {"normal": 0.6, "strong": 1.0}
SSAO_SHADER = "vo_ssao"
AO_APPLY_SHADER = "luminance_adaption"
SHARPEN_SHADER = "filter:gaussian_blur_13tap"
# Ambient light: (darkening of the dark tones, glow gain) per strength.
AMBIENT = {"subtle": (0.15, 0.3), "strong": (0.35, 0.7)}
AMBIENT_COMBINE_SHADER = "blend"
AMBIENT_EXTRACT_SHADER = "filter:log_luminance"
# Soft shadow edges: (radius on the ground in metres, taps per side) per choice.
SOFT_SHADOWS = {"soft": (0.025, 4), "softer": (0.05, 6)}
SHADOW_MASKS = ("global_shadow_mask", "global_shadow_mask:cloud_shadows")
# Shadow distance: the far end of the sun's shadow cascade, metres of view depth, per choice.
SHADOW_DISTANCE = {"long": 35.0, "longer": 60.0}
# Render scale: the scene's size over the back buffer's, per choice.
RENDER_SCALE = {"off": None, "150": 1.5, "200": 2.0}
DOWNSAMPLE_SHADER = "copy:encode_as_rgbm"


def walk(node):
    """Every dict in a tree, depth first."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def one(found, what, first=False):
    found = list(found)
    if first and found:
        return found[0]
    if len(found) != 1:
        raise SystemExit(f"expected one {what}, found {len(found)}")
    return found[0]


def fp(shader, inp, out):
    return {"input": inp, "output": out, "shader": shader, "type": "fullscreen_pass"}


def mp(material, inp, out):
    """A pass through the planet's shading material of that name, which feeds the shader the shading's
    values by name (ssao_radius, ssao_strength). A plain shader pass leaves them at 0 (probed in game)."""
    return {"input": inp, "material": material, "output": out, "type": "fullscreen_pass"}


def ssao_branch():
    """Before global lighting: the occlusion at half size, blurred; on ambient light only, into mask.a."""
    return {
        "pass_modifiers": [
            mp("vo_ssao", "depth", "downscale_half0"),
            fp("ssao_blur:SEPARABLE_GAUSSIAN_13TAP_X", "downscale_half0", "downscale_half1"),
            fp("ssao_blur:SEPARABLE_GAUSSIAN_13TAP_Y", "downscale_half1", "downscale_half0"),
            {"pass_modifiers": [mp("ssao_upscale", "downscale_half0", "mask")],
             "render_settings": {"ev_ao_all": False}, "type": "dynamic_branch"},
        ],
        "profiling_scope": "ssao",
        "render_settings": {"ssao_enabled": True},
        "type": "dynamic_branch",
    }


def ao_all_branch():
    """After global lighting, before point and spot lights: sun and ambient light times the occlusion.
    The lighting is copied first, as a pass can't read the target it writes."""
    return {
        "pass_modifiers": [
            fp("copy", "light_accumulation", "light_accumulation1"),
            fp("luminance_adaption", "light_accumulation1 downscale_half0", "light_accumulation"),
        ],
        "profiling_scope": "ssao",
        "render_settings": {"ev_ao_all": True, "ssao_enabled": True},
        "type": "dynamic_branch",
    }


def glow_chain():
    """Ambient light's wide glow, in the bloom pyramid's own targets once the pyramid has summed its
    levels into hdr_downscale_one_fourth1 (every other level is free by then): a bright-weighted copy
    of the picture at a quarter size, blurred at a sixteenth and a sixty-fourth by the game's own
    filters, the two blurs added, then added to the bloom, all into hdr_downscale_one_fourth0."""
    q, s0, s1 = "hdr_downscale_one_fourth0", "hdr_downscale_one_sixteenth0", "hdr_downscale_one_sixteenth1"
    t0, t1 = "hdr_downscale_one_sixtyforth0", "hdr_downscale_one_sixtyforth1"
    return [
        fp("filter:LOG_LUMINANCE", "albedo", q),
        fp("filter:DOWNSAMPLE_4x4", q, s0),
        fp("filter:SEPARABLE_GAUSSIAN_13TAP_X", s0, s1),
        fp("filter:SEPARABLE_GAUSSIAN_13TAP_Y", s1, s0),
        fp("filter:DOWNSAMPLE_4x4", s0, t0),
        fp("filter:SEPARABLE_GAUSSIAN_13TAP_X", t0, t1),
        fp("filter:SEPARABLE_GAUSSIAN_13TAP_Y", t1, t0),
        # blend adds while the first input is the smaller one (shaders/ambient_combine.hlsl).
        fp("blend", f"{t0} {s0}", s1),
        fp("blend", f"{s1} hdr_downscale_one_fourth1", q),
    ]


def ambient_light(t):
    """Each pass that adds the bloom to the picture (blend, "albedo" first) becomes a branch on the render
    setting ev_ambient: on, the glow chain and the ambient combine; off, the same add with the bloom
    first, which the replaced blend treats as the game's plain add. The loadout preview keeps the game's
    look, with its inputs turned round the same way."""
    found = []
    for d in walk(t["resource_generators"]["post_processing"]):
        for key in ("modifiers", "pass_modifiers", "fail_modifiers"):
            mods = d.get(key)
            if isinstance(mods, list):
                found += [(mods, i) for i, m in enumerate(mods)
                          if m.get("shader") == "blend" and m.get("input", "").split()[:1] == ["albedo"]]
    if len(found) < 3:
        raise SystemExit(f"expected the bloom add in at least three post-processing branches, found {len(found)}")
    for mods, i in found:
        m = mods[i]
        bloom = m["input"].split()[1]
        on = dict(m, input="albedo hdr_downscale_one_fourth0")
        off = dict(m, input=f"{bloom} albedo")
        mods[i] = {"fail_modifiers": [off], "pass_modifiers": glow_chain() + [on],
                   "profiling_scope": "ev_ambient", "render_settings": {"ev_ambient": True},
                   "type": "dynamic_branch"}
    for d in walk(t["resource_generators"]["post_processing_preview"]):
        ins = d.get("input", "").split()
        if d.get("shader") == "blend" and len(ins) == 2 and ins[0] == "albedo":
            d["input"] = f"{ins[1]} albedo"


def render_scale(t, scale):
    """The scene at `scale` times the back buffer, brought down to it before color grading.

    On PC every scene target follows albedo, and albedo follows the back buffer at 1: albedo at `scale`
    takes the G-buffer, depth, lighting, AO, bloom and DOF targets with it (as the PS4 Pro branch takes
    them to half width). The viewport still draws into the back buffer, so the HUD stays at the screen's
    resolution. D3D11 wants a render target and its depth buffer the same size, so everything that draws
    into the back buffer (FXAA, sharpening, the HUD's layers, the brightness and overlay viewports) gets a
    depth buffer of its own at the back buffer's size, cleared by the first of those layers. The scene's
    last picture (mask) goes through shaders/downsample.hlsl into ev_scaled_down, which color grading
    then reads. The 26 shaders that read back_buffer_size come patched with the scene's size
    (tools/bbsize.py)."""
    albedo = one((d for d in walk(t["global_resources"]) if d.get("name") == "albedo" and d.get("depends_on") == "back_buffer"
                  and d.get("w_scale") == 1 and d.get("h_scale") == 1), "the PC albedo target")
    albedo["w_scale"] = albedo["h_scale"] = scale
    for name, fmt in (("ev_output_depth", "DEPTH_STENCIL"), ("ev_scaled_down", "R8G8B8A8")):
        t["global_resources"].append({"depends_on": "back_buffer", "format": fmt, "h_scale": 1, "hint_needs_clearing": False,
                                      "name": name, "type": "render_target", "w_scale": 1})
    for vp in ("overlay", "adjust_brightness"):
        t["viewports"][vp]["output_dst"] = "ev_output_depth"
    moved = 0
    for tree in (t["layer_configs"]["default"], t["resource_generators"]):
        for d in walk(tree):
            if d.get("depth_stencil_target") == "output_dst" and "output_rt" in (d.get("render_targets"), d.get("output")):
                d["depth_stencil_target"] = "ev_output_depth"
                moved += 1
    win = one((d for d in walk(t["layer_configs"]["default"])
               if d.get("type") == "static_branch" and d.get("platform") == "win"
               and any(p.get("name") == "fxaa" for p in d.get("pass", []))), "win branch with fxaa")
    wire = one((p for p in win["pass"] if p.get("name") == "wireframe"), "the win wireframe layer")
    assert wire["depth_stencil_target"] == "ev_output_depth" and win["pass"].index(wire) == 0
    wire["clear_flags"] = "DEPTH|STENCIL"
    # transparent_in_world (glass such as the ship's shield) draws over the finished picture and is hidden
    # by the scene's depth. ev_output_depth holds none of it, so the layer draws instead into ev_world, the
    # picture brought back up to render scale, with the scene's own depth buffer, and the result comes back
    # down to the screen (both ways through shaders/downsample.hlsl).
    t["global_resources"].append({"depends_on": "albedo", "format": "R8G8B8A8", "h_scale": 1, "hint_needs_clearing": False,
                                  "name": "ev_world", "type": "render_target", "w_scale": 1})
    for name, inp, out in (("ev_world_up", "output_rt", "ev_world"), ("ev_world_down", "ev_world", "output_rt")):
        t["resource_generators"][name] = {"modifiers": [fp("copy:ENCODE_AS_RGBM", inp, out)]}
    tiw = one((i for i, p in enumerate(win["pass"]) if p.get("name") == "transparent_in_world"), "transparent_in_world layer")
    win["pass"][tiw]["render_targets"] = "ev_world"
    win["pass"][tiw]["depth_stencil_target"] = "output_dst"
    win["pass"].insert(tiw + 1, {"depth_stencil_target": "ev_output_depth", "name": "ev_world_down", "profiling_scope": "transparent",
                                 "render_targets": "output_rt", "resource_generator": "ev_world_down"})
    win["pass"].insert(tiw, {"depth_stencil_target": "output_dst", "name": "ev_world_up", "profiling_scope": "transparent",
                             "render_targets": "ev_world", "resource_generator": "ev_world_up"})
    graded = []
    for d in walk(t["resource_generators"]["post_processing"]):
        for key in ("pass_modifiers", "fail_modifiers", "modifiers"):
            mods = d.get(key)
            if isinstance(mods, list):
                graded += [(mods, m) for m in mods if m.get("material") == "color_grading" and m.get("input") == "mask"]
    if len(graded) < 2 or moved < 6:
        raise SystemExit(f"render scale: {len(graded)} color grading passes, {moved} depth targets moved")
    for mods, m in graded:
        m["input"] = "ev_scaled_down"
        mods.insert(mods.index(m), fp("copy:ENCODE_AS_RGBM", "mask", "ev_scaled_down"))


def patch(tree, shadows, scale=None):
    t = copy.deepcopy(tree)

    ambient_light(t)

    # The sky fix.
    clear = one((d for d in walk(t["layer_configs"]["default"]) if d.get("name") == "clear_pass"), "clear pass")
    if "albedo" not in clear["render_targets"].split():
        clear["render_targets"] = "albedo normal " + clear["render_targets"]

    # Ambient occlusion, just before global lighting.
    mods = t["resource_generators"]["deferred_shading"]["modifiers"]
    at = one((i for i, m in enumerate(mods) if m.get("material") == "global_lighting"), "global lighting pass")
    mods.insert(at + 1, ao_all_branch())
    mods.insert(at, ssao_branch())

    # Sharpening, after FXAA where the platform is win.
    t["resource_generators"]["ev_sharpen"] = {"modifiers": [{
        "pass_modifiers": [{"depth_stencil_target": "output_dst", "input": "output_rt", "output": "output_rt",
                            "shader": "filter:GAUSSIAN_BLUR_13TAP", "type": "fullscreen_pass"}],
        "render_settings": {"debug_rendering": False, "ev_sharpen": True},
        "type": "dynamic_branch",
    }]}
    win = one((d for d in walk(t["layer_configs"]["default"])
               if d.get("type") == "static_branch" and d.get("platform") == "win"
               and any(p.get("name") == "fxaa" for p in d.get("pass", []))), "win branch with fxaa")
    fx = one((i for i, p in enumerate(win["pass"]) if p.get("name") == "fxaa"), "fxaa layer")
    win["pass"].insert(fx + 1, {"depth_stencil_target": "output_dst", "name": "ev_sharpen", "profiling_scope": "ev_sharpen",
                                "render_targets": "output_rt", "resource_generator": "ev_sharpen"})

    rs = t["render_settings"]
    rs["ssao_enabled"] = True
    rs["ev_ao_all"] = True
    rs["ev_sharpen"] = True
    rs["ev_ambient"] = True

    # Shadows: the win branch of the global resources holds the sun's map and the point lights' cube.
    size = SHADOWS[shadows]
    if size:
        sun = one((d for d in walk(t["global_resources"]) if d.get("size_from_render_setting") == "shadow_map_size"),
                  "shadow map sized by the setting")
        del sun["size_from_render_setting"]
        sun["width"] = sun["height"] = size
        cubes = [d for d in walk(t["global_resources"]) if d.get("name") == "shadow_map_cube"]
        if not cubes:
            raise SystemExit("no shadow cube for the point lights")
        for c in cubes:
            c["width"] = c["height"] = 2048
    if scale:
        render_scale(t, scale)
    return t


def shader_entry(name, source, defines=""):
    entry = shaderlib.load(BUNDLE, st.murmur64(name))
    blob = dxbc.compile(defines + source, name=name)
    out = shaderlib.replace_stage(entry, "ps", blob)
    check = [s for s in shaderlib.stages(out) if s[3] == "ps"][0][2]
    assert check == blob
    return out


def shadow_distances():
    """The planets' shading environments with the far end of the sun's shadow cascade pushed out. Each
    holds shadow_slice_depth_ranges, one (start, end) pair in view depth (tools/shading_env.py): 17.9 to
    21 m on the mission planets, so at full zoom-out the top of the screen has no sun shadow. The pair is
    rewritten in place; nothing else in the resource moves. Ranges under 15 m and over the new end stay as
    they are. Answers {level: [(bundle, name hash, bytes)]}, read from the game install."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import shading_env  # noqa: E402
    want = {}
    for p in sorted((ROOT / "internal" / "dump" / "shading_environment").glob("*.bin")):
        bundle, name = p.stem.split("_")
        want.setdefault(bundle, set()).add(int(name, 16))
    kind = st.murmur64("shading_environment")
    var = st.murmur32("shadow_slice_depth_ranges")
    out = {level: [] for level in SHADOW_DISTANCE}
    for bundle, names in sorted(want.items()):
        for k, n, datas in st.entries(st.CONTENTS / bundle):
            if k != kind or n not in names or not datas[0]:
                continue
            data = datas[0]
            _, _, count = struct.unpack_from("<IQI", data, 0)
            recs = [struct.unpack_from("<IIII", data, 16 + 16 * i) for i in range(count)]
            base = 16 + 16 * count + 12
            hit = [r for r in recs if r[2] == var]
            if len(hit) != 1 or hit[0][1] != 1:
                continue
            at = base + hit[0][3]
            start, end = struct.unpack_from("<2f", data, at)
            for level, far in SHADOW_DISTANCE.items():
                if 15.0 <= end < far:
                    patched = bytearray(data)
                    struct.pack_into("<2f", patched, at, start, far)
                    assert shading_env.parse(bytes(patched))[2] != shading_env.parse(data)[2]
                    out[level].append((bundle, n, bytes(patched)))
    return out


def bundles_with(name):
    """The bundles whose copy of a shader library entry was dumped (tools/survey.py)."""
    h = st.murmur64(name)
    return sorted(p.name.split("_")[0] for p in shaderlib.DUMP.glob(f"*_{h:016x}.bin"))


def soft_shadow_entry(bundle, name, source, radius, taps):
    """Every pixel stage of a shadow mask entry replaced by shaders/shadow_mask.hlsl, with the defines
    its own bindings call for."""
    entry = shaderlib.load(bundle, st.murmur64(name))
    for n in range(len(shaderlib.stages(entry))):
        _, _, blob, kind = shaderlib.stages(entry)[n]
        if kind != "ps":
            continue
        binds = shaderlib.bindings(blob)
        defines = f"#define SOFT_RADIUS {radius}\n#define SOFT_TAPS {taps}\n"
        names = [b.split(":")[0] for b in binds]
        defines += "".join(f"#define {d}\n" for d, word in (("STEREO", "nv_stereo_params"), ("SMOKE", "__tex_shadow_map_color"),
                                                          ("CLOUDS", "__tex_cloud_shadow_map")) if word in names)
        new = dxbc.compile(defines + source, name=f"{name}#{n}")
        if shaderlib.bindings(new) != binds:
            raise SystemExit(f"{name} stage {n}: bindings {shaderlib.bindings(new)} differ from the game's {binds}")
        entry = shaderlib.replace_nth(entry, n, new)
    return entry


def main():
    RES.mkdir(exist_ok=True)
    vanilla = st.find(BUNDLE, "render_config", int(BUNDLE, 16))
    tree = datatree.decode(vanilla)
    if datatree.encode(tree) != vanilla:
        raise SystemExit("the render config doesn't round-trip; the game may have changed")
    for old in RES.glob("render_config_*.bin"):
        old.unlink()
    written = []
    for shadows in SHADOWS:
        for scale_id, scale in RENDER_SCALE.items():
            data = datatree.encode(patch(tree, shadows, scale))
            back = datatree.decode(data)
            assert datatree.encode(back) == data
            name = f"render_config_{shadows}.bin" if not scale else f"render_config_{shadows}_scale{scale_id}.bin"
            (RES / name).write_bytes(data)
            written.append((name, {"shadows": shadows, "render_scale": scale_id}))
    print(f"render config: {len(vanilla)} -> {len(data)} bytes, {len(written)} variants")

    ssao = (ROOT / "shaders" / "ssao.hlsl").read_text()
    (RES / "ssao.shader_library").write_bytes(shader_entry(SSAO_SHADER, ssao))
    apply = (ROOT / "shaders" / "ao_apply.hlsl").read_text()
    (RES / "ao_apply.shader_library").write_bytes(shader_entry(AO_APPLY_SHADER, apply))
    for old in [*RES.glob("sharpen_*.shader_library"), *RES.glob("ambient_*.shader_library")]:
        old.unlink()
    sharpen = (ROOT / "shaders" / "sharpen.hlsl").read_text()
    for level, s in SHARPNESS.items():
        (RES / f"sharpen_{level}.shader_library").write_bytes(shader_entry(SHARPEN_SHADER, sharpen, f"#define SHARPNESS {s}\n"))
    combine = (ROOT / "shaders" / "ambient_combine.hlsl").read_text()
    extract = (ROOT / "shaders" / "ambient_extract.hlsl").read_text()
    for level, (darken, gain) in AMBIENT.items():
        (RES / f"ambient_combine_{level}.shader_library").write_bytes(
            shader_entry(AMBIENT_COMBINE_SHADER, combine, f"#define DARKEN {darken}\n"))
        (RES / f"ambient_extract_{level}.shader_library").write_bytes(
            shader_entry(AMBIENT_EXTRACT_SHADER, extract, f"#define GLOW_GAIN {gain}\n"))
    print("shaders: vo_ssao, sharpen x", len(SHARPNESS), "ambient light x", len(AMBIENT))
    mask = (ROOT / "shaders" / "shadow_mask.hlsl").read_text()
    soft = []
    for level, (radius, taps) in SOFT_SHADOWS.items():
        for name in SHADOW_MASKS:
            for bundle in bundles_with(name):
                file = f"{name.replace(':', '_')}_{level}_{bundle[:4]}.shader_library"
                (RES / file).write_bytes(soft_shadow_entry(bundle, name, mask, radius, taps))
                soft.append((name, bundle, file, level))
    print("shaders: soft shadow masks x", len(soft))
    envs = []
    folder = RES / "shadow_distance"
    for old in folder.glob("*/*.shading_environment"):
        old.unlink()
    for level, items in shadow_distances().items():
        (folder / level).mkdir(parents=True, exist_ok=True)
        for bundle, name, data in items:
            file = f"shadow_distance/{level}/{bundle}_{name:016x}.shading_environment"
            (RES / file).write_bytes(data)
            envs.append((bundle, name, file, level))
    print("shading environments:", {level: sum(e[3] == level for e in envs) for level in SHADOW_DISTANCE})
    down = (ROOT / "shaders" / "downsample.hlsl").read_text()
    (RES / "downsample.shader_library").write_bytes(shader_entry(DOWNSAMPLE_SHADER, down))
    scaled = []
    folder = RES / "render_scale"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.shader_library"):
        old.unlink()
    left_total = 0
    for p in sorted(shaderlib.DUMP.glob("*.bin")):
        bundle, name = p.stem.split("_")
        entry, patched, left = bbsize.patch_entry(p.read_bytes())
        left_total += left
        if patched:
            file = f"render_scale/{bundle}_{name}.shader_library"
            (RES / file).write_bytes(entry)
            scaled.append((bundle, name, file))
    print(f"render scale: downsample, {len(scaled)} entries reading back_buffer_size patched, {left_total} stages left")

    # The manifest's resources, written in place in obsidian.json.
    resources = []
    for name, when in written:
        resources.append({"type": "render_config", "name": f"#{BUNDLE}", "bundle": BUNDLE, "data": f"res/{name}", "when": when})
    resources.append({"type": "shader_library", "name": f"#{st.murmur64(SSAO_SHADER):016x}", "bundle": BUNDLE,
                      "data": "res/ssao.shader_library"})
    resources.append({"type": "shader_library", "name": f"#{st.murmur64(AO_APPLY_SHADER):016x}", "bundle": BUNDLE,
                      "data": "res/ao_apply.shader_library"})
    # Off ships the first strength too: the passes stay in the render config, behind their render
    # settings, and the ambient combine takes the place of the bloom's blend, which always runs.
    for choice in ("off", *SHARPNESS):
        level = choice if choice in SHARPNESS else next(iter(SHARPNESS))
        resources.append({"type": "shader_library", "name": f"#{st.murmur64(SHARPEN_SHADER):016x}", "bundle": BUNDLE,
                          "data": f"res/sharpen_{level}.shader_library", "when": {"sharpen": choice}})
    for choice in ("off", *AMBIENT):
        level = choice if choice in AMBIENT else next(iter(AMBIENT))
        for shader, part in ((AMBIENT_COMBINE_SHADER, "combine"), (AMBIENT_EXTRACT_SHADER, "extract")):
            resources.append({"type": "shader_library", "name": f"#{st.murmur64(shader):016x}", "bundle": BUNDLE,
                              "data": f"res/ambient_{part}_{level}.shader_library", "when": {"ambient": choice}})
    for name, bundle, file, level in soft:
        resources.append({"type": "shader_library", "name": f"#{st.murmur64(name):016x}", "bundle": bundle,
                          "data": f"res/{file}", "when": {"soft_shadows": level}})
    for bundle, name, file, level in envs:
        resources.append({"type": "shading_environment", "name": f"#{name:016x}", "bundle": bundle,
                          "data": f"res/{file}", "when": {"shadow_distance": level}})
    for scale_id, scale in RENDER_SCALE.items():
        if not scale:
            continue
        resources.append({"type": "shader_library", "name": f"#{st.murmur64(DOWNSAMPLE_SHADER):016x}", "bundle": BUNDLE,
                          "data": "res/downsample.shader_library", "when": {"render_scale": scale_id}})
        for bundle, name, file in scaled:
            resources.append({"type": "shader_library", "name": f"#{name}", "bundle": bundle, "data": f"res/{file}",
                              "when": {"render_scale": scale_id}})
    manifest_path = ROOT / "obsidian.json"
    text = manifest_path.read_text(encoding="utf-8")
    m = json.loads(text)
    m["resources"] = resources
    manifest_path.write_bytes(dump_manifest(m).encode("utf-8"))
    print("obsidian.json:", len(resources), "resources")


def dump_manifest(m):
    """The manifest with one line per option, keybind and resource, as the other mods keep theirs."""
    lines = ["{"]
    keys = list(m)
    for i, k in enumerate(keys):
        v = m[k]
        comma = "," if i < len(keys) - 1 else ""
        if isinstance(v, list) and v and isinstance(v[0], dict):
            lines.append(f"  {json.dumps(k)}: [")
            for j, item in enumerate(v):
                lines.append("    " + json.dumps(item, ensure_ascii=False) + ("," if j < len(v) - 1 else ""))
            lines.append("  ]" + comma)
        else:
            lines.append(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
