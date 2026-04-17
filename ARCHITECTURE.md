# TTS Pipeline — Architecture Document

## What This System Does

A user types text into a browser. As they type, completed sentences are detected and sent over a WebSocket to a FastAPI backend. The backend checks Redis for a cached result — a hit returns audio in ~10ms. On a miss, it calls Google Cloud TTS, caches the result in Redis, and sends the MP3 audio directly to the browser as base64. The browser decodes and plays the audio in order, one sentence at a time.

Daily usage counters (sessions, chunks, characters billed, cache hits, estimated cost) accumulate in Redis throughout the day and are flushed as a single row to BigQuery at 00:05 UTC each night.

The result: audio begins playing before the user finishes typing, with a full observability and cost-tracking layer underneath.

---

## System Diagram

```
Browser (Next.js)
│
│  WebSocket (ws://localhost:8080/ws/stream)
│
│  send → { chunk_id, text, voice, speed }
│  recv ← { chunk_id, status: "processing" }
│  recv ← { chunk_id, status: "done", audio_b64, cached }
│
FastAPI Server
│
├── Per-IP daily quota check (Redis)
│   └── rejects if IP exceeds TTS_DAILY_CHAR_LIMIT chars/day
│
├── Redis cache (SHA256 keyed, 24h TTL)
│   └── hit  → base64 encode → send immediately (~10ms)
│   └── miss → call Google Cloud TTS API
│
├── Circuit breaker
│   └── opens after 5 consecutive GCP failures
│   └── returns retryable=true errors while open
│
├── Global concurrency semaphore (20 slots)
│   └── caps simultaneous GCP TTS calls across all connections
│
└── Per-connection concurrency semaphore (4 slots)
    └── caps parallel chunks per WebSocket session

Redis
├── tts:{sha256}          → MP3 bytes (24h TTL) — audio cache
├── quota:{ip}:{date}     → char count (25h TTL) — per-IP daily budget
└── daily:{field}:{date}  → int counters (48h TTL) — aggregated stats

BigQuery (tts_pipeline.daily_stats)
└── One row per UTC day: sessions, chunks, cache_hit_rate,
    chars_billed, chars_cached, estimated_cost_usd
    Partitioned by date, 1-year retention.
```

---

## Key Design Decisions

### 1. WebSocket over HTTP polling or SSE

**Decision:** Persistent WebSocket connection for both sending text and receiving audio.

**Why:** HTTP polling would require the browser to constantly ask "is audio ready?" — wasteful and adds latency equal to the poll interval. SSE (Server-Sent Events) is one-directional — good for server-pushing events but can't receive text chunks. WebSocket is full-duplex: text goes up, audio comes down, over the same connection.

**Trade-off:** WebSockets require the server to maintain a stateful connection per client. Under high concurrency (thousands of simultaneous users), connection count becomes a resource concern. HTTP is stateless and scales more naturally behind load balancers.

---

### 2. Sentence boundary detection on the client

**Decision:** The browser detects `.`, `!`, `?` boundaries and sends complete sentences, not individual keystrokes.

**Why:** Sending every keystroke to the backend would be expensive — the TTS API charges per character, and synthesizing "H", "He", "Hel", "Hell", "Hello" for a single word is both wasteful and produces incorrect audio. Sentence-level chunking gives natural prosody (the TTS model synthesizes a complete thought, not a fragment).

**Trade-off:** Sentence detection by regex is imperfect. "Dr. Smith arrived at 3 p.m. today." gets split at every period. Abbreviations, decimal numbers, and ellipses all trigger false splits. A proper sentence tokenizer (spaCy, NLTK) would be more accurate but adds latency and complexity.

**The 800ms debounce:** If the user pauses without completing a sentence, the remaining partial text is sent after 800ms. This balances responsiveness against accuracy — too short and partial sentences produce choppy audio, too long and the user waits.

---

### 3. Audio sent as base64 over the WebSocket (not stored to GCS)

**Decision:** Audio bytes are base64-encoded and sent directly in the WebSocket message. Nothing is written to disk or cloud storage.

**Why:** The previous design uploaded every sentence to GCS and sent the browser a public URL. This added a ~300ms GCS write + the browser then had to make a second HTTP request to fetch the audio. For a streaming use case, that round-trip is pure overhead. Base64 in the WebSocket message eliminates both.

**Trade-off:** Base64 encoding inflates data size by ~33%. A 30KB MP3 becomes ~40KB in the message. For typical sentence-length audio this is negligible, but for long paragraphs it adds up. Once the browser tab closes, the audio is gone — there's no URL to share or revisit.

---

### 4. Redis cache (replaces in-process dict)

**Decision:** Synthesized audio bytes are cached in Redis, keyed by `SHA256(text + voice + speed)` with a 24h TTL.

**Why:** The earlier design used a Python dict in process memory. It was fast but ephemeral — a server restart cleared it, and multiple instances behind a load balancer each had separate caches with no shared hits. Redis fixes both: it survives restarts, is shared across all instances, and TTL-based eviction prevents unbounded growth.

**Cache key design:** `tts:{sha256hex}` — content-addressed, so the same sentence at the same voice and speed always maps to the same key with no coordination needed. If two server instances synthesize the same text simultaneously, the second write is a harmless overwrite.

**Trade-off:** Redis is an external dependency. If Redis is unavailable, cache lookups and writes fail. The server treats Redis failure as a cache miss — synthesis still works, it just becomes more expensive (GCP calls instead of cache hits). This is intentional: degrade gracefully rather than return errors.

---

### 5. Async TTS synthesis (replaces ThreadPoolExecutor)

**Decision:** TTS synthesis uses Google's async client and runs directly in the event loop under a semaphore.

**Why:** The earlier design used `ThreadPoolExecutor` with 4 workers because the synchronous TTS client blocked the event loop. Google's async client removes that constraint — synthesis becomes a proper coroutine that yields at every `await`, letting the event loop handle other connections during the wait.

**Concurrency controls:** Two semaphores gate access:
- **Global semaphore (20 slots):** caps total simultaneous GCP calls across all connections, preventing rate limit hits
- **Per-connection semaphore (4 slots):** caps parallel chunks per session, so one fast-typing user can't starve others

**Trade-off:** Semaphore limits are tuned for a single-instance deployment. Under horizontal scaling, the global semaphore is per-process — total concurrency = 20 × number of instances. If GCP rate limits are hit, increase slots carefully.

---

### 6. Circuit breaker for GCP failures

**Decision:** A circuit breaker tracks consecutive GCP TTS failures. After 5 failures, it opens and rejects synthesis requests immediately (without calling GCP) until it resets.

**Why:** Without a circuit breaker, a GCP outage causes every incoming request to wait for the full timeout (~30s) before failing. This queues up connections, exhausts the semaphore slots, and makes the server unresponsive. The circuit breaker fails fast and returns `retryable: true` so the client can back off and retry later.

**Trade-off:** A brief GCP blip can trigger the circuit breaker and block synthesis for longer than the underlying issue lasted. The reset timer and manual override endpoints (`/debug/circuit-breaker/open` and `/close`, only available when `DEBUG=true`) allow recovery without a restart.

---

### 7. Per-IP daily character quota

**Decision:** Each client IP is limited to `TTS_DAILY_CHAR_LIMIT` characters synthesized per UTC day (default: 50,000).

**Why:** The WebSocket endpoint, if open, allows anyone who can reach the server to make TTS API calls billed to the GCP account. Even with API key auth, a single misbehaving client could run up cost. The quota is a second line of defence — it caps the blast radius per IP.

**Implementation:** Redis key `quota:{ip}:{YYYY-MM-DD}` with a 25h TTL (auto-expires after the day rolls over). `INCRBY` is atomic — no race condition when multiple chunks arrive concurrently from the same IP.

**Trade-off:** IP-based quotas are easy to circumvent (VPN, rotating IPs). They're a cost-protection measure, not a security control. A production system would tie quotas to authenticated user IDs.

---

### 8. Ordered audio playback in the browser

**Decision:** The frontend maintains a FIFO queue. Even if sentence 3 finishes synthesizing before sentence 2, it waits to play until sentence 2 finishes.

**Why:** TTS API latency is non-deterministic. A longer sentence sent first can take longer to synthesize than a shorter sentence sent second. Without ordering, the audio would play out of sequence ("World hello" instead of "Hello world").

**Trade-off:** Ordering means a slow sentence blocks all subsequent ones. If sentence 2 takes 2 seconds and sentence 3 was cached (instant), sentence 3 is still delayed. An alternative is to play sentences out of order but display them in order — but that's confusing UX when audio doesn't match visual position. The current approach trades throughput for predictability.

---

### 9. BigQuery for daily stats (not per-request writes)

**Decision:** Daily aggregate counters (sessions, chunks total/cached/failed, chars billed/cached) accumulate in Redis throughout the day. At 00:05 UTC, a background task reads them and writes a single row to BigQuery `tts_pipeline.daily_stats`.

**Why:** Writing to BigQuery on every TTS request would add ~200ms of latency per chunk (BQ streaming insert round-trip) and incur streaming insert costs. Redis `INCRBY` is ~0.5ms and free at this scale. The daily flush pattern gives a permanent, queryable cost and usage history without touching the hot path.

**Schema:**
```
date               DATE     — UTC calendar date
sessions_count     INTEGER  — WebSocket sessions opened
chunks_total       INTEGER  — total sentence chunks processed
chunks_cached      INTEGER  — chunks served from Redis
chunks_failed      INTEGER  — chunks that failed synthesis
cache_hit_rate     FLOAT    — chunks_cached / chunks_total
chars_billed       INTEGER  — characters sent to GCP TTS
chars_cached       INTEGER  — characters served from cache
estimated_cost_usd FLOAT    — chars_billed × $16/1M (Neural2 rate)
```

**Table config:** Partitioned by `date`, 1-year partition expiry, `require_partition_filter = true` to prevent accidental full-table scans.

**Trade-off:** Stats are one day stale — you can't query today's running totals from BQ. For intraday monitoring, the `/dashboard` endpoint reads live Prometheus counters. For historical trend analysis, BQ is the source of truth.

---

### 10. Prometheus metrics + live dashboard

**Decision:** Key counters and histograms are exposed at `/metrics` (Prometheus scrape format) and visualised at `/dashboard` (auto-refreshing HTML, no JS framework).

**Metrics tracked:**
- `tts_requests_total{status}` — cached / synthesized / failed
- `tts_chars_total{billed}` — true (GCP) / false (cache)
- `tts_synthesis_seconds` — histogram of GCP call duration (p50/p95/p99)
- `tts_active_connections` — live WebSocket count
- `tts_circuit_breaker_open` — 1 when open, 0 when closed

**Why:** The dashboard gives instant visibility into cache hit rate, GCP spend, and latency percentiles without needing a Grafana setup. The `/metrics` endpoint lets you plug in Prometheus + Grafana later without changing the instrumentation.

---

## Latency Breakdown

For a new sentence (cache miss):

| Step | Time |
|---|---|
| Sentence boundary detected in browser | ~0ms (regex, local) |
| WebSocket send to backend | ~1ms (localhost) |
| Redis cache lookup (miss) | ~1ms |
| Per-IP quota check (Redis INCRBY) | ~1ms |
| Google Cloud TTS API call (async) | 500ms – 1500ms |
| Redis cache write | ~1ms (fire-and-forget) |
| base64 encode + WebSocket send back | ~2ms |
| Browser decode + audio element creation | ~5ms |
| **Total (new sentence)** | **~510ms – 1510ms** |

For a repeated sentence (cache hit):

| Step | Time |
|---|---|
| Redis cache lookup (hit) | ~1ms |
| base64 encode + WebSocket send back | ~2ms |
| Browser decode | ~5ms |
| **Total (cached)** | **~10ms** |

The GCP TTS API call is the dominant cost. Everything else is noise.

---

## What Is Good About This Design

**Low latency for the use case.** Audio starts within one sentence of the user typing. They hear the first sentence while still writing the second.

**Shared, persistent cache.** Redis survives restarts and is shared across all server instances. The same sentence is never synthesized twice — not just within a session, but across sessions and server restarts.

**Cost visibility.** The BigQuery daily_stats table gives a per-day breakdown of GCP spend, cache savings, and usage volume. The `/dashboard` shows the same data live.

**Graceful degradation.** Redis failure → treated as cache miss, synthesis still works. GCP failure → circuit breaker opens, returns retryable errors instead of hanging. Neither takes the server down.

**Content-addressed caching.** `SHA256(text + voice + speed)` means the cache key is deterministic and collision-resistant. No lookup table, no cache invalidation logic.

---

## What Can Be Improved

### Sentence tokenization
Replace the regex with a proper tokenizer. The current `/[^.!?]*[.!?]+(?:\s|$)/g` splits on every period — abbreviations like "Mr.", "U.S.A.", and decimal numbers like "3.14" all cause false splits.

### Auto-reconnect backoff
The current reconnect logic retries every 2 seconds unconditionally. Exponential backoff (2s, 4s, 8s… capped at 30s) would be more polite under sustained outages.

### Streaming audio (chunked synthesis)
Google Cloud TTS returns the entire audio for a sentence before sending anything. The Streaming API (v2) returns audio chunks as they're generated — first audible sound could drop from ~500ms to ~100ms.

### Quota by user identity
Per-IP quotas are easy to circumvent. Tying quotas to authenticated user IDs (via `TTS_API_KEY` + a user identity token) would make the budget meaningful.

### BigQuery intraday stats
The nightly flush means BQ is always one day stale. For real-time cost monitoring, publish counters to a time-series store (Cloud Monitoring, Datadog) in addition to the daily BQ flush.

---

## Why We Dropped GCS and Pub/Sub

The project went through three generations:

**Generation 1 — Feed pipeline:** Text POSTed to `/feed`, background watcher polled every 200ms, synthesized audio uploaded to GCS, browser fetched public URL. Latency: ~4–6 seconds. The GCS write took ~300ms and status tracking through GCS added another ~2–3 seconds per status change.

**Generation 2 — Async pipeline:** Text published to Pub/Sub, a separate worker consumed messages, uploaded to GCS, caller polled `/job/{id}`. Designed for external batch callers who submit jobs and retrieve results later. Added ordering keys to fix out-of-order playback.

**Generation 3 — WebSocket streaming (current):** Client sends text, server synthesizes and sends audio directly back. Redis replaced both the in-process dict and GCS for caching. BigQuery added for durable daily reporting. Prometheus + dashboard added for live observability. Latency dropped to ~500ms–1.5s.

GCS was dropped because the streaming use case doesn't need permanent storage — audio is consumed immediately and discarded. Pub/Sub was dropped because synthesis happens inline; there's no decoupled worker. The architectural complexity those systems added was justified when there were multiple consumers, durable job state, and retry semantics to manage. With a direct WebSocket, none of that applies.
