"""
Per-conversation state.

A ConversationSession owns:
  - 2 RealtimeSTT instances (customer + agent).
  - A Foundry thread id, the agent_id in use.
  - The set of subscribed Agent-Assist WebSockets (UI).
  - The transcript history (for summary).

It exposes:
  - feed_customer_audio(pcm16_24k) / feed_agent_audio(pcm16_24k)
  - on transcripts: broadcasts to subscribers, persists to Cosmos,
    and (only for customer final turns) fires Foundry agent stream.
  - subscribe(ws) / unsubscribe(ws): manage UI listeners.
  - close(): tear everything down.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Dict, Optional

from fastapi import WebSocket

from . import cosmos_store
from .config import settings
from .foundry_agent import FoundryAgentClient
from .stt_realtime import RealtimeSTT
from .summary import generate_summary

log = logging.getLogger(__name__)


class ConversationSession:
    def __init__(self, conversation_id: str, agent_client: FoundryAgentClient,
                 agent_name: str, language: str):
        self.conversation_id = conversation_id
        self.agent_client = agent_client
        self.agent_name = agent_name
        self.language = language
        self.foundry_conv_id: Optional[str] = None

        self.subscribers: set[WebSocket] = set()
        self.turns: list[dict] = []  # {channel, text}
        self._pending_run: Optional[asyncio.Task] = None
        # Index of the next turn that still has to be sent to the Foundry
        # agent. We label every turn with its speaker so the model has the
        # full conversational context (not just the customer side).
        self._last_sent_turn_idx: int = 0

        self._stt_customer = RealtimeSTT("customer", language, self._on_transcript)
        self._stt_agent = RealtimeSTT("agent", language, self._on_transcript)

    async def start(self) -> None:
        # Critical path — the AudioHook client is blocked on this until we
        # send back the `opened` reply, so we parallelise everything we can:
        #   * Foundry create_conversation (REST)  ~300-800 ms
        #   * 2 × Azure OpenAI Realtime WS connect ~300-800 ms each
        # AAD tokens for both scopes are pre-warmed at app startup.
        async def _create_conv() -> None:
            self.foundry_conv_id = await self.agent_client.create_conversation()

        await asyncio.gather(
            _create_conv(),
            self._stt_customer.start(),
            self._stt_agent.start(),
        )
        # Non-critical — fire-and-forget so we don't delay the `opened` reply.
        asyncio.create_task(
            cosmos_store.save_conversation_started(self.conversation_id, self.agent_name)
        )
        asyncio.create_task(self._broadcast({
            "type": "session.started",
            "conversationId": self.conversation_id,
            "agentName": self.agent_name,
        }))

    # ---- Audio ingestion -------------------------------------------------
    async def feed_customer_audio(self, pcm16_24k: bytes) -> None:
        await self._stt_customer.push_audio(pcm16_24k)

    async def feed_agent_audio(self, pcm16_24k: bytes) -> None:
        await self._stt_agent.push_audio(pcm16_24k)

    # ---- Subscribers (Agent Assist UI) ----------------------------------
    async def subscribe(self, ws: WebSocket) -> None:
        self.subscribers.add(ws)
        # Replay current transcript so the UI catches up
        try:
            await ws.send_text(json.dumps({
                "type": "session.snapshot",
                "conversationId": self.conversation_id,
                "agentName": self.agent_name,
                "turns": self.turns,
            }))
        except Exception:
            # Client closed before we finished sending the initial snapshot.
            self.subscribers.discard(ws)
            raise

    def unsubscribe(self, ws: WebSocket) -> None:
        self.subscribers.discard(ws)

    async def _broadcast(self, payload: dict) -> None:
        if not self.subscribers:
            return
        data = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self.subscribers):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    # ---- STT callback ----------------------------------------------------
    async def _on_transcript(self, channel: str, text: str, is_final: bool) -> None:
        await self._broadcast({
            "type": "transcript",
            "channel": channel,
            "text": text,
            "final": is_final,
        })
        if not is_final:
            return
        self.turns.append({"channel": channel, "text": text})
        log.info(
            "[conv %s] FINAL turn #%d (%s): %r",
            self.conversation_id, len(self.turns), channel, text,
        )
        # Persist the turn fire-and-forget so the Foundry call below is not
        # blocked by the Cosmos round-trip. The id is generated locally so we
        # can pass it to the suggestion doc immediately.
        turn_id = str(uuid.uuid4())
        asyncio.create_task(
            cosmos_store.save_turn(self.conversation_id, channel, text, turn_id)
        )
        # Trigger Foundry only on customer final turns
        if channel == "customer":
            # Build a labeled block with every turn that hasn't been sent to
            # the agent yet (any agent turns that happened since the last
            # trigger, plus the new customer turn). This gives the model the
            # full conversation context — not only the customer's side — so
            # it can suggest in light of what the human agent already said.
            pending = self.turns[self._last_sent_turn_idx:]
            self._last_sent_turn_idx = len(self.turns)
            labeled = "\n".join(
                f"[{'Customer' if t['channel'] == 'customer' else 'Agent'}]: {t['text']}"
                for t in pending
            )
            log.info(
                "[conv %s] -> Foundry agent %r (%d turn(s), %d chars):\n%s",
                self.conversation_id, self.agent_name,
                len(pending), len(labeled), labeled,
            )
            # cancel previous in-flight run if a new customer turn arrives
            if self._pending_run and not self._pending_run.done():
                self._pending_run.cancel()
            self._pending_run = asyncio.create_task(
                self._run_agent(labeled, turn_id)
            )

    # ---- Foundry streaming run ------------------------------------------
    async def _run_agent(self, input_text: str, turn_id: str) -> None:
        assert self.foundry_conv_id is not None
        try:
            await self._broadcast({"type": "suggestion.started"})
            collected: list[str] = []

            async def on_delta(delta: str) -> None:
                collected.append(delta)
                await self._broadcast({"type": "suggestion.delta", "text": delta})

            async def on_completed(full: str) -> None:
                log.info(
                    "[conv %s] <- Foundry suggestion (%d chars):\n%s",
                    self.conversation_id, len(full), full,
                )
                await self._broadcast({"type": "suggestion.completed", "text": full})
                await cosmos_store.save_suggestion(self.conversation_id, full, turn_id)

            await self.agent_client.ask(
                self.agent_name, self.foundry_conv_id, input_text,
                on_delta=on_delta, on_completed=on_completed,
            )
        except asyncio.CancelledError:
            log.info("agent run cancelled (newer customer turn)")
        except Exception as e:
            log.exception("agent run error")
            await self._broadcast({"type": "suggestion.error", "text": str(e)})

    # ---- Wrap-up ---------------------------------------------------------
    async def wrap_up(self) -> dict:
        text, cats = await generate_summary(self.turns, list(settings.categories))
        await cosmos_store.save_conversation_summary(self.conversation_id, text, cats)
        payload = {"type": "summary", "text": text, "categories": cats}
        await self._broadcast(payload)
        return {"summary": text, "categories": cats}

    async def stop_audio(self) -> None:
        """Halt the Realtime STT sessions but keep transcript + Foundry state
        in memory so the UI can still trigger /api/wrapup."""
        await asyncio.gather(
            self._stt_customer.close(),
            self._stt_agent.close(),
            return_exceptions=True,
        )
        await self._broadcast({"type": "session.closed"})

    async def close(self) -> None:
        try:
            await asyncio.gather(
                self._stt_customer.close(),
                self._stt_agent.close(),
                return_exceptions=True,
            )
        finally:
            await self._broadcast({"type": "session.closed"})


# ───────────── registry ─────────────
_sessions: Dict[str, ConversationSession] = {}
_lock = asyncio.Lock()


async def get_or_create(conversation_id: str, agent_client: FoundryAgentClient,
                        agent_name: str, language: str) -> ConversationSession:
    async with _lock:
        sess = _sessions.get(conversation_id)
        if sess is None:
            sess = ConversationSession(conversation_id, agent_client, agent_name, language)
            await sess.start()
            _sessions[conversation_id] = sess
        return sess


def get(conversation_id: str) -> Optional[ConversationSession]:
    return _sessions.get(conversation_id)


async def drop(conversation_id: str) -> None:
    async with _lock:
        sess = _sessions.pop(conversation_id, None)
    if sess:
        await sess.close()
