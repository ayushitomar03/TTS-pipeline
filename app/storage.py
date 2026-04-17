import hashlib
import os

import redis.asyncio as aioredis

_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# How long a cached audio entry lives in Redis before expiring.
# Default 24h — covers a full day's session without burning memory forever.
_CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", str(24 * 3600)))

# Module-level Redis client with a connection pool.
# Created once at first use, reused for the lifetime of the process.
# max_connections=20 → up to 20 simultaneous Redis ops per worker process.
_client: aioredis.Redis | None = None


def _get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            _REDIS_URL,
            max_connections=20,
            decode_responses=False,  # audio is raw bytes, not strings
        )
    return _client


def _cache_key(text: str, voice: str, speed: float) -> str:
    digest = hashlib.sha256(f"{text}|{voice}|{speed}".encode()).hexdigest()
    return f"tts:{digest}"


async def get_cached(text: str, voice: str, speed: float) -> bytes | None:
    """
    Returns cached MP3 bytes for this (text, voice, speed) triple, or None on
    a cache miss or Redis failure. Redis failure is treated as a miss so the
    server degrades gracefully instead of returning errors to the client.
    """
    try:
        return await _get_client().get(_cache_key(text, voice, speed))
    except Exception:
        return None


async def set_cached(text: str, voice: str, speed: float, audio_bytes: bytes) -> None:
    """
    Stores MP3 bytes in Redis with a TTL. Fire-and-forget — failure to cache
    is not fatal; the audio was already synthesized and will be returned to
    the client regardless.
    """
    try:
        await _get_client().set(
            _cache_key(text, voice, speed),
            audio_bytes,
            ex=_CACHE_TTL,
        )
    except Exception:
        pass


# ── Per-IP daily character quota ─────────────────────────────────────────────

def _quota_key(ip: str) -> str:
    from datetime import date
    return f"quota:{ip}:{date.today().isoformat()}"


async def get_daily_chars(ip: str) -> int:
    """Returns how many characters this IP has synthesized today."""
    try:
        val = await _get_client().get(_quota_key(ip))
        return int(val) if val else 0
    except Exception:
        return 0


async def increment_daily_chars(ip: str, chars: int) -> int:
    """
    Increments the daily char counter for this IP and returns the new total.
    Sets a 25-hour TTL on first write so the key expires after the day rolls over.
    """
    try:
        key = _quota_key(ip)
        new_total = await _get_client().incrby(key, chars)
        if new_total == chars:
            # First write of the day — set TTL so it auto-expires
            await _get_client().expire(key, 25 * 3600)
        return new_total
    except Exception:
        return 0


# ── Global daily aggregate counters ──────────────────────────────────────────
# One set of counters per calendar day (UTC). Used by the nightly flush job
# to write a single daily_stats row to BigQuery without storing per-session rows.

def _daily_key(field: str, date_str: str | None = None) -> str:
    from datetime import date
    d = date_str or date.today().isoformat()
    return f"daily:{field}:{d}"


async def _incr_daily(field: str, amount: int = 1) -> None:
    """Increment a daily counter. Sets a 48h TTL on first write. Fire-and-forget."""
    try:
        key = _daily_key(field)
        new_val = await _get_client().incrby(key, amount)
        if new_val == amount:
            await _get_client().expire(key, 48 * 3600)
    except Exception:
        pass


async def incr_daily_sessions() -> None:
    await _incr_daily("sessions")

async def incr_daily_chunks_total() -> None:
    await _incr_daily("chunks_total")

async def incr_daily_chunks_cached() -> None:
    await _incr_daily("chunks_cached")

async def incr_daily_chunks_failed() -> None:
    await _incr_daily("chunks_failed")

async def incr_daily_chars_billed(chars: int) -> None:
    await _incr_daily("chars_billed", chars)

async def incr_daily_chars_cached(chars: int) -> None:
    await _incr_daily("chars_cached", chars)


async def get_daily_stats(date_str: str) -> dict[str, int]:
    """Read all daily aggregate counters for a given date (YYYY-MM-DD)."""
    fields = ["sessions", "chunks_total", "chunks_cached", "chunks_failed", "chars_billed", "chars_cached"]
    try:
        keys = [_daily_key(f, date_str) for f in fields]
        values = await _get_client().mget(keys)
        return {f: int(v or 0) for f, v in zip(fields, values)}
    except Exception:
        return {f: 0 for f in fields}
