"""
Human-readable metrics dashboard served at GET /dashboard.
Auto-refreshes every 3 seconds. Reads live values from the
prometheus_client registry so it always reflects current state.
"""

from prometheus_client import REGISTRY
from fastapi.responses import HTMLResponse


def _sample(metric_name: str, label_filter: dict | None = None) -> float:
    """
    Pull a single sample value from the prometheus registry.

    prometheus_client strips '_total' from Counter names internally, so
    Counter('tts_requests_total') is stored as metric.name='tts_requests'.
    We try both the given name and the name with '_total' removed.
    We also skip '_created' timestamp samples (value ~1.7e9) and '_bucket'
    samples so we only ever return the actual counter/gauge value.
    """
    candidates = {metric_name, metric_name.removesuffix("_total")}
    for metric in REGISTRY.collect():
        if metric.name not in candidates:
            continue
        for sample in metric.samples:
            if sample.name.endswith(("_created", "_bucket", "_sum", "_count")):
                continue
            if label_filter is None or all(
                sample.labels.get(k) == v for k, v in label_filter.items()
            ):
                return sample.value
    return 0.0


def _histogram_percentile(metric_name: str, percentile: float) -> float:
    """
    Estimate a percentile from a Prometheus histogram's bucket samples.
    Uses linear interpolation within the matching bucket.
    """
    buckets: list[tuple[float, float]] = []  # (upper_bound, cumulative_count)
    total = 0.0

    for metric in REGISTRY.collect():
        if metric.name != metric_name:
            continue
        for sample in metric.samples:
            if sample.name.endswith("_bucket"):
                le = sample.labels.get("le", "+Inf")
                buckets.append((float("inf") if le == "+Inf" else float(le), sample.value))
            elif sample.name.endswith("_count"):
                total = sample.value

    if total == 0 or not buckets:
        return 0.0

    target = percentile * total
    prev_bound, prev_count = 0.0, 0.0
    for bound, count in sorted(buckets, key=lambda x: x[0]):
        if count >= target:
            if count == prev_count:
                return prev_bound
            # Linear interpolation
            frac = (target - prev_count) / (count - prev_count)
            upper = bound if bound != float("inf") else prev_bound * 2
            return prev_bound + frac * (upper - prev_bound)
        prev_bound, prev_count = bound, count

    return prev_bound


def build_dashboard() -> HTMLResponse:
    # ── Gather metrics ────────────────────────────────────────────────────────
    hits    = _sample("tts_requests_total", {"status": "cached"})
    misses  = _sample("tts_requests_total", {"status": "synthesized"})
    failed  = _sample("tts_requests_total", {"status": "failed"})
    total   = hits + misses + failed
    hit_pct = round((hits / total * 100) if total > 0 else 0, 1)

    chars_billed = _sample("tts_chars_total", {"billed": "true"})
    chars_saved  = _sample("tts_chars_total", {"billed": "false"})

    active_conns   = int(_sample("tts_active_connections"))
    breaker_open   = int(_sample("tts_circuit_breaker_open"))

    p50 = round(_histogram_percentile("tts_synthesis_seconds", 0.50) * 1000)
    p95 = round(_histogram_percentile("tts_synthesis_seconds", 0.95) * 1000)
    p99 = round(_histogram_percentile("tts_synthesis_seconds", 0.99) * 1000)

    # ── Derived display values ────────────────────────────────────────────────
    breaker_label = "OPEN — GCP degraded" if breaker_open else "Closed"
    breaker_color = "#ef4444" if breaker_open else "#22c55e"
    conn_color    = "#3b82f6" if active_conns > 0 else "#6b7280"
    hit_color     = "#22c55e" if hit_pct >= 50 else "#f59e0b" if hit_pct >= 20 else "#ef4444"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="refresh" content="3" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>TTS Pipeline — Metrics</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
      padding: 2rem;
    }}

    header {{
      display: flex;
      align-items: baseline;
      gap: 1rem;
      margin-bottom: 2rem;
      border-bottom: 1px solid #1e293b;
      padding-bottom: 1rem;
    }}

    header h1 {{ font-size: 1.25rem; font-weight: 600; color: #f8fafc; }}
    header span {{ font-size: 0.75rem; color: #64748b; }}

    .pulse {{
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      background: #22c55e;
      margin-right: 0.5rem;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.3; }}
    }}

    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }}

    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 1.25rem 1.5rem;
    }}

    .card .label {{
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #64748b;
      margin-bottom: 0.5rem;
    }}

    .card .value {{
      font-size: 2rem;
      font-weight: 700;
      line-height: 1;
    }}

    .card .sub {{
      font-size: 0.75rem;
      color: #94a3b8;
      margin-top: 0.4rem;
    }}

    .section-title {{
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #475569;
      margin-bottom: 0.75rem;
    }}

    .bar-row {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      margin-bottom: 0.6rem;
    }}
    .bar-label {{ font-size: 0.8rem; color: #94a3b8; width: 60px; text-align: right; }}
    .bar-track {{
      flex: 1;
      height: 8px;
      background: #1e293b;
      border-radius: 4px;
      overflow: hidden;
    }}
    .bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.4s ease; }}
    .bar-count {{ font-size: 0.75rem; color: #64748b; width: 50px; text-align: right; }}

    .latency-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }}
    .latency-table td {{
      padding: 0.4rem 0.75rem;
      border-bottom: 1px solid #1e293b;
    }}
    .latency-table td:last-child {{ text-align: right; font-weight: 600; }}
    .latency-table tr:last-child td {{ border-bottom: none; }}

    .two-col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
      margin-bottom: 2rem;
    }}
    .panel {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 1.25rem 1.5rem;
    }}

    footer {{
      font-size: 0.7rem;
      color: #334155;
      text-align: center;
      margin-top: 2rem;
    }}
  </style>
</head>
<body>

<header>
  <h1><span class="pulse"></span>TTS Pipeline — Live Metrics</h1>
  <span>auto-refreshes every 3s</span>
</header>

<!-- ── Top stat cards ── -->
<div class="grid">

  <div class="card">
    <div class="label">Active Connections</div>
    <div class="value" style="color:{conn_color}">{active_conns}</div>
    <div class="sub">WebSocket sessions open</div>
  </div>

  <div class="card">
    <div class="label">Circuit Breaker</div>
    <div class="value" style="color:{breaker_color}; font-size:1.1rem; padding-top:0.5rem">
      {breaker_label}
    </div>
    <div class="sub">Opens after 5 consecutive GCP failures</div>
  </div>

  <div class="card">
    <div class="label">Cache Hit Rate</div>
    <div class="value" style="color:{hit_color}">{hit_pct}%</div>
    <div class="sub">{int(hits)} cached / {int(total)} total requests</div>
  </div>

  <div class="card">
    <div class="label">Total Requests</div>
    <div class="value" style="color:#f8fafc">{int(total)}</div>
    <div class="sub">{int(failed)} failed</div>
  </div>

</div>

<!-- ── Request breakdown + Latency ── -->
<div class="two-col">

  <div class="panel">
    <div class="section-title">Request Breakdown</div>
    {"".join([
      _bar_row(label, count, total, color)
      for label, count, color in [
        ("Cached",      hits,   "#22c55e"),
        ("Synthesized", misses, "#3b82f6"),
        ("Failed",      failed, "#ef4444"),
      ]
    ])}
  </div>

  <div class="panel">
    <div class="section-title">GCP TTS Latency</div>
    {"" if misses == 0 else ""}
    <table class="latency-table">
      <tr><td style="color:#94a3b8">p50 (median)</td><td style="color:#22c55e">{p50} ms</td></tr>
      <tr><td style="color:#94a3b8">p95</td><td style="color:#f59e0b">{p95} ms</td></tr>
      <tr><td style="color:#94a3b8">p99</td><td style="color:#ef4444">{p99} ms</td></tr>
      <tr><td style="color:#94a3b8">Samples</td><td style="color:#64748b">{int(misses)}</td></tr>
    </table>
    {"<p style='font-size:0.75rem;color:#475569;margin-top:0.75rem'>No GCP calls yet — all zeros until first synthesis.</p>" if misses == 0 else ""}
  </div>

</div>

<!-- ── Character metering ── -->
<div class="two-col">

  <div class="panel">
    <div class="section-title">Characters — GCP Billed</div>
    <div style="font-size:1.75rem;font-weight:700;color:#ef4444">{int(chars_billed):,}</div>
    <div style="font-size:0.75rem;color:#94a3b8;margin-top:0.4rem">sent to Google Cloud TTS API</div>
  </div>

  <div class="panel">
    <div class="section-title">Characters — Saved by Cache</div>
    <div style="font-size:1.75rem;font-weight:700;color:#22c55e">{int(chars_saved):,}</div>
    <div style="font-size:0.75rem;color:#94a3b8;margin-top:0.4rem">served from Redis, never billed</div>
  </div>

</div>

<footer>Raw Prometheus format → <a href="/metrics" style="color:#3b82f6">/metrics</a></footer>

</body>
</html>"""
    return HTMLResponse(content=html)


def _bar_row(label: str, count: float, total: float, color: str) -> str:
    pct = (count / total * 100) if total > 0 else 0
    return f"""
    <div class="bar-row">
      <span class="bar-label">{label}</span>
      <div class="bar-track">
        <div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div>
      </div>
      <span class="bar-count">{int(count)}</span>
    </div>"""
