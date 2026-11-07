"""Surveys every bundle: counts resources by type and writes the render-side ones (render_config, shading
environments and their mappings, shader libraries) to internal/dump/<type>/<bundle>_<name>.bin.

Usage: python tools/survey.py
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stingray as st  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "internal" / "dump"
KEEP = {"render_config", "shading_environment", "shading_environment_mapping", "shader_library",
        "shader_library_group", "shader", "config"}


def main():
    counts, where = Counter(), {}
    for b in st.bundles():
        try:
            for kind, name, datas in st.entries(b):
                t = st.TYPE_NAMES.get(kind, f"#{kind:016x}")
                counts[t] += 1
                where.setdefault(t, set()).add(b.name)
                if t in KEEP:
                    d = OUT / t
                    d.mkdir(parents=True, exist_ok=True)
                    (d / f"{b.name}_{name:016x}.bin").write_bytes(datas[0])
        except Exception as e:  # a bundle this reader doesn't know
            print("skip", b.name, e)
    for t, c in counts.most_common():
        print(f"{c:7d}  {t:32s} {len(where[t])} bundles")


if __name__ == "__main__":
    main()
