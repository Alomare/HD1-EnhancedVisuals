"""Reads shading environments: the per-level lighting values (fog, sun, exposure, bloom, shadows...).

Layout (version 8): u32 version, u64 template, u32 count, then count x (u32 type, u32 count, u32 name, u32
offset), then u64 ?, u32 data size, the data. Name is murmur32 (top half of murmur64) of the variable's name.
Types seen: 0 scalar, 1 vector2, 2 vector3, 10 vector4, 6 resource (u64), 4 64 bytes (an array).

Usage: python tools/shading_env.py [file...]   names come from identifiers in the dumped shader libraries.
"""
import glob
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stingray as st  # noqa: E402

DUMP = Path(__file__).resolve().parent.parent / "internal" / "dump"
SIZES = {0: 4, 1: 8, 2: 12, 10: 16, 6: 8}


def names():
    words = set()
    for f in glob.glob(str(DUMP / "shader_library" / "*.bin")) + glob.glob(str(DUMP / "render_config" / "*.bin")):
        words |= {m.group().decode() for m in re.finditer(rb"[A-Za-z_][A-Za-z0-9_]{2,63}", Path(f).read_bytes())}
    words |= {"shadow_slice_depth_ranges", "color_grading_map", "ssao_radius", "emissive_particle_intensity"}
    return {st.murmur32(w): w for w in words}


def parse(data):
    version, template, count = struct.unpack_from("<IQI", data, 0)
    vars_ = [struct.unpack_from("<IIII", data, 16 + 16 * i) for i in range(count)]
    base = 16 + 16 * count
    _, size = struct.unpack_from("<QI", data, base)
    blob = data[base + 12: base + 12 + size]
    out = []
    for kind, n, name, off in vars_:
        if kind in (0, 1, 2, 10):
            k = SIZES[kind] // 4
            val = list(struct.unpack_from(f"<{k * n}f", blob, off))
        elif kind == 6:
            val = f"{struct.unpack_from('<Q', blob, off)[0]:016x}"
        else:
            val = blob[off:off + 64].hex()
        out.append((kind, n, name, val))
    return version, template, out, base + 12 + size


if __name__ == "__main__":
    table = names()
    files = sys.argv[1:] or sorted(glob.glob(str(DUMP / "shading_environment" / "*.bin")))[:1]
    for f in files:
        d = Path(f).read_bytes()
        if not d:
            continue
        v, t, vs, end = parse(d)
        print(f"== {Path(f).name} v{v} template {t:016x} {len(vs)} vars, table ends {end} of {len(d)}")
        for kind, n, name, val in vs:
            print(f"  {kind:2d} {n} {table.get(name, f'#{name:08x}'):40s} {val}")
