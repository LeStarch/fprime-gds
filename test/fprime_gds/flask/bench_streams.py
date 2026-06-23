#!/usr/bin/env python3
"""Stress test / benchmark for WebSocket streaming pipeline.

Simulates the DOOM-level workload: 35 Hz telemetry rate, 105 channels per
frame, a configurable number of which carry 3200-byte pixel arrays.  Measures:

  - msgpack encoding throughput (MB/s, frames/s)
  - wire bandwidth (bytes/s)
  - comparison vs JSON encoding for the same payloads
  - sender loop effective batch rate through the full pipeline

Run:
    python -m pytest test/fprime_gds/flask/bench_streams.py -v -s
    # or directly:
    python test/fprime_gds/flask/bench_streams.py
"""
from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Ensure the package is importable when run directly
# ---------------------------------------------------------------------------
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from fprime_gds.flask import json as flask_json
from fprime_gds.flask import streams
from fprime_gds.flask import ws_codec


# =========================================================================
# Test doubles — lightweight stand-ins for ChData / EventData / CmdData
# =========================================================================

class _FakeTime:
    """Mimics TimeType with the fields flask_json.time_type expects."""
    def __init__(self):
        self.timeBase = type("TB", (), {"numeric_value": 2})()
        self.timeContext = 0
        self.seconds = 1000
        self.useconds = 500000

    def to_dict(self):
        return {
            "base": self.timeBase.numeric_value,
            "context": self.timeContext,
            "seconds": self.seconds,
            "microseconds": self.useconds,
        }


class _FakeValObj:
    """Mimics a ValueType whose .val returns the stored value."""
    def __init__(self, val):
        self._val = val

    @property
    def val(self):
        return self._val


class _FakeErt:
    """Mimics a datetime-like ERT with isoformat()."""
    def isoformat(self):
        return "2026-01-01T00:00:00Z"


class _FakeChan:
    """Lightweight ChData stand-in."""
    def __init__(self, cid: int, val: Any, display_text: str = ""):
        self.id = cid
        self.time = _FakeTime()
        self.val_obj = _FakeValObj(val)
        self.display_text = display_text
        self.ert = _FakeErt()


class _FakeEvent:
    """Lightweight EventData stand-in."""
    def __init__(self, eid: int, display_text: str = "event"):
        self.id = eid
        self.time = _FakeTime()
        self.display_text = display_text


class _FakeCmd:
    """Lightweight CmdData stand-in."""
    def __init__(self, cid: int):
        self.id = cid
        self.time = _FakeTime()

    def get_arg_vals(self):
        return []


# =========================================================================
# Benchmark configuration
# =========================================================================

@dataclass
class BenchConfig:
    target_hz: float = 35.0
    total_channels: int = 105
    byte_array_channels: int = 10
    byte_array_size: int = 3200
    scalar_channels: int = 95  # total - byte_array
    events_per_frame: int = 2
    commands_per_frame: int = 0
    duration_s: float = 5.0

    def __post_init__(self):
        self.scalar_channels = self.total_channels - self.byte_array_channels


# =========================================================================
# Helpers
# =========================================================================

def _build_frame(cfg: BenchConfig, frame_idx: int,
                  use_bytes_val: bool = False) -> List[Any]:
    """Build one telemetry frame of fake data objects.

    When *use_bytes_val* is True, byte-array channels carry ``bytes``
    values directly (simulating the _is_compact_array optimization
    that converts list[int] -> bytes for U8 ArrayType channels).
    """
    items: List[Any] = []
    # Byte-array channels
    pixel_data_list = list(range(256)) * (cfg.byte_array_size // 256)
    pixel_data_list = pixel_data_list[:cfg.byte_array_size]
    pixel_data = bytes(pixel_data_list) if use_bytes_val else pixel_data_list
    for i in range(cfg.byte_array_channels):
        items.append(_FakeChan(cid=i, val=pixel_data))
    # Scalar channels
    for i in range(cfg.scalar_channels):
        cid = cfg.byte_array_channels + i
        items.append(_FakeChan(cid=cid, val=frame_idx * 1000 + i))
    # Events
    for i in range(cfg.events_per_frame):
        items.append(_FakeEvent(eid=10000 + frame_idx * 10 + i,
                                display_text=f"Event at frame {frame_idx}"))
    # Commands
    for i in range(cfg.commands_per_frame):
        items.append(_FakeCmd(cid=20000 + i))
    return items


def _encode_json(payload: Dict[str, Any]) -> bytes:
    """JSON encoding for comparison (same as old _encode before msgpack)."""
    return json.dumps(payload, default=flask_json.default, allow_nan=True).encode("utf-8")


# =========================================================================
# Benchmark: raw encoding throughput
# =========================================================================

def _patch_types():
    """Patch streams module types and register fakes with JSON encoders."""
    orig_ch = streams.ChData
    orig_ev = streams.EventData
    orig_cmd = streams.CmdData
    streams.ChData = _FakeChan
    streams.EventData = _FakeEvent
    streams.CmdData = _FakeCmd
    # Register _FakeTime so flask_json.default can serialize it
    flask_json.JSON_ENCODERS[_FakeTime] = lambda t: t.to_dict()
    return orig_ch, orig_ev, orig_cmd


def _unpatch_types(orig_ch, orig_ev, orig_cmd):
    streams.ChData = orig_ch
    streams.EventData = orig_ev
    streams.CmdData = orig_cmd
    flask_json.JSON_ENCODERS.pop(_FakeTime, None)


def bench_encoding_throughput(cfg: BenchConfig):
    """Measure encoding speed for a single frame of envelopes."""

    orig_ch, orig_ev, orig_cmd = _patch_types()

    try:
        frame = _build_frame(cfg, 0)

        # Convert all items to envelopes
        envelopes = [streams.StreamHub._to_envelope(item) for item in frame]
        envelopes = [e for e in envelopes if e is not None]

        # Group into batched payloads (mimics _sender_loop)
        grouped, passthrough = streams._group_batch(envelopes)
        payloads = []
        for kind, items in grouped.items():
            payloads.append({"type": kind, "data": items})
        for envelope in passthrough:
            payloads.append(envelope)

        # --- Msgpack encoding ---
        iterations = 200
        msgpack_sizes = []

        start = time.perf_counter()
        for _ in range(iterations):
            for payload in payloads:
                encoded = streams._encode(payload)
                msgpack_sizes.append(len(encoded))
        elapsed_msgpack = time.perf_counter() - start

        msgpack_frame_bytes = sum(msgpack_sizes[:len(payloads)])
        msgpack_fps = iterations / elapsed_msgpack

        # --- JSON encoding (comparison) ---
        json_sizes = []

        start = time.perf_counter()
        for _ in range(iterations):
            for payload in payloads:
                encoded = _encode_json(payload)
                json_sizes.append(len(encoded))
        elapsed_json = time.perf_counter() - start

        json_frame_bytes = sum(json_sizes[:len(payloads)])
        json_fps = iterations / elapsed_json

        return {
            "msgpack_frame_bytes": msgpack_frame_bytes,
            "json_frame_bytes": json_frame_bytes,
            "compression_ratio": json_frame_bytes / msgpack_frame_bytes,
            "msgpack_fps": msgpack_fps,
            "json_fps": json_fps,
            "msgpack_bandwidth_kBps": (msgpack_frame_bytes * msgpack_fps) / 1024,
            "json_bandwidth_kBps": (json_frame_bytes * json_fps) / 1024,
            "msgpack_encoding_time_ms": (elapsed_msgpack / iterations) * 1000,
            "json_encoding_time_ms": (elapsed_json / iterations) * 1000,
        }
    finally:
        _unpatch_types(orig_ch, orig_ev, orig_cmd)


# =========================================================================
# Benchmark: full sender pipeline simulation
# =========================================================================

class _FakeWs:
    """Records all send() calls for bandwidth measurement."""
    def __init__(self):
        self.frames: List[bytes] = []
        self.total_bytes = 0
        self.lock = threading.Lock()

    def send(self, data):
        with self.lock:
            self.frames.append(data)
            self.total_bytes += len(data)

    def receive(self, timeout=1.0):
        time.sleep(timeout)
        return None


def bench_sender_pipeline(cfg: BenchConfig, use_bytes_val: bool = False):
    """Simulate the full sender loop: hub → subscriber → encode → send.

    Measures effective batch rate and wire bandwidth under sustained load.
    """
    orig_ch, orig_ev, orig_cmd = _patch_types()

    try:
        hub = streams.StreamHub(max_depth=2048)
        sub = hub.register()

        fake_ws = _FakeWs()
        stop_event = threading.Event()

        # --- Producer thread: push frames at target_hz ---
        produced_frames = [0]

        def producer():
            interval = 1.0 / cfg.target_hz
            while not stop_event.is_set():
                frame = _build_frame(cfg, produced_frames[0],
                                     use_bytes_val=use_bytes_val)
                for item in frame:
                    hub.data_callback(item)
                produced_frames[0] += 1
                time.sleep(interval)

        # --- Sender thread: mimics _StreamSession._sender_loop ---
        send_batches = [0]

        def sender():
            while not stop_event.is_set() and sub.active:
                batch = sub.drain(timeout_s=0.05, batch_window_s=0.028)
                if not batch:
                    continue
                grouped, passthrough = streams._group_batch(batch)
                for kind, items in grouped.items():
                    if stop_event.is_set():
                        return
                    encoded = streams._encode({"type": kind, "data": items})
                    fake_ws.send(encoded)
                    send_batches[0] += 1
                for envelope in passthrough:
                    if stop_event.is_set():
                        return
                    encoded = streams._encode(envelope)
                    fake_ws.send(encoded)
                    send_batches[0] += 1

        # --- Run ---
        t_prod = threading.Thread(target=producer, daemon=True)
        t_send = threading.Thread(target=sender, daemon=True)

        t_send.start()
        t_prod.start()

        time.sleep(cfg.duration_s)
        stop_event.set()
        sub.close()
        t_prod.join(timeout=2.0)
        t_send.join(timeout=2.0)

        elapsed = cfg.duration_s
        stats = hub.stats()

        return {
            "duration_s": elapsed,
            "produced_frames": produced_frames[0],
            "produced_fps": produced_frames[0] / elapsed,
            "ws_sends": len(fake_ws.frames),
            "ws_send_rate_hz": len(fake_ws.frames) / elapsed,
            "total_wire_bytes": fake_ws.total_bytes,
            "wire_bandwidth_kBps": fake_ws.total_bytes / elapsed / 1024,
            "avg_frame_bytes": (fake_ws.total_bytes / len(fake_ws.frames)
                                if fake_ws.frames else 0),
            "dropped": stats["dropped"],
            "send_batches": send_batches[0],
            "batch_rate_hz": send_batches[0] / elapsed,
        }
    finally:
        _unpatch_types(orig_ch, orig_ev, orig_cmd)


# =========================================================================
# Benchmark: byte-array-only vs scalar-only comparison
# =========================================================================

def bench_byte_array_comparison():
    """Compare wire sizes: bytes-as-list vs bytes-as-bytes in msgpack."""
    pixel_data_list = list(range(256)) * 12 + list(range(128))  # 3200 ints
    pixel_data_bytes = bytes(pixel_data_list)

    envelope_list = {"type": "channel", "data": [{"id": 1, "val": pixel_data_list}]}
    envelope_bytes = {"type": "channel", "data": [{"id": 1, "val": pixel_data_bytes}]}

    msgpack_list = ws_codec.encode(envelope_list)
    msgpack_bytes = ws_codec.encode(envelope_bytes)
    json_list = json.dumps(envelope_list).encode("utf-8")

    return {
        "payload_elements": 3200,
        "json_bytes": len(json_list),
        "msgpack_as_list": len(msgpack_list),
        "msgpack_as_bytes": len(msgpack_bytes),
        "json_vs_msgpack_bytes_ratio": len(json_list) / len(msgpack_bytes),
        "list_vs_bytes_msgpack_ratio": len(msgpack_list) / len(msgpack_bytes),
    }


# =========================================================================
# Main / pytest entry
# =========================================================================

def _print_section(title: str, results: dict):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    for key, val in results.items():
        if isinstance(val, float):
            print(f"  {key:40s}: {val:>12.2f}")
        else:
            print(f"  {key:40s}: {val:>12}")


def bench_sender_pipeline_json(cfg: BenchConfig):
    """Same as bench_sender_pipeline but using JSON encoding for comparison."""
    orig_ch, orig_ev, orig_cmd = _patch_types()

    try:
        hub = streams.StreamHub(max_depth=2048)
        sub = hub.register()

        fake_ws = _FakeWs()
        stop_event = threading.Event()

        produced_frames = [0]

        def producer():
            interval = 1.0 / cfg.target_hz
            while not stop_event.is_set():
                frame = _build_frame(cfg, produced_frames[0])
                for item in frame:
                    hub.data_callback(item)
                produced_frames[0] += 1
                time.sleep(interval)

        send_batches = [0]

        def sender():
            while not stop_event.is_set() and sub.active:
                batch = sub.drain(timeout_s=0.05, batch_window_s=0.028)
                if not batch:
                    continue
                grouped, passthrough = streams._group_batch(batch)
                for kind, items in grouped.items():
                    if stop_event.is_set():
                        return
                    encoded = _encode_json({"type": kind, "data": items})
                    fake_ws.send(encoded)
                    send_batches[0] += 1
                for envelope in passthrough:
                    if stop_event.is_set():
                        return
                    encoded = _encode_json(envelope)
                    fake_ws.send(encoded)
                    send_batches[0] += 1

        t_prod = threading.Thread(target=producer, daemon=True)
        t_send = threading.Thread(target=sender, daemon=True)

        t_send.start()
        t_prod.start()

        time.sleep(cfg.duration_s)
        stop_event.set()
        sub.close()
        t_prod.join(timeout=2.0)
        t_send.join(timeout=2.0)

        elapsed = cfg.duration_s
        stats = hub.stats()

        return {
            "duration_s": elapsed,
            "produced_frames": produced_frames[0],
            "produced_fps": produced_frames[0] / elapsed,
            "ws_sends": len(fake_ws.frames),
            "ws_send_rate_hz": len(fake_ws.frames) / elapsed,
            "total_wire_bytes": fake_ws.total_bytes,
            "wire_bandwidth_kBps": fake_ws.total_bytes / elapsed / 1024,
            "avg_frame_bytes": (fake_ws.total_bytes / len(fake_ws.frames)
                                if fake_ws.frames else 0),
            "dropped": stats["dropped"],
            "send_batches": send_batches[0],
            "batch_rate_hz": send_batches[0] / elapsed,
        }
    finally:
        _unpatch_types(orig_ch, orig_ev, orig_cmd)


def run_all():
    cfg = BenchConfig()

    print(f"\nBenchmark config:")
    print(f"  Target Hz:            {cfg.target_hz}")
    print(f"  Total channels:       {cfg.total_channels}")
    print(f"  Byte-array channels:  {cfg.byte_array_channels}")
    print(f"  Byte-array size:      {cfg.byte_array_size}")
    print(f"  Scalar channels:      {cfg.scalar_channels}")
    print(f"  Events per frame:     {cfg.events_per_frame}")
    print(f"  Duration:             {cfg.duration_s}s")

    # 1) Byte-array comparison
    r = bench_byte_array_comparison()
    _print_section("Byte-Array Wire Size Comparison (3200 elements)", r)

    # 2) Encoding throughput
    r = bench_encoding_throughput(cfg)
    _print_section("Encoding Throughput (single frame, 200 iterations)", r)

    # 3) Full pipeline — msgpack (list arrays, no type metadata)
    r_msgpack_list = bench_sender_pipeline(cfg, use_bytes_val=False)
    _print_section(f"Full Pipeline — MSGPACK, arrays as list ({cfg.duration_s}s)", r_msgpack_list)

    # 4) Full pipeline — msgpack (bytes optimization, simulating ArrayType)
    r_msgpack_bin = bench_sender_pipeline(cfg, use_bytes_val=True)
    _print_section(f"Full Pipeline — MSGPACK, arrays as bytes ({cfg.duration_s}s)", r_msgpack_bin)

    # 5) Full pipeline — JSON (baseline)
    r_json = bench_sender_pipeline_json(cfg)
    _print_section(f"Full Pipeline — JSON baseline ({cfg.duration_s}s)", r_json)

    # 6) Summary comparison
    print(f"\n{'='*60}")
    print(f"  SUMMARY: Wire Bandwidth Comparison")
    print(f"{'='*60}")
    bw_red_list = 1.0 - (r_msgpack_list["wire_bandwidth_kBps"] / r_json["wire_bandwidth_kBps"]) if r_json["wire_bandwidth_kBps"] > 0 else 0
    bw_red_bin = 1.0 - (r_msgpack_bin["wire_bandwidth_kBps"] / r_json["wire_bandwidth_kBps"]) if r_json["wire_bandwidth_kBps"] > 0 else 0
    print(f"  JSON baseline:           {r_json['wire_bandwidth_kBps']:>8.1f} KB/s")
    print(f"  msgpack (list arrays):   {r_msgpack_list['wire_bandwidth_kBps']:>8.1f} KB/s  ({bw_red_list*100:.0f}% reduction)")
    print(f"  msgpack (bytes arrays):  {r_msgpack_bin['wire_bandwidth_kBps']:>8.1f} KB/s  ({bw_red_bin*100:.0f}% reduction)")
    print(f"")
    print(f"  JSON avg frame:          {r_json['avg_frame_bytes']:>8.0f} bytes")
    print(f"  msgpack list avg frame:  {r_msgpack_list['avg_frame_bytes']:>8.0f} bytes")
    print(f"  msgpack bin avg frame:   {r_msgpack_bin['avg_frame_bytes']:>8.0f} bytes")
    print(f"")
    print(f"  Drops (JSON / mp-list / mp-bin): {r_json['dropped']} / {r_msgpack_list['dropped']} / {r_msgpack_bin['dropped']}")
    print(f"  Batch Hz (JSON / mp-list / mp-bin): {r_json['batch_rate_hz']:.1f} / {r_msgpack_list['batch_rate_hz']:.1f} / {r_msgpack_bin['batch_rate_hz']:.1f}")


# pytest entry point
def test_stress_encoding_throughput():
    """Verify msgpack can encode a full frame faster than the 35 Hz target."""
    cfg = BenchConfig()
    results = bench_encoding_throughput(cfg)
    # We need to encode at least 35 frames/s to hit the target
    assert results["msgpack_fps"] > cfg.target_hz, (
        f"msgpack encoding too slow: {results['msgpack_fps']:.1f} fps < {cfg.target_hz} fps"
    )
    print(f"\n  msgpack: {results['msgpack_fps']:.0f} fps, "
          f"{results['msgpack_frame_bytes']} bytes/frame")
    print(f"  JSON:    {results['json_fps']:.0f} fps, "
          f"{results['json_frame_bytes']} bytes/frame")
    print(f"  Compression ratio: {results['compression_ratio']:.2f}x")


def test_stress_sender_pipeline():
    """Verify the sender pipeline sustains target batch rate under load."""
    cfg = BenchConfig(duration_s=5.0)
    results = bench_sender_pipeline(cfg, use_bytes_val=True)
    print(f"\n  Produced:   {results['produced_frames']} frames "
          f"({results['produced_fps']:.1f} fps)")
    print(f"  WS sends:   {results['ws_sends']} "
          f"({results['ws_send_rate_hz']:.1f} Hz)")
    print(f"  Bandwidth:  {results['wire_bandwidth_kBps']:.1f} KB/s")
    print(f"  Avg frame:  {results['avg_frame_bytes']:.0f} bytes")
    print(f"  Dropped:    {results['dropped']}")
    # The sender should keep up with the producer with minimal drops
    assert results["dropped"] == 0, f"Dropped {results['dropped']} items"


def test_byte_array_comparison():
    """Verify byte-array encoding compactness."""
    results = bench_byte_array_comparison()
    print(f"\n  JSON:           {results['json_bytes']:>8} bytes")
    print(f"  msgpack (list): {results['msgpack_as_list']:>8} bytes")
    print(f"  msgpack (bin):  {results['msgpack_as_bytes']:>8} bytes")
    print(f"  JSON/bin ratio: {results['json_vs_msgpack_bytes_ratio']:.1f}x")
    # bytes encoding should be at least 4x smaller than JSON
    assert results["json_vs_msgpack_bytes_ratio"] > 4.0


if __name__ == "__main__":
    run_all()
