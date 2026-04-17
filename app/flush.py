"""
Nightly BigQuery flush: reads yesterday's Redis daily counters and writes
one row to the daily_stats table.

Called automatically by a background task in main.py at 00:05 UTC each day.
Can also be run as a standalone script:

    python -m app.flush [YYYY-MM-DD]

Environment variables:
    GCP_PROJECT  — GCP project ID (required; flush is skipped if unset)
    BQ_DATASET   — dataset name  (default: tts_pipeline)
    BQ_LOCATION  — dataset region (default: US)
"""

import asyncio
import os
import sys
from datetime import date, timedelta

from google.cloud import bigquery

from .storage import get_daily_stats
from .logger import get_logger

log = get_logger(__name__)

_PROJECT  = os.getenv("GCP_PROJECT")
_DATASET  = os.getenv("BQ_DATASET",  "tts_pipeline")
_TABLE    = "daily_stats"
_LOCATION = os.getenv("BQ_LOCATION", "US")

_COST_PER_CHAR = 16.0 / 1_000_000  # Neural2: $16 per 1M chars

_SCHEMA = [
    bigquery.SchemaField("date",               "DATE",    description="UTC calendar date"),
    bigquery.SchemaField("sessions_count",      "INTEGER", description="WebSocket sessions opened"),
    bigquery.SchemaField("chunks_total",        "INTEGER", description="Total sentence chunks processed"),
    bigquery.SchemaField("chunks_cached",       "INTEGER", description="Chunks served from Redis cache"),
    bigquery.SchemaField("chunks_failed",       "INTEGER", description="Chunks that failed to synthesize"),
    bigquery.SchemaField("cache_hit_rate",      "FLOAT",   description="chunks_cached / chunks_total"),
    bigquery.SchemaField("chars_billed",        "INTEGER", description="Characters sent to GCP TTS"),
    bigquery.SchemaField("chars_cached",        "INTEGER", description="Characters served from cache"),
    bigquery.SchemaField("estimated_cost_usd",  "FLOAT",   description="Estimated GCP TTS spend for the day"),
]


# ── BQ table bootstrap (called once) ─────────────────────────────────────────

def _ensure_table(client: bigquery.Client) -> str:
    ds_ref = f"{_PROJECT}.{_DATASET}"
    ds = bigquery.Dataset(ds_ref)
    ds.location = _LOCATION
    client.create_dataset(ds, exists_ok=True)

    table_ref = f"{ds_ref}.{_TABLE}"
    tbl = bigquery.Table(table_ref, schema=_SCHEMA)
    tbl.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="date",
        expiration_ms=365 * 24 * 3600 * 1000,  # auto-expire partitions after 1 year
    )
    tbl.require_partition_filter = True
    tbl.description = "One row per UTC calendar day — aggregated TTS usage and cost"
    client.create_table(tbl, exists_ok=True)
    return table_ref


# ── Public API ────────────────────────────────────────────────────────────────

async def flush_daily_stats(target_date: str | None = None) -> None:
    """
    Read Redis counters for target_date and write one row to BQ daily_stats.
    Defaults to yesterday (UTC) so the day is complete before flushing.
    Skips silently if GCP_PROJECT is unset or if all counters are zero.
    """
    if not _PROJECT:
        log.warning("bq_flush_skipped", reason="GCP_PROJECT not set")
        return

    d = target_date or (date.today() - timedelta(days=1)).isoformat()

    stats = await get_daily_stats(d)

    if stats["chunks_total"] == 0:
        log.info("bq_flush_skipped", date=d, reason="no traffic recorded")
        return

    chunks_total = stats["chunks_total"]
    chunks_cached = stats["chunks_cached"]
    cache_hit_rate = round(chunks_cached / chunks_total, 4) if chunks_total else 0.0

    row = {
        "date":               d,
        "sessions_count":     stats["sessions"],
        "chunks_total":       chunks_total,
        "chunks_cached":      chunks_cached,
        "chunks_failed":      stats["chunks_failed"],
        "cache_hit_rate":     cache_hit_rate,
        "chars_billed":       stats["chars_billed"],
        "chars_cached":       stats["chars_cached"],
        "estimated_cost_usd": round(stats["chars_billed"] * _COST_PER_CHAR, 6),
    }

    log.info("bq_flush_starting", date=d, **row)

    try:
        client = bigquery.Client(project=_PROJECT)
        table_ref = await asyncio.to_thread(_ensure_table, client)
        errors = await asyncio.to_thread(client.insert_rows_json, table_ref, [row])
        if errors:
            log.error("bq_flush_errors", date=d, errors=errors)
        else:
            log.info("bq_flush_done", date=d, cost_usd=row["estimated_cost_usd"])
    except Exception as exc:
        log.error("bq_flush_failed", date=d, error=str(exc))


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(flush_daily_stats(target))
