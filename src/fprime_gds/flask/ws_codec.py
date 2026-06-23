"""MessagePack codec for WebSocket streaming.

Encoding uses the C-accelerated ``msgpack`` library for production
throughput.  A ``default`` callback converts application types
(``TimeType``, ``ValueType``, enums, etc.) to primitives before packing,
mirroring the contract of :func:`json.dumps`.

The :func:`decode` function is a pure-Python implementation kept for
testing and diagnostics; the primary consumer of the wire bytes is the
JavaScript ``@msgpack/msgpack`` decoder running in the browser.
"""

from __future__ import annotations

import struct
from typing import Any, Callable, Optional

import msgpack as _msgpack


def encode(obj: Any, *, default: Optional[Callable] = None) -> bytes:
    """Encode *obj* to MessagePack bytes using the C-accelerated library.

    Parameters
    ----------
    obj:
        Arbitrary Python object composed of dicts, lists, strings,
        ints, floats, bools, None, and bytes.
    default:
        Optional callable invoked for objects that are not natively
        serializable (same contract as ``json.dumps(default=...)``)
    """
    return _msgpack.packb(obj, default=default, use_bin_type=True)


# =========================================================================
# Decoder  (pure-Python — kept for tests and diagnostics)
# =========================================================================

_float64_be = struct.Struct(">d")
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
