"""Streaming endpoints for the F Prime Flask GDS.

The default REST API exposes channel and event histories that the front-end
polls on a timer. For high-rate deployments (many channels at high cadence)
polling is a poor fit: every poll re-serializes the full per-client history
and the browser parses a multi-megabyte response while still-arriving
samples accumulate behind the request. This module supplements the REST
API with a WebSocket push channel.

A single :class:`StreamHub` registers itself with the F Prime pipeline as a
channel, event, and command consumer. As data arrives the hub enqueues a
JSON envelope into the per-client outbox of each subscriber. A WebSocket
route, registered on the Flask app through :func:`register_stream_routes`,
runs the per-client I/O loop and drains the outbox to the wire.

The hub is designed to **never block** the F Prime decoder threads.
Per-client outboxes are bounded; channel samples are coalesced per id
(the front-end ``MappedHistory`` already retains only the latest sample
per channel id, so coalescing on the way *in* preserves identical display
semantics while bounding the channel state by the number of *unique* ids
instead of the incoming sample *rate*). Events and commands are unique
and ride a bounded FIFO; on overflow the oldest item is dropped and a
counter is incremented so that overruns surface as telemetry rather than
as a stalled pipeline.

The WebSocket support is conditional on the optional ``flask-sock``
dependency. If unavailable, :func:`register_stream_routes` is a no-op and
the rest of the GDS continues to operate via REST polling.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, Iterable, List, Optional

from fprime_gds.common.data_types.ch_data import ChData
from fprime_gds.common.data_types.cmd_data import CmdData
from fprime_gds.common.data_types.event_data import EventData
from fprime_gds.common.handlers import DataHandler
from fprime_gds.flask import json as flask_json

try:  # pragma: no cover - optional dependency
    from flask_sock import Sock
    HAVE_FLASK_SOCK = True
except Exception:  # pragma: no cover - import-time fallback
    Sock = None  # type: ignore[assignment]
    HAVE_FLASK_SOCK = False


logger = logging.getLogger("fprime_gds.flask.streams")


# ---------------------------------------------------------------------------
# Envelope kinds. The names are part of the wire protocol; the values are
# also used as the discriminator in :func:`StreamHub._to_envelope` and on
# the client side in ``stream.js``.
# ---------------------------------------------------------------------------
KIND_CHANNEL = "channel"
KIND_EVENT = "event"
KIND_COMMAND = "command"
KIND_HELLO = "hello"
KIND_ERROR = "error"

#: Kinds that are coalesced into a per-kind batched ``ws.send`` payload by
#: the sender thread.
_BATCHABLE_KINDS = (KIND_CHANNEL, KIND_EVENT, KIND_COMMAND)


DEFAULT_QUEUE_DEPTH = 1024
"""Default per-client outbox depth (in messages)."""

MAX_SUBSCRIBERS = 16
"""Maximum concurrent WebSocket subscribers. New connections beyond this
limit are rejected with a 503 (Service Unavailable) error envelope and
immediate close. This bounds total thread allocation (2 threads per client)
and prevents a network peer from exhausting GDS resources."""

DEFAULT_DRAIN_TIMEOUT_S = 0.05
"""Wait timeout for the sender thread between drains."""

DEFAULT_BATCH_WINDOW_S = 0.028
"""After the first envelope wakes the sender, wait this long for more to
accumulate before draining. Coalesces same-kind samples into a single
``ws.send`` so the browser does one ``JSON.parse`` / handler dispatch per
kind per window instead of one per sample.

Default is sized to one F Prime frame at 35 Hz (1/35 s = 28.6 ms).
"""

DEFAULT_RECEIVE_TIMEOUT_S = 1.0
"""Wait timeout for the receive loop between reads."""


# ---------------------------------------------------------------------------
# Per-client subscriber state
# ---------------------------------------------------------------------------

class _Subscriber:
    """Per-WebSocket subscriber state held by :class:`StreamHub`.

    Holds:

    * a bounded outbox for events and commands (FIFO, drop-oldest on
      overflow),
    * a per-id coalescing dict for channel samples (the front-end
      ``MappedHistory`` only keeps the latest per id, so we coalesce on
      the way in rather than letting bursts overflow the outbox).
    """

    __slots__ = (
        "id",
        "_outbox",
        "_latest_channels",
        "_outbox_cv",
        "_max_depth",
        "dropped",
        "active",
    )

    def __init__(self, sub_id: str, max_depth: int):
        self.id = sub_id
        self._outbox: deque = deque()
        self._latest_channels: Dict[int, Dict[str, Any]] = {}
        self._outbox_cv = threading.Condition()
        self._max_depth = max_depth
        self.dropped = 0
        self.active = True

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------
    def enqueue(self, envelope: Dict[str, Any]) -> None:
        """Enqueue an envelope into this subscriber's outbox.

        For channel envelopes with a known id, coalesces with any prior
        sample for the same id (latest wins). All other envelopes ride a
        FIFO deque bounded to ``max_depth``; overflow drops the oldest
        entry and bumps the ``dropped`` counter.
        """
        with self._outbox_cv:
            if not self.active:
                return
            kind = envelope.get("type") if isinstance(envelope, dict) else None
            if kind == KIND_CHANNEL:
                cid = envelope.get("id")
                if cid is not None:
                    self._latest_channels[cid] = envelope
                else:
                    self._push_bounded(envelope)
            else:
                self._push_bounded(envelope)
            self._outbox_cv.notify()

    def _push_bounded(self, envelope: Dict[str, Any]) -> None:
        """Append to the FIFO outbox, drop-oldest on overflow.

        Caller must hold ``self._outbox_cv``.
        """
        if len(self._outbox) >= self._max_depth:
            self._outbox.popleft()
            self.dropped += 1
        self._outbox.append(envelope)

    def drain(self, timeout_s: float, batch_window_s: float = 0.0) -> List[Dict[str, Any]]:
        """Drain queued envelopes, blocking up to ``timeout_s`` for the first.

        Once at least one envelope has arrived, the call sleeps for
        ``batch_window_s`` outside the condition lock so producers can
        add more envelopes to the batch before the drain runs. Returns
        an empty list when the subscriber has been closed or nothing
        arrived within ``timeout_s``.
        """
        with self._outbox_cv:
            if self._empty_locked():
                self._outbox_cv.wait(timeout=timeout_s)
            if not self.active or self._empty_locked():
                return []
        if batch_window_s > 0:
            time.sleep(batch_window_s)
        with self._outbox_cv:
            if not self.active:
                return []
            drained = list(self._outbox)
            self._outbox.clear()
            drained.extend(self._latest_channels.values())
            self._latest_channels.clear()
            return drained

    def _empty_locked(self) -> bool:
        """Whether both the outbox and the per-id channel slot are empty.

        Caller must hold ``self._outbox_cv``.
        """
        return not self._outbox and not self._latest_channels

    def close(self) -> None:
        """Mark the subscriber inactive and wake any blocked drain."""
        with self._outbox_cv:
            self.active = False
            self._outbox.clear()
            self._latest_channels.clear()
            self._outbox_cv.notify_all()


# ---------------------------------------------------------------------------
# StreamHub: pipeline integration + fan-out
# ---------------------------------------------------------------------------

class StreamHub(DataHandler):
    """Fan-out of F Prime decoded data to WebSocket subscribers."""

    def __init__(self, max_depth: int = DEFAULT_QUEUE_DEPTH):
        self._max_depth = max_depth
        self._subscribers: Dict[str, _Subscriber] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Pipeline registration
    # ------------------------------------------------------------------
    def attach_to_pipeline(self, pipeline) -> None:
        """Register this hub with the standard pipeline's decoders."""
        coders = pipeline.coders
        coders.register_channel_consumer(self)
        coders.register_event_consumer(self)
        coders.register_command_consumer(self)

    # ------------------------------------------------------------------
    # Subscriber lifecycle
    # ------------------------------------------------------------------
    def register(self, max_depth: Optional[int] = None) -> Optional[_Subscriber]:
        """Create and return a new subscriber, or ``None`` if the limit is reached."""
        with self._lock:
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                return None
            sub = _Subscriber(str(uuid.uuid4()), max_depth or self._max_depth)
            self._subscribers[sub.id] = sub
        return sub

    def unregister(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subscribers.pop(sub.id, None)
        sub.close()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "clients": len(self._subscribers),
                "dropped": sum(s.dropped for s in self._subscribers.values()),
            }

    # ------------------------------------------------------------------
    # DataHandler API
    # ------------------------------------------------------------------
    def data_callback(self, data, sender=None) -> None:
        try:
            envelope = self._to_envelope(data)
        except Exception:  # pragma: no cover - defensive
            logger.exception("StreamHub: failed to serialize datum")
            return
        if envelope is None:
            return
        with self._lock:
            subscribers = list(self._subscribers.values())
        if not subscribers:
            return
        for sub in subscribers:
            sub.enqueue(envelope)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _to_envelope(data) -> Optional[Dict[str, Any]]:
        if isinstance(data, ChData):
            return {
                "type": KIND_CHANNEL,
                "id": data.id,
                "data": flask_json.minimal_channel(data),
            }
        if isinstance(data, EventData):
            return {
                "type": KIND_EVENT,
                "id": data.id,
                "data": flask_json.minimal_event(data),
            }
        if isinstance(data, CmdData):
            return {
                "type": KIND_COMMAND,
                "id": data.id,
                "data": flask_json.minimal_command(data),
            }
        return None


# ---------------------------------------------------------------------------
# Wire encoding helpers (shared by sender thread + tests)
# ---------------------------------------------------------------------------

def _encode(payload: Any) -> str:
    """JSON-encode an envelope (or batched envelope) for the wire."""
    return json.dumps(payload, default=flask_json.default, allow_nan=True)


def _group_batch(envelopes: Iterable[Dict[str, Any]]):
    """Split a drained batch into per-kind grouped lists + a passthrough.

    Returns ``(grouped, passthrough)`` where:

    * ``grouped`` is ``{kind: [data, ...]}`` containing the inner
      ``data`` payload of each envelope, suitable for sending as one
      batched ``{"type": kind, "data": [...]}`` envelope.
    * ``passthrough`` is a list of envelopes whose kind is not in
      :data:`_BATCHABLE_KINDS`; these are forwarded individually.
    """
    grouped: Dict[str, list] = {}
    passthrough: List[Dict[str, Any]] = []
    for envelope in envelopes:
        kind = envelope.get("type") if isinstance(envelope, dict) else None
        if kind in _BATCHABLE_KINDS:
            grouped.setdefault(kind, []).append(envelope.get("data"))
        else:
            passthrough.append(envelope)
    return grouped, passthrough


# ---------------------------------------------------------------------------
# Per-connection session (sender + receiver threads)
# ---------------------------------------------------------------------------

class _StreamSession:
    """Run the sender + receiver loops for one WebSocket connection.

    Lifecycle:

    1. ``__init__`` registers a subscriber on the hub.
    2. ``run`` greets the client, spawns the sender thread, and blocks
       in the receive loop until the connection closes or an error trips
       ``stop_event``.
    3. On exit the subscriber is unregistered and the sender thread
       joined.
    """

    def __init__(
        self,
        ws,
        hub: StreamHub,
        max_depth: int,
        drain_timeout: float,
        batch_window: float,
        receive_timeout: float,
    ) -> None:
        self._ws = ws
        self._hub = hub
        self._drain_timeout = drain_timeout
        self._batch_window = batch_window
        self._receive_timeout = receive_timeout
        self._sub: Optional[_Subscriber] = hub.register(max_depth=max_depth)
        self._ws_lock = threading.Lock()
        self._stop = threading.Event()

    def run(self) -> None:
        if self._sub is None:
            self._send({"type": KIND_ERROR, "reason": "max_subscribers_reached"})
            return
        thread = threading.Thread(
            target=self._sender_loop,
            name=f"fprime-gds-stream-sender-{self._sub.id[:8]}",
            daemon=True,
        )
        started = False
        try:
            self._send({"type": KIND_HELLO, "subscriber_id": self._sub.id})
            thread.start()
            started = True
            self._receiver_loop()
        finally:
            self._stop.set()
            self._hub.unregister(self._sub)
            if started:
                thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------
    def _sender_loop(self) -> None:
        try:
            while not self._stop.is_set() and self._sub.active:
                batch = self._sub.drain(self._drain_timeout, self._batch_window)
                if not batch:
                    continue
                grouped, passthrough = _group_batch(batch)
                for kind, items in grouped.items():
                    if self._stop.is_set():
                        return
                    self._send_raw(_encode({"type": kind, "data": items}))
                for envelope in passthrough:
                    if self._stop.is_set():
                        return
                    self._send_raw(_encode(envelope))
        except Exception:
            logger.exception("StreamHub sender thread crashed")
            self._stop.set()

    def _receiver_loop(self) -> None:
        while not self._stop.is_set() and self._sub.active:
            try:
                message = self._ws.receive(timeout=self._receive_timeout)
            except Exception:
                break
            if message is None:
                continue
            # The receiver loop keeps the WS alive. Client messages are
            # accepted but currently ignored (subscribe-to-all). This
            # leaves the door open for future subscription narrowing
            # without a protocol change.

    # ------------------------------------------------------------------
    # Wire I/O
    # ------------------------------------------------------------------
    def _send(self, payload: Dict[str, Any]) -> None:
        self._send_raw(_encode(payload))

    def _send_raw(self, payload: str) -> None:
        with self._ws_lock:
            self._ws.send(payload)


# ---------------------------------------------------------------------------
# Flask route registration
# ---------------------------------------------------------------------------

def register_stream_routes(app, hub: StreamHub) -> bool:
    """Register the WebSocket route on the Flask app.

    Returns ``True`` if the route was registered, ``False`` if WebSocket
    support is unavailable or disabled via app configuration.
    """
    if not HAVE_FLASK_SOCK:
        logger.info("flask-sock not installed; WebSocket stream disabled")
        return False
    if not app.config.get("STREAM_ENABLED", True):
        logger.info("STREAM_ENABLED is False; WebSocket stream disabled")
        return False

    sock = Sock(app)
    drain_timeout = float(app.config.get("STREAM_DRAIN_TIMEOUT_S", DEFAULT_DRAIN_TIMEOUT_S))
    batch_window = float(app.config.get("STREAM_BATCH_WINDOW_S", DEFAULT_BATCH_WINDOW_S))
    max_depth = int(app.config.get("STREAM_QUEUE_DEPTH", DEFAULT_QUEUE_DEPTH))
    receive_timeout = float(app.config.get("STREAM_RECEIVE_TIMEOUT_S", DEFAULT_RECEIVE_TIMEOUT_S))

    @sock.route("/api/stream")
    def _stream(ws):  # pragma: no cover - exercised via integration tests
        session = _StreamSession(
            ws=ws,
            hub=hub,
            max_depth=max_depth,
            drain_timeout=drain_timeout,
            batch_window=batch_window,
            receive_timeout=receive_timeout,
        )
        session.run()

    return True
