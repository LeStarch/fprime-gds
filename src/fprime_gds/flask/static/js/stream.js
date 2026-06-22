/**
 * stream.js:
 *
 * WebSocket telemetry stream client for high-rate F Prime data. Replaces the
 * REST polling for /channels, /events, and /commands endpoints when the
 * server has the streaming route registered.
 *
 * The client subscribes to all channels, events, and commands. Subscription
 * filtering is intentionally omitted to keep the implementation minimal;
 * optimization via selective subscriptions can be added in a follow-up.
 *
 * Exponential-backoff reconnection is built in so a momentary server restart
 * does not require a page reload.
 */

const ENVELOPE_TYPE_CHANNEL = "channel";
const ENVELOPE_TYPE_EVENT = "event";
const ENVELOPE_TYPE_COMMAND = "command";
const ENVELOPE_TYPE_HELLO = "hello";
const ENVELOPE_TYPE_ERROR = "error";

// Map of stream envelope kinds -> datastore endpoint name.
const STREAM_KIND_TO_ENDPOINT = {
    [ENVELOPE_TYPE_CHANNEL]: "channels",
    [ENVELOPE_TYPE_EVENT]: "events",
    [ENVELOPE_TYPE_COMMAND]: "command_history",
};

const RECONNECT_MS_MIN = 250;
const RECONNECT_MS_MAX = 5000;

// Binary frame constants — must stay in sync with streams.py.
const BINARY_CHANNEL_MSG = 0x01;
const BINARY_HEADER_SIZE = 25;

class StreamClient {
    /**
     * @param {string} url - WebSocket URL (defaults to /api/stream on current origin).
     * @param {object} handlers - Map of endpoint name to a function called with
     *     an array of new items.
     */
    constructor(url, handlers) {
        this.url = url || StreamClient.defaultUrl();
        this.handlers = handlers || {};
        this.ws = null;
        this.connected = false;
        this.shutdown = false;
        this._reconnectMs = RECONNECT_MS_MIN;
        this._listeners = {
            statechange: [],
        };
        this._counters = {
            received: 0,
            errors: 0,
            reconnects: 0,
        };
    }

    static defaultUrl() {
        let proto = (window.location.protocol === "https:") ? "wss:" : "ws:";
        return `${proto}//${window.location.host}/api/stream`;
    }

    /**
     * Open the connection. Idempotent; calling twice does nothing.
     */
    start() {
        if (this.shutdown || this.ws) {
            return;
        }
        this._open();
    }

    /**
     * Close the connection permanently. Use when switching transport modes.
     */
    stop() {
        this.shutdown = true;
        if (this.ws) {
            try {
                this.ws.close();
            } catch (e) {
                // ignore
            }
            this.ws = null;
        }
        this._setConnected(false);
    }

    /**
     * Register a listener for connection state changes.
     */
    on(event, fn) {
        if (this._listeners[event]) {
            this._listeners[event].push(fn);
        }
    }

    counters() {
        return Object.assign({}, this._counters);
    }

    // ------------------------------------------------------------------
    // Internals
    // ------------------------------------------------------------------
    _open() {
        try {
            this.ws = new WebSocket(this.url);
        } catch (e) {
            this._scheduleReconnect();
            return;
        }
        this.ws.onopen = () => {
            this._reconnectMs = RECONNECT_MS_MIN;
            this._setConnected(true);
        };
        this.ws.onclose = () => this._handleClose();
        this.ws.onerror = () => {
            this._counters.errors += 1;
        };
        this.ws.binaryType = "arraybuffer";
        this.ws.onmessage = (event) => this._dispatch(event.data);
    }

    _handleClose() {
        if (this.ws) {
            this.ws = null;
        }
        this._setConnected(false);
        if (!this.shutdown) {
            this._scheduleReconnect();
        }
    }

    _scheduleReconnect() {
        this._counters.reconnects += 1;
        let delay = this._reconnectMs;
        this._reconnectMs = Math.min(this._reconnectMs * 2, RECONNECT_MS_MAX);
        setTimeout(() => {
            if (!this.shutdown) {
                this._open();
            }
        }, delay);
    }

    _setConnected(value) {
        if (this.connected !== value) {
            this.connected = value;
            for (let fn of this._listeners.statechange) {
                try { fn(value); } catch (e) { /* swallow */ }
            }
        }
    }

    _dispatch(raw) {
        if (raw instanceof ArrayBuffer) {
            this._dispatchBinary(raw);
            return;
        }
        let envelope;
        try {
            envelope = JSON.parse(raw);
        } catch (e) {
            this._counters.errors += 1;
            return;
        }
        this._counters.received += 1;
        let kind = envelope.type;
        if (kind in STREAM_KIND_TO_ENDPOINT) {
            this._invoke(STREAM_KIND_TO_ENDPOINT[kind], envelope.data);
            return;
        }
        switch (kind) {
            case ENVELOPE_TYPE_HELLO:
                break;
            case ENVELOPE_TYPE_ERROR:
                this._counters.errors += 1;
                console.warn("[stream] server reported error:", envelope.reason);
                break;
            default:
                break;
        }
    }

    /**
     * Dispatch a batched envelope's data to the registered handler.
     *
     * The server sends ``data`` as an array of samples coalesced from a
     * single drain tick. A scalar fallback is supported for compatibility.
     */
    _invoke(endpoint, data) {
        let handler = this.handlers[endpoint];
        if (!handler) {
            return;
        }
        let items;
        if (Array.isArray(data)) {
            items = data;
        } else if (data == null) {
            return;
        } else {
            items = [data];
        }
        if (items.length === 0) {
            return;
        }
        try {
            handler(items);
        } catch (e) {
            this._counters.errors += 1;
            console.error("[stream] handler error:", e);
        }
    }
    /**
     * Decode a binary WebSocket frame carrying a byte-array channel value.
     *
     * Frame layout (25-byte header + N-byte payload):
     *   [1B msg_type] [4B channel_id] [4B time_base] [4B time_context]
     *   [4B time_seconds] [4B time_useconds] [4B data_length] [raw bytes]
     */
    _dispatchBinary(buffer) {
        if (buffer.byteLength < BINARY_HEADER_SIZE) {
            this._counters.errors += 1;
            return;
        }
        let view = new DataView(buffer);
        let msgType = view.getUint8(0);
        if (msgType !== BINARY_CHANNEL_MSG) {
            this._counters.errors += 1;
            return;
        }
        let channelId = view.getUint32(1);
        let timeBase = view.getUint32(5);
        let timeContext = view.getUint32(9);
        let timeSeconds = view.getUint32(13);
        let timeUseconds = view.getUint32(17);
        let dataLength = view.getUint32(21);
        if (buffer.byteLength < BINARY_HEADER_SIZE + dataLength) {
            this._counters.errors += 1;
            return;
        }
        let payload = new Uint8Array(buffer, BINARY_HEADER_SIZE, dataLength);
        let item = {
            id: channelId,
            val: payload,
            time: {
                base: timeBase,
                context: timeContext,
                seconds: timeSeconds,
                microseconds: timeUseconds,
            },
            display_text: null,
            ert: null,
        };
        this._counters.received += 1;
        this._invoke(STREAM_KIND_TO_ENDPOINT[ENVELOPE_TYPE_CHANNEL], [item]);
    }
}

export {StreamClient};
