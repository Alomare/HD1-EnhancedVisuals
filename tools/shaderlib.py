"""Shader library entries: one shader of the game, as the engine loads it.

An entry (version 0x35) is a header (the shader's id, the names of its constant buffers, textures and
global buffers with their places, the variables in each buffer by name and offset, render state, sampler
state), then each compiled stage as a u32 size and a DXBC container, each followed by a table of the
buffers and resources that stage binds to which slots. Nothing points past the header by offset, so a
stage can be swapped for one of another size as long as it binds the same slots and keeps the variables
where the tables say they are.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dxbc  # noqa: E402

DUMP = Path(__file__).resolve().parent.parent / "internal" / "dump" / "shader_library"


def stages(entry):
    """(offset of the size field, offset of the DXBC, its bytes, 'vs' or 'ps') for each stage."""
    out = []
    for off, blob in dxbc.blobs(entry):
        size = struct.unpack_from("<I", entry, off - 4)[0]
        if size != len(blob):
            raise ValueError(f"stage at {off:#x}: size field {size} but the container says {len(blob)}")
        kind = dxbc.disassemble(blob).lstrip(";/ \n").split("\n")
        kind = next((line[:2] for line in kind if line[:3] in ("vs_", "ps_")), "?")
        out.append((off - 4, off, blob, kind))
    return out


def replace_stage(entry, kind, blob):
    """The entry with its one stage of `kind` ('ps' or 'vs') replaced by `blob`."""
    found = [s for s in stages(entry) if s[3] == kind]
    if len(found) != 1:
        raise ValueError(f"expected one {kind} stage, found {len(found)}")
    at, off, old, _ = found[0]
    return entry[:at] + struct.pack("<I", len(blob)) + blob + entry[off + len(old):]


def replace_nth(entry, n, blob):
    """The entry with its n-th stage (0-based, in file order, any kind) replaced by `blob`. Entries with
    several permutations hold one vs and ps pair each."""
    at, off, old, _ = stages(entry)[n]
    return entry[:at] + struct.pack("<I", len(blob)) + blob + entry[off + len(old):]


def bindings(blob):
    """What a stage binds, from its reflection data, as name:slot in the order the disassembly lists them."""
    out = []
    for line in dxbc.disassemble(blob).splitlines():
        parts = line.split()
        if line.startswith("// ") and len(parts) >= 7 and parts[2] in ("texture", "sampler", "sampler_c", "cbuffer"):
            out.append(f"{parts[1]}:{parts[-2]}")
    return out


def load(bundle, name_hash):
    return (DUMP / f"{bundle}_{name_hash:016x}.bin").read_bytes()
