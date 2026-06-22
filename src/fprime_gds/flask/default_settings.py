####
# default_settings.py:
#
# Contains default setup for the F prime flask application. Specifically, it is used to pass configuration
# down to the GDS config layers, and is used to specify a dictionary and packet spec for specifying
# the event, channels, and commands setup.
#
# Note: flask configuration is all done via Python files
#
####
import os

STANDARD_PIPELINE_ARGUMENTS = os.environ.get("STANDARD_PIPELINE_ARGUMENTS").split("|")

SERVE_LOGS = os.environ.get("SERVE_LOGS", "YES") == "YES"

MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # Max length of request is 32MiB

JS_CONFIGURATION_FILE = os.path.join(os.path.dirname(__file__), "static", "js", "config.js")

# ---------------------------------------------------------------------------
# WebSocket telemetry stream (see fprime_gds.flask.streams).
# ---------------------------------------------------------------------------

# When True the /api/stream WebSocket route is registered (provided
# ``flask-sock`` is installed) and the front-end may use it instead of
# polling /channels and /events. When False the route is omitted and the
# front-end falls back to REST polling.
STREAM_ENABLED = os.environ.get("FP_STREAM_ENABLED", "YES") == "YES"

# Per-client outbox depth. When full, the oldest message is dropped so the
# F Prime decoder threads are never blocked by a slow consumer.
STREAM_QUEUE_DEPTH = int(os.environ.get("FP_STREAM_QUEUE_DEPTH", "1024"))

# Batch window (seconds) the sender thread waits after the first envelope
# before draining. Smaller values give faster updates; larger values
# reduce ws.send / browser JSON.parse calls. Default (28 ms) is sized for
# one F Prime frame at 35 Hz.
STREAM_BATCH_WINDOW_S = float(os.environ.get("FP_STREAM_BATCH_WINDOW_S", "0.028"))

# Default transport advertised to the front-end via /api/stream/status.
# Accepts "stream" or "poll". The browser uses this on every page load.
STREAM_DEFAULT_TRANSPORT = os.environ.get("FP_STREAM_DEFAULT_TRANSPORT", "stream").lower()

# TODO: load real config
