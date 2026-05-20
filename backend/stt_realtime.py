"""
Per-channel realtime STT against Azure OpenAI Realtime API
(gpt-4o-mini-transcribe). One instance = one Azure WebSocket session.

Audio in: PCM16 24 kHz little-endian (bytes), pushed via push_audio().
Text out: callback(channel, text, is_final) on every transcription event.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable, Optional

import websockets
from azure.identity import AzureCliCredential

from .config import settings

log = logging.getLogger(__name__)

_AAD_SCOPE = "https://cognitiveservices.azure.com/.default"
_credential: AzureCliCredential | None = None


def _get_aad_token() -> str:
    global _credential
    if _credential is None:
        _credential = AzureCliCredential(process_timeout=30)
    return _credential.get_token(_AAD_SCOPE).token


async def warm_up() -> None:
    """Pre-acquire the AAD token used by Realtime STT so the first call
    doesn't pay the cold token-cache cost (~1-2 s)."""
    if settings.aoai_key:
        return  # api-key auth: no token to warm
    try:
        await asyncio.to_thread(_get_aad_token)
        log.info("Realtime STT AAD token pre-warmed.")
    except Exception as e:
        log.warning("Realtime STT warm-up failed: %s", e)


TranscriptCallback = Callable[[str, str, bool], Awaitable[None]]


class RealtimeSTT:
    def __init__(self, channel: str, language: str, on_transcript: TranscriptCallback):
        self.channel = channel
        self.language = language
        self.on_transcript = on_transcript
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        if not settings.aoai_endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is not configured.")
        host = settings.aoai_endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        url = (
            f"wss://{host}/openai/realtime"
            f"?api-version={settings.aoai_api_version}&intent=transcription"
        )
        if settings.aoai_key:
            headers = {"api-key": settings.aoai_key}
        else:
            # No API key → authenticate with Entra ID (AzureCliCredential)
            token = await asyncio.to_thread(_get_aad_token)
            headers = {"Authorization": f"Bearer {token}"}
            log.info("[STT %s] using Entra ID (no api-key)", self.channel)
        log.info("[STT %s] connecting %s", self.channel, url)
        self._ws = await websockets.connect(
            url,
            additional_headers=headers,
            max_size=None,
            ping_interval=20,
        )
        session_config = {
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": settings.aoai_transcribe_deployment,
                    "language": self.language,
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 200,
                },
            },
        }
        await self._ws.send(json.dumps(session_config))
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def push_audio(self, pcm16_24k: bytes) -> None:
        if self._closed or not self._ws or not pcm16_24k:
            return
        b64 = base64.b64encode(pcm16_24k).decode("ascii")
        msg = json.dumps({"type": "input_audio_buffer.append", "audio": b64})
        try:
            async with self._send_lock:
                await self._ws.send(msg)
        except Exception as e:
            log.warning("[STT %s] push_audio failed: %s", self.channel, e)

    async def close(self) -> None:
        self._closed = True
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task:
            self._recv_task.cancel()

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                t = evt.get("type", "")
                if t == "conversation.item.input_audio_transcription.delta":
                    delta = evt.get("delta") or ""
                    if delta:
                        await self.on_transcript(self.channel, delta, False)
                elif t == "conversation.item.input_audio_transcription.completed":
                    text = (evt.get("transcript") or "").strip()
                    if text:
                        await self.on_transcript(self.channel, text, True)
                elif t == "error":
                    log.error("[STT %s] %s", self.channel, evt)
        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed:
            log.info("[STT %s] connection closed", self.channel)
        except Exception as e:
            log.exception("[STT %s] recv error: %s", self.channel, e)
