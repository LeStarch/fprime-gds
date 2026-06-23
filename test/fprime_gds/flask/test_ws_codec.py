"""Unit tests for :mod:`fprime_gds.flask.ws_codec` (MessagePack encoder/decoder)."""

from __future__ import annotations

import math

import pytest

from fprime_gds.flask import ws_codec


def _roundtrip(obj):
    """Encode then decode, returning the result."""
    return ws_codec.decode(ws_codec.encode(obj))


class TestPrimitives:
    def test_none(self):
        assert _roundtrip(None) is None

    def test_bool_true(self):
        assert _roundtrip(True) is True

    def test_bool_false(self):
        assert _roundtrip(False) is False

    def test_positive_fixint(self):
        for v in (0, 1, 42, 127):
            assert _roundtrip(v) == v

    def test_negative_fixint(self):
        for v in (-1, -16, -32):
            assert _roundtrip(v) == v

    def test_uint8(self):
        assert _roundtrip(200) == 200

    def test_uint16(self):
        assert _roundtrip(40000) == 40000

    def test_uint32(self):
        assert _roundtrip(3_000_000_000) == 3_000_000_000

    def test_uint64(self):
        v = 2**40
        assert _roundtrip(v) == v

    def test_int8(self):
        assert _roundtrip(-100) == -100

    def test_int16(self):
        assert _roundtrip(-1000) == -1000

    def test_int32(self):
        assert _roundtrip(-100_000) == -100_000

    def test_int64(self):
        v = -(2**40)
        assert _roundtrip(v) == v

    def test_float64(self):
        assert _roundtrip(3.14159) == 3.14159

    def test_float_nan(self):
        result = _roundtrip(float("nan"))
        assert math.isnan(result)


class TestStrings:
    def test_fixstr(self):
        assert _roundtrip("hello") == "hello"

    def test_empty_str(self):
        assert _roundtrip("") == ""

    def test_str8(self):
        s = "x" * 200
        assert _roundtrip(s) == s

    def test_str16(self):
        s = "y" * 300
        assert _roundtrip(s) == s

    def test_unicode(self):
        s = "héllo wörld 日本語"
        assert _roundtrip(s) == s


class TestBinary:
    def test_bin8(self):
        v = bytes(range(100))
        result = _roundtrip(v)
        assert result == v
        assert isinstance(result, bytes)

    def test_bin16(self):
        v = bytes(range(256)) * 2
        result = _roundtrip(v)
        assert result == v

    def test_empty_bin(self):
        result = _roundtrip(b"")
        assert result == b""

    def test_bytearray(self):
        v = bytearray([1, 2, 3])
        result = _roundtrip(v)
        assert result == bytes(v)


class TestContainers:
    def test_fixarray(self):
        v = [1, 2, 3]
        assert _roundtrip(v) == v

    def test_empty_array(self):
        assert _roundtrip([]) == []

    def test_array16(self):
        v = list(range(20))
        assert _roundtrip(v) == v

    def test_fixmap(self):
        v = {"a": 1, "b": 2}
        assert _roundtrip(v) == v

    def test_empty_map(self):
        assert _roundtrip({}) == {}

    def test_nested(self):
        v = {
            "type": "channel",
            "data": [
                {"id": 1, "val": 42, "nested": {"a": True}},
                {"id": 2, "val": None},
            ],
        }
        assert _roundtrip(v) == v


class TestDefault:
    def test_default_callback(self):
        class Custom:
            def __init__(self, x):
                self.x = x

        result = ws_codec.decode(
            ws_codec.encode(Custom(42), default=lambda o: o.x)
        )
        assert result == 42

    def test_no_default_raises(self):
        with pytest.raises(TypeError):
            ws_codec.encode(object())


class TestCompactness:
    def test_bytes_smaller_than_int_list(self):
        raw = bytes(range(256))
        compact = ws_codec.encode(raw)
        as_list = ws_codec.encode(list(raw))
        # bin encoding: ~3 + N bytes; array encoding: ~3 + 1.5N bytes (avg)
        assert len(compact) < len(as_list) * 0.75

    def test_envelope_with_bytes_val(self):
        raw = bytes(range(256))
        envelope = {"type": "channel", "data": [{"id": 1, "val": raw}]}
        encoded = ws_codec.encode(envelope)
        decoded = ws_codec.decode(encoded)
        assert decoded["data"][0]["val"] == raw
        assert isinstance(decoded["data"][0]["val"], bytes)
