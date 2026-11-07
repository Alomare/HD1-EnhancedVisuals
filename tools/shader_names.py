"""Names the shaders in each shader library group by guessing: a library entry is named by the murmur64 of
its shader's name, and a group lists the hashes of its shaders (u32 version, u32 count, u64 each).

Candidates are identifiers from the exe, the render configs, the shader libraries' own strings and the game's
Lua, alone and joined in pairs and threes with "_". Writes internal/research/shader_names.txt.

Usage: python tools/shader_names.py
"""
import glob
import itertools
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stingray as st  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "internal" / "dump"
LIFTED = ROOT.parent.parent / "ProjectObsidian" / "ObsidianModManager" / "internal" / "research" / "lifted"
EXE = st.CONTENTS.parent / "binaries" / "x64" / "helldivers.exe"
GROUPS = {"water", "particle_billboard", "decals", "color_grading", "terrain", "health_3d", "default_shaders",
          "deferred_shading", "post_processing", "base", "additive", "war_front", "fxaa"}

WORDS = """ssao vo ao ambient occlusion obscurance volumetric blur bilateral upsample downsample apply resolve combine
compute pass source mask depth normal half quarter linear linearize filter gaussian box separable x y h v
horizontal vertical near far coc dof bloom bright brightpass threshold luminance adapt adaptation exposure
tone mapping tonemap color grading lut copy blit fullscreen output final composite merge sharpen sharpening
noise dither vignette grain chromatic aberration lens flare streak godrays rays sun shafts light scatter
fog volume volumes apply cloud clouds shadow shadows mask fill global lighting deferred omni spot proxy
cutter skydome sky sun water foam refraction reflection reflections cubemap ssr motion velocity taa temporal
history jitter fxaa mlaa smaa edge edges detect detection weights neighborhood blending area search
checkerboard debug visualization gbuffer encode decode rgbm hdr ldr distortion particles heat haze
selection outline silhouette health war front map overlay gui""".split()


def identifiers():
    words = set(WORDS)
    sources = [EXE] + [Path(p) for p in glob.glob(str(DUMP / "render_config" / "*.bin"))]
    sources += [Path(p) for p in glob.glob(str(DUMP / "shader_library" / "1a9c33dcd0301dfd_*.bin"))]
    for src in sources:
        words |= {m.group().decode() for m in re.finditer(rb"[A-Za-z_][A-Za-z0-9_:]{1,63}", src.read_bytes())}
    for lua in LIFTED.rglob("*.lua"):
        words |= set(re.findall(r'"([A-Za-z_][A-Za-z0-9_:]{1,63})"', lua.read_text(errors="ignore")))
    return words


def groups():
    out = {}
    names = {st.murmur64("core/rendering/" + g): "core/rendering/" + g for g in GROUPS}
    for f in sorted((DUMP / "shader_library_group").glob("1a9c33dcd0301dfd_*.bin")):
        d = f.read_bytes()
        if len(d) < 8:
            continue
        _, count = struct.unpack_from("<II", d, 0)
        out[names.get(int(f.stem.split("_")[1], 16), f.stem)] = list(struct.unpack_from(f"<{count}Q", d, 8))
    return out


def main():
    gs = groups()
    wanted = {h for hs in gs.values() for h in hs}
    words = identifiers()
    found = {}

    def test(name):
        h = st.murmur64(name)
        if h in wanted and h not in found:
            found[h] = name

    for w in words:
        test(w)
        test(w.lower())
    base = sorted({w.lower() for w in WORDS} | {w for w in words if w.islower() and len(w) <= 14 and "_" not in w})
    small = sorted(set(WORDS))
    for a, b in itertools.product(base, base):
        test(f"{a}_{b}")
    for a, b, c in itertools.product(small, small, small):
        test(f"{a}_{b}_{c}")
    lines = []
    for g, hs in gs.items():
        lines.append(f"== {g} ({len(hs)})")
        for h in hs:
            lines.append(f"  {h:016x} {found.get(h, '?')}")
    (ROOT / "internal" / "research" / "shader_names.txt").write_text("\n".join(lines) + "\n")
    print(f"named {len(found)} of {len(wanted)}")


if __name__ == "__main__":
    main()
