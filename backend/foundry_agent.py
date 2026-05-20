"""
Foundry Agent Service **v2** wrapper.

Uses the new `azure-ai-projects` 2.x SDK and invokes the agent through the
**Responses API** (`openai.responses.create(... extra_body={"agent_reference": ...})`).
Per-conversation continuity is provided by a Foundry *conversation* object
(`openai.conversations.create()`) — the v2 replacement for the old threads.

The blocking openai SDK call is off-loaded to a worker thread so the asyncio
event loop stays responsive; deltas are dispatched back to the loop via
`asyncio.run_coroutine_threadsafe`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from .config import settings

log = logging.getLogger(__name__)

DeltaCallback = Callable[[str], Awaitable[None]]
CompletedCallback = Callable[[str], Awaitable[None]]


class FoundryAgentClient:
    """Thin wrapper around `AIProjectClient` + its embedded OpenAI client."""

    def __init__(self) -> None:
        if not settings.project_endpoint:
            raise RuntimeError("PROJECT_ENDPOINT is not configured.")
        self._project = AIProjectClient(
            endpoint=settings.project_endpoint,
            credential=AzureCliCredential(process_timeout=30),
        )
        # The OpenAI client returned here is already wired to the Foundry
        # project endpoint and uses the same AAD credential.
        self._openai = self._project.get_openai_client()

    # ---- Conversations ---------------------------------------------------
    async def create_conversation(self) -> str:
        """Create a Foundry conversation and return its id."""
        return await asyncio.to_thread(
            lambda: self._openai.conversations.create().id
        )

    # ---- Agents listing --------------------------------------------------
    async def list_agents(self) -> list[dict]:
        """Best-effort enumeration of agents in the project.

        v2 agents are keyed by *name* (+ version) rather than by GUID, so the
        ``id`` field returned here is the agent **name** for compatibility
        with the rest of the codebase.
        """
        def _list() -> list[dict]:
            out: list[dict] = []
            try:
                # azure-ai-projects 2.x exposes versioned agents under
                # `project.agents`. The exact iterator name has shifted across
                # preview SDKs, so try a few sensible options.
                iterator = None
                for attr in ("list_versions", "list", "list_agents"):
                    fn = getattr(self._project.agents, attr, None)
                    if callable(fn):
                        try:
                            iterator = fn()
                            break
                        except TypeError:
                            continue
                if iterator is None:
                    return out
                seen: set[str] = set()
                for a in iterator:
                    name = getattr(a, "name", None) or getattr(a, "agent_name", None)
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    out.append({"id": name, "name": name})
            except Exception as e:  # pragma: no cover - best effort
                log.debug("list_agents: %s", e)
            return out

        try:
            return await asyncio.to_thread(_list)
        except Exception as e:
            log.warning("list_agents failed: %s", e)
            return []

    # ---- Warm-up --------------------------------------------------------
    async def warm_up_agent(self, agent_name: str) -> None:
        """Prime the server-side ``agent_reference`` resolution path.

        The first ``responses.create`` for a given agent typically pays a
        version lookup / instruction load on the Foundry side (~0.5-1 s).
        Sending one standalone (no ``conversation`` parameter) request at
        startup pre-warms that cache for the whole app lifetime without
        polluting any user conversation.
        """
        if not agent_name:
            return
        openai = self._openai

        def _run() -> None:
            try:
                stream = openai.responses.create(
                    extra_body={
                        "agent_reference": {
                            "name": agent_name,
                            "type": "agent_reference",
                        }
                    },
                    input=" ",
                    stream=True,
                )
                with stream as events:
                    for _ in events:
                        pass
            except Exception as e:
                log.debug("warm_up_agent _run: %s", e)

        try:
            await asyncio.to_thread(_run)
            log.info("Foundry agent %r warmed up.", agent_name)
        except Exception as e:
            log.warning("warm_up_agent failed: %s", e)

    # ---- Streaming response ---------------------------------------------
    async def ask(
        self,
        agent_name: str,
        conversation_id: str,
        content: str,
        on_delta: DeltaCallback,
        on_completed: Optional[CompletedCallback] = None,
    ) -> str:
        """Send a user message and stream the agent's response.

        Uses the Responses API with an ``agent_reference`` extra-body, bound
        to the Foundry ``conversation`` for multi-turn context.
        """
        loop = asyncio.get_running_loop()
        openai = self._openai
        collected: list[str] = []

        log.info(
            "responses.create agent=%r conversation=%s input_len=%d",
            agent_name, conversation_id, len(content),
        )

        def _run() -> str:
            try:
                stream = openai.responses.create(
                    conversation=conversation_id,
                    extra_body={
                        "agent_reference": {
                            "name": agent_name,
                            "type": "agent_reference",
                        }
                    },
                    input=content,
                    stream=True,
                )
            except Exception as e:
                log.exception("responses.create failed")
                raise

            with stream as events:
                for event in events:
                    etype = getattr(event, "type", "") or ""
                    # Text deltas
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", None)
                        if delta:
                            collected.append(delta)
                            asyncio.run_coroutine_threadsafe(
                                on_delta(delta), loop
                            )
                    elif etype == "response.error" or etype.endswith(".failed"):
                        err = getattr(event, "error", None) or getattr(event, "message", None)
                        log.error("responses stream error: %s", err)
                    # response.completed / response.output_item.done etc. are
                    # ignored — completion is signalled by stream exit.
            return "".join(collected)

        try:
            answer = await asyncio.to_thread(_run)
        except Exception as e:
            log.exception("ask agent failed")
            answer = f"⚠️ {e}"
        if on_completed:
            await on_completed(answer)
        return answer
