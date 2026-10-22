"""Bounded Qwen audio transcription for the question composer."""

from __future__ import annotations

import base64
import io
import os
import wave
from time import perf_counter
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from semantic_text2sql.models import TokenUsage
from semantic_text2sql.runtime import RequestBudget, RequestBudgetExceeded

MAX_AUDIO_SECONDS = 60
MAX_AUDIO_BYTES = 2 * 1024 * 1024
QWEN_AUDIO_MODEL = "qwen3.8-omni-flash"
GROQ_AUDIO_MODEL = "whisper-large-v3-turbo"


class VoiceInputError(ValueError):
    """An expected input or transcription failure safe to show in the composer."""


class VoiceProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class TranscriptionResult(BaseModel):
    text: str
    provider: Literal["qwen", "groq"] = "qwen"
    model: str
    duration_seconds: float
    latency_ms: int
    token_usage: TokenUsage = Field(default_factory=TokenUsage)


def validate_audio(audio: bytes) -> float:
    """Accept complete, bounded mono 16-bit/16-kHz PCM WAV, never arbitrary URLs."""
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise VoiceInputError("Record up to 60 seconds of audio and try again.")
    try:
        with wave.open(io.BytesIO(audio), "rb") as recording:
            if (
                recording.getnchannels() != 1
                or recording.getsampwidth() != 2
                or recording.getframerate() != 16_000
                or recording.getcomptype() != "NONE"
            ):
                raise VoiceInputError("The recording must be mono, 16-bit, 16-kHz WAV audio.")
            frames = recording.getnframes()
            duration = frames / recording.getframerate()
            if not 0 < duration <= MAX_AUDIO_SECONDS:
                raise VoiceInputError("Record between a moment and 60 seconds of audio.")
            if len(recording.readframes(frames)) != frames * 2:
                raise VoiceInputError("The recording is incomplete. Please record it again.")
    except (wave.Error, EOFError) as exc:
        raise VoiceInputError("The recording could not be read. Please record it again.") from exc
    return duration


class QwenVoiceTranscriber:
    provider = "qwen"
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        *,
        model: str = QWEN_AUDIO_MODEL,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = model
        self.timeout = min(120.0, max(1.0, timeout))
        self.transport = transport

    @classmethod
    def from_environment(cls) -> QwenVoiceTranscriber:
        return cls(
            os.environ.get("QWEN_AUDIO_API_KEY") or os.environ.get("DASHSCOPE_API_KEY"),
            os.environ.get("QWEN_AUDIO_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL"),
            model=os.environ.get("QWEN_AUDIO_MODEL") or QWEN_AUDIO_MODEL,
            timeout=float(os.environ.get("QWEN_AUDIO_TIMEOUT_SECONDS", "60")),
        )

    def unavailable_reason(self) -> str | None:
        if not self.api_key:
            return "Voice input needs QWEN_AUDIO_API_KEY in the project environment."
        if not self.base_url or "{" in self.base_url:
            return "Voice input needs your workspace's QWEN_AUDIO_BASE_URL."
        return None

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        if reason := self.unavailable_reason():
            raise VoiceProviderError(reason, 503)
        duration = validate_audio(audio)
        started = perf_counter()
        budget = RequestBudget(self.timeout, max_model_calls=1, max_database_calls=0)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": "data:audio/wav;base64,"
                                + base64.b64encode(audio).decode("ascii"),
                                "format": "wav",
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Transcribe only the spoken words in this recording. Preserve the "
                            "original language, numbers, IDs, dates, currencies, "
                            "and technical terms. Do not answer the question, translate, "
                            "generate SQL, or add commentary. "
                                "Return only the transcript, or an empty string if there is "
                                "no clear speech."
                            ),
                        },
                    ],
                }
            ],
            "modalities": ["text"],
            "reasoning_effort": "none",
            "max_tokens": 2_048,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url + "/", timeout=self.timeout, transport=self.transport
            ) as client:
                response = await budget.wait(
                    client.post(
                        "chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    ),
                    kind="model",
                )
            response.raise_for_status()
            body = response.json()
            message = body["choices"][0]["message"]
            text = message["content"]
            if not isinstance(text, str):
                raise ValueError("Missing transcript")
            if body["choices"][0].get("finish_reason") == "length":
                raise VoiceProviderError("The transcript was cut short. Try a shorter recording.")
            usage = body.get("usage") or {}
            token_usage = TokenUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise VoiceProviderError(
                    "Qwen rejected the audio credentials. Check the key and workspace region."
                ) from exc
            if status == 429:
                raise VoiceProviderError(
                    "Voice transcription is busy or its quota was reached. Please try again later.",
                    429,
                ) from exc
            raise VoiceProviderError(
                f"Qwen could not transcribe this recording (HTTP {status}). Please try again."
            ) from exc
        except (httpx.TimeoutException, RequestBudgetExceeded) as exc:
            raise VoiceProviderError(
                "Voice transcription timed out. Please try again.", 504
            ) from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise VoiceProviderError(
                "Qwen returned no usable transcript. Please try again."
            ) from exc
        return TranscriptionResult(
            text=text.strip(),
            model=self.model,
            duration_seconds=round(duration, 2),
            latency_ms=round((perf_counter() - started) * 1_000),
            token_usage=token_usage,
        )


class GroqVoiceTranscriber:
    """Use the already-configured Groq key when no Qwen audio workspace is configured."""

    provider = "groq"

    def __init__(
        self,
        api_key: str | None,
        *,
        model: str = GROQ_AUDIO_MODEL,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = model
        self.timeout = min(120.0, max(1.0, timeout))
        self.transport = transport

    @classmethod
    def from_environment(cls) -> GroqVoiceTranscriber:
        return cls(
            os.environ.get("GROQ_API_KEY"),
            model=os.environ.get("GROQ_AUDIO_MODEL") or GROQ_AUDIO_MODEL,
            timeout=float(os.environ.get("GROQ_AUDIO_TIMEOUT_SECONDS", "60")),
        )

    def unavailable_reason(self) -> str | None:
        if not self.api_key:
            return "Voice input needs a Qwen audio workspace or GROQ_API_KEY."
        return None

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        if reason := self.unavailable_reason():
            raise VoiceProviderError(reason, 503)
        duration = validate_audio(audio)
        started = perf_counter()
        budget = RequestBudget(self.timeout, max_model_calls=1, max_database_calls=0)
        try:
            async with httpx.AsyncClient(
                base_url="https://api.groq.com/openai/v1/",
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await budget.wait(
                    client.post(
                        "audio/transcriptions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        data={"model": self.model, "response_format": "json"},
                        files={"file": ("question.wav", audio, "audio/wav")},
                    ),
                    kind="model",
                )
            response.raise_for_status()
            text = response.json()["text"]
            if not isinstance(text, str):
                raise ValueError("Missing transcript")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise VoiceProviderError("Groq rejected the audio credentials.") from exc
            if status == 429:
                raise VoiceProviderError(
                    "Voice transcription is busy or its quota was reached. Try again later.", 429
                ) from exc
            raise VoiceProviderError(
                f"Groq could not transcribe this recording (HTTP {status}). Try again."
            ) from exc
        except (httpx.TimeoutException, RequestBudgetExceeded) as exc:
            raise VoiceProviderError("Voice transcription timed out. Try again.", 504) from exc
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise VoiceProviderError("Groq returned no usable transcript. Try again.") from exc
        return TranscriptionResult(
            text=text.strip(),
            provider="groq",
            model=self.model,
            duration_seconds=round(duration, 2),
            latency_ms=round((perf_counter() - started) * 1_000),
        )
