# TTS Pipeline

This project documents how a TTS pipeline evolves under latency and cost constraints — from a polling-based architecture (~4–6s) to sub-second WebSocket streaming — with production concerns like cost tracking, quotas, and failure handling built in alongside the trade-offs.

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js, TypeScript, Tailwind CSS |
| Backend | FastAPI, Python, uvicorn |
| TTS | Google Cloud TTS (Neural2 voices) |
| Cache | Redis (SHA256-keyed, 24h TTL) |
| Daily stats | BigQuery (`tts_pipeline.daily_stats`) |
| Observability | Prometheus metrics + live dashboard at `/dashboard` |

## How It Works

1. Browser detects sentence boundaries (`.` `!` `?`) and sends each sentence over a WebSocket
2. Backend checks Redis — cache hit returns audio in ~10ms, miss calls Google Cloud TTS (~500ms–1.5s)
3. Audio is returned as base64 over the same WebSocket and played in order
4. Daily counters (sessions, chunks, chars billed/cached, estimated cost) accumulate in Redis and are flushed to BigQuery at 00:05 UTC each night

## Running Locally

**Backend**
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
export REDIS_URL=redis://localhost:6379
export GCP_PROJECT=your-project-id

uvicorn app.main:app --host 0.0.0.0 --port 8080
```

**Frontend**
```bash
cd frontend
npm install && npm run dev
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to GCP service account JSON |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `GCP_PROJECT` | — | GCP project ID (required for BigQuery flush) |
| `BQ_DATASET` | `tts_pipeline` | BigQuery dataset name |
| `TTS_API_KEY` | — | Optional API key to protect the WebSocket endpoint |
| `TTS_DAILY_CHAR_LIMIT` | `50000` | Per-IP daily character budget |
| `CACHE_TTL_SECONDS` | `86400` | Redis audio cache TTL |

## Key Design Decisions & Trade-offs

**WebSocket over HTTP polling** — full-duplex: text goes up, audio comes down on the same connection. Polling would add latency equal to the poll interval.

**Client-side sentence detection** — avoids billing the TTS API for every keystroke. Trade-off: regex splits on abbreviations ("Dr.", "U.S.A.") and decimals.

**Audio as base64 inline** — eliminates the GCS write (~300ms) and a second browser HTTP fetch. Trade-off: ~33% size inflation, audio is not persisted.

**Redis cache** — SHA256(text + voice + speed) key means identical inputs always hit the same entry. Survives restarts, shared across instances, TTL-based eviction. Replaces the earlier in-process dict which was lost on every restart.

**BigQuery for daily stats** — Redis counters accumulate intraday (fast, cheap writes), then one row per day is flushed to BQ at midnight. Gives a permanent audit trail of usage and estimated GCP cost without writing to BQ on every request.

## How We Got Here

**Gen 1** — Text → GCS upload → browser polls `/job/{id}`. ~4–6s latency due to GCS writes and polling delays.

**Gen 2** — Added Pub/Sub + a separate worker process for batch/external callers. Ordering keys fixed out-of-order playback. Still too slow for live streaming.

**Gen 3 (current)** — WebSocket with inline synthesis. No GCS, no Pub/Sub, no polling. Latency dropped to ~500ms–1.5s. Redis replaced the in-process cache dict. BigQuery added for durable daily reporting.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for a full breakdown of every design decision, latency analysis, and component diagram.
