"""A Direct3D 11 device on WARP (the software rasterizer every Windows 10 has), driven through ctypes, to
run pixel shaders over images: the mod's own and the game's, straight from their DXBC.

Draws one full-screen triangle with a vertex shader of its own that hands the pixel shader what the
game's full-screen vertex shader hands it: SV_POSITION and TEXCOORD0 (0..1, y down).

    dev = Device()
    out = dev.run(ps_dxbc, (w, h), textures=[array, ...], cbuffers=[bytes, ...])   # float32 HxWx4
"""
import ctypes
import struct
from ctypes import POINTER, byref, c_float, c_uint, c_void_p, wintypes

import numpy as np

import dxbc

_d3d11 = ctypes.WinDLL("d3d11.dll")

FORMATS = {  # numpy layout -> DXGI format
    ("float32", 1): 41,   # R32_FLOAT
    ("float32", 2): 16,   # R32G32_FLOAT
    ("float32", 4): 2,    # R32G32B32A32_FLOAT
    ("float16", 2): 34,   # R16G16_FLOAT
    ("uint8", 4): 28,     # R8G8B8A8_UNORM
}

VS = """
struct VS_OUT { float4 position : SV_POSITION; float2 uv : TEXCOORD0; };
VS_OUT vs_main(uint id : SV_VertexID)
{
	VS_OUT o;
	float2 t = float2((id << 1) & 2, id & 2);
	o.uv = t;
	o.position = float4(t.x * 2.0 - 1.0, 1.0 - t.y * 2.0, 0.0, 1.0);
	return o;
}
"""


class _TEX2D(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint), ("ArraySize", c_uint),
                ("Format", c_uint), ("Count", c_uint), ("Quality", c_uint), ("Usage", c_uint),
                ("BindFlags", c_uint), ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]


class _DATA(ctypes.Structure):
    _fields_ = [("pSysMem", c_void_p), ("SysMemPitch", c_uint), ("SysMemSlicePitch", c_uint)]


class _BUF(ctypes.Structure):
    _fields_ = [("ByteWidth", c_uint), ("Usage", c_uint), ("BindFlags", c_uint), ("CPUAccessFlags", c_uint),
                ("MiscFlags", c_uint), ("StructureByteStride", c_uint)]


class _VIEWPORT(ctypes.Structure):
    _fields_ = [(n, c_float) for n in ("X", "Y", "W", "H", "MinZ", "MaxZ")]


class _SAMPLER(ctypes.Structure):
    _fields_ = [("Filter", c_uint), ("U", c_uint), ("V", c_uint), ("W", c_uint), ("Bias", c_float),
                ("MaxAniso", c_uint), ("Cmp", c_uint), ("Border", c_float * 4), ("MinLOD", c_float), ("MaxLOD", c_float)]


class _MAPPED(ctypes.Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


def _method(obj, index, restype, *argtypes):
    vtable = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p))[0], POINTER(c_void_p))
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtable[index])


def _check(hr, what):
    if hr != 0:
        raise RuntimeError(f"{what} failed: {hr & 0xffffffff:#010x}")


class Device:
    def __init__(self):
        dev, ctx, level = c_void_p(), c_void_p(), c_uint()
        _d3d11.D3D11CreateDevice.argtypes = [c_void_p, c_uint, c_void_p, c_uint, c_void_p, c_uint, c_uint,
                                             POINTER(c_void_p), POINTER(c_uint), POINTER(c_void_p)]
        _check(_d3d11.D3D11CreateDevice(None, 5, None, 0, None, 0, 7, byref(dev), byref(level), byref(ctx)),
               "D3D11CreateDevice (WARP)")
        self.dev, self.ctx = dev, ctx
        vs = dxbc.compile(VS, entry="vs_main", target="vs_4_0", name="warp_vs.hlsl")
        self.vs = self._shader(12, vs)
        desc = _SAMPLER(0x15, 3, 3, 3, 0.0, 1, 0, (c_float * 4)(), 0.0, 3.4e38)   # linear, clamp
        self.linear = c_void_p()
        _check(_method(dev, 23, ctypes.HRESULT, POINTER(_SAMPLER), POINTER(c_void_p))(dev, byref(desc), byref(self.linear)),
               "CreateSamplerState")
        desc.Filter = 0
        desc.U = desc.V = desc.W = 1   # point, wrap
        self.point_wrap = c_void_p()
        _check(_method(dev, 23, ctypes.HRESULT, POINTER(_SAMPLER), POINTER(c_void_p))(dev, byref(desc), byref(self.point_wrap)),
               "CreateSamplerState")
        desc = _SAMPLER(0x95, 3, 3, 3, 0.0, 1, 4, (c_float * 4)(), 0.0, 3.4e38)   # comparison linear, less-equal, clamp
        self.comparison = c_void_p()
        _check(_method(dev, 23, ctypes.HRESULT, POINTER(_SAMPLER), POINTER(c_void_p))(dev, byref(desc), byref(self.comparison)),
               "CreateSamplerState")
        self._vs_cache = {}

    def vertex_shader(self, source):
        """A vertex shader of the caller's (HLSL, entry vs_main, SV_VertexID 0..2 as VS above), for pixel
        shaders that read more than TEXCOORD0."""
        if source not in self._vs_cache:
            self._vs_cache[source] = self._shader(12, dxbc.compile(source, entry="vs_main", target="vs_4_0", name="warp_vs2.hlsl"))
        return self._vs_cache[source]

    def _shader(self, index, blob):
        out = c_void_p()
        buf = ctypes.create_string_buffer(blob, len(blob))
        _check(_method(self.dev, index, ctypes.HRESULT, c_void_p, ctypes.c_size_t, c_void_p, POINTER(c_void_p))(
            self.dev, buf, len(blob), None, byref(out)), "CreatePixelShader" if index == 15 else "CreateVertexShader")
        return out

    def _texture(self, array, bind=0x8, usage=0, cpu=0):
        a = np.ascontiguousarray(array)
        h, w = a.shape[:2]
        comps = 1 if a.ndim == 2 else a.shape[2]
        fmt = FORMATS[(str(a.dtype), comps)]
        desc = _TEX2D(w, h, 1, 1, fmt, 1, 0, usage, bind, cpu, 0)
        data = _DATA(a.ctypes.data, a.strides[0], 0)
        tex = c_void_p()
        _check(_method(self.dev, 5, ctypes.HRESULT, POINTER(_TEX2D), POINTER(_DATA), POINTER(c_void_p))(
            self.dev, byref(desc), byref(data) if usage != 3 else None, byref(tex)), "CreateTexture2D")
        return tex

    def run(self, ps_blob, size, textures=(), cbuffers=(), samplers=None, vs=None):
        """Runs a pixel shader over a target of size (w, h); answers its picture as float32 HxWx4. vs: an
        HLSL vertex shader source in place of the built-in one."""
        w, h = size
        ps = self._shader(15, ps_blob)
        srvs = []
        for t in textures:
            tex = self._texture(t)
            srv = c_void_p()
            _check(_method(self.dev, 7, ctypes.HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(self.dev, tex, None, byref(srv)),
                   "CreateShaderResourceView")
            srvs.append(srv)
        cbs = []
        for raw in cbuffers:
            raw = bytes(raw) + bytes(-len(raw) % 16)
            desc = _BUF(len(raw), 0, 0x4, 0, 0, 0)
            buf = ctypes.create_string_buffer(raw, len(raw))
            data = _DATA(ctypes.cast(buf, c_void_p), 0, 0)
            cb = c_void_p()
            _check(_method(self.dev, 3, ctypes.HRESULT, POINTER(_BUF), POINTER(_DATA), POINTER(c_void_p))(
                self.dev, byref(desc), byref(data), byref(cb)), "CreateBuffer")
            cbs.append(cb)
        target = self._texture(np.zeros((h, w, 4), np.float32), bind=0x20)
        rtv = c_void_p()
        _check(_method(self.dev, 9, ctypes.HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(self.dev, target, None, byref(rtv)),
               "CreateRenderTargetView")
        ctx = self.ctx
        _method(ctx, 33, None, c_uint, POINTER(c_void_p), c_void_p)(ctx, 1, byref(rtv), None)
        vp = _VIEWPORT(0, 0, w, h, 0, 1)
        _method(ctx, 44, None, c_uint, POINTER(_VIEWPORT))(ctx, 1, byref(vp))
        _method(ctx, 17, None, c_void_p)(ctx, None)
        _method(ctx, 24, None, c_uint)(ctx, 4)
        _method(ctx, 11, None, c_void_p, c_void_p, c_uint)(ctx, self.vertex_shader(vs) if vs else self.vs, None, 0)
        _method(ctx, 9, None, c_void_p, c_void_p, c_uint)(ctx, ps, None, 0)
        if srvs:
            arr = (c_void_p * len(srvs))(*srvs)
            _method(ctx, 8, None, c_uint, c_uint, c_void_p)(ctx, 0, len(srvs), arr)
        smp = samplers or [self.linear] * max(1, len(srvs))
        arr = (c_void_p * len(smp))(*smp)
        _method(ctx, 10, None, c_uint, c_uint, c_void_p)(ctx, 0, len(smp), arr)
        if cbs:
            arr = (c_void_p * len(cbs))(*cbs)
            _method(ctx, 16, None, c_uint, c_uint, c_void_p)(ctx, 0, len(cbs), arr)
        _method(ctx, 13, None, c_uint, c_uint)(ctx, 3, 0)
        staging = self._texture(np.zeros((h, w, 4), np.float32), bind=0, usage=3, cpu=0x20000)
        _method(ctx, 47, None, c_void_p, c_void_p)(ctx, staging, target)
        m = _MAPPED()
        _check(_method(ctx, 14, ctypes.HRESULT, c_void_p, c_uint, c_uint, c_uint, POINTER(_MAPPED))(
            ctx, staging, 0, 1, 0, byref(m)), "Map")
        rows = np.ctypeslib.as_array(ctypes.cast(m.pData, POINTER(ctypes.c_ubyte)), shape=(h * m.RowPitch,))
        out = np.frombuffer(rows.tobytes(), np.float32).reshape(h, m.RowPitch // 4)[:, :w * 4].reshape(h, w, 4).copy()
        _method(ctx, 15, None, c_void_p, c_uint)(ctx, staging, 0)
        return out
