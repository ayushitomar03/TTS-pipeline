import time

from google.cloud import texttospeech
from google.api_core.exceptions import (
    ResourceExhausted,
    ServiceUnavailable,
    DeadlineExceeded,
    InternalServerError,
    BadGateway,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Errors that indicate GCP availability problems — transient, worth retrying
# and counting toward the circuit breaker.
# 400 InvalidArgument (bad voice name) is a CLIENT error — not GCP's fault,
# should NOT open the circuit breaker.
# Errors that are safe to retry — GCP availability problems, not client mistakes.
# Exported so main.py can mark these failures as retryable in WebSocket responses.
RETRYABLE_ERRORS = (
    ServiceUnavailable,
    ResourceExhausted,
    DeadlineExceeded,
    InternalServerError,
    BadGateway,
)

_TRANSIENT_ERRORS = RETRYABLE_ERRORS

from .logger import get_logger
from .metrics import tts_circuit_breaker_open

log = get_logger(__name__)

AVAILABLE_VOICES = {
    "en-US-Neural2-C": "English (US) - Female",
    "en-US-Neural2-D": "English (US) - Male",
    "en-US-Neural2-F": "English (US) - Female 2",
    "en-US-Neural2-J": "English (US) - Male 2",
    "en-GB-Neural2-A": "English (UK) - Female",
    "en-GB-Neural2-B": "English (UK) - Male",
}

# ── Async client singleton ────────────────────────────────────────────────────

_client: texttospeech.TextToSpeechAsyncClient | None = None


def _get_client() -> texttospeech.TextToSpeechAsyncClient:
    global _client
    if _client is None:
        _client = texttospeech.TextToSpeechAsyncClient()
    return _client


# ── Circuit breaker ───────────────────────────────────────────────────────────

class _CircuitBreaker:
    """
    Opens after FAILURE_THRESHOLD consecutive GCP TTS failures.
    Stays open for RECOVERY_TIMEOUT seconds, then allows one probe request.
    If the probe succeeds the breaker closes; if it fails it reopens.

    This prevents cascading timeouts when GCP TTS is degraded — instead of
    every request waiting the full timeout, they fail-fast immediately.
    """

    FAILURE_THRESHOLD = 5
    RECOVERY_TIMEOUT = 30  # seconds

    def __init__(self):
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        elapsed = time.monotonic() - self._opened_at
        if elapsed >= self.RECOVERY_TIMEOUT:
            return False  # half-open: let one probe through
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        tts_circuit_breaker_open.set(0)

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.FAILURE_THRESHOLD:
            if self._opened_at is None:
                log.warning("circuit_breaker_opened", failures=self._failures)
            self._opened_at = time.monotonic()
            tts_circuit_breaker_open.set(1)


_breaker = _CircuitBreaker()


def force_breaker_open() -> None:
    """Force the circuit breaker open. Only for testing."""
    _breaker._failures = _breaker.FAILURE_THRESHOLD
    _breaker._opened_at = time.monotonic()
    tts_circuit_breaker_open.set(1)


def force_breaker_closed() -> None:
    """Reset the circuit breaker to closed. Only for testing."""
    _breaker._failures = 0
    _breaker._opened_at = None
    tts_circuit_breaker_open.set(0)


# ── Synthesize with retry ─────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type(_TRANSIENT_ERRORS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
async def _call_gcp(text: str, voice: str, speed: float) -> bytes:
    """Raw GCP call with tenacity retry on transient errors."""
    response = await _get_client().synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code=voice[:5],
            name=voice,
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=speed,
        ),
    )
    return response.audio_content


async def synthesize(text: str, voice: str = "en-US-Neural2-C", speed: float = 1.0) -> bytes:
    if _breaker.is_open:
        log.warning("circuit_breaker_rejected", voice=voice, chars=len(text))
        raise RuntimeError("GCP TTS circuit breaker is open — try again shortly")

    try:
        audio = await _call_gcp(text, voice, speed)
        _breaker.record_success()
        return audio
    except _TRANSIENT_ERRORS as exc:
        # Only transient server-side errors count against the circuit breaker.
        _breaker.record_failure()
        log.error("tts_transient_failure", voice=voice, chars=len(text), error=str(exc))
        raise
    except Exception as exc:
        # Client errors (bad voice, invalid input) — don't penalise the breaker.
        log.error("tts_client_error", voice=voice, chars=len(text), error=str(exc))
        raise


def list_voices() -> dict:
    return AVAILABLE_VOICES
