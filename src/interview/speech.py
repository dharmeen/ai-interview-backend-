"""The interviewer's voice: reads questions aloud with MiniMax text-to-speech.

Audio takes 2-4 s to make, so it is made in the background as soon as a question exists
(prewarm) and kept in memory; by the time the candidate reaches the question it plays instantly.
"""

import logging
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx

from src.utils.settings import settings

logger = logging.getLogger(__name__)

TTS_URL_PATH = "/t2a_v2"
TTS_TIMEOUT_SECONDS = 20.0
# mp3 at 64 kbps is ~60-100 KB per question, so this holds about 30 MB
CACHE_SIZE = 300


class SpeechError(Exception):
    """Text-to-speech failed (no key, network, provider error). The question still shows as text."""


_cache: "OrderedDict[uuid.UUID, bytes]" = OrderedDict()
_in_flight: dict[uuid.UUID, threading.Event] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tts")


def _synthesize(text: str) -> bytes:
    if settings.MINIMAX_API_KEY is None or not settings.MINIMAX_API_KEY.get_secret_value():
        raise SpeechError("MINIMAX_API_KEY is not set")
    try:
        response = httpx.post(
            settings.MINIMAX_BASE_URL.rstrip("/") + TTS_URL_PATH,
            headers={"Authorization": f"Bearer {settings.MINIMAX_API_KEY.get_secret_value()}"},
            json={
                "model": settings.TTS_MODEL,
                "text": text,
                "stream": False,
                "output_format": "hex",
                "language_boost": "English",
                "voice_setting": {"voice_id": settings.TTS_VOICE, "speed": settings.TTS_SPEED, "vol": 1, "pitch": 0},
                "audio_setting": {"sample_rate": 24000, "bitrate": 64000, "format": "mp3", "channel": 1},
            },
            timeout=TTS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        # the message never includes the key: it's only in a header
        raise SpeechError(f"TTS request failed: {type(e).__name__}") from e

    status_code = (data.get("base_resp") or {}).get("status_code")
    audio_hex = (data.get("data") or {}).get("audio")
    if status_code != 0 or not audio_hex:
        raise SpeechError(f"TTS provider error {status_code}: {(data.get('base_resp') or {}).get('status_msg')}")
    try:
        return bytes.fromhex(audio_hex)
    except ValueError as e:
        raise SpeechError("TTS returned invalid audio") from e


def _remember(question_id: uuid.UUID, audio: bytes) -> None:
    with _lock:
        _cache[question_id] = audio
        _cache.move_to_end(question_id)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)  # the least recently used one


def get_audio(question_id: uuid.UUID, text: str) -> bytes:
    """The question read aloud as mp3. Waits for a prewarm already running instead of
    paying for the same audio twice. Raises SpeechError."""
    with _lock:
        if question_id in _cache:
            _cache.move_to_end(question_id)
            return _cache[question_id]
        running = _in_flight.get(question_id)
    if running is not None:
        running.wait(timeout=TTS_TIMEOUT_SECONDS)
        with _lock:
            if question_id in _cache:
                return _cache[question_id]
        # the prewarm failed: try once more ourselves

    audio = _synthesize(text)
    _remember(question_id, audio)
    return audio


def _prewarm(question_id: uuid.UUID, text: str) -> None:
    try:
        audio = _synthesize(text)
        _remember(question_id, audio)
    except SpeechError as e:
        logger.info("Could not prepare audio for question %s: %s", question_id, e)
    except Exception:
        logger.exception("Preparing audio for question %s failed", question_id)
    finally:
        with _lock:
            event = _in_flight.pop(question_id, None)
        if event:
            event.set()


def prewarm(question_id: uuid.UUID, text: str) -> None:
    """Starts making the audio in the background; returns at once."""
    if not settings.TTS_ENABLED:
        return
    with _lock:
        if question_id in _cache or question_id in _in_flight:
            return
        _in_flight[question_id] = threading.Event()
    _pool.submit(_prewarm, question_id, text)
