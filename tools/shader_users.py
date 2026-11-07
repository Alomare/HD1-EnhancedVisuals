"""Which resources mention a shader by its name hash: every material, unit, level and shader library in
every bundle is searched for the 64-bit hash (and the 32-bit top half, materials keep some names short).

Usage: python tools/shader_users.py <shader name> [...]
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stingray as st  # noqa: E402

KINDS = {st.murmur64(k): k for k in ("material", "unit", "level", "particles", "shader_library", "shader_library_group",
                                     "render_config", "lua")}


def main():
    names = sys.argv[1:]
    want = {n: (struct.pack("<Q", st.murmur64(n.lower())), struct.pack("<I", st.murmur32(n.lower()))) for n in names}
    hits = {n: [] for n in names}
    for b in st.bundles():
        try:
            for kind, name, datas in st.entries(b):
                if kind not in KINDS:
                    continue
                for n, (h64, h32) in want.items():
                    if h64 in datas[0] or (KINDS[kind] == "material" and h32 in datas[0]):
                        hits[n].append(f"{b.name}:{KINDS[kind]}:{name:016x}")
        except Exception as e:
            print("skip", b.name, e)
    for n, h in hits.items():
        print(n, len(h), h[:12])


if __name__ == "__main__":
    main()
