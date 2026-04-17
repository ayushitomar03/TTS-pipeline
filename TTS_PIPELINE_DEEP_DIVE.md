# TTS Pipeline — Complete Deep Dive Documentation

---

# Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [System Architecture](#2-system-architecture)
3. [feed.py — The Text Source](#3-feedpy--the-text-source)
4. [watcher.py — The Detector](#4-watcherpy--the-detector)
5. [jobs.py — The Status Board](#5-jobspy--the-status-board)
6. [storage.py — The File Manager](#6-storagepy--the-file-manager)
7. [tts_engine.py — The Voice](#7-tts_enginepy--the-voice)
8. [queue.py — The External Inbox](#8-queuepy--the-external-inbox)
9. [worker.py — The Pub/Sub Consumer](#9-workerpy--the-pubsub-consumer)
10. [main.py — The API Server](#10-mainpy--the-api-server)
11. [index.html — The Browser UI](#11-indexhtml--the-browser-ui)
12. [Infrastructure — GCP Components](#12-infrastructure--gcp-components)
13. [Complete Data Flow](#13-complete-data-flow)
14. [Why Each Decision Was Made](#14-why-each-decision-was-made)

---

# 1. What This System Does

At its core, this system takes a piece of text and turns it into audio.

That sounds simple. The complexity comes from doing it:
- **Fast** (under 3 seconds)
- **At scale** (multiple requests at the same time)
- **Without duplicating work** (same text = reuse existing audio)
- **In order** (text sent first plays first)
- **Reliably** (failures are retried, nothing is lost)

The system is a pipeline. A pipeline means data flows through a series of stages, each doing one job, passing results to the next.

```
Stage 1: Text arrives
Stage 2: Is this text already converted? (cache check)
Stage 3: If not, convert it (TTS API)
Stage 4: Save the audio file (GCS)
Stage 5: Tell the browser it's ready
Stage 6: Browser plays the audio
```

Each stage is a separate component in the code.

---

# 2. System Architecture

## What "architecture" means

Architecture is the blueprint of how components connect. Before writing a single line of code, you decide:
- What components exist
- What each one is responsible for
- How they talk to each other
- Where data lives

## The components and their relationships

```
┌─────────────────────────────────────────────────────────────────┐
│                         Browser (index.html)                     │
│                                                                   │
│   ┌─────────────┐     SSE stream      ┌─────────────────────┐   │
│   │  Text Input  │ ─────────────────→ │   Left Panel        │   │
│   │  (textarea)  │                    │   (text cards)       │   │
│   └──────┬───────┘                    └─────────────────────┘   │
│          │ POST /feed                                             │
│          ↓                            ┌─────────────────────┐   │
│   ┌─────────────┐   poll /job/{id}    │   Right Panel       │   │
│   │  trackJob() │ ←────────────────→ │   (audio players)    │   │
│   └─────────────┘                    └─────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
                │ POST /feed
                ↓
┌──────────────────────────────────────────────────────────────────┐
│                       FastAPI Server (main.py)                    │
│                                                                   │
│   ┌──────────┐    ┌──────────┐    ┌───────────┐   ┌──────────┐  │
│   │ /feed    │    │/feed/    │    │/synthesize│   │/job/{id} │  │
│   │ endpoint │    │stream    │    │-async     │   │ endpoint │  │
│   └────┬─────┘    └────┬─────┘    └─────┬─────┘   └────┬─────┘  │
│        │               │                │               │         │
│        ↓               ↓                ↓               ↓         │
│   ┌──────────────────────────────────────────────────────────┐   │
│   │                    feed.py                               │   │
│   │   _lines[]      _subscribers[]    _last_processed_index  │   │
│   └───────────────────────┬──────────────────────────────────┘   │
│                           │                                       │
│                    ┌──────┴──────┐                                │
│                    │  watcher.py  │ (background asyncio task)     │
│                    │  polls feed  │                                │
│                    └──────┬───────┘                               │
│                           │ run_in_executor                       │
│                           ↓                                       │
│                    ┌──────────────┐                               │
│                    │  Thread Pool  │ (5 threads)                  │
│                    │ _process_line │                               │
│                    └──────┬───────┘                               │
│                           │                                       │
│          ┌────────────────┼────────────────┐                     │
│          ↓                ↓                ↓                     │
│   ┌────────────┐  ┌─────────────┐  ┌───────────┐               │
│   │ tts_engine │  │  storage.py  │  │  jobs.py  │               │
│   │ (TTS API)  │  │ (GCS files)  │  │ (memory)  │               │
│   └────────────┘  └─────────────┘  └───────────┘               │
└──────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────┴───────────┐
                    │   Google Cloud (GCP)   │
                    │                       │
                    │  ┌─────────────────┐  │
                    │  │  TTS API        │  │
                    │  │ (Neural2 voices) │  │
                    │  └─────────────────┘  │
                    │                       │
                    │  ┌─────────────────┐  │
                    │  │  Cloud Storage  │  │
                    │  │  (MP3 files)    │  │
                    │  └─────────────────┘  │
                    │                       │
                    │  ┌─────────────────┐  │
                    │  │    Pub/Sub      │  │
                    │  │ (batch jobs)    │  │
                    │  └─────────────────┘  │
                    └───────────────────────┘
```

---

# 3. feed.py — The Text Source

## The full code

```python
import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

_lines: list[dict] = []
_subscribers: list[asyncio.Queue] = []
_last_processed_index: int = 0


def get_all_lines() -> list[dict]:
    return list(_lines)


def get_unprocessed_lines() -> list[dict]:
    global _last_processed_index
    new = _lines[_last_processed_index:]
    _last_processed_index = len(_lines)
    return new


async def add_line(text: str) -> dict:
    entry = {
        "id": len(_lines),
        "job_id": str(uuid.uuid4()),
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _lines.append(entry)
    for q in _subscribers:
        await q.put(entry)
    return entry


async def stream_events() -> AsyncGenerator[str, None]:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    try:
        for line in _lines:
            yield f"data: {json.dumps(line)}\n\n"
        while True:
            entry = await q.get()
            yield f"data: {json.dumps(entry)}\n\n"
    finally:
        _subscribers.remove(q)
```

## Module-level variables — why they exist outside any function

The three variables `_lines`, `_subscribers`, `_last_processed_index` are declared at the **module level**, not inside a class or function.

In Python, when you import a module, it runs once and its variables live in memory for the lifetime of the program. This means these variables are shared across all requests. Every call to `add_line()`, every call to `stream_events()`, every call to `get_unprocessed_lines()` — they all access the same `_lines` list in memory.

The underscore prefix (`_lines` not `lines`) is a Python convention meaning "this is private to this module, don't access it from outside."

## `_lines: list[dict] = []`

This is a Python list that holds dictionaries. Each dict looks like:
```python
{
    "id": 0,                           # position in the list (0, 1, 2...)
    "job_id": "abc-123-def-456",       # UUID for tracking this specific job
    "text": "Hello world",             # the actual text to convert
    "timestamp": "2026-04-12T18:33Z"   # when it was added (ISO 8601 format)
}
```

**Why a list and not a database?**
Speed. A Python list append is O(1) — instant, no matter how many items are in it. A database write involves network I/O, disk I/O, and transaction overhead. For a feed that might receive 10 items per second, memory is the right choice.

**Tradeoff:** When the server restarts, the list is empty. This is acceptable because the audio files are permanently stored in GCS. Only the display history is lost.

## `uuid.uuid4()` — what a UUID is

UUID stands for Universally Unique Identifier. `uuid4()` generates a random 128-bit number formatted as a 36-character string:
```
"550e8400-e29b-41d4-a716-446655440000"
```

The probability of generating two identical UUIDs is astronomically small (~1 in 5.3 × 10^36). This means you can generate job IDs on any server, at any time, without coordination, and they'll never collide.

Why not just use `id` (the list index)? Because the ID needs to be assigned before the item is added to the list, and it needs to be unique across server restarts. A sequential number would reset to 0 on restart.

## `_subscribers: list[asyncio.Queue] = []`

This list holds one `asyncio.Queue` per open browser tab.

**What is asyncio.Queue?**

An `asyncio.Queue` is a thread-safe, async-compatible data structure with two operations:
- `put(item)` — add an item to the queue (non-blocking)
- `get()` — remove and return an item. If empty, **suspends the coroutine** until something is put in.

The suspension is the key part. When `stream_events()` calls `await q.get()` and the queue is empty, Python's event loop pauses that coroutine and runs other work. When `add_line()` calls `q.put(entry)`, the event loop wakes up the suspended `stream_events()` coroutine and gives it the new item.

This is how real-time push works without polling.

## `_last_processed_index: int = 0`

This is a pointer into `_lines`. The watcher uses it to know which lines it has already seen.

```
_lines = [line0, line1, line2, line3, line4]
                              ↑
               _last_processed_index = 3
```

`get_unprocessed_lines()` returns `_lines[3:]` = `[line3, line4]`, then moves the index to 5.

Next call: `_lines[5:]` = `[]` (nothing new).

**Why `global _last_processed_index`?**

In Python, if you assign to a variable inside a function (`_last_processed_index = len(_lines)`), Python treats it as a local variable. The `global` keyword tells Python: "when I assign to this variable, modify the module-level one, not a local copy."

## `add_line()` — deep dive

```python
async def add_line(text: str) -> dict:
    entry = {
        "id": len(_lines),
        "job_id": str(uuid.uuid4()),
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _lines.append(entry)
    for q in _subscribers:
        await q.put(entry)
    return entry
```

**Why `async def`?**

Because `await q.put(entry)` is an async operation. In asyncio, you can only use `await` inside an `async def` function. Even though `q.put()` doesn't actually suspend here (it's instant since the queue has no size limit), it must still be declared async to use the `await` keyword.

**`datetime.now(timezone.utc).isoformat()`**

- `datetime.now(timezone.utc)` creates a timezone-aware datetime object representing the current moment in UTC
- `.isoformat()` converts it to a string: `"2026-04-12T18:33:51.411826+00:00"`
- UTC (Coordinated Universal Time) is used instead of local time because servers might be in different timezones

**`for q in _subscribers: await q.put(entry)`**

This loops through every connected browser tab and drops the new entry into each one's queue. Each tab's `stream_events()` coroutine is waiting on `await q.get()` — as soon as we `put()`, they all wake up and send the data to their respective browsers.

## `stream_events()` — SSE in depth

```python
async def stream_events() -> AsyncGenerator[str, None]:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    try:
        for line in _lines:
            yield f"data: {json.dumps(line)}\n\n"
        while True:
            entry = await q.get()
            yield f"data: {json.dumps(entry)}\n\n"
    finally:
        _subscribers.remove(q)
```

**`AsyncGenerator[str, None]`**

This is a type hint saying: "this function is an async generator that yields strings and returns None when done." An async generator is like a regular generator (uses `yield`) but can also use `await`.

**What `yield` means in a generator**

A regular function runs, returns a value, and exits. A generator function pauses at each `yield`, returns that value to the caller, and resumes from where it paused on the next call.

```python
# Regular function
def get_items():
    return [1, 2, 3]   # returns all at once

# Generator
def get_items():
    yield 1             # returns 1, pauses
    yield 2             # returns 2, pauses
    yield 3             # returns 3, pauses
```

`StreamingResponse` in FastAPI calls `yield` one at a time, sending each piece to the browser as it's produced.

**The SSE format**

SSE has a specific text format:
```
data: {"id": 0, "text": "Hello"}\n\n
```

- Must start with `data: `
- Must end with `\n\n` (two newlines)
- Single `\n` is a multi-line event continuation
- Double `\n\n` signals end of one event

**The `try/finally` block**

```python
try:
    ...  # main logic
finally:
    _subscribers.remove(q)  # always runs, even if an exception occurs
```

When the browser closes the tab, the HTTP connection drops. FastAPI detects this and raises a `GeneratorExit` exception inside the generator. The `finally` block always executes regardless, removing this tab's queue from `_subscribers`. Without this cleanup, dead queues would accumulate and `add_line()` would waste time putting items into queues nobody is reading.

**Why send existing lines first**

```python
for line in _lines:
    yield f"data: {json.dumps(line)}\n\n"
```

If you open the page and there are already 5 text lines, you should see them immediately. This loop sends all historical lines before starting the live stream. After this, the `while True` loop takes over for new arrivals.

---

# 4. watcher.py — The Detector

## The full code

```python
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from google.cloud import storage
from . import feed
from .tts_engine import synthesize
from .storage import upload_audio, get_cached_url
from .jobs import set_status

GCS_BUCKET = os.environ.get("GCS_BUCKET", "tts-pipeline-audio-prod")
POLL_INTERVAL = 0.2

_executor = ThreadPoolExecutor(max_workers=5)


def _process_line_sync(job_id, text, voice, speed):
    print(f"[watcher] processing {job_id}: {text[:50]}")
    try:
        cached = get_cached_url(GCS_BUCKET, text, voice, speed)
        if cached:
            set_status(job_id, "done", cached)
            return

        set_status(job_id, "processing")
        audio = synthesize(text, voice, speed)
        url = upload_audio(audio, GCS_BUCKET, text, voice, speed)
        set_status(job_id, "done", url)
    except Exception as e:
        print(f"[watcher] failed {job_id}: {e}")
        set_status(job_id, "failed")


async def watch():
    loop = asyncio.get_running_loop()
    print("[watcher] started")
    while True:
        new_lines = feed.get_unprocessed_lines()
        for line in new_lines:
            set_status(line["job_id"], "queued")
            loop.run_in_executor(
                _executor,
                _process_line_sync,
                line["job_id"],
                line["text"],
                "en-US-Neural2-C",
                1.0,
            )
        await asyncio.sleep(POLL_INTERVAL)
```

## Python's asyncio event loop — what it actually is

Your computer's CPU executes one instruction at a time (ignoring multi-core for now). When you write a web server, you need to handle many requests "simultaneously." There are two ways to do this:

**Option 1: Threads**
Create one thread per request. Each thread runs independently. The OS switches between them rapidly, giving the illusion of parallelism.

**Option 2: Event loop (asyncio)**
Run everything in one thread, but switch between tasks whenever one is waiting for I/O.

asyncio uses Option 2. The event loop is an infinite loop that:
1. Checks: which coroutines are ready to run?
2. Runs each one until it hits an `await`
3. While one coroutine waits (e.g., for a network response), runs another
4. When the wait is over, resumes the original coroutine

```
Time →
Event loop: [handle_request_A] [handle_request_B] [resume_A] [handle_request_C] ...
                   ↓                  ↓               ↓
              hits await          hits await      A's I/O done
              (waiting)           (waiting)       (resumes)
```

This is called **cooperative multitasking** — tasks voluntarily yield control at `await` points.

## Why some code can't run in the event loop

The event loop works great for I/O-bound tasks (network, disk) that spend most time waiting. But it breaks down for **CPU-bound** or **blocking** operations:

```python
# This BLOCKS the event loop for 2 seconds
# No other requests can be handled during this time
result = some_api_call_that_takes_2_seconds()
```

The Google TTS API call creates a new HTTPS connection, sends data, waits for a response. This can take 1-3 seconds of actual waiting. During those seconds, the event loop is stuck — it can't handle any other browser requests, SSE streams, or anything else.

## `ThreadPoolExecutor` — the solution

```python
_executor = ThreadPoolExecutor(max_workers=5)
```

A `ThreadPoolExecutor` manages a pool of threads. When you submit work to it:
1. Work is assigned to an available thread
2. That thread runs the code **outside** the event loop
3. The event loop continues handling other things
4. When the thread finishes, it notifies the event loop
5. The event loop resumes the awaiting coroutine with the result

```python
loop.run_in_executor(_executor, _process_line_sync, job_id, text, voice, speed)
```

This says: "run `_process_line_sync(job_id, text, voice, speed)` in one of the executor's threads. Don't wait for it — continue immediately."

**Why `max_workers=5`?**

Each worker thread can handle one TTS job at a time. With 5 workers:
- Up to 5 jobs process simultaneously
- Each job takes ~2 seconds
- Throughput: ~2-3 jobs/second

More workers = more parallelism, but also more memory (each thread uses ~8MB) and more risk of hitting Google's API rate limits.

## `os.environ.get("GCS_BUCKET", "tts-pipeline-audio-prod")`

`os.environ` is a dictionary of all environment variables. `get(key, default)` returns the value of `key` if it exists, otherwise returns `default`.

Environment variables are how you configure applications without hardcoding values. On your Mac:
```bash
export GCS_BUCKET=tts-pipeline-audio-prod
```

On Cloud Run, you set them in the deployment config. The code doesn't need to change between environments — just the env vars.

## `_process_line_sync` — every line explained

```python
def _process_line_sync(job_id, text, voice, speed):
```

Note: this is a regular `def`, not `async def`. It runs in a thread, not the event loop. No `await` allowed here.

```python
    try:
```

Wrapping everything in try/except ensures that if anything fails (network error, GCS error, TTS error), we catch it and mark the job as failed instead of crashing the thread silently.

```python
        cached = get_cached_url(GCS_BUCKET, text, voice, speed)
        if cached:
            set_status(job_id, "done", cached)
            return
```

Before making an expensive TTS API call, check if we've already converted this exact text with this exact voice and speed. If yes, return the existing URL immediately. This is a cache hit — takes ~200ms instead of ~2000ms.

```python
        set_status(job_id, "processing")
```

Update the job status immediately. The browser is polling `/job/{id}` every 500ms. This ensures the UI shows "processing" badge quickly, giving the user feedback that work is happening.

```python
        audio = synthesize(text, voice, speed)
```

Call Google's TTS API. This makes an HTTPS request to Google's servers, waits for a response, and returns raw MP3 bytes. Takes 1-2 seconds.

```python
        url = upload_audio(audio, GCS_BUCKET, text, voice, speed)
```

Upload the MP3 bytes to Google Cloud Storage. This creates a publicly accessible file at a permanent URL. Takes ~0.5 seconds.

```python
        set_status(job_id, "done", url)
```

Update job status with the final URL. The next time the browser polls `/job/{id}`, it gets `{"status": "done", "url": "https://..."}` and shows the audio player.

```python
    except Exception as e:
        print(f"[watcher] failed {job_id}: {e}")
        set_status(job_id, "failed")
```

`Exception` catches almost everything: network errors, authentication errors, timeouts, GCS errors, etc. (It doesn't catch `SystemExit` or `KeyboardInterrupt`, which is intentional.) We log the error and mark the job failed so the UI can show an error state.

## `await asyncio.sleep(POLL_INTERVAL)`

```python
await asyncio.sleep(0.2)
```

This is fundamentally different from `time.sleep(0.2)`.

`time.sleep(0.2)` blocks the entire thread for 200ms. In an async context, this would freeze the event loop.

`asyncio.sleep(0.2)` tells the event loop: "I'm done for now, wake me up in 200ms." The event loop can handle other work during those 200ms.

---

# 5. jobs.py — The Status Board

## The full code

```python
from typing import Optional

_store: dict[str, dict] = {}


def set_status(job_id: str, status: str, url: str = "") -> None:
    _store[job_id] = {"job_id": job_id, "status": status, "url": url}


def get_status(job_id: str) -> Optional[dict]:
    return _store.get(job_id)
```

## Why this exists — the GCS problem

Originally, job status was written to GCS (Google Cloud Storage). Here's what that looked like:

```
set_status("processing"):
  1. Create storage.Client()     ← authenticates with Google (~100ms)
  2. Get bucket reference        ← no network call (local object)
  3. Create blob reference       ← no network call (local object)
  4. blob.upload_from_string()   ← HTTPS request to GCS (~500-2000ms)
  5. blob.make_public()          ← another HTTPS request (~500ms)
```

Each status update = 1-2 seconds. With 3 updates per job (queued, processing, done), that's 3-6 seconds of overhead just for status tracking — before any TTS work.

## What a Python dict is at the machine level

A Python dict is a **hash table**. When you do `_store[job_id] = value`:

1. Python calls `hash(job_id)` — a fast O(1) operation that converts the string to an integer
2. Uses that integer to find a slot in an array
3. Stores the value in that slot

Reading `_store.get(job_id)` does the same hash, finds the slot, returns the value.

This is O(1) — it takes the same amount of time whether the dict has 1 entry or 1,000,000 entries. And it happens in nanoseconds (billionths of a second) in RAM.

Compare to GCS: O(network_latency) — typically 50-500ms.

## Thread safety consideration

The watcher runs `_process_line_sync` in background threads. These threads call `set_status()`, which modifies `_store`. The main event loop calls `get_status()` to read from `_store`.

**Is this safe?**

In CPython (the standard Python interpreter), the **GIL (Global Interpreter Lock)** ensures that only one thread executes Python bytecode at a time. This means dict operations are effectively atomic — you can't have two threads corrupting the dict simultaneously.

If you were using a different Python implementation (like Jython or PyPy without GIL), you'd need to add locking.

## What happens on server restart

`_store` is a module-level variable in RAM. When the server process exits, RAM is freed. The dict is gone.

For production use, replace `jobs.py` with a Redis client:
```python
import redis
r = redis.Redis(host='localhost', port=6379)

def set_status(job_id, status, url=""):
    r.setex(job_id, 3600, json.dumps({...}))  # expires after 1 hour

def get_status(job_id):
    data = r.get(job_id)
    return json.loads(data) if data else None
```

Redis is an in-memory database that persists to disk and survives restarts, while still being millisecond-fast.

---

# 6. storage.py — The File Manager

## The full code

```python
import hashlib
import os
import uuid
from google.cloud import storage


def _cache_key(text: str, voice: str, speed: float) -> str:
    raw = f"{text}|{voice}|{speed}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_cached_url(bucket_name, text, voice, speed):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob_name = f"audio/{_cache_key(text, voice, speed)}.mp3"
    blob = bucket.blob(blob_name)
    if blob.exists():
        return blob.public_url
    return None


def upload_audio(audio_bytes, bucket_name, text, voice, speed):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob_name = f"audio/{_cache_key(text, voice, speed)}.mp3"
    blob = bucket.blob(blob_name)
    blob.upload_from_string(audio_bytes, content_type="audio/mpeg")
    blob.make_public()
    return blob.public_url


def save_locally(audio_bytes, output_dir="/tmp/tts_output"):
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{uuid.uuid4()}.mp3")
    with open(filepath, "wb") as f:
        f.write(audio_bytes)
    return filepath
```

## SHA256 — how it actually works

SHA256 is a **cryptographic hash function**. It takes any input (text, file, bytes) and produces a fixed 256-bit (64 hex character) output called a digest.

```
Input:  "Hello world|en-US-Neural2-C|1.0"
Output: "4fd177a47d3e00bc8cb5cd5fb33a9c80..."  (64 hex chars)
```

**Properties that make it useful for caching:**

1. **Deterministic**: Same input always produces same output. "Hello world" will always hash to the same 64-char string.

2. **Fixed size**: Whether your input is 1 character or 1 million characters, the output is always 64 characters.

3. **Avalanche effect**: Changing one character completely changes the output. "Hello world" and "hello world" produce completely different hashes.

4. **Collision resistance**: Finding two different inputs that produce the same output would take longer than the age of the universe with current computers.

**What `hashlib.sha256(raw.encode()).hexdigest()` does step by step:**

```python
raw = "Hello world|en-US-Neural2-C|1.0"

# Step 1: encode() converts string to bytes
# Python strings are Unicode (abstract characters)
# SHA256 needs bytes (raw binary data)
raw_bytes = raw.encode()
# raw_bytes = b'Hello world|en-US-Neural2-C|1.0'

# Step 2: sha256() creates a hash object and processes the bytes
hash_obj = hashlib.sha256(raw_bytes)

# Step 3: hexdigest() returns the hash as a hex string
result = hash_obj.hexdigest()
# result = "4fd177a47d3e00bc8cb5cd5fb33a9c8049026d870fbfa90c75ce6608aabfd094"
```

**Why include voice and speed in the hash?**

"Hello world" at speed 1.0 and "Hello world" at speed 2.0 are different audio files — different duration, different rhythm. The hash must include all parameters that affect the output.

```python
raw = f"{text}|{voice}|{speed}"
```

The `|` separator prevents ambiguity. Without it:
- text="ab", voice="cd" → "abcd"
- text="a", voice="bcd" → "abcd"  ← same hash, different input!

## How Google Cloud Storage works

GCS organizes files in **buckets** (like folders) and **blobs** (like files).

```
Bucket: tts-pipeline-audio-prod
├── audio/
│   ├── 4fd177a4...mp3    (cached TTS audio)
│   └── 7e67066a...mp3
└── jobs/
    └── abc-123.json      (old: job status files)
```

**Creating a client:**
```python
client = storage.Client()
```
This creates an authenticated connection to GCS using `GOOGLE_APPLICATION_CREDENTIALS`. It reads your `credentials.json` file and exchanges it for an access token.

**Getting a bucket:**
```python
bucket = client.bucket(bucket_name)
```
This creates a local Python object representing the bucket. No network call yet.

**Getting a blob:**
```python
blob = bucket.blob(blob_name)
```
Again, local object only. No network call.

**Checking existence:**
```python
blob.exists()
```
This makes one HTTPS GET request to GCS: "does this file exist?" Returns True or False.

**Uploading:**
```python
blob.upload_from_string(audio_bytes, content_type="audio/mpeg")
```
Makes an HTTPS PUT request, sending the bytes to GCS. The `content_type` tells browsers how to handle the file (as audio, not as a generic download).

**Making public:**
```python
blob.make_public()
```
Updates the file's access control list (ACL) to allow public reads. After this, anyone with the URL can download the file without authentication.

**`blob.public_url`:**
Returns the public URL format:
```
https://storage.googleapis.com/{bucket_name}/{blob_name}
https://storage.googleapis.com/tts-pipeline-audio-prod/audio/4fd177a4....mp3
```

## `save_locally` — the fallback

```python
def save_locally(audio_bytes, output_dir="/tmp/tts_output"):
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{uuid.uuid4()}.mp3")
    with open(filepath, "wb") as f:
        f.write(audio_bytes)
    return filepath
```

Used in local development when `GCS_BUCKET` is not set.

`os.makedirs(output_dir, exist_ok=True)` creates the directory if it doesn't exist. `exist_ok=True` means don't raise an error if it already exists.

`open(filepath, "wb")` opens a file for writing in binary mode (`b`). MP3 is binary data — if you open in text mode, Python might corrupt it by converting line endings.

`f.write(audio_bytes)` writes the raw bytes to disk.

Note: local files use random UUIDs as names (not cache keys) because there's no cache check for local files.

---

# 7. tts_engine.py — The Voice

## The full code

```python
from google.cloud import texttospeech

AVAILABLE_VOICES = {
    "en-US-Neural2-C": "English (US) - Female",
    "en-US-Neural2-D": "English (US) - Male",
    "en-US-Neural2-F": "English (US) - Female 2",
    "en-US-Neural2-J": "English (US) - Male 2",
    "en-GB-Neural2-A": "English (UK) - Female",
    "en-GB-Neural2-B": "English (UK) - Male",
}


def synthesize(text, voice="en-US-Neural2-C", speed=1.0) -> bytes:
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=voice[:5],
        name=voice,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speed,
    )
    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )
    return response.audio_content


def list_voices() -> dict:
    return AVAILABLE_VOICES
```

## What happens when you call `synthesize()`

**Step 1: Create the client**
```python
client = texttospeech.TextToSpeechClient()
```
Creates an authenticated gRPC connection to Google's TTS API servers. gRPC is a high-performance protocol built on HTTP/2 used by Google's internal services.

**Step 2: Build the request**
```python
synthesis_input = texttospeech.SynthesisInput(text=text)
```
Wraps your text in a protobuf message. Protocol Buffers (protobuf) is Google's format for serializing structured data — like JSON but binary and faster.

```python
voice_params = texttospeech.VoiceSelectionParams(
    language_code=voice[:5],   # "en-US" (first 5 chars)
    name=voice,                # "en-US-Neural2-C" (full name)
)
```

`voice[:5]` is Python slice notation — takes characters at positions 0, 1, 2, 3, 4. For "en-US-Neural2-C", that's "en-US".

The language code tells Google which language model to use. The full voice name selects the specific voice within that language.

```python
audio_config = texttospeech.AudioConfig(
    audio_encoding=texttospeech.AudioEncoding.MP3,
    speaking_rate=speed,
)
```

`AudioEncoding.MP3` is an enum value (a named constant). Alternatives are `LINEAR16` (uncompressed WAV, much larger), `OGG_OPUS` (good compression, not universal browser support).

`speaking_rate` is a float between 0.25 (very slow) and 4.0 (very fast). 1.0 is normal speed.

**Step 3: Make the API call**
```python
response = client.synthesize_speech(
    input=synthesis_input,
    voice=voice_params,
    audio_config=audio_config,
)
```

This sends an HTTPS/gRPC request to Google's servers. On their end:
1. The text is preprocessed: numbers are spelled out, abbreviations expanded
2. The text is split into phonemes (sound units)
3. A neural network generates a waveform
4. The waveform is encoded as MP3
5. The MP3 bytes are returned

**What Neural2 means:**

Neural2 voices use a neural network architecture called a WaveNet or similar deep learning model. They're trained on thousands of hours of human speech. The result sounds natural — varying pitch, natural pauses, emphasis. Earlier voices (Standard, Wavenet) are either robotic or slower/lower quality.

**Step 4: Return the bytes**
```python
return response.audio_content
```

`audio_content` is a Python `bytes` object — raw binary data. For 5 seconds of speech, it's typically 40-80KB.

## What MP3 actually is

MP3 (MPEG-1 Audio Layer III) is a lossy audio compression format.

**Raw audio (PCM):**
- CD quality: 44,100 samples per second, 16 bits per sample, 2 channels
- Size: 44100 × 2 × 2 = 176,400 bytes/second = ~10MB per minute

**MP3 compression:**
- Exploits psychoacoustics: humans can't hear certain frequencies at certain times
- Removes inaudible information
- Typical compression: 10:1 ratio
- Result: ~1MB per minute at 128kbps

The `audio_content` bytes are a valid MP3 file. You can write them to disk, upload them to GCS, or stream them in an HTTP response — they'll play in any audio player.

---

# 8. queue.py — The External Inbox

## The full code

```python
import json
import os
from google.cloud import pubsub_v1

PROJECT_ID = os.environ.get("GCP_PROJECT", "tts-pipeline-prod")
TOPIC = "tts-jobs"

_publisher = None


def _get_publisher():
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(
                enable_message_ordering=True,
            )
        )
    return _publisher


def publish_job(job_id, text, voice, speed):
    publisher = _get_publisher()
    topic_path = publisher.topic_path(PROJECT_ID, TOPIC)
    message = json.dumps({
        "job_id": job_id,
        "text": text,
        "voice": voice,
        "speed": speed,
    }).encode("utf-8")
    publisher.publish(topic_path, message, ordering_key="feed").result()
```

## What Google Pub/Sub is

Pub/Sub is a **message queue** — a system for passing messages between services asynchronously.

**The problem it solves:**

Without a queue, services call each other directly:
```
Service A → calls → Service B
```
If Service B is down, Service A's call fails. If Service B is slow, Service A waits. If Service B can't handle the load, requests pile up and crash it.

With a queue:
```
Service A → publishes to queue → Service B reads from queue
```
Service B can be down — messages wait in the queue. Service B reads at its own pace. The queue absorbs traffic spikes.

## Topics and Subscriptions

**Topic** (`tts-jobs`): A named channel. Publishers send messages here.

**Subscription** (`tts-jobs-sub`): A named receiver. Subscribers pull messages from here.

One topic can have multiple subscriptions (fan-out). Each subscription gets its own copy of every message.

```
Publisher → Topic: tts-jobs
                    ├── Subscription: tts-jobs-sub    ← our worker
                    └── Subscription: tts-jobs-logs   ← could add for logging
```

## Message ordering in depth

By default, Pub/Sub delivers messages to any available subscriber as fast as possible. If you have two workers and three messages, the messages might be distributed like:

```
Worker 1 gets: message A, message C
Worker 2 gets: message B
```

And since shorter text processes faster, `C` might finish before `B` — out of order.

**Ordering keys** solve this. Messages with the same ordering key are guaranteed to be delivered to the same subscriber, in publish order.

```python
publisher.publish(topic_path, message, ordering_key="feed")
```

All messages have the same key `"feed"`, so they all go to the same worker, in the order they were published.

**Why the publisher must also enable ordering:**
```python
pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
```
Pub/Sub requires the publisher to explicitly opt into ordered delivery. This changes how the client batches and sends messages.

## `_get_publisher()` — lazy initialization

```python
_publisher = None

def _get_publisher():
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient(...)
    return _publisher
```

Creating a `PublisherClient` involves network connections and authentication. We don't want to do this on every `publish_job()` call.

This pattern is called **lazy initialization** or a **singleton**. The client is created once (on first use) and reused for all subsequent calls. `_publisher = None` at module level; if it's None, create it; otherwise, return the existing one.

## `.result()` — waiting for confirmation

```python
publisher.publish(topic_path, message, ordering_key="feed").result()
```

`publisher.publish()` returns a **Future** — a promise of a result that will be available later. It's non-blocking.

`.result()` blocks until the future resolves — until Pub/Sub confirms it received and stored the message. This ensures message durability: if the network drops between publishing and confirmation, the future won't resolve and an exception is raised.

Without `.result()`, you'd be "fire and forget" — you send the message but don't know if it arrived.

## `json.dumps().encode("utf-8")`

Pub/Sub messages are raw bytes, not strings.

```python
message = json.dumps({
    "job_id": job_id,
    "text": text,
    "voice": voice,
    "speed": speed,
}).encode("utf-8")
```

1. `json.dumps({...})` converts the Python dict to a JSON string: `'{"job_id": "abc", "text": "Hello", ...}'`
2. `.encode("utf-8")` converts the string to bytes: `b'{"job_id": "abc", ...}'`

UTF-8 is the character encoding. It represents each character as 1-4 bytes. ASCII characters (a-z, 0-9) are 1 byte each. Emoji and other unicode characters can be 2-4 bytes.

---

# 9. worker.py — The Pub/Sub Consumer

## The full code

```python
import json
import os
from google.cloud import pubsub_v1, storage

from .tts_engine import synthesize
from .storage import upload_audio

PROJECT_ID = os.environ.get("GCP_PROJECT", "tts-pipeline-prod")
SUBSCRIPTION = "tts-jobs-sub"
GCS_BUCKET = os.environ.get("GCS_BUCKET", "tts-pipeline-audio-prod")


def _mark_job(bucket_name, job_id, status, url=""):
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(f"jobs/{job_id}.json")
    blob.upload_from_string(
        json.dumps({"job_id": job_id, "status": status, "url": url}),
        content_type="application/json",
    )
    blob.make_public()


def process(message):
    data = json.loads(message.data.decode("utf-8"))
    job_id = data["job_id"]
    text, voice, speed = data["text"], data["voice"], data["speed"]

    try:
        from .storage import get_cached_url
        cached_url = get_cached_url(GCS_BUCKET, text, voice, speed)
        if cached_url:
            _mark_job(GCS_BUCKET, job_id, "done", cached_url)
            message.ack()
            return

        _mark_job(GCS_BUCKET, job_id, "processing")
        audio = synthesize(text, voice, speed)
        url = upload_audio(audio, GCS_BUCKET, text, voice, speed)
        _mark_job(GCS_BUCKET, job_id, "done", url)
        message.ack()
    except Exception as e:
        print(f"[worker] failed {job_id}: {e}")
        _mark_job(GCS_BUCKET, job_id, "failed")
        message.nack()


def run():
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(PROJECT_ID, SUBSCRIPTION)
    flow_control = pubsub_v1.types.FlowControl(max_messages=5)
    streaming_pull = subscriber.subscribe(sub_path, callback=process, flow_control=flow_control)
    try:
        streaming_pull.result()
    except KeyboardInterrupt:
        streaming_pull.cancel()
```

## How streaming pull works internally

When you call `subscriber.subscribe(sub_path, callback=process)`:

1. The Pub/Sub client library opens a **bidirectional streaming gRPC connection** to Google's servers
2. Google starts sending messages down this stream as they arrive
3. For each message, the client library calls `callback(message)` in a thread pool
4. Your callback processes the message and calls `message.ack()` or `message.nack()`
5. The acknowledgement is sent back up the stream to Google

This is different from polling (asking "any messages?" repeatedly). With streaming pull, Google pushes messages to you as soon as they're available, with minimal latency.

## `message.data.decode("utf-8")`

```python
data = json.loads(message.data.decode("utf-8"))
```

`message.data` is the raw bytes you published. `.decode("utf-8")` converts bytes back to a string. `json.loads()` parses the JSON string into a Python dict.

This is the reverse of what `queue.py` does:
```
publish: dict → json.dumps() → string → .encode() → bytes
receive: bytes → .decode() → string → json.loads() → dict
```

## `message.ack()` vs `message.nack()`

**`ack()` (acknowledge):**
- Tells Pub/Sub: "I processed this successfully, you can delete it"
- Pub/Sub removes the message from the subscription
- Message will never be delivered again

**`nack()` (negative acknowledge):**
- Tells Pub/Sub: "I failed to process this, try again"
- Pub/Sub makes the message available for redelivery
- After the `ack_deadline` (60 seconds), even un-acked messages are redelivered

**What happens if you never ack:**
The message stays "in-flight" until the ack deadline expires. Then Pub/Sub redelivers it. This provides **at-least-once delivery** — messages might be delivered more than once if the worker crashes after processing but before acking.

## `FlowControl(max_messages=5)`

```python
flow_control = pubsub_v1.types.FlowControl(max_messages=5)
```

Without flow control, Pub/Sub might send 1000 messages to your worker at once. Your worker can only handle 5 at a time — the other 995 sit in memory, waiting.

`max_messages=5` means: "at any moment, I have at most 5 messages in-flight (received but not yet acked)." Pub/Sub won't send message 6 until you ack one of the first 5.

---

# 10. main.py — The API Server

## What FastAPI is

FastAPI is a Python web framework. It handles:
- Routing: matching URL paths to functions
- Serialization: converting Python objects to JSON
- Validation: checking request data is correct
- Documentation: auto-generating API docs at `/docs`

When a request comes in:
```
HTTP Request → FastAPI router → finds matching function → runs it → returns response
```

## The lifespan context manager

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(watch())   # startup
    yield
    task.cancel()                          # shutdown
```

`@asynccontextmanager` turns an async generator into a context manager. The code before `yield` runs on startup. The code after `yield` runs on shutdown.

`asyncio.create_task(watch())` schedules the watcher coroutine to run concurrently with the event loop. It doesn't block — it registers the coroutine and returns immediately.

`yield` suspends the lifespan function and lets the server run. The server processes requests here.

`task.cancel()` sends a `CancelledError` to the watcher coroutine, stopping it cleanly.

## Pydantic models — validation in depth

```python
class TTSRequest(BaseModel):
    text: str
    voice: str = "en-US-Neural2-C"
    speed: float = 1.0
    store: bool = False

    @field_validator("text")
    @classmethod
    def text_must_not_be_empty(cls, v):
        if not v.strip():
            raise ValueError("text cannot be empty")
        return v
```

When a request body is received:
1. FastAPI parses the JSON
2. Pydantic tries to create a `TTSRequest` from the parsed data
3. Type coercion: `"1.5"` (string) is automatically converted to `1.5` (float)
4. Validators run: `text_must_not_be_empty` is called with the text value
5. If validation fails: FastAPI returns a 422 Unprocessable Entity response with details
6. If validation passes: the function receives a `TTSRequest` instance

`@classmethod` means the method receives the class (`cls`) as first argument, not an instance. This is required by Pydantic's validator system.

`v.strip()` removes whitespace from both ends. `"   "` stripped is `""`, which is falsy.

## Dependency injection — how auth works

```python
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(key: str = Security(api_key_header)):
    if not USE_AUTH:
        return
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

@app.post("/synthesize", dependencies=[Depends(verify_api_key)])
async def synthesize_text(req: TTSRequest):
    ...
```

`APIKeyHeader(name="X-API-Key")` is a dependency that extracts the `X-API-Key` header from incoming requests. `auto_error=False` means if the header is missing, it passes `None` instead of immediately erroring.

`Security(api_key_header)` is FastAPI's way of saying "call `api_key_header` and pass the result as `key`."

`Depends(verify_api_key)` on the route means: "before running `synthesize_text`, run `verify_api_key`. If it raises an exception, stop there and return that error response."

## StreamingResponse for SSE

```python
@app.get("/feed/stream")
async def stream_feed():
    return StreamingResponse(
        feed.stream_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

`StreamingResponse` wraps an async generator. Instead of collecting all data and returning at once, it sends data to the client as it's produced.

`media_type="text/event-stream"` is the MIME type for SSE. The browser's `EventSource` API requires this to recognize the response as SSE.

`"Cache-Control": "no-cache"` tells browsers and proxies not to cache this response. Every request must go to the server.

`"X-Accel-Buffering": "no"` tells Nginx (a common reverse proxy) not to buffer the response. Without this, Nginx might hold data and send it in chunks, breaking the real-time nature of SSE.

---

# 11. index.html — The Browser UI

## How EventSource works at the browser level

```javascript
const evtSource = new EventSource('/feed/stream');

evtSource.onmessage = (e) => {
    const line = JSON.parse(e.data);
    addTextCard(line);
    trackJob(line);
};
```

`EventSource` is a browser API that:
1. Opens an HTTP GET connection to the URL
2. Keeps the connection open indefinitely
3. Parses the SSE format (`data: ...\n\n`)
4. Fires `onmessage` for each event

The browser automatically reconnects if the connection drops. The reconnect delay starts at a few seconds and backs off exponentially.

Unlike WebSockets, SSE is one-directional (server → browser only). It's simpler and sufficient for this use case.

## `fetch()` — how browser HTTP requests work

```javascript
const res = await fetch('/feed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: text })
});
const data = await res.json();
```

`fetch()` returns a Promise. `await` pauses the function until the Promise resolves.

`JSON.stringify()` converts a JavaScript object to a JSON string.

`res.json()` reads the response body and parses it as JSON. This is also async because reading the body might take time.

## `setInterval` — how polling works

```javascript
const interval = setInterval(async () => {
    const res = await fetch(`/job/${line.job_id}`, { headers });
    const data = await res.json();
    updateBadge(card, data.status);
    if (data.status === 'done' || data.status === 'failed') {
        clearInterval(interval);
        entry.done = true;
        flushAudioQueue();
    }
}, 500);
```

`setInterval(callback, 500)` calls `callback` every 500 milliseconds. It returns an interval ID.

`clearInterval(interval)` stops the interval. Without this, it would keep running forever, making requests to a job that's already done.

## The ordered display queue — step by step

```javascript
const jobQueue = [];
let displayPointer = 0;

function flushAudioQueue() {
    while (displayPointer < jobQueue.length) {
        const entry = jobQueue[displayPointer];
        if (!entry.done) break;
        if (entry.url) showAudioPlayer(entry.card, entry.url);
        displayPointer++;
    }
}
```

**The problem:** Jobs 1, 2, 3 are submitted. Job 3 might finish before Job 2 (shorter text). But you want players to appear in order: 1, then 2, then 3.

**The solution:** Track completion separately from display.

`jobQueue` is an array (ordered list). Each entry is `{job_id, card, done, url}`. Jobs are pushed in order they're submitted.

`displayPointer` points to the next entry to show. It starts at 0.

`flushAudioQueue` moves the pointer forward as long as the current entry is done:
- If `jobQueue[0].done = true` → show player, move pointer to 1
- If `jobQueue[1].done = false` → stop (wait for job 2)
- (Even if `jobQueue[2].done = true`, don't show it yet)

When job 2 finishes, `flushAudioQueue` is called again:
- `jobQueue[1].done = true` → show player, pointer to 2
- `jobQueue[2].done = true` → show player, pointer to 3
- Loop ends

Both players 2 and 3 appear at the same moment.

## HTML/CSS structure

```html
<div class="main">
    <div class="panel">           <!-- left: text feed -->
        <div class="panel-body" id="feed-panel">
            <!-- text cards added here dynamically -->
        </div>
        <div class="input-area">  <!-- text input at bottom -->
    </div>
    <div class="panel">           <!-- right: audio output -->
        <div class="panel-body" id="audio-panel">
            <!-- audio cards added here dynamically -->
        </div>
    </div>
</div>
```

Cards are created dynamically with JavaScript:

```javascript
function addTextCard(line) {
    const panel = document.getElementById('feed-panel');
    const card = document.createElement('div');
    card.className = 'line-card';
    card.innerHTML = `
        <div class="line-text">${escHtml(line.text)}</div>
        <div class="line-meta">#${line.id}</div>
    `;
    panel.appendChild(card);
    panel.scrollTop = panel.scrollHeight;  // scroll to bottom
}
```

`document.createElement('div')` creates a new DOM element in memory.
`card.innerHTML = ...` sets its content.
`panel.appendChild(card)` adds it to the page.

## XSS protection — `escHtml()`

```javascript
function escHtml(s) {
    return s.replace(/&/g,'&amp;')
            .replace(/</g,'&lt;')
            .replace(/>/g,'&gt;');
}
```

If a user types `<script>alert('hacked')</script>` as text, and you set `innerHTML` directly, the browser would execute it. This is called **Cross-Site Scripting (XSS)**.

`escHtml()` replaces `<` with `&lt;`, `>` with `&gt;`, `&` with `&amp;`. The browser displays these as literal characters instead of interpreting them as HTML tags.

---

# 12. Infrastructure — GCP Components

## Cloud Run

Cloud Run is a **serverless container platform**. You give it a Docker image; it runs it.

**How it works:**
- Your container starts when requests arrive
- Scales from 0 instances (no requests) to up to 10 instances (many requests)
- Each instance handles multiple concurrent requests
- You pay only for actual CPU/memory usage (not idle time)

**`min-instances 0`:** When no traffic for a while, all instances shut down. Cost = $0.

**`max-instances 10`:** Under heavy load, up to 10 containers run simultaneously.

**Why `--platform linux/amd64` matters:**
Your Mac has an ARM chip (Apple Silicon). Docker images built on ARM don't run on x86 machines. Cloud Run uses x86/AMD64 CPUs. The `--platform linux/amd64` flag tells Docker to build for x86 even though you're on ARM. This uses QEMU emulation during the build.

## Artifact Registry

Artifact Registry is GCP's Docker image registry — like Docker Hub but private.

When you run `docker push us-central1-docker.pkg.dev/...`, the image is uploaded there. When Cloud Run deploys, it pulls the image from there.

**Why not use Docker Hub?**
- Artifact Registry is in the same network as Cloud Run — faster pulls
- Access is controlled by GCP IAM — more secure
- Images never leave Google's network

## Pub/Sub architecture

```
Publisher (your server)
    │
    │ HTTPS/gRPC
    ▼
┌─────────────────────────────────────────┐
│           Google Pub/Sub                │
│                                         │
│  Topic: tts-jobs                        │
│     │                                   │
│     └── Subscription: tts-jobs-sub      │
│         ├── Message: {"job_id": "abc"}  │
│         ├── Message: {"job_id": "def"}  │
│         └── Message: {"job_id": "ghi"}  │
└─────────────────────────────────────────┘
    │
    │ streaming pull
    ▼
Worker (python -m app.worker)
```

Pub/Sub guarantees:
- **At-least-once delivery**: messages are delivered at least once (might be duplicate)
- **Ordered delivery** (with ordering keys): messages with same key are delivered in order
- **Durability**: messages are stored redundantly, not lost if Pub/Sub has issues
- **Retention**: undelivered messages are kept for up to 7 days by default

## GCS (Google Cloud Storage)

GCS stores the actual MP3 files permanently. Unlike local disk or memory, GCS:
- Survives server restarts
- Is accessible from any server, any region
- Has 99.999999999% (11 nines) durability — data is replicated across multiple data centers
- Public URLs work anywhere in the world

**Bucket**: Container for files (like an S3 bucket or a folder)
**Object/Blob**: Individual file (like an S3 object)
**ACL (Access Control List)**: Permissions on who can read/write

When you call `blob.make_public()`, it adds a public read permission. The URL format:
```
https://storage.googleapis.com/{bucket}/{path}
```
Anyone with this URL can download the file directly — no authentication needed.

---

# 13. Complete Data Flow

Let's trace a single text line through every component from beginning to end.

## Step 0: Server starts

```
Python starts uvicorn
  → FastAPI initializes
  → lifespan() runs
  → asyncio.create_task(watch()) schedules the watcher
  → Server starts accepting requests
  → Watcher loop begins: polls feed every 200ms
```

## Step 1: Browser opens the page

```
Browser GET /
  → FastAPI returns index.html
  → Browser renders HTML
  → JavaScript runs
  → new EventSource('/feed/stream') opens SSE connection
  → FastAPI creates asyncio.Queue, adds to _subscribers
  → stream_events() starts: sends existing lines (if any), then waits
```

## Step 2: User types text and presses Enter

```
JavaScript keydown event fires (Enter key)
  → sendText() is called
  → fetch('POST /feed', {text: "Hello pipeline"})
  → HTTP request goes to FastAPI
  → add_to_feed() is called
  → feed.add_line("Hello pipeline") is called
    → entry = {id: 0, job_id: "abc-123", text: "Hello pipeline", timestamp: "..."}
    → _lines.append(entry)
    → for each queue in _subscribers:
        await q.put(entry)  ← puts into the browser's SSE queue
  → FastAPI returns the entry as JSON
  → fetch() resolves with the response
```

## Step 3: SSE delivers the line to the browser

```
q.put(entry) woke up stream_events()
  → yield f"data: {json.dumps(entry)}\n\n"
  → StreamingResponse sends this to the browser over the open SSE connection
  → Browser's EventSource fires onmessage(event)
  → line = JSON.parse(event.data)
  → addTextCard(line)  ← creates div, appends to left panel
  → trackJob(line)     ← starts polling /job/abc-123 every 500ms
```

## Step 4: Watcher detects the new line

```
200ms passes
  → watch() wakes up from asyncio.sleep(0.2)
  → feed.get_unprocessed_lines() returns [entry]
    → _lines[0:] = [entry]
    → _last_processed_index = 1
  → set_status("abc-123", "queued")  ← _store["abc-123"] = {status: "queued"}
  → loop.run_in_executor(_executor, _process_line_sync, "abc-123", "Hello pipeline", ...)
    → picks an available thread from the pool
    → schedules _process_line_sync to run in that thread
    → returns immediately (non-blocking)
  → asyncio.sleep(0.2) again
```

## Step 5: Thread processes the job

```
Thread pool thread wakes up, runs _process_line_sync("abc-123", "Hello pipeline", ...)

Step 5a: Cache check
  → get_cached_url(bucket, "Hello pipeline", "en-US-Neural2-C", 1.0)
    → hash_key = sha256("Hello pipeline|en-US-Neural2-C|1.0")
    → blob_name = "audio/{hash_key}.mp3"
    → storage.Client() creates GCS connection
    → blob.exists() → HTTPS GET to GCS
    → returns None (first time this text is used)

Step 5b: Mark processing
  → set_status("abc-123", "processing")  ← instant memory write

Step 5c: TTS API call
  → texttospeech.TextToSpeechClient()
  → synthesize_speech(input, voice, audio_config)
    → gRPC call to Google's TTS servers
    → Google generates audio
    → returns MP3 bytes (~50KB for 5 words)
    → takes ~1.5 seconds

Step 5d: Upload to GCS
  → blob_name = "audio/{hash_key}.mp3"
  → blob.upload_from_string(audio_bytes, "audio/mpeg")
    → HTTPS PUT to GCS
    → takes ~0.5 seconds
  → blob.make_public()
  → url = "https://storage.googleapis.com/tts-pipeline-audio-prod/audio/{hash}.mp3"

Step 5e: Mark done
  → set_status("abc-123", "done", url)  ← instant memory write
```

## Step 6: Browser poll detects completion

```
500ms timer fires in browser
  → fetch('/job/abc-123')
  → FastAPI: get_status("abc-123")
  → returns {job_id, status: "done", url: "https://..."}

Browser receives response:
  → updateBadge(card, "done")  ← badge turns green
  → entry.done = true
  → entry.url = "https://..."
  → flushAudioQueue()
    → displayPointer = 0
    → jobQueue[0].done = true → show player, pointer = 1
    → loop ends
  → showAudioPlayer(card, url)
    → card.innerHTML += '<audio controls src="https://..."></audio>'
```

## Step 7: User plays audio

```
User clicks play button on audio element
  → Browser makes GET request directly to GCS URL
  → GCS returns MP3 bytes
  → Browser decodes MP3 and plays through speakers
```

**Total time:** ~2.3 seconds for new text, ~0.3 seconds for cached text.

---

# 14. Why Each Decision Was Made

## Why FastAPI over Flask or Django?

**Flask:** Synchronous by default. Each request blocks until complete. Doesn't support `async/await` natively. Would require complex workarounds for SSE and background tasks.

**Django:** Full-featured but heavy. Designed for database-backed web apps, not API-first microservices. The ORM, admin, templates are all unnecessary here.

**FastAPI:** Built on asyncio from the ground up. Native `async/await` support. Auto-generated API documentation. Pydantic integration. The right tool for an async API service.

## Why memory for job status instead of database?

Jobs live for 2-3 seconds. Writing to a database for a 2-second lifetime is wasteful. Memory access is ~1000x faster than database writes. The tradeoff (losing status on restart) is acceptable because jobs complete faster than any realistic restart scenario.

## Why SHA256 for cache keys instead of a database?

A database cache would require:
- A Redis or PostgreSQL instance
- Schema design
- Connection management
- Potential race conditions on writes

SHA256 gives us a content-addressable cache with zero infrastructure. The same input always maps to the same filename. No coordination between servers needed. If two servers process the same text simultaneously, they both try to write the same filename — the second write is redundant but harmless.

## Why Pub/Sub for external jobs but direct processing for UI?

Pub/Sub adds ~1-2 seconds of delivery latency. For the live UI, this is unacceptable — users expect feedback within 3 seconds.

For batch/external jobs, the tradeoff is worth it: Pub/Sub provides durability (messages survive server restarts), retry logic (nack causes redelivery), and scalability (add more workers by starting more worker processes).

## Why SSE instead of WebSockets?

WebSockets are bidirectional — both sides can send messages. SSE is unidirectional — server pushes to client.

For a text feed, only the server needs to push (new lines). The client sends new text via regular HTTP POST. SSE is simpler, built into browsers natively, and reconnects automatically. WebSockets require more setup and don't auto-reconnect.

## Why `run_in_executor` instead of `asyncio.run_in_executor`?

`asyncio.get_event_loop().run_in_executor()` is deprecated in Python 3.10+. `asyncio.get_running_loop()` gets the currently running loop (more explicit, safer). But we store it as `loop = asyncio.get_running_loop()` at the start of `watch()` to avoid calling it repeatedly.

## Why `max_workers=5` in the thread pool?

Empirical balance:
- 1 worker: fully sequential, too slow for multiple simultaneous submissions
- 10 workers: risk hitting Google API rate limits, high memory overhead
- 5 workers: handles normal load, stays within free-tier API limits, ~40MB thread overhead

---

# Quick Reference

## Environment Variables

| Variable | What it does | Example |
|----------|-------------|---------|
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP credentials JSON | `/path/to/credentials.json` |
| `GCS_BUCKET` | GCS bucket for audio storage | `tts-pipeline-audio-prod` |
| `GCP_PROJECT` | GCP project ID | `tts-pipeline-prod` |
| `API_KEY` | API key for auth | `your-secret-key` |
| `PORT` | Port to listen on | `8080` |

## API Endpoints

| Method | Path | Auth | What it does |
|--------|------|------|-------------|
| GET | `/` | No | Serve web UI |
| GET | `/health` | No | Server status |
| GET | `/voices` | No | List voices |
| POST | `/feed` | No | Add text to live feed |
| GET | `/feed` | No | Get all feed lines |
| GET | `/feed/stream` | No | SSE stream |
| POST | `/synthesize` | Yes | Sync TTS |
| POST | `/synthesize-async` | Yes | Async TTS via Pub/Sub |
| GET | `/job/{id}` | Yes | Job status |

## Starting the server locally

```bash
cd ~/tts-pipeline
source venv/bin/activate
export GOOGLE_APPLICATION_CREDENTIALS=/Users/ayushitomar/tts-pipeline/credentials.json
export GCS_BUCKET=tts-pipeline-audio-prod
export GCP_PROJECT=tts-pipeline-prod
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Starting the Pub/Sub worker (separate terminal)

```bash
cd ~/tts-pipeline
source venv/bin/activate
export GOOGLE_APPLICATION_CREDENTIALS=/Users/ayushitomar/tts-pipeline/credentials.json
export GCS_BUCKET=tts-pipeline-audio-prod
export GCP_PROJECT=tts-pipeline-prod
python -m app.worker
```
