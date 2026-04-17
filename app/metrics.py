"""
Prometheus metrics for the TTS pipeline.

Exposes a /metrics HTTP endpoint that Prometheus can scrape.
Import the counters/histograms here and instrument them at call sites.

Metrics defined:
  tts_requests_total{status}     - counter  (status: cached | synthesized | failed)
  tts_chars_total{billed}        - counter  (billed: true | false)
  tts_synthesis_seconds          - histogram of GCP TTS call duration
  tts_active_connections         - gauge    of live WebSocket connections
  tts_circuit_breaker_open       - gauge    1 when circuit is open, 0 when closed
"""

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response

# ── Counters ────────────────────────────────────────────────────────────────

tts_requests = Counter(
    "tts_requests_total",
    "Total TTS chunk requests",
    ["status"],  # cached | synthesized | failed
)

tts_chars = Counter(
    "tts_chars_total",
    "Characters processed by the TTS pipeline",
    ["billed"],  # true = sent to GCP, false = served from cache
)

# ── Histograms ───────────────────────────────────────────────────────────────

tts_synthesis_seconds = Histogram(
    "tts_synthesis_seconds",
    "GCP TTS API call duration in seconds",
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0],
)

# ── Gauges ───────────────────────────────────────────────────────────────────

tts_active_connections = Gauge(
    "tts_active_connections",
    "Number of currently open WebSocket connections",
)

tts_circuit_breaker_open = Gauge(
    "tts_circuit_breaker_open",
    "1 if the GCP TTS circuit breaker is open (requests blocked), 0 if closed",
)


# ── FastAPI route ─────────────────────────────────────────────────────────────

def metrics_response() -> Response:
    """Returns a Prometheus text-format scrape response."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
