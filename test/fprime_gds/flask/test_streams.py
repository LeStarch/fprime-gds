"""Unit tests for :mod:`fprime_gds.flask.streams`.

Focus is on the parts that have actually bitten us in browser testing:
the per-channel coalescing inside ``_Subscriber`` (so a dense channel
burst can no longer fill the bounded outbox and cause drops), the
event/command path that must *not* coalesce, and the close handshake.
"""

from __future__ import annotations

import struct
import threading
import time

import pytest

from fprime_gds.flask import streams


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


def test_encode_produces_valid_json():
    import json as stdlib_json
    envelope = {"type": "channel", "data": [{"id": 1, "val": 42}]}
    encoded = streams._encode(envelope)
    decoded = stdlib_json.loads(encoded)
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
# Binary encoding tests
# ---------------------------------------------------------------------------


def test_is_byte_array_bytes():
    assert streams._is_byte_array(b"\x00\x01\xff") is True
    assert streams._is_byte_array(bytearray([0, 1, 255])) is True


def test_is_byte_array_list():
    # Below threshold -> False
    assert streams._is_byte_array(list(range(10))) is False
    # At threshold -> True
    big = list(range(256)) * 2  # 512 ints in [0..255]
    assert streams._is_byte_array(big) is True
    # Contains non-int -> False
    bad = [0] * 100
    bad[50] = "x"
    assert streams._is_byte_array(bad) is False
    # Contains out-of-range int -> False
    bad2 = [0] * 100
    bad2[0] = 300
    assert streams._is_byte_array(bad2) is False


def test_is_byte_array_other():
    assert streams._is_byte_array(42) is False
    assert streams._is_byte_array("hello") is False
    assert streams._is_byte_array(None) is False


def test_encode_binary_channel_roundtrip():
    raw_data = bytes(range(256))
    data_dict = {
        "id": 42,
        "_binary": True,
        "_raw_bytes": raw_data,
        "_time_parts": (2, 0, 1000, 500000),
    }
    frame = streams._encode_binary_channel(data_dict)
    # Header is 25 bytes + payload
    assert len(frame) == 25 + len(raw_data)
    # Unpack header
    msg_type, ch_id, t_base, t_ctx, t_sec, t_usec, d_len = struct.unpack(
        "!BIIIIII", frame[:25]
    )
    assert msg_type == streams.BINARY_CHANNEL_MSG
    assert ch_id == 42
    assert t_base == 2
    assert t_ctx == 0
    assert t_sec == 1000
    assert t_usec == 500000
    assert d_len == 256
    # Payload matches
    assert frame[25:] == raw_data


def test_encode_binary_channel_default_time():
    data_dict = {
        "id": 7,
        "_binary": True,
        "_raw_bytes": b"\xab\xcd",
    }
    frame = streams._encode_binary_channel(data_dict)
    _, _, t_base, t_ctx, t_sec, t_usec, _ = struct.unpack(
        "!BIIIIII", frame[:25]
    )
    assert (t_base, t_ctx, t_sec, t_usec) == (0, 0, 0, 0)


def test_to_envelope_marks_binary_channel():
    """A channel whose value is a large byte list gets the _binary flag."""
    pixel_data = list(range(256)) * 4  # 1024 bytes
    chan = _FakeChan(id=10, val=pixel_data)
    envelope = streams.StreamHub._to_envelope(chan)
    assert envelope["type"] == "channel"
    data = envelope["data"]
    assert data["_binary"] is True
    assert data["_raw_bytes"] == bytes(pixel_data)
    assert data["_time_parts"] == (0, 0, 0, 0)  # _FakeChan has no time


def test_to_envelope_leaves_small_channel_as_json():
    """A normal scalar channel must not be flagged as binary."""
    chan = _FakeChan(id=5, val=42)
    envelope = streams.StreamHub._to_envelope(chan)
    data = envelope["data"]
    assert "_binary" not in data
    assert "_raw_bytes" not in data


def test_group_batch_preserves_binary_flag():
    """_group_batch extracts the inner data dict; _binary must survive."""
    binary_data = {"id": 1, "val": None, "_binary": True, "_raw_bytes": b"\x00"}
    json_data = {"id": 2, "val": 42}
    envelopes = [
        {"type": "channel", "id": 1, "data": binary_data},
        {"type": "channel", "id": 2, "data": json_data},
    ]
    grouped, passthrough = streams._group_batch(envelopes)
    items = grouped["channel"]
    assert len(items) == 2
    assert items[0].get("_binary") is True
    assert items[1].get("_binary") is None


def test_sender_sends_binary_frames():
    """Verify that binary-flagged channels are sent as bytes, not JSON."""
    hub = streams.StreamHub(max_depth=8)
    sub = hub.register()

    # Enqueue a binary channel
    pixel_vals = list(range(256))
    chan = _FakeChan(id=99, val=pixel_vals)
    hub.data_callback(chan)

    # Also enqueue a normal event for contrast
    hub.data_callback(_FakeEvent(id=1, val="hello"))

    drained = sub.drain(timeout_s=0)
    grouped, passthrough = streams._group_batch(drained)

    # Channel items: the binary one should encode to bytes
    ch_items = grouped["channel"]
    binary_items = [i for i in ch_items if i.get("_binary")]
    json_items = [i for i in ch_items if not i.get("_binary")]
    assert len(binary_items) == 1
    assert len(json_items) == 0

    frame = streams._encode_binary_channel(binary_items[0])
    assert isinstance(frame, bytes)
    assert frame[25:] == bytes(pixel_vals)

    # Event items should still JSON-encode
    ev_items = grouped["event"]
    encoded_json = streams._encode({"type": "event", "data": ev_items})
    assert isinstance(encoded_json, str)
