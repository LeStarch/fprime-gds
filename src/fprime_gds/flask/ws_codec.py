"""MessagePack codec for WebSocket streaming.

Thin wrapper around the ``msgpack`` C-accelerated library.  The
``default`` callback convention mirrors :func:`json.dumps` so that
application types (``TimeType``, ``ValueType``, enums, etc.) are
converted to primitives before packing.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import msgpack as _msgpack


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
    return _msgpack.packb(obj, default=default, use_bin_type=True)


def decode(data: bytes) -> Any:
    """Decode MessagePack *data* into a Python object.

    Binary (bin) payloads are returned as ``bytes``; strings as ``str``.
    Maps become ``dict``, arrays become ``list``.
    """
    return _msgpack.unpackb(data, raw=False)
