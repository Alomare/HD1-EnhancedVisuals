"""Render scale: pixel shaders that read back_buffer_size, patched to read the size of a scene texture.

33 of the game's pixel shaders turn the screen position into a texture coordinate, or work out a texel,
by dividing by back_buffer_size from the global_viewport constants: div r.xy, v0.xyxx, cb0[18].zwzz, or
div r.xy, l(1, 1, 1, 1), cb0[18].zwzz. With the scene drawn larger than the back buffer, and the viewport
still drawing into the back buffer, that value stays the back buffer's whichever way the engine fills it,
and these shaders would read the wrong place: decals, water, the reflections pass, soft particles.

The patch asks the scene texture the stage already samples (depth, light_accumulation, normal, albedo or
mask) for its size once, at the top (resinfo into a new temp), and points every operand that read
back_buffer_size (register 18, z and w, of the global_viewport slot) at that temp, swizzle for swizzle.
At scale 1 the scene textures are the back buffer's size, so the result is the same as the game's.

The container is re-signed with the DXBC checksum (a modified MD5, as vkd3d computes it), which
`checksum` reproduces for every shader of the game before anything is patched (tools/check.py).
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dxbc  # noqa: E402
import shaderlib  # noqa: E402

SCENE = ("__tex_depth", "__tex_light_accumulation", "__tex_normal", "__tex_albedo", "__tex_mask")
OP_DIV, OP_RESINFO, OP_CUSTOMDATA, OP_DCL_TEMPS = 14, 61, 53, 104
DECLARATIONS = set(range(88, 107)) | set(range(143, 184)) | {OP_CUSTOMDATA}


# The DXBC checksum: MD5 over the container past its first 20 bytes, with its own last block.
_S = [7, 12, 17, 22] * 4 + [5, 9, 14, 20] * 4 + [4, 11, 16, 23] * 4 + [6, 10, 15, 21] * 4
_K = [int(abs(__import__("math").sin(i + 1)) * 2 ** 32) & 0xFFFFFFFF for i in range(64)]


def _transform(state, block):
    x = struct.unpack("<16I", block)
    a, b, c, d = state
    for i in range(64):
        if i < 16:
            f, g = (b & c) | (~b & d), i
        elif i < 32:
            f, g = (d & b) | (~d & c), (5 * i + 1) % 16
        elif i < 48:
            f, g = b ^ c ^ d, (3 * i + 5) % 16
        else:
            f, g = c ^ (b | ~d), (7 * i) % 16
        f = (f + a + _K[i] + x[g]) & 0xFFFFFFFF
        a, d, c = d, c, b
        b = (b + ((f << _S[i]) | (f >> (32 - _S[i])))) & 0xFFFFFFFF
    return [(s + v) & 0xFFFFFFFF for s, v in zip(state, (a, b, c, d))]


def checksum(container):
    data = container[20:]
    state = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]
    full = len(data) - len(data) % 64
    for i in range(0, full, 64):
        state = _transform(state, data[i:i + 64])
    rest = data[full:]
    bits = (len(data) * 8) & 0xFFFFFFFF
    tail = bytearray(rest) + b"\x80"
    if len(tail) > 56:
        tail += bytes(64 - len(tail))
        state = _transform(state, bytes(tail))
        block = bytearray(64)
    else:
        block = bytearray(4) + tail + bytes(60 - 4 - len(tail))
    block[0:4] = struct.pack("<I", bits)
    block[60:64] = struct.pack("<I", (bits >> 2) | 1)
    state = _transform(state, bytes(block))
    return struct.pack("<4I", *state)


def _chunks(blob):
    count = struct.unpack_from("<I", blob, 28)[0]
    offs = struct.unpack_from(f"<{count}I", blob, 32)
    return [(blob[o:o + 4], o, struct.unpack_from("<I", blob, o + 4)[0]) for o in offs]


def _operand_len(code, at):
    """Dwords taken by the operand at `at` (token, extended tokens, indices, immediates), or None."""
    start, tok = at, code[at]
    at += 1
    ext = tok >> 31
    while ext:
        ext = code[at] >> 31
        at += 1
    kind, comps = (tok >> 12) & 0xFF, tok & 3
    if kind == 4:      # immediate32
        at += 1 if comps == 1 else 4
    elif kind == 5:    # immediate64
        at += 2 if comps == 1 else 8
    for d in range((tok >> 20) & 3):
        rep = (tok >> (22 + 3 * d)) & 7
        if rep > 4:
            return None
        at += {0: 1, 1: 2, 2: 0, 3: 1, 4: 2}[rep]
        if rep in (2, 3, 4):     # a relative index: a whole operand of its own
            inner = _operand_len(code, at)
            if inner is None:
                return None
            at += inner
    return at - start


def _operands(code, at, end):
    """(start, length) of each operand from `at` to `end` (dword indices), or None where unparsable."""
    out = []
    while at < end:
        n = _operand_len(code, at)
        if not n:
            return None
        out.append((at, n))
        at += n
    return out if at == end else None


def _temp(index, swizzle):
    """A 4-component temp operand with a swizzle (list of 4 component numbers)."""
    sel = sum(c << (2 * i) for i, c in enumerate(swizzle))
    return [2 | (1 << 2) | (sel << 4) | (0 << 12) | (1 << 20), index]


def patch_stage(blob, gv_slot, tex_slot):
    """The stage with back_buffer_size read from texture slot tex_slot's size, or None if the stage has
    nothing to patch or holds something this patch doesn't parse."""
    chunks = _chunks(blob)
    shdr = next(((tag, off, size) for tag, off, size in chunks if tag in (b"SHDR", b"SHEX")), None)
    if not shdr:
        return None
    tag, off, size = shdr
    code = list(struct.unpack_from(f"<{size // 4}I", blob, off + 8))
    length = code[1]
    instrs, i = [], 2
    while i < length:
        op = code[i] & 0x7FF
        n = code[i + 1] if op == OP_CUSTOMDATA else (code[i] >> 24) & 0x7F
        if n == 0:
            return None
        instrs.append((i, n, op))
        i += n
    if i != length:
        return None
    temps = next((s for s, n, op in instrs if op == OP_DCL_TEMPS), None)
    if temps is None:
        return None
    t = code[temps + 1]
    last_decl = max(s + n for s, n, op in instrs if op in DECLARATIONS)
    new_code, hits = code[:2], 0
    for s, n, op in instrs:
        body = code[s:s + n]
        if op not in DECLARATIONS:
            skip = 1
            while body[skip - 1] >> 31 and skip < n:
                skip += 1
            ops = _operands(code, s + skip, s + n)
            if ops is None:
                return None
            rebuilt = body[:skip]
            for o, ln in ops:
                tok = code[o]
                if ((tok >> 12) & 0xFF) == 8 and ((tok >> 20) & 3) == 2 and ln == 3 and not tok >> 31 \
                        and ((tok >> 22) & 0x3F) == 0 and code[o + 1] == gv_slot and code[o + 2] == 18:
                    mode = (tok >> 2) & 3
                    comps = [(tok >> (4 + 2 * k)) & 3 for k in range(4)] if mode == 1 else None
                    if mode == 2:
                        comps = [(tok >> 4) & 3] * 4
                    if comps and all(c in (2, 3) for c in comps):
                        if mode != 1 or op != OP_DIV:
                            return None
                        rebuilt += _temp(t, [c - 2 for c in comps])
                        hits += 1
                        continue
                    if comps and any(c in (2, 3) for c in comps):
                        return None
                rebuilt += code[o:o + ln]
            rebuilt[0] = (rebuilt[0] & ~(0x7F << 24)) | (len(rebuilt) << 24)
            body = rebuilt
        if op == OP_DCL_TEMPS:
            body = [body[0], t + 1]
        new_code += body
        if s + n == last_decl:
            # resinfo r<t>.xyzw, l(0), t<tex_slot>.xyzw
            dest = [2 | (0 << 2) | (0xF << 4) | (0 << 12) | (1 << 20), t]
            mip = [1 | (4 << 12), 0]
            res = [2 | (1 << 2) | (0xE4 << 4) | (7 << 12) | (1 << 20), tex_slot]
            ins = dest + mip + res
            new_code += [OP_RESINFO | ((1 + len(ins)) << 24)] + ins
    if not hits:
        return None
    new_code[1] = len(new_code)
    new_shdr = tag + struct.pack("<I", len(new_code) * 4) + struct.pack(f"<{len(new_code)}I", *new_code)
    old_end = off + 8 + size
    grow = len(new_shdr) - (8 + size)
    out = bytearray(blob[:off] + new_shdr + blob[old_end:])
    count = struct.unpack_from("<I", out, 28)[0]
    for k in range(count):
        o = struct.unpack_from("<I", out, 32 + 4 * k)[0]
        if o > off:
            struct.pack_into("<I", out, 32 + 4 * k, o + grow)
    struct.pack_into("<I", out, 24, len(out))
    out[4:20] = checksum(bytes(out))
    return bytes(out)


def reads_back_buffer_size(blob):
    return any("back_buffer_size" in l and "[unused]" not in l for l in dxbc.disassemble(blob).splitlines() if l.startswith("//"))


def patch_entry(entry):
    """The entry with each pixel stage that reads back_buffer_size patched; answers (entry, patched count,
    stages left as they were)."""
    patched, left = 0, 0
    n = 0
    while n < len(shaderlib.stages(entry)):
        _, _, blob, kind = shaderlib.stages(entry)[n]
        if kind == "ps" and reads_back_buffer_size(blob):
            binds = dict(b.split(":") for b in shaderlib.bindings(blob))
            gv = binds.get("global_viewport")
            tex = next((binds[name] for name in SCENE if name in binds), None)
            new = patch_stage(blob, int(gv[2:]), int(tex[1:])) if gv and tex else None
            if new:
                entry = shaderlib.replace_nth(entry, n, new)
                patched += 1
            else:
                left += 1
        n += 1
    return entry, patched, left
