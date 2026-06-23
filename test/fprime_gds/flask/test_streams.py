"""Unit tests for :mod:`fprime_gds.flask.streams`.

Focus is on the parts that have actually bitten us in browser testing:
the per-channel coalescing inside ``_Subscriber`` (so a dense channel
burst can no longer fill the bounded outbox and cause drops), the
event/command path that must *not* coalesce, and the close handshake.
"""

from __future__ import annotations

import threading
import time

import pytest

from fprime_gds.flask import streams
from fprime_gds.flask import ws_codec


def _channel(cid: int, val: object = 0, ert: float = 0.0):
    return {"type": "channel", "id": cid, "data": {"id": cid, "val": val, "ert": ert}}


def _event(eid: int, val: object = 0):
    return {"type": "event", "id": eid, "data": {"id": eid, "val": val}}


def _command(cid: int, val: object = 0):
    return {"type": "command", "id": cid, "data": {"id": cid, "val": val}}


def test_channel_enqueue_coalesces_by_id():
    sub = streams._Subscriber("s", max_depth=4)
    # 1000 samples, only 3 unique ids -> outbox stays small, no drops
    for i in range(1000):
        sub.enqueue(_channel(i % 3, val=i))
    drained = sub.drain(timeout_s=0)
    by_id = {env["id"]: env for env in drained}
    assert set(by_id.keys()) == {0, 1, 2}
    # Each id keeps the LATEST sample (highest ``val``) we enqueued.
    assert by_id[0]["data"]["val"] == 999
    assert by_id[1]["data"]["val"] == 997
    assert by_id[2]["data"]["val"] == 998
    assert sub.dropped == 0


def test_event_path_does_not_coalesce_and_drops_on_overflow():
    sub = streams._Subscriber("s", max_depth=4)
    # 10 unique events; the deque is bounded to 4.
    for i in range(10):
        sub.enqueue(_event(i))
    drained = sub.drain(timeout_s=0)
    # Oldest is dropped to keep latency bounded under sustained overrun.
    assert [env["id"] for env in drained] == [6, 7, 8, 9]
    assert sub.dropped == 6


def test_command_path_does_not_coalesce():
    sub = streams._Subscriber("s", max_depth=8)
    for i in range(3):
        sub.enqueue(_command(42, val=i))
    drained = sub.drain(timeout_s=0)
    # Commands carry per-issue side effects; we must not lose any of
    # them just because two were issued back-to-back.
    assert [env["data"]["val"] for env in drained] == [0, 1, 2]


def test_drain_mixed_kinds_preserves_outbox_then_channels():
    sub = streams._Subscriber("s", max_depth=8)
    sub.enqueue(_event(1))
    sub.enqueue(_channel(100, val="a"))
    sub.enqueue(_event(2))
    sub.enqueue(_channel(100, val="b"))
    sub.enqueue(_command(7))
    drained = sub.drain(timeout_s=0)
    types_in_order = [env["type"] for env in drained]
    # Events/commands ride the FIFO outbox; channels are appended after,
    # coalesced to the latest value per id.
    assert types_in_order == ["event", "event", "command", "channel"]
    chan_envs = [env for env in drained if env["type"] == "channel"]
    assert chan_envs[0]["data"]["val"] == "b"


def test_drain_returns_empty_after_close():
    sub = streams._Subscriber("s", max_depth=4)
    sub.enqueue(_channel(1))
    sub.enqueue(_event(2))
    sub.close()
    assert sub.drain(timeout_s=0) == []
    # Subsequent enqueues on a closed subscriber are no-ops.
    sub.enqueue(_channel(3))
    sub.enqueue(_event(4))
    assert sub.drain(timeout_s=0) == []


def test_drain_blocks_until_first_envelope_then_batches():
    sub = streams._Subscriber("s", max_depth=16)

    def producer():
        time.sleep(0.02)
        for cid in (1, 2, 3):
            sub.enqueue(_channel(cid, val=cid))
        # Give the batch window a chance to absorb a follow-up update
        # for an existing channel id.
        time.sleep(0.005)
        sub.enqueue(_channel(2, val="latest"))

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    drained = sub.drain(timeout_s=1.0, batch_window_s=0.05)
    t.join(timeout=1.0)
    by_id = {env["id"]: env["data"]["val"] for env in drained}
    assert by_id == {1: 1, 2: "latest", 3: 3}


def test_hub_fanout_delivers_to_all_subscribers():
    hub = streams.StreamHub(max_depth=8)
    sub_a = hub.register()
    sub_b = hub.register()
    hub.data_callback(_FakeChan(id=1, val=1))
    hub.data_callback(_FakeEvent(id=2, val=2))
    hub.data_callback(_FakeCmd(id=3, val=3))
    drained_a = sub_a.drain(timeout_s=0)
    drained_b = sub_b.drain(timeout_s=0)
    # Both subscribers receive identical data with correct types and payloads
    for drained in (drained_a, drained_b):
        assert len(drained) == 3
        by_type = {env["type"]: env for env in drained}
        assert set(by_type.keys()) == {"channel", "event", "command"}
        assert by_type["channel"]["data"] == {"id": 1, "val": 1}
        assert by_type["event"]["data"] == {"id": 2, "val": 2}
        assert by_type["command"]["data"] == {"id": 3, "val": 3}


def test_hub_stats_aggregates_drops():
    hub = streams.StreamHub(max_depth=2)
    sub_a = hub.register()
    sub_b = hub.register()
    # Force drops on the event path of both subscribers.
    # Depth=2 means 5 events -> 3 dropped per subscriber, 6 total.
    for i in range(5):
        sub_a.enqueue(_event(i))
        sub_b.enqueue(_event(i))
    stats = hub.stats()
    assert stats["clients"] == 2
    assert stats["dropped"] == 6


def test_hub_rejects_beyond_max_subscribers():
    hub = streams.StreamHub(max_depth=4)
    subs = []
    for _ in range(streams.MAX_SUBSCRIBERS):
        sub = hub.register()
        assert sub is not None
        subs.append(sub)
    # Next registration is rejected
    assert hub.register() is None
    assert hub.stats()["clients"] == streams.MAX_SUBSCRIBERS
    # Freeing one slot allows a new subscriber
    hub.unregister(subs.pop())
    sub = hub.register()
    assert sub is not None
    assert hub.stats()["clients"] == streams.MAX_SUBSCRIBERS


def test_group_batch_separates_kinds():
    envelopes = [
        {"type": "channel", "id": 1, "data": {"id": 1, "val": 10}},
        {"type": "event", "id": 2, "data": {"id": 2, "val": 20}},
        {"type": "channel", "id": 3, "data": {"id": 3, "val": 30}},
        {"type": "command", "id": 4, "data": {"id": 4, "val": 40}},
    ]
    grouped, passthrough = streams._group_batch(envelopes)
    assert "channel" in grouped
    assert len(grouped["channel"]) == 2
    assert "event" in grouped
    assert len(grouped["event"]) == 1
    assert "command" in grouped
    assert len(grouped["command"]) == 1
    assert passthrough == []


def test_encode_produces_valid_msgpack():
    """_encode returns msgpack bytes that roundtrip to the original structure."""
    envelope = {"type": "channel", "data": [{"id": 1, "val": 42}]}
    encoded = streams._encode(envelope)
    assert isinstance(encoded, bytes)
    decoded = ws_codec.decode(encoded)
    assert decoded == envelope


# ---------------------------------------------------------------------------
# Light-weight test doubles so we can exercise StreamHub without dragging in
# the full F Prime decoder/dictionary machinery.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_minimal(monkeypatch):
    """Replace the JSON minimisers with identity functions so the tests
    can pass plain values through without building real ChData/EventData/
    CmdData objects (those require dictionaries to construct)."""
    from fprime_gds.flask import json as flask_json

    monkeypatch.setattr(flask_json, "minimal_channel", lambda d: {"id": d.id, "val": d.val})
    monkeypatch.setattr(flask_json, "minimal_event", lambda d: {"id": d.id, "val": d.val})
    monkeypatch.setattr(flask_json, "minimal_command", lambda d: {"id": d.id, "val": d.val})


class _FakeChan:
    def __init__(self, id: int, val: object):
        self.id = id
        self.val = val


class _FakeEvent:
    def __init__(self, id: int, val: object):
        self.id = id
        self.val = val


class _FakeCmd:
    def __init__(self, id: int, val: object):
        self.id = id
        self.val = val


@pytest.fixture(autouse=True)
def _rebind_fprime_types(monkeypatch):
    monkeypatch.setattr(streams, "ChData", _FakeChan)
    monkeypatch.setattr(streams, "EventData", _FakeEvent)
    monkeypatch.setattr(streams, "CmdData", _FakeCmd)


# ---------------------------------------------------------------------------
# MessagePack encoding tests
# ---------------------------------------------------------------------------


def test_is_compact_array_false_without_type_metadata():
    """_FakeChan has no val_obj; _is_compact_array must return False."""
    chan = _FakeChan(id=1, val=list(range(256)))
    assert streams._is_compact_array(chan) is False


def test_to_envelope_preserves_list_without_type_metadata():
    """Without ArrayType metadata, list values stay as lists (not bytes)."""
    large_list = list(range(256))
    chan = _FakeChan(id=10, val=large_list)
    envelope = streams.StreamHub._to_envelope(chan)
    data = envelope["data"]
    assert data["val"] == large_list
    assert isinstance(data["val"], list)


def test_to_envelope_scalar_channel_unchanged():
    """A normal scalar channel value passes through unmodified."""
    chan = _FakeChan(id=5, val=42)
    envelope = streams.StreamHub._to_envelope(chan)
    assert envelope["data"]["val"] == 42


def test_encode_roundtrip_all_types():
    """All JSON-like types survive a msgpack encode-decode roundtrip."""
    envelope = {
        "type": "channel",
        "data": [
            {"id": 1, "val": 42},
            {"id": 2, "val": 3.14},
            {"id": 3, "val": "hello"},
            {"id": 4, "val": None},
            {"id": 5, "val": True},
            {"id": 6, "val": [1, 2, 3]},
        ],
    }
    encoded = streams._encode(envelope)
    assert isinstance(encoded, bytes)
    decoded = ws_codec.decode(encoded)
    assert decoded == envelope


def test_encode_bytes_val_roundtrips_as_bytes():
    """A bytes value encodes compactly and decodes back to bytes."""
    raw = bytes(range(256))
    envelope = {"type": "channel", "data": [{"id": 1, "val": raw}]}
    encoded = streams._encode(envelope)
    decoded = ws_codec.decode(encoded)
    assert decoded["data"][0]["val"] == raw
    assert isinstance(decoded["data"][0]["val"], bytes)


def test_encode_bytes_is_compact():
    """Bytes values should encode far smaller than list-of-ints."""
    raw = bytes(range(256))
    compact = streams._encode({"val": raw})
    as_list = streams._encode({"val": list(raw)})
    # bin encoding: ~3 + N bytes; array encoding: ~3 + 1.5N bytes (avg)
    assert len(compact) < len(as_list) * 0.75


def test_sender_sends_msgpack_frames():
    """All frames from the sender loop are msgpack bytes."""
    hub = streams.StreamHub(max_depth=8)
    sub = hub.register()

    hub.data_callback(_FakeChan(id=99, val=42))
    hub.data_callback(_FakeEvent(id=1, val="hello"))
    hub.data_callback(_FakeCmd(id=7, val="run"))

    drained = sub.drain(timeout_s=0)
    grouped, passthrough = streams._group_batch(drained)

    for kind, items in grouped.items():
        frame = streams._encode({"type": kind, "data": items})
        assert isinstance(frame, bytes)
        decoded = ws_codec.decode(frame)
        assert decoded["type"] == kind
        assert isinstance(decoded["data"], list)
