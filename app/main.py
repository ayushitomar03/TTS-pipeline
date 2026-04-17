import asyncio
import base64
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware

from .tts_engine import synthesize, RETRYABLE_ERRORS, force_breaker_open, force_breaker_closed
from .storage import (
    get_cached, set_cached,
    get_daily_chars, increment_daily_chars,
    incr_daily_sessions, incr_daily_chunks_total,
    incr_daily_chunks_cached, incr_daily_chunks_failed,
    incr_daily_chars_billed, incr_daily_chars_cached,
)
from .flush import flush_daily_stats
from .logger import get_logger
from .metrics import (
    tts_requests,
    tts_chars,
    tts_synthesis_seconds,
    tts_active_connections,
    metrics_response,
)
from .dashboard import build_dashboard

log = get_logger(__name__)


async def _nightly_flush_loop() -> None:
    """Background task that flushes yesterday's Redis counters to BQ at 00:05 UTC."""
    while True:
        now = datetime.now(timezone.utc)
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())
        await flush_daily_stats()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_nightly_flush_loop())
    yield
    task.cancel()


app = FastAPI(title="TTS Pipeline", version="1.0.0", lifespan=lifespan)

_ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_API_KEY = os.getenv("TTS_API_KEY", "")
_GLOBAL_TTS_SEM = asyncio.Semaphore(int(os.getenv("TTS_GLOBAL_CONCURRENCY", "20")))
_PER_CONN_CONCURRENCY = int(os.getenv("TTS_PER_CONN_CONCURRENCY", "4"))

# Input validation
_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "500"))

# Daily character budget per IP. Default 50,000 chars ≈ ~500 sentences/day.
_DAILY_CHAR_LIMIT = int(os.getenv("TTS_DAILY_CHAR_LIMIT", "50000"))


# ── Debug routes (only registered when DEBUG=true) ────────────────────────────

if os.getenv("DEBUG", "").lower() == "true":
    @app.post("/debug/circuit-breaker/open")
    def debug_breaker_open():
        force_breaker_open()
        return {"circuit_breaker": "open"}

    @app.post("/debug/circuit-breaker/close")
    def debug_breaker_close():
        force_breaker_closed()
        return {"circuit_breaker": "closed"}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/voices")
def voices():
    from .tts_engine import list_voices
    return {"voices": list_voices()}


@app.get("/metrics")
def metrics():
    return metrics_response()


@app.get("/dashboard")
def dashboard():
    return build_dashboard()


@app.get("/stats/{ip}")
async def stats(ip: str):
    """Returns how many characters this IP has synthesized today."""
    used = await get_daily_chars(ip)
    return {"ip": ip, "chars_today": used, "daily_limit": _DAILY_CHAR_LIMIT}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket):
    # ── Auth ─────────────────────────────────────────────────────────────────
    if _API_KEY:
        token = (
            websocket.headers.get("x-api-key")
            or websocket.query_params.get("api_key")
        )
        if token != _API_KEY:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()

    client_ip = websocket.client.host if websocket.client else "unknown"
    conn_id = str(uuid.uuid4())[:8]

    tts_active_connections.inc()
    log.info("ws_connected", conn_id=conn_id, ip=client_ip)
    asyncio.create_task(incr_daily_sessions())

    conn_sem = asyncio.Semaphore(_PER_CONN_CONCURRENCY)

    async def process_chunk(chunk_id: str, text: str, voice: str, speed: float) -> dict:
        chars = len(text)

        # ── Input validation ─────────────────────────────────────────────────
        if chars > _MAX_CHARS:
            log.warning("text_too_long", conn_id=conn_id, chars=chars, limit=_MAX_CHARS)
            asyncio.create_task(incr_daily_chunks_total())
            asyncio.create_task(incr_daily_chunks_failed())
            return {
                "chunk_id": chunk_id,
                "status": "failed",
                "audio_b64": None,
                "error": f"text exceeds {_MAX_CHARS} character limit",
                "retryable": False,
            }

        # ── Per-IP daily quota ───────────────────────────────────────────────
        daily_total = await increment_daily_chars(client_ip, chars)
        if daily_total > _DAILY_CHAR_LIMIT:
            log.warning("quota_exceeded", conn_id=conn_id, ip=client_ip, daily_total=daily_total)
            asyncio.create_task(incr_daily_chunks_total())
            asyncio.create_task(incr_daily_chunks_failed())
            return {
                "chunk_id": chunk_id,
                "status": "failed",
                "audio_b64": None,
                "error": "daily character quota exceeded",
                "retryable": False,
            }

        # ── Cache lookup ─────────────────────────────────────────────────────
        try:
            cached_bytes = await get_cached(text, voice, speed)
        except Exception:
            cached_bytes = None

        if cached_bytes:
            tts_requests.labels(status="cached").inc()
            tts_chars.labels(billed="false").inc(chars)
            log.info("cache_hit", conn_id=conn_id, chunk_id=chunk_id, chars=chars, voice=voice)
            asyncio.create_task(incr_daily_chunks_total())
            asyncio.create_task(incr_daily_chunks_cached())
            asyncio.create_task(incr_daily_chars_cached(chars))
            return {
                "chunk_id": chunk_id,
                "status": "done",
                "audio_b64": base64.b64encode(cached_bytes).decode(),
                "cached": True,
            }

        # ── GCP TTS synthesis ────────────────────────────────────────────────
        try:
            t0 = time.monotonic()
            async with _GLOBAL_TTS_SEM:
                audio = await synthesize(text, voice, speed)
            elapsed = time.monotonic() - t0
            latency_ms = round(elapsed * 1000, 1)

            tts_synthesis_seconds.observe(elapsed)
            tts_requests.labels(status="synthesized").inc()
            tts_chars.labels(billed="true").inc(chars)
            log.info(
                "tts_synthesized",
                conn_id=conn_id,
                chunk_id=chunk_id,
                chars=chars,
                voice=voice,
                latency_ms=round(elapsed * 1000),
            )

            await set_cached(text, voice, speed, audio)
            asyncio.create_task(incr_daily_chunks_total())
            asyncio.create_task(incr_daily_chars_billed(chars))

            return {
                "chunk_id": chunk_id,
                "status": "done",
                "audio_b64": base64.b64encode(audio).decode(),
                "cached": False,
            }
        except Exception as exc:
            tts_requests.labels(status="failed").inc()
            log.error(
                "tts_error",
                conn_id=conn_id,
                chunk_id=chunk_id,
                chars=chars,
                voice=voice,
                error=str(exc),
            )
            asyncio.create_task(incr_daily_chunks_total())
            asyncio.create_task(incr_daily_chunks_failed())
            # RuntimeError = circuit breaker open; RETRYABLE_ERRORS = transient GCP failures
            retryable = isinstance(exc, RETRYABLE_ERRORS) or isinstance(exc, RuntimeError)
            return {
                "chunk_id": chunk_id,
                "status": "failed",
                "audio_b64": None,
                "error": str(exc),
                "retryable": retryable,
            }

    try:
        while True:
            data = await websocket.receive_json()
            chunk_id = data.get("chunk_id", str(uuid.uuid4()))
            text = data.get("text", "").strip()
            voice = data.get("voice", "en-US-Neural2-C")
            speed = float(data.get("speed", 1.0))

            if not text:
                continue

            await websocket.send_json({"chunk_id": chunk_id, "status": "processing"})

            async def handle(cid=chunk_id, t=text, v=voice, s=speed):
                async with conn_sem:
                    result = await process_chunk(cid, t, v, s)
                try:
                    await websocket.send_json(result)
                except Exception:
                    pass

            asyncio.create_task(handle())

    except WebSocketDisconnect:
        log.info("ws_disconnected", conn_id=conn_id, ip=client_ip)
    except Exception as exc:
        log.error("ws_error", conn_id=conn_id, ip=client_ip, error=str(exc))
    finally:
        tts_active_connections.dec()
