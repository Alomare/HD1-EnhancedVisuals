"""Finds the DX11 shader blobs (DXBC) inside a shader library resource and disassembles them with the
d3dcompiler_47.dll that ships with Windows 10.

Usage: python tools/dxbc.py <shader library .bin | shader name> [...]
A name is looked up in the core rendering bundle (1a9c33dcd0301dfd) by murmur64 of its lowercase form.
"""
import ctypes
import struct
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stingray as st  # noqa: E402

DUMP = Path(__file__).resolve().parent.parent / "internal" / "dump"
_d3d = ctypes.WinDLL("d3dcompiler_47.dll")


class _Blob(ctypes.Structure):
    pass


_BlobP = ctypes.POINTER(_Blob)
_d3d.D3DDisassemble.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.UINT, ctypes.c_char_p,
                                ctypes.POINTER(ctypes.c_void_p)]


def _blob_bytes(p):
    # ID3DBlob: vtable[3] GetBufferPointer, vtable[4] GetBufferSize, vtable[2] Release
    vt = ctypes.cast(ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    get_ptr = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vt[3])
    get_size = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vt[4])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])
    out = ctypes.string_at(get_ptr(p), get_size(p))
    release(p)
    return out


def disassemble(blob):
    out = ctypes.c_void_p()
    buf = ctypes.create_string_buffer(blob, len(blob))
    hr = _d3d.D3DDisassemble(buf, len(blob), 0, None, ctypes.byref(out))
    if hr != 0:
        return f"; D3DDisassemble failed {hr & 0xffffffff:#x}"
    return _blob_bytes(out).decode("latin-1").rstrip("\0")


_d3d.D3DCompile.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_char_p, ctypes.c_char_p, wintypes.UINT, wintypes.UINT,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
D3DCOMPILE_OPTIMIZATION_LEVEL3 = 1 << 15


def compile(source, entry="ps_main", target="ps_4_0", name="shader.hlsl"):
    """HLSL to DXBC, as the game's own shaders were built (shader model 4.0)."""
    src = source.encode()
    code, errors = ctypes.c_void_p(), ctypes.c_void_p()
    hr = _d3d.D3DCompile(src, len(src), name.encode(), None, None, entry.encode(), target.encode(),
                         D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, ctypes.byref(code), ctypes.byref(errors))
    msg = _blob_bytes(errors).decode("latin-1") if errors.value else ""
    if hr != 0:
        raise SystemExit(f"{name}: D3DCompile failed {hr & 0xffffffff:#x}\n{msg}")
    if msg.strip():
        print(msg.strip())
    return _blob_bytes(code)


def blobs(data):
    """(offset, bytes) of each DXBC container: magic, 16-byte checksum, u32 1, u32 total size."""
    i = 0
    while True:
        i = data.find(b"DXBC", i)
        if i < 0:
            return
        size = struct.unpack_from("<I", data, i + 24)[0]
        if 32 < size <= len(data) - i:
            yield i, data[i:i + size]
            i += size
        else:
            i += 4


def load(arg):
    p = Path(arg)
    if p.exists():
        return p.read_bytes()
    f = DUMP / "shader_library" / f"1a9c33dcd0301dfd_{st.murmur64(arg.lower()):016x}.bin"
    return f.read_bytes()


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        data = load(arg)
        for off, b in blobs(data):
            print(f";;;;;;;; {arg} @ {off:#x} ({len(b)} bytes)")
            print(disassemble(b))
