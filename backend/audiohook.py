"""
AudioHook v2 protocol helpers + µ-law / sample-rate conversion.

We implement enough of the protocol to act as a server: open/opened, ping/pong,
close/closed, and binary audio frames. Audio frames carry interleaved µ-law
samples for the channels declared in the `opened` message (here: ["external",
"internal"] = customer + agent), 8 kHz.

For the realtime STT we need PCM16 24 kHz mono per channel, so we provide
helpers to deinterleave µ-law, decode, and upsample 8 kHz → 24 kHz (×3).
"""
from __future__ import annotations

import numpy as np

# ───────────── µ-law tables (G.711) ─────────────
_BIAS = 0x84
_CLIP = 32635


def _build_decode_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        table[i] = -sample if sign else sample
    return table


_ULAW_DECODE = _build_decode_table()


def ulaw_decode(ulaw_bytes: bytes) -> np.ndarray:
    """Decode µ-law bytes to int16 PCM samples."""
    return _ULAW_DECODE[np.frombuffer(ulaw_bytes, dtype=np.uint8)]


def deinterleave_stereo_ulaw(frame: bytes) -> tuple[np.ndarray, np.ndarray]:
    """
    Split a µ-law frame with 2 interleaved channels (external, internal).
    Returns (customer_pcm16_8k, agent_pcm16_8k) as int16 arrays.
    """
    arr = np.frombuffer(frame, dtype=np.uint8)
    if arr.size % 2 != 0:
        arr = arr[:-1]
    customer = _ULAW_DECODE[arr[0::2]]
    agent = _ULAW_DECODE[arr[1::2]]
    return customer, agent


def upsample_8k_to_24k(pcm16_8k: np.ndarray) -> bytes:
    """
    Simple ×3 upsampler 8 kHz → 24 kHz with linear interpolation.
    Fast enough for realtime; quality is adequate for STT.
    Returns raw little-endian PCM16 bytes.
    """
    if pcm16_8k.size == 0:
        return b""
    n = pcm16_8k.size
    x = np.arange(n)
    xp = np.arange(n * 3) / 3.0
    out = np.interp(xp, x, pcm16_8k.astype(np.float32)).astype(np.int16)
    return out.tobytes()


# ───────────── Protocol message helpers ─────────────

def build_opened(session_id: str, client_seq: int, server_seq: int,
                 channels: list[str]) -> dict:
    return {
        "version": "2",
        "id": session_id,
        "type": "opened",
        "seq": server_seq,
        "clientseq": client_seq,
        "parameters": {
            "media": [{
                "type": "audio",
                "format": "PCMU",
                "channels": channels,
                "rate": 8000,
            }],
            "startPaused": False,
        },
    }


def build_pong(session_id: str, client_seq: int, server_seq: int) -> dict:
    return {
        "version": "2",
        "id": session_id,
        "type": "pong",
        "seq": server_seq,
        "clientseq": client_seq,
        "parameters": {},
    }


def build_closed(session_id: str, client_seq: int, server_seq: int) -> dict:
    return {
        "version": "2",
        "id": session_id,
        "type": "closed",
        "seq": server_seq,
        "clientseq": client_seq,
        "parameters": {},
    }
