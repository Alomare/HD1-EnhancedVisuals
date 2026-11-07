"""The compiled data tree Bitsquid uses for render configs and other SJSON resources.

Layout, from the main render config: the root is (type, offset). A value is a type and a u32 that is either
the value itself or an offset from the start of the resource:
  0 nil, 1 bool (inline), 2 integer (inline i32), 3 number (inline f32), 4 string (offset to a NUL-terminated string),
  5 array (offset to: element type, count, then count u32s, each an inline value or an offset),
  6 object (offset to: count, then count x (key offset, type, value)).
Strings are padded to four bytes, each written once. Object keys are in byte order. decode() gives Python
values; encode() writes them back in the same layout, so a config can be changed as a tree.
"""
import json
import struct
import sys


class Raw:
    """A value of a type this module doesn't know, kept as (type, u32)."""
    def __init__(self, kind, word):
        self.kind, self.word = kind, word

    def __repr__(self):
        return f"Raw({self.kind}, {self.word:#x})"


def _str(data, off):
    end = data.index(b"\0", off)
    return data[off:end].decode("latin-1")


def _value(data, kind, word):
    if kind == 0:
        return None
    if kind == 1:
        return bool(word)
    if kind == 2:
        return struct.unpack("<i", struct.pack("<I", word))[0]
    if kind == 3:
        return struct.unpack("<f", struct.pack("<I", word))[0]
    if kind == 4:
        return _str(data, word)
    if kind == 5:
        etype, count = struct.unpack_from("<II", data, word)
        words = struct.unpack_from(f"<{count}I", data, word + 8)
        return [_value(data, etype, w) for w in words]
    if kind == 6:
        (count,) = struct.unpack_from("<I", data, word)
        out = {}
        for i in range(count):
            k, t, w = struct.unpack_from("<III", data, word + 4 + 12 * i)
            out[_str(data, k)] = _value(data, t, w)
        return out
    return Raw(kind, word)


def decode(data):
    kind, word = struct.unpack_from("<II", data, 0)
    return _value(data, kind, word)


def _kind(v):
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1
    if isinstance(v, int):
        return 2
    if isinstance(v, float):
        return 3
    if isinstance(v, str):
        return 4
    if isinstance(v, list):
        return 5
    if isinstance(v, dict):
        return 6
    if isinstance(v, Raw):
        return v.kind
    raise TypeError(type(v))


class _Writer:
    def __init__(self):
        self.buf = bytearray()
        self.strings = {}

    def alloc(self, n):
        at = len(self.buf)
        self.buf += bytes(n)
        return at

    def string(self, s):
        # The game's compiler writes each distinct string once, where it first comes up.
        if s in self.strings:
            return self.strings[s]
        at = len(self.buf)
        b = s.encode("latin-1") + b"\0"
        self.buf += b + bytes(-len(b) % 4)
        self.strings[s] = at
        return at

    def word(self, v):
        """The u32 for a value: inline, or the offset of what was written for it."""
        k = _kind(v)
        if k == 0:
            return 0
        if k == 1:
            return 1 if v else 0
        if k == 2:
            return struct.unpack("<I", struct.pack("<i", v))[0]
        if k == 3:
            return struct.unpack("<I", struct.pack("<f", v))[0]
        if k == 4:
            return self.string(v)
        if k == 5:
            kinds = {_kind(x) for x in v}
            etype = kinds.pop() if len(kinds) == 1 else (0 if not v else None)
            if etype is None:
                raise TypeError(f"mixed array: {v!r}")
            at = self.alloc(8 + 4 * len(v))
            struct.pack_into("<II", self.buf, at, etype, len(v))
            for i, x in enumerate(v):
                struct.pack_into("<I", self.buf, at + 8 + 4 * i, self.word(x))
            return at
        if k == 6:
            at = self.alloc(4 + 12 * len(v))
            struct.pack_into("<I", self.buf, at, len(v))
            # Keys in byte order, as every object the game's compiler wrote has them (508 of 508 in the
            # render configs and settings): the engine may look them up by bisection.
            for i, (key, x) in enumerate(sorted(v.items(), key=lambda kv: kv[0].encode("latin-1"))):
                ko = self.string(key)
                struct.pack_into("<III", self.buf, at + 4 + 12 * i, ko, _kind(x), self.word(x))
            return at
        return v.word


def encode(tree):
    w = _Writer()
    w.alloc(8)
    word = w.word(tree)
    struct.pack_into("<II", w.buf, 0, _kind(tree), word)
    return bytes(w.buf)


def dumps(tree):
    return json.dumps(tree, indent=1, default=repr)


if __name__ == "__main__":
    t = decode(open(sys.argv[1], "rb").read())
    print(dumps(t))
