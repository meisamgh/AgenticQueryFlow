from __future__ import annotations

import asyncio
import base64
import io
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from semantic_text2sql.api import create_app
from semantic_text2sql.voice import (
    MAX_AUDIO_BYTES,
    QwenVoiceTranscriber,
    VoiceInputError,
    VoiceProviderError,
    validate_audio,
)


def recording(seconds: float = 0.1, *, rate: int = 16_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x01\x00" * int(rate * seconds))
    return output.getvalue()


def test_voice_calls_qwen_once_and_returns_editable_transcript_with_usage() -> None:
    captured: list[dict[str, object]] = []
    audio = recording()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer voice-test-key"
        payload = json.loads(request.content)
        captured.append(payload)
        assert payload["model"] == "qwen3.8-omni-flash"
        assert payload["reasoning_effort"] == "none"
        assert payload["modalities"] == ["text"]
        assert payload["stream"] is False
        content = payload["messages"][0]["content"]
        assert base64.b64decode(content[0]["input_audio"]["data"].split(",", 1)[1]) == audio
        assert "Do not answer the question" in content[1]["text"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "How much did customer 6000 consume?"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 15},
            },
        )

    transcriber = QwenVoiceTranscriber(
        "voice-test-key",
        "https://qwen.test/compatible-mode/v1",
        transport=httpx.MockTransport(handler),
    )
    response = TestClient(create_app(voice_transcriber=transcriber)).post(
        "/api/voice/transcribe", content=audio, headers={"Content-Type": "audio/wav"}
    )

    assert response.status_code == 200
    assert response.json()["text"] == "How much did customer 6000 consume?"
    assert response.json()["token_usage"]["input_tokens"] == 120
    assert response.json()["duration_seconds"] == 0.1
    assert response.headers["cache-control"] == "no-store"
    assert len(captured) == 1


def test_voice_configuration_exposes_no_credentials() -> None:
    transcriber = QwenVoiceTranscriber("secret-key", "https://private-workspace.test/v1")
    response = TestClient(create_app(voice_transcriber=transcriber)).get("/api/voice")
    assert response.json()["configured"] is True
    assert response.json()["max_duration_seconds"] == 60
    assert "secret-key" not in response.text
    assert "private-workspace" not in response.text


def test_missing_audio_configuration_is_clear_and_does_not_break_text_api() -> None:
    client = TestClient(create_app(voice_transcriber=QwenVoiceTranscriber(None, None)))
    assert client.get("/api/voice").json()["configured"] is False
    assert client.get("/api/health").status_code == 200
    response = client.post(
        "/api/voice/transcribe", content=recording(), headers={"Content-Type": "audio/wav"}
    )
    assert response.status_code == 503
    assert "QWEN_AUDIO_API_KEY" in response.json()["detail"]


@pytest.mark.parametrize(
    "audio", [b"not a WAV", b"", recording(rate=48_000), recording(60.1), recording()[:-2]]
)
def test_invalid_audio_never_reaches_provider(audio: bytes) -> None:
    def unexpected_call(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid audio was sent to the provider")

    transcriber = QwenVoiceTranscriber(
        "key", "https://qwen.test/v1", transport=httpx.MockTransport(unexpected_call)
    )
    with pytest.raises(VoiceInputError):
        asyncio.run(transcriber.transcribe(audio))


def test_sixty_second_limit_and_empty_speech_are_supported() -> None:
    assert validate_audio(recording(60)) == 60
    transcriber = QwenVoiceTranscriber(
        "key",
        "https://qwen.test/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": ""}}],
                },
            )
        ),
    )
    result = asyncio.run(transcriber.transcribe(recording()))
    assert result.text == ""
    assert result.token_usage.total_tokens is None


def test_upload_size_and_content_type_limits() -> None:
    client = TestClient(
        create_app(voice_transcriber=QwenVoiceTranscriber("key", "https://qwen.test/v1"))
    )
    assert client.post("/api/voice/transcribe", json={"url": "http://private"}).status_code == 415
    response = client.post(
        "/api/voice/transcribe",
        content=b"x" * (MAX_AUDIO_BYTES + 1),
        headers={"Content-Type": "audio/wav"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_failures_are_sanitized_without_retries(status: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "private key detail"}})

    transcriber = QwenVoiceTranscriber(
        "key", "https://qwen.test/v1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(VoiceProviderError) as error:
        asyncio.run(transcriber.transcribe(recording()))
    assert "private key detail" not in str(error.value)
    assert calls == 1
    assert error.value.status_code == (429 if status == 429 else 502)


def test_provider_timeout_and_truncated_output_are_not_returned_as_transcripts() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private endpoint")

    transcriber = QwenVoiceTranscriber(
        "key", "https://qwen.test/v1", transport=httpx.MockTransport(timeout)
    )
    with pytest.raises(VoiceProviderError, match="timed out"):
        asyncio.run(transcriber.transcribe(recording()))

    transcriber.transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Partial"}, "finish_reason": "length"}],
            },
        )
    )
    with pytest.raises(VoiceProviderError, match="cut short"):
        asyncio.run(transcriber.transcribe(recording()))
