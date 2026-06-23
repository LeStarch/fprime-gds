"""Minimal MessagePack encoder/decoder for WebSocket streaming.

Implements the subset of the `MessagePack specification
<https://github.com/msgpack/msgpack/blob/master/spec.md>`_ required by the
GDS WebSocket wire format.  Uses the same ``default`` callback convention as
:func:`json.dumps` so that application types (``TimeType``, ``ValueType``,
enums, etc.) are converted to primitive dicts/lists before packing.

The :func:`decode` function is provided for testing and diagnostics; the
primary consumer of the wire bytes is the JavaScript ``msgpack.js`` decoder
running in the browser.

No external dependencies are required.  If the ``msgpack`` C-accelerated
package is installed the module can be swapped in as a drop-in replacement
since the wire format is identical.
"""

from __future__ import annotations

import struct
from typing import Any, Callable, Optional

_float64_be = struct.Struct(">d")


def encode(obj: Any, *, default: Optional[Callable] = None) -> bytes:
    """Encode *obj* to MessagePack bytes.

    Parameters
    ----------
    obj:
        Arbitrary Python object composed of dicts, lists, strings,
        ints, floats, bools, None, and bytes.
    default:
        Optional callable invoked for objects that are not natively
        serializable (same contract as ``json.dumps(default=...)``)
    """
    buf = bytearray()
    _pack(obj, buf, default or _no_default)
    return bytes(buf)


def _no_default(obj: object) -> None:
    raise TypeError(
        f"Object of type {type(obj).__name__} is not MessagePack serializable"
    )


# -- packing helpers ----------------------------------------------------------

def _pack(obj: Any, buf: bytearray, default: Callable) -> None:
    if obj is None:
        buf.append(0xC0)
    elif obj is True:
        buf.append(0xC3)
    elif obj is False:
        buf.append(0xC2)
    elif isinstance(obj, int):
        _pack_int(obj, buf)
    elif isinstance(obj, float):
        buf.append(0xCB)
        buf.extend(_float64_be.pack(obj))
    elif isinstance(obj, str):
        _pack_str(obj, buf)
    elif isinstance(obj, (bytes, bytearray, memoryview)):
        _pack_bin(bytes(obj), buf)
    elif isinstance(obj, dict):
        _pack_map(obj, buf, default)
    elif isinstance(obj, (list, tuple)):
        _pack_array(obj, buf, default)
    else:
        _pack(default(obj), buf, default)


def _pack_int(n: int, buf: bytearray) -> None:
    if 0 <= n <= 0x7F:
        buf.append(n)
    elif -32 <= n < 0:
        buf.append(n & 0xFF)
    elif 0 <= n <= 0xFF:
        buf.append(0xCC)
        buf.append(n)
    elif 0 <= n <= 0xFFFF:
        buf.append(0xCD)
        buf.extend(n.to_bytes(2, "big"))
    elif 0 <= n <= 0xFFFF_FFFF:
        buf.append(0xCE)
        buf.extend(n.to_bytes(4, "big"))
    elif 0 <= n:
        buf.append(0xCF)
        buf.extend(n.to_bytes(8, "big"))
    elif -128 <= n:
        buf.append(0xD0)
        buf.extend(n.to_bytes(1, "big", signed=True))
    elif -32768 <= n:
        buf.append(0xD1)
        buf.extend(n.to_bytes(2, "big", signed=True))
    elif -2_147_483_648 <= n:
        buf.append(0xD2)
        buf.extend(n.to_bytes(4, "big", signed=True))
    else:
        buf.append(0xD3)
        buf.extend(n.to_bytes(8, "big", signed=True))


def _pack_str(s: str, buf: bytearray) -> None:
    data = s.encode("utf-8")
    n = len(data)
    if n <= 31:
        buf.append(0xA0 | n)
    elif n <= 0xFF:
        buf.append(0xD9)
        buf.append(n)
    elif n <= 0xFFFF:
        buf.append(0xDA)
        buf.extend(n.to_bytes(2, "big"))
    else:
        buf.append(0xDB)
        buf.extend(n.to_bytes(4, "big"))
    buf.extend(data)


def _pack_bin(data: bytes, buf: bytearray) -> None:
    n = len(data)
    if n <= 0xFF:
        buf.append(0xC4)
        buf.append(n)
    elif n <= 0xFFFF:
        buf.append(0xC5)
        buf.extend(n.to_bytes(2, "big"))
    else:
        buf.append(0xC6)
        buf.extend(n.to_bytes(4, "big"))
    buf.extend(data)


def _pack_array(items: Any, buf: bytearray, default: Callable) -> None:
    n = len(items)
    if n <= 15:
        buf.append(0x90 | n)
    elif n <= 0xFFFF:
        buf.append(0xDC)
        buf.extend(n.to_bytes(2, "big"))
    else:
        buf.append(0xDD)
        buf.extend(n.to_bytes(4, "big"))
    for item in items:
        _pack(item, buf, default)


def _pack_map(d: dict, buf: bytearray, default: Callable) -> None:
    n = len(d)
    if n <= 15:
        buf.append(0x80 | n)
    elif n <= 0xFFFF:
        buf.append(0xDE)
        buf.extend(n.to_bytes(2, "big"))
    else:
        buf.append(0xDF)
        buf.extend(n.to_bytes(4, "big"))
    for key, value in d.items():
        _pack(key, buf, default)
        _pack(value, buf, default)


# =========================================================================
# Decoder
# =========================================================================

_float32_be = struct.Struct(">f")


def decode(data: bytes) -> Any:
    """Decode MessagePack *data* into a Python object.

    Binary (bin) payloads are returned as ``bytes``; strings as ``str``.
    Maps become ``dict``, arrays become ``list``.
    """
    state = {"offset": 0}
    result = _unpack(data, state)
    return result


def _read(data: bytes, state: dict, n: int) -> bytes:
    end = state["offset"] + n
    chunk = data[state["offset"]:end]
    state["offset"] = end
    return chunk


def _unpack(data: bytes, state: dict) -> Any:
    byte = data[state["offset"]]
    state["offset"] += 1

    # positive fixint
    if byte <= 0x7F:
        return byte
    # fixmap
    if (byte & 0xF0) == 0x80:
        return _read_map(data, state, byte & 0x0F)
    # fixarray
    if (byte & 0xF0) == 0x90:
        return _read_array(data, state, byte & 0x0F)
    # fixstr
    if (byte & 0xE0) == 0xA0:
        return _read(data, state, byte & 0x1F).decode("utf-8")
    # negative fixint
    if byte >= 0xE0:
        return byte - 256

    if byte == 0xC0:
        return None
    if byte == 0xC2:
        return False
    if byte == 0xC3:
        return True

    # bin 8 / 16 / 32
    if byte == 0xC4:
        n = data[state["offset"]]; state["offset"] += 1
        return bytes(_read(data, state, n))
    if byte == 0xC5:
        n = int.from_bytes(_read(data, state, 2), "big")
        return bytes(_read(data, state, n))
    if byte == 0xC6:
        n = int.from_bytes(_read(data, state, 4), "big")
        return bytes(_read(data, state, n))

    # float 32 / 64
    if byte == 0xCA:
        return _float32_be.unpack(_read(data, state, 4))[0]
    if byte == 0xCB:
        return _float64_be.unpack(_read(data, state, 8))[0]

    # uint 8 / 16 / 32 / 64
    if byte == 0xCC:
        v = data[state["offset"]]; state["offset"] += 1; return v
    if byte == 0xCD:
        return int.from_bytes(_read(data, state, 2), "big")
    if byte == 0xCE:
        return int.from_bytes(_read(data, state, 4), "big")
    if byte == 0xCF:
        return int.from_bytes(_read(data, state, 8), "big")

    # int 8 / 16 / 32 / 64
    if byte == 0xD0:
        return int.from_bytes(_read(data, state, 1), "big", signed=True)
    if byte == 0xD1:
        return int.from_bytes(_read(data, state, 2), "big", signed=True)
    if byte == 0xD2:
        return int.from_bytes(_read(data, state, 4), "big", signed=True)
    if byte == 0xD3:
        return int.from_bytes(_read(data, state, 8), "big", signed=True)

    # str 8 / 16 / 32
    if byte == 0xD9:
        n = data[state["offset"]]; state["offset"] += 1
        return _read(data, state, n).decode("utf-8")
    if byte == 0xDA:
        n = int.from_bytes(_read(data, state, 2), "big")
        return _read(data, state, n).decode("utf-8")
    if byte == 0xDB:
        n = int.from_bytes(_read(data, state, 4), "big")
        return _read(data, state, n).decode("utf-8")

    # array 16 / 32
    if byte == 0xDC:
        n = int.from_bytes(_read(data, state, 2), "big")
        return _read_array(data, state, n)
    if byte == 0xDD:
        n = int.from_bytes(_read(data, state, 4), "big")
        return _read_array(data, state, n)

    # map 16 / 32
    if byte == 0xDE:
        n = int.from_bytes(_read(data, state, 2), "big")
        return _read_map(data, state, n)
    if byte == 0xDF:
        n = int.from_bytes(_read(data, state, 4), "big")
        return _read_map(data, state, n)

    raise ValueError(f"Unknown msgpack type: 0x{byte:02x}")


def _read_array(data: bytes, state: dict, count: int) -> list:
    return [_unpack(data, state) for _ in range(count)]


def _read_map(data: bytes, state: dict, count: int) -> dict:
    result = {}
    for _ in range(count):
        key = _unpack(data, state)
        result[key] = _unpack(data, state)
    return result
