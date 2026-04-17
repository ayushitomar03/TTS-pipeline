# TTS Pipeline — Architecture Document

## What This System Does

A user types text into a browser. As they type, completed sentences are detected and sent over a WebSocket to a FastAPI backend. The backend calls Google Cloud TTS, gets back MP3 audio, and sends it directly to the browser as base64. The browser decodes and plays the audio in order, one sentence at a time.

The result: audio begins playing before the user finishes typing.

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
├── In-memory cache (SHA256 dict)
│   └── hit  → encode to base64, return immediately
│   └── miss → call Google Cloud TTS API
│
└── ThreadPoolExecutor (4 workers)
    └── runs TTS calls without blocking the event loop
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

**Trade-off:** Base64 encoding inflates data size by ~33%. A 30KB MP3 becomes ~40KB in the message. For typical sentence-length audio this is negligible, but for long paragraphs it adds up. The bigger issue: once the browser tab closes, the audio is gone — there's no URL to share or revisit.

---

### 4. In-process memory cache (not Redis, not GCS)

**Decision:** Synthesized audio bytes are cached in a Python dict keyed by SHA256(text + voice + speed).

**Why:** If the user types the same sentence twice in a session, the second synthesis is instant — no TTS API call, no network round-trip. The cache key is content-addressed: identical text with identical voice/speed always maps to the same key, so the cache is automatically deduplicated.

**Trade-off:** The cache is process-local and ephemeral. A server restart clears it. Multiple server instances (e.g., behind a load balancer) each have their own cache and don't share hits. The cache also grows unbounded — there's no eviction. Long-running servers serving many unique sentences will accumulate memory indefinitely. A production system would use Redis with a TTL, which survives restarts and is shared across instances.

---

### 5. ThreadPoolExecutor for TTS calls

**Decision:** TTS synthesis runs in a thread pool (4 workers), not directly in the async event loop.

**Why:** `google.cloud.texttospeech.TextToSpeechClient()` is synchronous — it blocks while waiting for the API response (~500ms–1.5s). Calling it directly in an async function would block the entire event loop, freezing all other WebSocket connections for the duration. The thread pool runs it off the event loop, keeping the server responsive.

**Trade-off:** 4 workers means a maximum of 4 concurrent TTS calls per server process. The 5th request queues behind them. For a single-user local tool this is fine. Under load, this becomes a bottleneck. The right fix at scale is to use an async TTS client (Google provides one) or increase workers, but more workers means more concurrent API calls which can hit GCP rate limits.

---

### 6. Ordered audio playback in the browser

**Decision:** The frontend maintains a FIFO queue. Even if sentence 3 finishes synthesizing before sentence 2, it waits to play until sentence 2 finishes.

**Why:** TTS API latency is non-deterministic. A longer sentence sent first can take longer to synthesize than a shorter sentence sent second. Without ordering, the audio would play out of sequence ("World hello" instead of "Hello world").

**Trade-off:** Ordering means a slow sentence blocks all subsequent ones. If sentence 2 takes 2 seconds and sentence 3 was cached (instant), sentence 3 is still delayed. An alternative is to play sentences out of order but display them in order — but that's confusing UX when audio doesn't match visual position. The current approach trades throughput for predictability.

---

## Latency Breakdown

For a new sentence (cache miss):

| Step | Time |
|---|---|
| Sentence boundary detected in browser | ~0ms (regex, local) |
| WebSocket send to backend | ~1ms (localhost) |
| Thread pool dispatch | ~1ms |
| Google Cloud TTS API call | 500ms – 1500ms |
| base64 encode + WebSocket send back | ~2ms |
| Browser decode + audio element creation | ~5ms |
| **Total (new sentence)** | **~510ms – 1510ms** |

For a repeated sentence (cache hit):

| Step | Time |
|---|---|
| Memory cache lookup | <1ms |
| base64 encode + WebSocket send back | ~2ms |
| Browser decode | ~5ms |
| **Total (cached)** | **~10ms** |

The TTS API call is the dominant cost. Everything else is noise.

---

## What Is Good About This Design

**Low latency for the use case.** Audio starts within one sentence of the user typing. They hear the first sentence while still writing the second.

**Simple and auditable.** The entire backend is ~70 lines. There's no message queue, no worker process, no database, no cron job. If something breaks, there are very few places to look.

**No storage costs.** No GCS bucket, no Pub/Sub topic. The only billable resource is Google Cloud TTS API calls (charged per character synthesized).

**Content-addressed caching.** The same sentence synthesized with the same voice at the same speed will always produce identical audio. The SHA256 cache key guarantees this — no need for a lookup table or separate cache invalidation logic.

**Parallel synthesis.** Multiple sentences can be synthesized concurrently (up to 4 via the thread pool). Sentence 2 doesn't wait for sentence 1 to finish before starting — they race, and both are ready by the time they need to play.

---

## What Can Be Improved

### Sentence tokenization
Replace the regex with a proper tokenizer. The current `/[^.!?]*[.!?]+(?:\s|$)/g` splits on every period — abbreviations like "Mr.", "U.S.A.", and decimal numbers like "3.14" all cause false splits, producing incorrect audio chunks.

### Persistent cache
Move the in-memory dict to Redis with a TTL (e.g., 24 hours). This survives server restarts, is shared across multiple instances, and supports eviction so it doesn't grow unbounded.

### Auto-reconnect backoff
The current reconnect logic retries every 2 seconds unconditionally. Under a sustained outage (e.g., server deploying), this hammers the server with connection attempts the moment it comes back up. Exponential backoff (2s, 4s, 8s… capped at 30s) would be more polite.

### Streaming audio (chunked synthesis)
Google Cloud TTS returns the entire audio for a sentence before sending anything. A more advanced approach uses the Streaming API (available in v2) which returns audio chunks as they're generated — audio could start playing mid-sentence. This would bring latency down from ~500ms to ~100ms for the first audible sound.

### Authentication
The WebSocket endpoint is open — anyone who can reach the server can make TTS API calls billed to your GCP account. Adding an API key check or origin validation before accepting the WebSocket would prevent abuse.

### Voice selection per session
Currently voice and speed are sent per-chunk, meaning the client could theoretically send different voices in the same session. It works, but it's cleaner to negotiate voice/speed at connection time via query parameters and lock them for the session.

---

## Why We Dropped GCS and Pub/Sub

The project went through three generations:

**Generation 1 — Feed pipeline:** Text POSTed to `/feed`, background watcher polled every 200ms, synthesized audio uploaded to GCS, browser fetched public URL. Latency: ~4–6 seconds. The GCS write took ~300ms and status tracking through GCS added another ~2–3 seconds per status change.

**Generation 2 — Async pipeline:** Text published to Pub/Sub, a separate worker consumed messages, uploaded to GCS, caller polled `/job/{id}`. Designed for external batch callers who submit jobs and retrieve results later. Added ordering keys to fix out-of-order playback.

**Generation 3 — WebSocket streaming (current):** Client sends text, server synthesizes and sends audio directly back. No storage. No polling. No external queue. Latency dropped to ~500ms–1.5s.

GCS was dropped because the streaming use case doesn't need permanent storage — the audio is consumed immediately and discarded. Pub/Sub was dropped because there's no longer a decoupled worker; synthesis happens inline in the request handler. The architectural complexity those systems added was justified when there were multiple consumers, durable job state, and retry semantics to manage. With a direct WebSocket, none of that applies.
